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
(:func:`assemble_movie`) assembles the film along the screenplay with the
existing engines.

Stage 3: the assembly engine
----------------------------
``monteur movie assemble`` builds the FILM along the screenplay: scenes in
order, each filled from its assigned footage folder, paced by its
``cut_intent``, with the clips' own sound. The rules, in full:

1. **Scene duration target** (:func:`scene_duration_target`) — a heuristic
   from the blueprint: base 6 s, plus 2.5 s per dialogue line, plus
   ``min(10, action_word_count / 4)`` seconds (words = whitespace-split
   ``scene.action``), clamped to 4..45 s. The notes state the estimate
   basis once ("scene lengths estimated from the script — trim in
   Resolve").
2. **Material per scene** — the scene's folder is sifted
   (:func:`monteur.sift.sift_directory`), or its reports are taken from
   the optional ``sift_cache`` (``{folder: [ClipReport]}``; folders not in
   the cache are sifted and ADDED to it, so several scenes shot into one
   folder sift it once). Take-numbered coverage is preferred: when
   filenames match ``S<scene>_T<take>`` for THIS scene's number (e.g.
   ``S03_T02.mp4`` for scene 3, case-insensitive, leading zeros optional),
   only those files are used and a note says so; otherwise everything in
   the folder is used. The scene's target duration is then filled with the
   best moments in CHRONOLOGICAL order (a scene has internal continuity)
   via the public :func:`monteur.montage.plan_montage`
   (``music=None, max_duration=target``, audio-original semantics); the
   resulting entries are shifted by the scene's record offset, so scenes
   tile the film timeline contiguously with no overlaps. When the folder
   holds less material than the target, the montage machinery's repetition
   guard shortens the scene rather than looping footage, and a note says
   how short it ran.
3. **cut_intent -> pacing/transitions** (:func:`parse_cut_intent`) — an
   offline keyword parse (German + English, mirroring
   :mod:`monteur.revise`'s vocabulary), never a model call:
   "ruhig/calm/langsam/slow" -> pace 3.0 s per shot;
   "schnell/fast/hektisch/snappy" -> pace 1.0 s; default 2.0 s (calm wins
   when both appear). "blende/dissolve/weich" -> "dissolves" for the
   scene's INTERNAL cuts; "harter schnitt/hard cut" -> "cuts" (hard-cut
   words win over dissolve words: "harter Schnitt, keine Blende" cuts);
   default "cuts". BETWEEN scenes the film cuts hard by default; when the
   PREVIOUS assembled scene's cut_intent asked for dissolves, the incoming
   scene's first clip carries a dissolve instead (clip metadata
   ``"transition"`` / ``"transition_frames"``, exactly like montage
   entries). Per-scene fades are never planned — a film's scenes butt
   together.
4. **Sound** — every video clip's own audio rides on A1 (audio="original"
   semantics: same source range, kind AUDIO, track A1), so the film keeps
   the sound recorded on set.
5. **Markers** — one Blue marker per scene start: name
   ``"Scene 3: EXT. WALDWEG - NIGHT"``, note = the scene summary. Fill
   notes (gaps, reused material) are kept, prefixed with the scene number.
6. **Dialogue scenes** — v1 keeps it honest: when a scene has dialogue
   lines AND transcript sidecars (``.json``/``.srt`` next to its clips,
   the :mod:`monteur.transcribe` convention) exist, a note recommends
   ``monteur assembly`` for line-accurate takes; the scene is still
   assembled visually here. No transcript matching is attempted — no fake
   precision.
7. **Notes** lead with a summary ("assembled 'Nachtfahrt': 6 of 8 scenes,
   94s at 25 fps"); scenes without a folder are skipped with a note.
8. **Canvas** — validated against :data:`monteur.montage.CANVASES` exactly
   like ``montage_to_timeline`` (invalid -> ValueError listing the
   presets).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from monteur.ai import DEFAULT_MODEL, MonteurAIError, complete

if TYPE_CHECKING:  # only for type hints — stages 2/3 import lazily at runtime
    from monteur.model import Timeline
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

    Goes through :func:`monteur.ai.complete`, so it works with either an
    Anthropic API key or an installed Claude Code CLI. Raises
    :class:`monteur.ai.MonteurAIError` when neither backend is available
    or the response is unusable.
    """
    prompt = (
        f"GENRE: {genre or 'filmmaker’s choice — pick what serves the idea'}\n"
        f"BRIEF (idea + real-world constraints):\n{brief}\n\n"
        "Draft the complete blueprint for this short film."
    )
    raw = complete(
        prompt,
        system=_MOVIE_SYSTEM,
        model=model,
        max_tokens=16000,
        json_schema=_MOVIE_SCHEMA,
    )
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


# --- stage 3: the assembly engine ----------------------------------------------------
#
# assemble_movie builds the FILM along the screenplay (rules documented in the
# module docstring's "Stage 3" section). Everything here is deterministic and
# offline; the only slow part is sifting the scene folders, which the optional
# sift_cache short-circuits.

# Scene duration heuristic (seconds); see the module docstring, rule 1.
_SCENE_BASE_SECONDS = 6.0
_SCENE_SECONDS_PER_LINE = 2.5
_SCENE_ACTION_MAX_SECONDS = 10.0
_SCENE_ACTION_WORDS_PER_SECOND = 4.0
_SCENE_MIN_SECONDS = 4.0
_SCENE_MAX_SECONDS = 45.0

# cut_intent keyword tables (DE + EN, mirrors monteur.revise's vocabulary).
# Substring matches on the lowercased intent, so "ruhiger"/"Blenden" hit too.
_PACE_CALM_WORDS = ("ruhig", "calm", "langsam", "slow")
_PACE_FAST_WORDS = ("schnell", "fast", "hektisch", "snappy")
_HARD_CUT_WORDS = ("harter schnitt", "harte schnitte", "hard cut")
_DISSOLVE_WORDS = ("blende", "dissolve", "weich")
_PACE_CALM = 3.0
_PACE_DEFAULT = 2.0
_PACE_FAST = 1.0

# Dissolve INTO a new scene (seconds): min(this, half the incoming first
# clip) — the same sizing rule montage uses for its gentle-phase dissolves.
_SCENE_DISSOLVE = 0.5


def scene_duration_target(scene: MovieScene) -> float:
    """Estimated screen time (seconds) a scene should get in the assembly.

    Heuristic from the blueprint: base 6 s + 2.5 s per dialogue line +
    ``min(10, action_word_count / 4)`` s (words = whitespace-split
    ``scene.action``), clamped to 4..45 s. An estimate, not a verdict —
    the notes tell the editor to trim in Resolve.
    """
    action_words = len(scene.action.split())
    target = (
        _SCENE_BASE_SECONDS
        + _SCENE_SECONDS_PER_LINE * len(scene.dialogue)
        + min(_SCENE_ACTION_MAX_SECONDS, action_words / _SCENE_ACTION_WORDS_PER_SECOND)
    )
    return min(_SCENE_MAX_SECONDS, max(_SCENE_MIN_SECONDS, target))


def parse_cut_intent(text: str) -> tuple[float, str]:
    """``(pace_seconds, transitions)`` from a scene's free-text cut_intent.

    Offline German + English keyword parse (mirrors :mod:`monteur.revise`'s
    vocabulary — no model call): "ruhig/calm/langsam/slow" -> pace 3.0,
    "schnell/fast/hektisch/snappy" -> pace 1.0, default 2.0 (calm wins when
    both appear). Transitions for the scene's INTERNAL cuts:
    "harter schnitt/hard cut" -> "cuts", "blende/dissolve/weich" ->
    "dissolves" (hard-cut words win when both appear), default "cuts".
    Text with no recognizable cue yields the defaults — it never guesses.
    """
    t = (text or "").lower()
    if any(w in t for w in _PACE_CALM_WORDS):
        pace = _PACE_CALM
    elif any(w in t for w in _PACE_FAST_WORDS):
        pace = _PACE_FAST
    else:
        pace = _PACE_DEFAULT
    if any(w in t for w in _HARD_CUT_WORDS):
        transitions = "cuts"
    elif any(w in t for w in _DISSOLVE_WORDS):
        transitions = "dissolves"
    else:
        transitions = "cuts"
    return pace, transitions


def _take_pattern(scene_number: int) -> re.Pattern:
    """Filename pattern for this scene's take-numbered coverage (S03_T02).

    Case-insensitive, leading zeros optional, and the ``S`` must not be
    preceded by a letter or digit so ``S11_T01`` never reads as scene 1.
    """
    return re.compile(rf"(?:^|[^0-9a-z])s0*{scene_number}_t\d+", re.IGNORECASE)


def _notify(progress: Callable | None, index: int, total: int, name: str, stage: str) -> None:
    """Invoke the assembly progress callback, swallowing its exceptions."""
    if progress is None:
        return
    try:
        progress(index, total, name, stage)
    except Exception:  # noqa: BLE001 — a broken callback must not abort assembly
        pass


def _has_transcript_sidecars(reports: list["ClipReport"]) -> bool:
    """True when any clip has a ``.json``/``.srt`` transcript next to it
    (the :mod:`monteur.transcribe` sidecar convention)."""
    for report in reports:
        clip = Path(report.path)
        if clip.with_suffix(".json").is_file() or clip.with_suffix(".srt").is_file():
            return True
    return False


def assemble_movie(
    project: MovieProject,
    fps: float = 25.0,
    canvas: str = "uhd",
    progress: Callable | None = None,
    sift_cache: dict | None = None,
) -> tuple["Timeline", list[str]]:
    """Assemble the film along the screenplay: ``(timeline, notes)``.

    Scenes in order, each filled from its assigned ``scene.folder``, paced
    by its ``cut_intent``, with the clips' own sound on A1 — the full rules
    live in the module docstring's "Stage 3" section.

    ``progress(index, total, name, stage)`` is called with stage
    ``"scene"`` once per scene (``name`` = the heading, ``index``/``total``
    over ALL scenes), and the sift's own per-clip callbacks are forwarded
    with their stages (``"start"``/``"done"``, per-folder clip counts).
    Callback exceptions are swallowed.

    ``sift_cache`` (``{folder: [ClipReport]}``) reuses pre-sifted reports;
    folders not in the dict are sifted and ADDED to it, so callers can keep
    the cache across runs and several scenes shot into one folder sift it
    once.

    Raises ValueError when no scene has a folder assigned, or on an unknown
    ``canvas`` (same presets as :func:`monteur.montage.montage_to_timeline`).
    Scenes without a folder are skipped with a note; a folder that cannot
    be sifted (missing directory, no ffmpeg) skips its scene with a note
    instead of aborting the film.
    """
    from monteur.media import MonteurMediaError
    from monteur.model import AUDIO, VIDEO, Clip, Marker, Timeline, seconds_to_frames
    from monteur.montage import CANVASES, CHRONOLOGICAL, plan_montage
    from monteur.sift import sift_directory

    if canvas not in CANVASES:
        valid = ", ".join(sorted(CANVASES))
        raise ValueError(f"unknown canvas {canvas!r}; valid canvases: {valid}")
    if not any(s.folder.strip() for s in project.scenes):
        raise ValueError(
            "no scene has footage assigned — assign folders in the Movie "
            "view or movie.json first"
        )

    width, height = CANVASES[canvas]
    timeline = Timeline(
        name=project.title or "Monteur Movie", fps=fps, width=width, height=height
    )
    cache = sift_cache if sift_cache is not None else {}
    scene_notes: list[str] = []
    total = len(project.scenes)
    cursor = 0  # record frames: where the next scene starts
    assembled = 0
    prev_transitions = "cuts"  # the previous ASSEMBLED scene's parsed intent

    def _forward_sift(index: int, clip_total: int, name: str, stage: str, _report) -> None:
        _notify(progress, index, clip_total, name, stage)

    for index, scene in enumerate(project.scenes, start=1):
        _notify(progress, index, total, scene.heading, "scene")
        where = f"scene {scene.number} ({scene.heading})"
        folder = scene.folder.strip()
        if not folder:
            scene_notes.append(f"{where}: no footage assigned — skipped")
            continue

        # -- material: cached reports or a fresh sift of the scene's folder --
        if folder in cache:
            reports = cache[folder]
        else:
            try:
                reports = sift_directory(folder, progress=_forward_sift)
            except MonteurMediaError as exc:
                scene_notes.append(f"{where}: {exc} — skipped")
                continue
            cache[folder] = reports

        # Prefer take-numbered coverage shot FOR this scene (S03_T02 for
        # scene 3); without take-named files, everything in the folder plays.
        take_re = _take_pattern(scene.number)
        takes = [r for r in reports if take_re.search(Path(r.path).name)]
        if takes:
            scene_reports = takes
            scene_notes.append(
                f"{where}: {len(takes)} take file"
                f"{'s' if len(takes) != 1 else ''} named "
                f"S{scene.number:02d}_T## — using only those"
            )
        else:
            scene_reports = reports

        if not any(r.moments for r in scene_reports):
            scene_notes.append(f"{where}: no usable footage in {folder} — skipped")
            continue

        # -- fill the scene: chronological best moments, paced by intent --
        target = scene_duration_target(scene)
        pace, transitions = parse_cut_intent(scene.cut_intent)
        plan = plan_montage(
            scene_reports,
            music=None,
            order=CHRONOLOGICAL,
            max_duration=target,
            style="auto",
            pace=pace,
            transitions=transitions,
        )
        if not plan.entries:
            scene_notes.append(f"{where}: no usable footage in {folder} — skipped")
            continue
        if plan.duration < target - 0.5:
            # The repetition guard shortened the scene rather than looping
            # its footage — say so in movie terms, not montage terms.
            scene_notes.append(
                f"{where}: footage supports about {plan.duration:.0f}s of the "
                f"{target:.0f}s target — scene runs short"
            )
        for note in plan.notes:
            if "gap at" in note or "material ran short" in note:
                scene_notes.append(f"scene {scene.number}: {note}")

        # -- render: shift the scene's entries onto the film timeline --
        timeline.markers.append(
            Marker(
                frame=cursor,
                name=f"Scene {scene.number}: {scene.heading}",
                note=scene.summary,
                color="Blue",
            )
        )
        scene_first_clip = len(timeline.clips)
        for entry in plan.entries:
            stem = Path(entry.clip_path).stem
            rec_in = cursor + seconds_to_frames(entry.record_start, fps)
            rec_out = cursor + seconds_to_frames(entry.record_end, fps)
            if rec_out <= rec_in:  # a slot rounded away at this frame rate
                continue
            src_in = seconds_to_frames(entry.source_start, fps)
            src_len = entry.source_end - entry.source_start
            rec_len = entry.record_end - entry.record_start
            if abs(src_len - rec_len) < 1e-6:
                # Keep source and record durations frame-exact together
                # (montage_to_timeline's rule).
                src_out = src_in + (rec_out - rec_in)
            else:
                src_out = seconds_to_frames(entry.source_end, fps)
            video = Clip(
                name=stem,
                track="V1",
                kind=VIDEO,
                source_in=src_in,
                source_out=src_out,
                record_in=rec_in,
                record_out=rec_out,
                source_name=stem,
                source_file=entry.clip_path,
            )
            video.metadata["media_start_seconds"] = entry.media_start
            video.metadata["media_duration_seconds"] = entry.clip_duration
            if entry.label:
                video.metadata["label"] = entry.label
            transition_frames = round(entry.transition * fps)
            if transition_frames > 0:
                video.metadata["transition"] = "dissolve"
                video.metadata["transition_frames"] = transition_frames
            timeline.clips.append(video)
            # The clip's own sound on A1 (audio="original" semantics): same
            # source range, kind AUDIO, track A1.
            timeline.clips.append(
                Clip(
                    name=stem,
                    track="A1",
                    kind=AUDIO,
                    source_in=src_in,
                    source_out=src_out,
                    record_in=rec_in,
                    record_out=rec_out,
                    source_name=stem,
                    source_file=entry.clip_path,
                    metadata={
                        "media_start_seconds": entry.media_start,
                        "media_duration_seconds": entry.clip_duration,
                    },
                )
            )

        # Between scenes: hard cut by default; a previous scene that asked
        # for dissolves hands over with a dissolve INTO this scene instead.
        if assembled and prev_transitions == "dissolves":
            incoming = timeline.clips[scene_first_clip]
            half_clip = (incoming.record_out - incoming.record_in) / fps / 2.0
            frames = round(min(_SCENE_DISSOLVE, half_clip) * fps)
            if frames > 0:
                incoming.metadata["transition"] = "dissolve"
                incoming.metadata["transition_frames"] = frames

        # Dialogue scenes: v1 assembles them visually and says so — no
        # transcript matching, no fake precision (that is monteur assembly).
        if scene.dialogue and _has_transcript_sidecars(scene_reports):
            scene_notes.append(
                f"scene {scene.number} has dialogue and transcripts — "
                "consider 'monteur assembly' for line-accurate takes; "
                "assembled visually here"
            )

        cursor += seconds_to_frames(plan.duration, fps)
        assembled += 1
        prev_transitions = transitions

    notes = [
        f"assembled {project.title!r}: {assembled} of {total} scenes, "
        f"{round(timeline.duration / fps)}s at {fps:g} fps",
        "scene lengths estimated from the script — trim in Resolve",
    ]
    notes.extend(scene_notes)
    return timeline, notes
