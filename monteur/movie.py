"""Movie Creator, stage 1: from an idea to a production blueprint.

Monteur's other engines start AFTER the shoot. ``monteur movie new``
stands beside the filmmaker BEFORE it: give it a genre, an idea and the
real-world constraints (locations, cast, target length), and Claude
drafts the whole pre-production package::

    monteur movie new projekt/ --genre thriller \\
        --brief "5 Minuten, 2 Personen, Wald und Auto, nachts"

    projekt/
      movie.json        # the machine-readable project (scenes, tips, intents)
      script.fountain   # the screenplay — readable, printable, and exactly
                        # the format `monteur assembly` consumes later
      shotlist.md       # the printable production plan: per scene the
                        # shooting tips, sound notes and a take checklist

Division of labor: Claude writes the CONTENT (logline, scenes, action,
dialogue, shooting advice); Monteur renders the FORMATS deterministically
(Fountain and Markdown are produced by pure, tested functions from the
JSON — the model never has to get a file format right). The blueprint is
written in the language of the brief.

The scene objects deliberately carry production-workflow fields
(``status``, ``folder``) that stay empty in stage 1 — stage 2 (the Studio
movie view) assigns shot footage per scene and tracks progress, stage 3
assembles the film along the screenplay with the existing engines.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from monteur.ai import DEFAULT_MODEL, MonteurAIError, _client

MOVIE_FORMAT_VERSION = 1

# Scene counts are clamped to keep first projects shootable: a beginner's
# short lives between a handful and a couple dozen scenes.
_MIN_SCENES = 3
_MAX_SCENES = 24


@dataclass
class DialogueLine:
    character: str
    line: str
    parenthetical: str = ""  # e.g. "(whispering)"


@dataclass
class MovieScene:
    number: int
    heading: str  # Fountain slug: "INT. KITCHEN - NIGHT"
    summary: str  # one line: what the scene does for the story
    action: str  # screenplay prose (what we see)
    dialogue: list[DialogueLine] = field(default_factory=list)
    shooting_tips: list[str] = field(default_factory=list)  # framing/light/takes
    sound_notes: str = ""  # what to record on set
    cut_intent: str = ""  # editorial intent ("slow build, hard cut into 4")
    # Stage-2 production tracking (empty in a fresh blueprint):
    status: str = "planned"  # "planned" | "shot" | "assigned"
    folder: str = ""  # footage folder assigned to this scene


@dataclass
class MovieProject:
    title: str
    genre: str
    brief: str
    logline: str
    scenes: list[MovieScene] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_MOVIE_SYSTEM = (
    "You are an experienced writer-director helping an ambitious hobbyist "
    "make a short film they can actually shoot. Write in the language of "
    "the brief. Respect the constraints LITERALLY: only locations, cast "
    "and means the brief offers. Keep scenes short and shootable — no "
    "crowds, no stunts, no VFX unless the brief asks. Shooting tips must "
    "be concrete craft advice (framing, camera height, light direction, "
    "how many takes), not platitudes. Scene headings are Fountain slugs "
    "in CAPS: 'INT./EXT. LOCATION - DAY/NIGHT' (keep them in English "
    "format words INT/EXT/DAY/NIGHT even when the content language is "
    "German). The cut_intent says how the scene should feel in the edit."
)

_MOVIE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "summary": {"type": "string"},
                    "action": {"type": "string"},
                    "dialogue": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "character": {"type": "string"},
                                "line": {"type": "string"},
                                "parenthetical": {"type": "string"},
                            },
                            "required": ["character", "line"],
                            "additionalProperties": False,
                        },
                    },
                    "shooting_tips": {"type": "array", "items": {"type": "string"}},
                    "sound_notes": {"type": "string"},
                    "cut_intent": {"type": "string"},
                },
                "required": [
                    "heading", "summary", "action", "dialogue",
                    "shooting_tips", "sound_notes", "cut_intent",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "logline", "scenes"],
    "additionalProperties": False,
}


def _validated(data: dict, genre: str, brief: str) -> MovieProject:
    """A MovieProject from (model-produced) JSON, defensively clamped."""
    scenes_raw = data.get("scenes") or []
    if not isinstance(scenes_raw, list) or len(scenes_raw) < _MIN_SCENES:
        raise MonteurAIError(
            f"Claude returned {len(scenes_raw) if isinstance(scenes_raw, list) else 0} "
            f"scenes — a shootable blueprint needs at least {_MIN_SCENES}. Try a "
            "slightly more detailed brief."
        )
    notes: list[str] = []
    if len(scenes_raw) > _MAX_SCENES:
        notes.append(f"clamped {len(scenes_raw)} scenes to {_MAX_SCENES}")
        scenes_raw = scenes_raw[:_MAX_SCENES]
    scenes: list[MovieScene] = []
    for i, raw in enumerate(scenes_raw, start=1):
        if not isinstance(raw, dict):
            continue
        dialogue = [
            DialogueLine(
                character=str(d.get("character", "")).strip().upper() or "VOICE",
                line=str(d.get("line", "")).strip(),
                parenthetical=str(d.get("parenthetical", "")).strip(),
            )
            for d in (raw.get("dialogue") or [])
            if isinstance(d, dict) and str(d.get("line", "")).strip()
        ]
        heading = str(raw.get("heading", "")).strip().upper() or f"SCENE {i}"
        scenes.append(
            MovieScene(
                number=i,
                heading=heading,
                summary=str(raw.get("summary", "")).strip(),
                action=str(raw.get("action", "")).strip(),
                dialogue=dialogue,
                shooting_tips=[
                    str(t).strip() for t in (raw.get("shooting_tips") or []) if str(t).strip()
                ][:8],
                sound_notes=str(raw.get("sound_notes", "")).strip(),
                cut_intent=str(raw.get("cut_intent", "")).strip(),
            )
        )
    return MovieProject(
        title=str(data.get("title", "")).strip() or "Untitled",
        genre=genre,
        brief=brief,
        logline=str(data.get("logline", "")).strip(),
        scenes=scenes,
        notes=notes,
    )


def generate_movie(
    brief: str, genre: str = "", model: str = DEFAULT_MODEL
) -> MovieProject:
    """Draft a movie blueprint with Claude (structured output).

    Raises :class:`monteur.ai.MonteurAIError` when the ``anthropic``
    package or credentials are missing, or the response is unusable.
    """
    client = _client()
    prompt = (
        f"GENRE: {genre or 'filmmaker’s choice — pick what serves the idea'}\n"
        f"BRIEF (idea + real-world constraints):\n{brief}\n\n"
        "Draft the complete blueprint for this short film."
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=_MOVIE_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _MOVIE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except MonteurAIError:
        raise
    except Exception as exc:  # pragma: no cover - network/auth failures
        raise MonteurAIError(f"Claude API request failed: {exc}") from exc
    if getattr(response, "stop_reason", None) == "refusal":
        raise MonteurAIError("The request was declined by the model's safety system.")
    raw = "".join(b.text for b in response.content if b.type == "text")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MonteurAIError(f"Claude returned an unparseable blueprint: {raw[:200]!r}") from exc
    return _validated(data, genre=genre, brief=brief)


# --- deterministic renderers -------------------------------------------------------


def render_fountain(project: MovieProject) -> str:
    """The screenplay as Fountain text (pure; formats what Claude wrote).

    Emits a title page, then per scene the slug, action and dialogue in
    standard Fountain conventions — exactly the dialect
    :mod:`monteur.screenplay` parses for ``monteur assembly``.
    """
    out: list[str] = [f"Title: {project.title}", "Credit: Blueprint by Monteur", ""]
    if project.logline:
        out += [f"> {project.logline}", ""]
    for scene in project.scenes:
        out += [scene.heading, ""]
        if scene.action:
            out += [scene.action, ""]
        for d in scene.dialogue:
            out.append(d.character)
            if d.parenthetical:
                paren = d.parenthetical
                if not paren.startswith("("):
                    paren = f"({paren})"
                out.append(paren)
            out += [d.line, ""]
    return "\n".join(out).rstrip() + "\n"


def shotlist_markdown(project: MovieProject) -> str:
    """The printable production plan (pure)."""
    out = [f"# {project.title} — production plan", ""]
    if project.logline:
        out += [f"*{project.logline}*", ""]
    out += [f"Genre: {project.genre or '-'}  ·  Scenes: {len(project.scenes)}", ""]
    for scene in project.scenes:
        out += [f"## Scene {scene.number} — {scene.heading}", ""]
        if scene.summary:
            out += [scene.summary, ""]
        if scene.shooting_tips:
            out.append("Shooting:")
            out += [f"- [ ] {tip}" for tip in scene.shooting_tips]
            out.append("")
        if scene.sound_notes:
            out += [f"Sound: {scene.sound_notes}", ""]
        if scene.cut_intent:
            out += [f"Edit intent: {scene.cut_intent}", ""]
        out += [f"Footage naming: `S{scene.number:02d}_T01`, `S{scene.number:02d}_T02`, ...", ""]
    out += [
        "---",
        "Shoot 2-3 takes per setup. Name files by scene and take "
        "(S03_T02) — `monteur assembly` routes them automatically.",
    ]
    return "\n".join(out).rstrip() + "\n"


# --- persistence -------------------------------------------------------------------


def project_to_dict(project: MovieProject) -> dict:
    d = asdict(project)
    d["monteur_movie"] = MOVIE_FORMAT_VERSION
    return d


def project_from_dict(d: dict) -> MovieProject:
    if not isinstance(d, dict) or d.get("monteur_movie") != MOVIE_FORMAT_VERSION:
        raise ValueError(
            "not a Monteur movie project (missing or unsupported "
            f"'monteur_movie' version; expected {MOVIE_FORMAT_VERSION})"
        )
    try:
        scenes = [
            MovieScene(
                **{**s, "dialogue": [DialogueLine(**dl) for dl in s.get("dialogue", [])]}
            )
            for s in d.get("scenes", [])
        ]
        return MovieProject(
            title=d["title"],
            genre=d.get("genre", ""),
            brief=d.get("brief", ""),
            logline=d.get("logline", ""),
            scenes=scenes,
            notes=list(d.get("notes", [])),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed movie project: {exc}") from exc


def save_project(project: MovieProject, project_dir: str | Path) -> list[Path]:
    """Write movie.json, script.fountain and shotlist.md; returns the paths."""
    root = Path(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = [root / "movie.json", root / "script.fountain", root / "shotlist.md"]
    paths[0].write_text(
        json.dumps(project_to_dict(project), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths[1].write_text(render_fountain(project), encoding="utf-8")
    paths[2].write_text(shotlist_markdown(project), encoding="utf-8")
    return paths


def load_project(project_dir: str | Path) -> MovieProject:
    path = Path(project_dir) / "movie.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no movie.json in {project_dir} — start with 'monteur movie new'"
        )
    return project_from_dict(json.loads(path.read_text(encoding="utf-8")))
