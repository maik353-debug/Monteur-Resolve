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
from typing import TYPE_CHECKING

from monteur.ai import DEFAULT_MODEL, MonteurAIError, _client

if TYPE_CHECKING:  # only for type hints — stage 2 never needs sift at runtime
    from monteur.sift import ClipReport

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


# --- stage 2: production slots & footage checks -------------------------------------
#
# The Studio's Movie view turns the blueprint into a production board: every
# scene is a slot that gets a footage folder assigned, and Monteur can hold
# the sifted (and optionally vision-labeled) footage against the scene text.
# Everything below is pure — no API calls, no file writes.

# find.py-style token matching, kept local on purpose: find's helpers are
# private, and the convention is small enough to restate — a token matches a
# word when either is a prefix of the other and the shorter side has at
# least 3 characters ("wald" finds "waldweg", "kurven" finds "kurve").
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MIN_TOKEN = 3

#: Fountain slug format words are structure, not content — the INT/EXT and
#: DAY/NIGHT checks read them; the token overlap must not count them.
_SLUG_WORDS = frozenset({"int", "ext", "day", "night", "i", "e"})

# Small curated EN+DE word lists, matched against vision labels/tags with
# the same prefix rule. Deliberately short: these produce HINTS, and a word
# list that tries to be complete only gets more confidently wrong.
_OUTDOOR_WORDS = (
    "forest", "wald", "woods", "road", "straße", "strasse", "street",
    "mountain", "berg", "sky", "himmel", "field", "feld", "meadow", "wiese",
    "beach", "strand", "river", "fluss", "lake", "tree", "baum", "outdoor",
    "outside", "landscape", "landschaft", "trail", "path", "garden", "garten",
)
_INDOOR_WORDS = (
    "room", "zimmer", "kitchen", "küche", "kueche", "interior", "indoor",
    "inside", "office", "büro", "buero", "wall", "wand", "table", "tisch",
    "sofa", "couch", "bed", "bett", "corridor", "flur", "hallway", "stairs",
    "treppe", "ceiling", "basement", "keller", "desk", "lamp", "lampe",
)
_NIGHT_WORDS = (
    "night", "nacht", "dark", "dunkel", "dusk", "dämmerung", "daemmerung",
    "evening", "abend", "moon", "mond", "headlight", "scheinwerfer",
)
_DAY_WORDS = (
    "day", "tag", "sunny", "sonnig", "daylight", "tageslicht", "sun",
    "sonne", "bright", "hell", "morning", "morgen", "noon", "mittag",
    "afternoon", "nachmittag",
)


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _token_match(token: str, word: str) -> bool:
    """Bidirectional prefix match; the shorter side needs >= 3 characters."""
    if token == word:
        return True
    if len(token) >= _MIN_TOKEN and word.startswith(token):
        return True
    if len(word) >= _MIN_TOKEN and token.startswith(word):
        return True
    return False


def _hits(words: tuple[str, ...] | list[str], vocabulary: set[str]) -> int:
    """How many of ``words`` appear in ``vocabulary`` (prefix rule)."""
    return sum(1 for w in words if any(_token_match(w, v) for v in vocabulary))


def assign_scene(project: MovieProject, scene_number: int, folder: str) -> MovieScene:
    """Assign a footage folder to a scene slot (or unassign with "").

    Sets ``scene.folder`` and flips ``status`` to ``"assigned"``; an empty
    folder resets the slot to ``"planned"``. Returns the mutated scene.
    Raises ValueError on an unknown scene number.
    """
    for scene in project.scenes:
        if scene.number == scene_number:
            scene.folder = str(folder or "").strip()
            scene.status = "assigned" if scene.folder else "planned"
            return scene
    raise ValueError(
        f"no scene {scene_number} in this project — it has "
        f"{len(project.scenes)} scene(s)"
    )


def project_progress(project: MovieProject) -> dict:
    """Shooting progress: ``{"scenes": N, "assigned": n, "percent": int}``."""
    total = len(project.scenes)
    assigned = sum(1 for s in project.scenes if s.status == "assigned")
    percent = round(100 * assigned / total) if total else 0
    return {"scenes": total, "assigned": assigned, "percent": percent}


def check_scene_footage(scene: MovieScene, reports: list[ClipReport]) -> dict:
    """Honest hints on whether a folder's sifted footage matches a scene.

    Returns exactly this shape::

        {
            "score": float,           # 0..1 (see scoring below)
            "content_checked": bool,  # True when vision labels/tags existed
            "clips": int,             # number of sifted clips
            "avg_usable": float,      # 0..1 mean usable_ratio across clips
            "findings": [str, ...],   # human sentences — hints, not verdicts
        }

    The findings come in three layers:

    1. Technical (always): clip count + average usable ratio, plus one line
       per clip whose sift notes flag it as mostly unusable. For a NIGHT
       scene, "mostly too dark" clips get a softened finding instead — dark
       footage FITS a night scene, the exposure heuristic just can't know.
    2. Content (only when the moments carry vision labels/tags — otherwise
       one finding points at ``monteur see`` / the Studio's "Let Claude
       watch" toggle): token overlap between the scene's heading+summary and
       the moments' labels/tags/groups, bidirectional prefix, min 3 chars.
    3. INT/EXT and DAY/NIGHT read from the heading, held against small EN+DE
       word lists over the vision labels/tags.

    Scoring: starts at 0.5; +0.25 for content overlap; +0.15 when INT/EXT
    agrees; +0.1 when DAY/NIGHT agrees; clamped to 0..1. Without vision data
    the score stays at the 0.5 baseline and ``content_checked`` is False —
    the caller must not read the baseline as a verdict either way.
    """
    findings: list[str] = []
    clips = len(reports)
    avg_usable = (
        sum(r.usable_ratio for r in reports) / clips if clips else 0.0
    )
    heading_words = set(_tokens(scene.heading))
    wants_int = "int" in heading_words
    wants_ext = "ext" in heading_words
    wants_day = "day" in heading_words
    wants_night = "night" in heading_words

    # -- 1. technical ------------------------------------------------------
    if clips:
        findings.append(
            f"{clips} clip{'s' if clips != 1 else ''} in the folder, "
            f"on average {round(avg_usable * 100)}% usable."
        )
    else:
        findings.append("No clips to judge — the folder sift found nothing.")
    for report in reports:
        name = Path(report.path).name
        for note in report.notes:
            if "unusable" not in note and "no usable stretch" not in note:
                continue
            if wants_night and "dark" in note:
                findings.append(
                    f"{name} is mostly dark — that fits a NIGHT scene, so "
                    "judge it by eye rather than by the numbers."
                )
            else:
                findings.append(f"{name}: {note}.")

    # -- 2 + 3. content (needs vision labels/tags on the moments) ----------
    vocabulary: set[str] = set()
    for report in reports:
        for moment in report.moments:
            vocabulary.update(_tokens(getattr(moment, "label", "") or ""))
            for tag in getattr(moment, "tags", None) or []:
                vocabulary.update(_tokens(str(tag)))
            vocabulary.update(_tokens(getattr(moment, "group", "") or ""))
    content_checked = bool(vocabulary)
    score = 0.5

    if not content_checked:
        findings.append(
            "Content checks need Claude's eyes — run 'monteur see' on the "
            "folder or turn on \"Let Claude watch\" and check again."
        )
    else:
        scene_tokens = [
            t
            for t in dict.fromkeys(_tokens(scene.heading + " " + scene.summary))
            if t not in _SLUG_WORDS
        ]
        matched = sorted(
            {
                t
                for t in scene_tokens
                if any(_token_match(t, w) for w in vocabulary)
            }
        )
        if matched:
            score += 0.25
            findings.append(
                "Looks related — the footage mentions: " + ", ".join(matched) + "."
            )
        else:
            findings.append(
                "No overlap between the scene description and what Claude "
                "saw in the footage — worth a look before you edit, though "
                "word matching is a blunt tool."
            )

        outdoor = _hits(_OUTDOOR_WORDS, vocabulary)
        indoor = _hits(_INDOOR_WORDS, vocabulary)
        lean = "ext" if outdoor > indoor else ("int" if indoor > outdoor else "")
        if lean and (wants_int or wants_ext):
            lean_word = "outdoor" if lean == "ext" else "indoor"
            if (lean == "ext" and wants_ext) or (lean == "int" and wants_int):
                score += 0.15
                findings.append(
                    f"The labels lean {lean_word} — that matches the "
                    f"{'EXT.' if lean == 'ext' else 'INT.'} heading."
                )
            else:
                findings.append(
                    f"The heading says {'INT.' if wants_int else 'EXT.'} but "
                    f"the labels lean {lean_word} — double-check that this "
                    "is the right folder."
                )

        nightish = _hits(_NIGHT_WORDS, vocabulary)
        dayish = _hits(_DAY_WORDS, vocabulary)
        tod = "night" if nightish > dayish else ("day" if dayish > nightish else "")
        if tod and (wants_day or wants_night):
            tod_word = "nighttime" if tod == "night" else "daytime"
            if (tod == "night" and wants_night) or (tod == "day" and wants_day):
                score += 0.1
                findings.append(
                    f"The labels sound like {tod_word} — that matches the "
                    f"{'NIGHT' if tod == 'night' else 'DAY'} heading."
                )
            else:
                findings.append(
                    f"The heading says {'NIGHT' if wants_night else 'DAY'} "
                    f"but the labels sound like {tod_word} — double-check "
                    "that this is the right folder."
                )

    return {
        "score": max(0.0, min(1.0, score)),
        "content_checked": content_checked,
        "clips": clips,
        "avg_usable": avg_usable,
        "findings": findings,
    }
