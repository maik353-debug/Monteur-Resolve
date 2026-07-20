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
9. **The film plan** — besides the timeline, :func:`assemble_movie`
   returns the assembled film as one :class:`monteur.montage.MontagePlan`
   (no music, entries at absolute film positions, dissolves included).
   That plan is what gives the assembled film the full plan toolchain:
   preview, direct export, Resolve build, director's notes.
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
    # The last footage-check result for this slot (the dict
    # :func:`check_scene_footage` returned, plus the ``"folder"`` it was
    # checked against — see :func:`record_scene_check`). ``{}`` = never
    # checked. Persisted in movie.json so the shoot plan survives restarts.
    last_check: dict = field(default_factory=dict)


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


def record_scene_check(project: MovieProject, scene_number: int, check: dict) -> MovieScene:
    """Persist a footage-check result on its scene slot (pure mutation).

    Stores ``check`` (a :func:`check_scene_footage` dict) as
    ``scene.last_check`` together with the ``"folder"`` it was checked
    against, so :func:`shoot_plan` can ignore a check that no longer
    matches the assigned folder. Returns the mutated scene; raises
    ValueError on an unknown scene number. The caller decides whether to
    :func:`save_project` afterwards.
    """
    for scene in project.scenes:
        if scene.number == scene_number:
            scene.last_check = {**dict(check or {}), "folder": scene.folder}
            return scene
    raise ValueError(
        f"no scene {scene_number} in this project — it has "
        f"{len(project.scenes)} scene(s)"
    )


# --- the shoot plan: what still has to be filmed --------------------------------------
#
# The blueprint IS the soll-list (scenes + shooting tips) and the checks are
# the ist-state. shoot_plan folds both into one actionable view — the movie
# sibling of monteur.coverage's pre-cut shot list, but scene-aware and fully
# deterministic (no AI, no sifting; at most a cheap directory listing to
# count take files).

#: Below this mean usable ratio a checked scene reads as reshoot material.
_WEAK_USABLE = 0.4

#: A content-checked score below this means Claude saw no overlap between
#: the scene text and the footage (0.5 baseline + 0.25 overlap bonus).
_OK_SCORE = 0.75

#: How many "shoot these first" entries a validated advice reply may carry.
ADVICE_LIMIT = 6

#: Structured-output contract for :func:`shoot_plan_advice` — validated
#: defensively by :func:`_validate_advice` either way (coverage.py pattern).
ADVICE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "first": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["scene", "why"],
            },
            "description": "scenes to shoot first, most urgent first",
        },
        "day_plan": {
            "type": "array",
            "items": {"type": "string"},
            "description": "a practical shooting-day order, one step per line",
        },
        "summary": {"type": "string"},
    },
    "required": ["first", "day_plan", "summary"],
}

_ADVICE_SYSTEM = (
    "You are an experienced first assistant director planning a hobbyist's "
    "shooting day. You get the screenplay's scene list with each scene's "
    "production state (unshot, assigned footage, checked ok/weak, thin "
    "coverage) and the shooting tips. Prioritize practically: group by "
    "location and light, name what blocks the story first, keep it "
    "shootable. Never invent scenes that are not in the list."
)


def count_scene_takes(scene: MovieScene) -> int | None:
    """How many take-named files (``S03_T##``) the scene's folder holds.

    A cheap directory listing — no sifting, no decoding. Returns ``None``
    when nothing can be counted (no folder assigned, or the folder is not
    a directory); ``0`` when the folder exists but holds no take files
    named for THIS scene (the footage may simply be unnamed — unknown is
    not thin).
    """
    folder = scene.folder.strip()
    if not folder:
        return None
    path = Path(folder)
    try:
        if not path.is_dir():
            return None
        from monteur.media import MEDIA_EXTENSIONS

        take_re = _take_pattern(scene.number)
        return sum(
            1
            for p in path.iterdir()
            if p.suffix.lower() in MEDIA_EXTENSIONS and take_re.search(p.name)
        )
    except OSError:
        return None


def _matching_check(scene: MovieScene, checks_by_scene: dict | None) -> dict | None:
    """The check to judge ``scene`` by: the caller's override, else the
    stored ``last_check`` — but only while it still matches the assigned
    folder (a re-assigned slot must not be judged by a stale check)."""
    if checks_by_scene and scene.number in checks_by_scene:
        check = checks_by_scene[scene.number]
        return dict(check) if isinstance(check, dict) else None
    check = scene.last_check
    if (
        isinstance(check, dict)
        and check
        and scene.folder
        and check.get("folder") == scene.folder
    ):
        return check
    return None


def _weak_reasons(check: dict) -> list[str]:
    """Why a checked scene reads as reshoot material ([] = it holds up).

    Deterministic re-reading of a :func:`check_scene_footage` dict: no
    clips at all, a mostly-unusable folder, a content check that saw no
    overlap, or an INT/EXT / DAY/NIGHT mismatch finding.
    """
    reasons: list[str] = []
    clips = int(check.get("clips") or 0)
    if clips == 0:
        reasons.append("the folder sift found no usable clips")
        return reasons
    avg = float(check.get("avg_usable") or 0.0)
    if avg < _WEAK_USABLE:
        reasons.append(f"only {round(avg * 100)}% of the footage is usable")
    if check.get("content_checked") and float(check.get("score") or 0.0) < _OK_SCORE:
        reasons.append(
            "Claude saw no overlap between the footage and the scene "
            "description"
        )
    for finding in check.get("findings") or []:
        if "double-check" in str(finding):
            reasons.append(str(finding))
    return reasons


def shoot_plan(project: MovieProject, checks_by_scene: dict | None = None) -> dict:
    """The scene-aware shoot plan: what still has to be filmed, and why.

    Deterministic — no AI, no sifting. Per scene it combines what the
    blueprint already knows (slugline, summary, shooting tips) with the
    production state:

    * ``"unshot"`` — no footage folder assigned;
    * ``"assigned"`` — a folder, but no (still-matching) check result;
    * ``"checked-ok"`` / ``"checked-weak"`` — judged from the scene's
      stored ``last_check`` (:func:`record_scene_check`), or from the
      optional ``checks_by_scene`` override (``{scene_number: check
      dict}``, e.g. fresh in-memory results). Weak = no clips, mean
      usable ratio below 40%, a content check without overlap, or an
      INT/EXT / DAY/NIGHT mismatch finding — the reasons land in the
      scene's ``why`` list verbatim.

    Returns::

        {"scenes": [{"number", "heading", "summary", "status", "folder",
                     "takes",        # take-named files (None = unknowable)
                     "tips", "why"}, ...],
         "unshot":  [{"scene", "heading", "summary", "tips"}, ...],
         "reshoot": [{"scene", "heading", "why", "tips"}, ...],
         "thin":    [{"scene", "heading", "why", "tips"}, ...],
         "counts": {"scenes", "unshot", "assigned",
                    "checked_ok", "checked_weak", "thin"},
         "percent": int}   # scenes with footage, like project_progress

    ``thin`` lists scenes whose folder holds EXACTLY ONE take file named
    for them (``S03_T01`` and nothing else) — one take means no
    alternative in the edit. A folder without take-named files stays out
    of ``thin``: unnamed footage is unknown, not thin.
    """
    scenes_out: list[dict] = []
    unshot: list[dict] = []
    reshoot: list[dict] = []
    thin: list[dict] = []
    counts = {
        "scenes": len(project.scenes),
        "unshot": 0,
        "assigned": 0,
        "checked_ok": 0,
        "checked_weak": 0,
        "thin": 0,
    }
    for scene in project.scenes:
        tips = list(scene.shooting_tips)
        why: list[str] = []
        takes = count_scene_takes(scene)
        if not scene.folder.strip():
            status = "unshot"
            counts["unshot"] += 1
            unshot.append(
                {
                    "scene": scene.number,
                    "heading": scene.heading,
                    "summary": scene.summary,
                    "tips": tips,
                }
            )
        else:
            check = _matching_check(scene, checks_by_scene)
            if check is None:
                status = "assigned"
                counts["assigned"] += 1
            else:
                why = _weak_reasons(check)
                if why:
                    status = "checked-weak"
                    counts["checked_weak"] += 1
                    reshoot.append(
                        {
                            "scene": scene.number,
                            "heading": scene.heading,
                            "why": "; ".join(why),
                            "tips": tips,
                        }
                    )
                else:
                    status = "checked-ok"
                    counts["checked_ok"] += 1
            if takes == 1:
                thin_why = (
                    f"only one take named S{scene.number:02d}_T## in the "
                    "folder — no alternative if it doesn't play"
                )
                why = why + [thin_why]
                counts["thin"] += 1
                thin.append(
                    {
                        "scene": scene.number,
                        "heading": scene.heading,
                        "why": thin_why,
                        "tips": tips,
                    }
                )
        scenes_out.append(
            {
                "number": scene.number,
                "heading": scene.heading,
                "summary": scene.summary,
                "status": status,
                "folder": scene.folder,
                "takes": takes,
                "tips": tips,
                "why": why,
            }
        )
    total = counts["scenes"]
    shot = total - counts["unshot"]
    return {
        "scenes": scenes_out,
        "unshot": unshot,
        "reshoot": reshoot,
        "thin": thin,
        "counts": counts,
        "percent": round(100 * shot / total) if total else 0,
    }


def _validate_advice(data, valid_scenes: set[int]) -> dict:
    """Defensively normalise a parsed advice reply (coverage.py pattern).

    ``first`` entries must name a scene that exists (hallucinated numbers
    are dropped) and carry a non-empty ``why``; the list is capped at
    :data:`ADVICE_LIMIT`. ``day_plan`` lines are coerced to non-empty
    strings (capped at 10); ``summary`` to a string.
    """
    if not isinstance(data, dict):
        data = {}
    first: list[dict] = []
    for raw in data.get("first") or []:
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw.get("scene"))
        except (TypeError, ValueError):
            continue
        why = str(raw.get("why") or "").strip()
        if number not in valid_scenes or not why:
            continue
        first.append({"scene": number, "why": why})
        if len(first) >= ADVICE_LIMIT:
            break
    day_plan = [
        str(line).strip() for line in (data.get("day_plan") or [])
        if str(line).strip()
    ][:10]
    return {
        "first": first,
        "day_plan": day_plan,
        "summary": str(data.get("summary") or ""),
    }


def shoot_plan_advice(
    project: MovieProject, plan: dict | None = None, model: str = DEFAULT_MODEL
) -> dict:
    """Turn the deterministic shoot plan into a prioritized day plan.

    ONE completion through :func:`monteur.ai.complete`
    (:data:`ADVICE_SCHEMA`): the screenplay's scene list with each
    scene's production state and shooting tips goes in, a "shoot these
    first" list plus a practical day plan comes back::

        {"first": [{"scene": int, "why": str}, ...],   # <= ADVICE_LIMIT
         "day_plan": [str, ...], "summary": str, "notes": [str, ...]}

    Callers MUST degrade gracefully: a :class:`monteur.ai.MonteurAIError`
    passes through unchanged (no backend, request failed, unparseable
    reply) and the deterministic plan alone is still a complete answer.
    A parseable but structurally odd reply is repaired by
    :func:`_validate_advice` (hallucinated scene numbers dropped, lists
    capped). When nothing is left to shoot, no model is called at all —
    the result says so in ``notes``.
    """
    plan = plan if plan is not None else shoot_plan(project)
    if not (plan["unshot"] or plan["reshoot"] or plan["thin"]):
        return {
            "first": [],
            "day_plan": [],
            "summary": (
                "Nothing left to shoot — every scene has footage that "
                "holds up. Assemble the film."
            ),
            "notes": ["no open scenes — answered without a model call"],
        }
    inventory = [
        {
            "scene": s["number"],
            "heading": s["heading"],
            "summary": s["summary"],
            "status": s["status"],
            "why": s["why"],
            "takes": s["takes"],
            "tips": s["tips"],
        }
        for s in plan["scenes"]
    ]
    prompt = (
        f"FILM: {project.title}\n"
        + (f"LOGLINE: {project.logline}\n" if project.logline else "")
        + "SCENES (with production state):\n"
        + json.dumps(inventory, ensure_ascii=False)
        + "\n\nPlan the shoot:\n"
        "- `first`: the scenes to shoot first, most urgent first — `why` "
        "names what they unblock (act, story beat, light window); at most "
        f"{ADVICE_LIMIT} entries, only scene numbers from the list;\n"
        "- `day_plan`: a practical order for the next shooting day, one "
        "step per line (group by location/light, reshoots where they fit);\n"
        "- `summary`: 1-2 sentences on where this production stands."
    )
    raw = complete(
        prompt, system=_ADVICE_SYSTEM, model=model, json_schema=ADVICE_SCHEMA
    )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MonteurAIError(
            f"the shoot-plan advice came back as unparseable JSON: {raw[:200]!r}"
        ) from exc
    result = _validate_advice(data, {s.number for s in project.scenes})
    result["notes"] = []
    return result


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
) -> tuple["Timeline", list[str], "MontagePlan"]:
    """Assemble the film along the screenplay: ``(timeline, notes, plan)``.

    Scenes in order, each filled from its assigned ``scene.folder``, paced
    by its ``cut_intent``, with the clips' own sound on A1 — the full rules
    live in the module docstring's "Stage 3" section.

    ``plan`` is the assembled film as ONE :class:`monteur.montage.
    MontagePlan` — no music, entries at their absolute film positions
    (frame-exact: positions derive from the same rounded frames the
    timeline uses), each carrying its dissolve (scene handovers included)
    and vision label, notes shared with ``notes``. It round-trips through
    :func:`monteur.montage.plan_to_dict` / :func:`plan_from_dict`, and
    ``montage_to_timeline(plan, fps, audio="original", canvas=...)``
    reproduces exactly this timeline's clips — which is what plugs the
    assembled film into every plan-based engine (preview, direct export,
    Resolve build, director's notes). Only the scene markers live on the
    timeline alone.

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
    from monteur.montage import (
        CANVASES,
        CHRONOLOGICAL,
        MontageEntry,
        MontagePlan,
        plan_montage,
    )
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
    # The assembled film as one MontagePlan entry list: mirrors the V1 clips
    # 1:1, in seconds derived from the SAME rounded frames, so the plan and
    # the timeline can never drift apart.
    film_entries: list[MontageEntry] = []

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
        scene_first_entry = len(film_entries)
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
            # The same cut in film-plan terms (seconds FROM the rounded
            # frames — round-tripping through seconds_to_frames yields the
            # identical timeline positions).
            film_entries.append(
                MontageEntry(
                    clip_path=entry.clip_path,
                    source_start=src_in / fps,
                    source_end=src_out / fps,
                    record_start=rec_in / fps,
                    record_end=rec_out / fps,
                    score=entry.score,
                    transition=(
                        transition_frames / fps if transition_frames > 0 else 0.0
                    ),
                    media_start=entry.media_start,
                    clip_duration=entry.clip_duration,
                    label=entry.label,
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
                if scene_first_entry < len(film_entries):
                    film_entries[scene_first_entry].transition = frames / fps

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
    film_plan = MontagePlan(
        music_path="",  # a film keeps set sound — audio mode "original"
        duration=timeline.duration / fps,
        entries=film_entries,
        notes=list(notes),
    )
    return timeline, notes, film_plan
