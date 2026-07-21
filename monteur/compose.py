"""Claude composes the cut: the engine locks the grid, Claude does the casting.

The montage planner (:mod:`monteur.montage`) is a craftsman's metronome —
beat grid, slot durations, drop alignment, smash-to-black dips are all
mechanically right — but its CASTING is heuristic: scores, motion vectors
and mild vision bonuses decide which shot lands where. That is why a
"trailer" could come out rhythmically perfect and still tell no story.
This module flips the roles: **Claude composes, the engine keeps the
craft guarantees.**

Three stages, one public function (:func:`compose_montage`):

1. **Engine (deterministic).** :func:`monteur.montage.plan_montage` runs
   unchanged and produces the exact grid a plain build would: slots with
   their durations and phases, dips, drop alignment, dissolves, fades —
   plus the heuristic casting, which doubles as the per-slot fallback.
   Nothing here is new code, so the grid is byte-identical to the
   feature-off path by construction.
2. **Claude (one completion).** The plan is condensed into a dossier —
   the style's craft brief (:data:`CRAFT_BRIEFS`), the editor's own
   ``brief`` ("what is this video?"), the slot list (index, phase,
   seconds, music section, drop/after-dip flags), the dip list and the
   full moment inventory (with vision labels/tags/roles/hero/groups when
   a ``--see`` pass ran) — and sent through the one AI seam
   :func:`monteur.ai.complete` with :data:`COMPOSE_SCHEMA`. Text-only, so
   it works over an API key OR the Claude Code CLI at no extra cost.
   Claude answers with a story line, a cast for EVERY slot (clip + start
   second), act titles for the dips and per-act reasoning.
3. **Engine (validation).** Every cast entry is validated and snapped:
   the clip must exist in the reports, the requested window must overlap
   a sifted moment, and the source window is trimmed/padded to the slot's
   exact duration with the same rules the planner's fill uses (snap into
   the moment, pad toward the clip's end). Any invalid pick — unknown
   clip, window outside every good moment, too little material, a slot
   Claude skipped — keeps the heuristic entry for THAT slot with a note;
   the cut as a whole never fails. Claude's titles land in
   ``plan.title_texts`` (aligned with the dips; picked up by
   :func:`monteur.resolve.titles_from_plan` and the timeline's title-slot
   markers), the story and per-act reasoning land in ``plan.notes``
   ("story: ...", "act 1: ...").

Failure semantics: with ``strict=False`` (the default, the CLI's mode) an
unreachable backend or an unparseable reply degrades to the plain
:func:`plan_montage` result plus a note ``"composer unavailable: ...;
heuristic cut"`` — the basic flow never breaks. ``strict=True`` (Studio's
mode) raises :class:`monteur.ai.MonteurAIError` instead: the user
explicitly asked for the AI cut and must see why it failed.

Vision is an upgrade, not a gate: without annotations the dossier says so,
Claude casts on scores/motion/order, and the plan notes recommend a
"Let Claude watch your clips" scan for a smarter cut.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

from monteur import ai
from monteur import montage as _montage
from monteur.montage import MontagePlan, plan_montage
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport, Moment

_EPS = 1e-6

#: Tolerance (seconds) when matching a slot's start against a drop time —
#: the cut-ahead lead shifts cuts slightly before their beat, and the
#: finishing pass uses the same 0.25 s window for its dip matching.
_DROP_MATCH = 0.25

#: The structured-output contract for the composer call. The API backend
#: enforces it; the CLI backend gets it as an instruction — either way the
#: parsed dict is re-validated defensively by :func:`_apply_cast`.
COMPOSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "story": {
            "type": "string",
            "description": "one-line story arc of the whole cut",
        },
        "cast": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "clip": {"type": "string"},
                    "start": {"type": "number"},
                },
                "required": ["slot", "clip", "start"],
            },
        },
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dip": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["dip", "text"],
            },
        },
        "why": {
            "type": "array",
            "items": {"type": "string"},
            "description": "one short line of reasoning per act",
        },
    },
    "required": ["story", "cast", "titles", "why"],
}

#: Per-style craft briefs: what a real editor knows about cutting each form.
#: These are the grammar the montage styles already encode (arcs, dips,
#: drop alignment), written out as editorial intent for the composer.
CRAFT_BRIEFS: dict[str, str] = {
    "trailer": (
        "Open cold on a hook — the single most intriguing image, not the "
        "prettiest one. The black dips are act titles: 2-6 words each that "
        "tell the story in steps. Escalate: every act cuts faster and hits "
        "harder than the last, the climax lands on the drop with the hero "
        "shot, and the outro is a stinger — end on the strongest image or "
        "a sharp button, never a slow fade of leftovers."
    ),
    "travel": (
        "Establish the place first — a wide, calm opener that says where "
        "we are. Then travel with geographic and temporal flow: morning "
        "before night, arrival before summit; keep neighbouring slots in "
        "the same leg of the journey. Breathe on the calm music — hold "
        "long scenic shots there — and save the peak action for the loud "
        "sections. Close on a farewell mood: sunset, a look back, leaving."
    ),
    "wedding": (
        "Faces and gestures over scenery. Open on anticipation — "
        "preparations, details, nerves — then follow the day's real order, "
        "and let the emotional peaks (vows, kiss, first dance, speeches) "
        "land on the musical peaks. Never rush a reaction; tears and "
        "laughter get their full slot. Close warm and quiet: the couple, "
        "golden light, a look that lingers."
    ),
    "music_video": (
        "Energy from the first frame — no slow establishing. Cut on motion "
        "and match motion across cuts; recurring visual motifs beat "
        "literal narrative. Put the boldest, most graphic images on the "
        "drops and loud sections, change location and angle fast, and end "
        "on attitude — a freeze-worthy hero image or an abrupt cut, not a "
        "fade."
    ),
    "auto": (
        "Follow the song. Calm sections carry calm, wide images; loud "
        "sections carry motion and action; the strongest material lands "
        "on the drops. Open with a shot that establishes place or "
        "subject, never put two takes of the same scene back to back, "
        "and end on a shot that resolves — wider, slower or emotionally "
        "final."
    ),
}

_SYSTEM = (
    "You are a seasoned film editor composing a montage. The beat grid is "
    "LOCKED: every slot's position and length is final, and so are the "
    "black dips and fades — your job is the casting (which clip, from "
    "which second, fills each slot), the act titles on the dips, and the "
    "story they add up to. Tell ONE story across the whole cut; every act "
    "must escalate or breathe on purpose. Use only clips and windows from "
    "the inventory — never invent material. Write titles in the language "
    "of the editor's brief and the footage labels."
)


# --- the dossier -----------------------------------------------------------------


def _r(value: float) -> float:
    """Round for the dossier — prompts don't need 15 decimals."""
    return round(float(value), 2)


def _has_vision(reports: list[ClipReport]) -> bool:
    """True when any moment carries vision annotations (label/role/...)."""
    for report in reports:
        for m in report.moments:
            if (
                getattr(m, "label", "")
                or getattr(m, "tags", [])
                or getattr(m, "role", "")
                or getattr(m, "group", "")
                or getattr(m, "hero", 0.0) > _EPS
            ):
                return True
    return False


def _phases_for(
    plan: MontagePlan, music: MusicAnalysis | None, style: _montage.MontageStyle
) -> list[tuple[float, float, str]]:
    """The arc phases the plan was cut on, recomputed deterministically.

    ``plan_montage`` does not store its phases, but they are a pure
    function of (grid music, length, style): the arc shares mapped onto
    the duration, drop-pinned and snapped by
    :func:`monteur.montage._build_style_grid` (music) or the raw shares
    from :func:`monteur.montage._build_pseudo_grid` (no music). Phase
    bounds never depend on the beat-step tables, so a ``pace`` override
    changes nothing here. Arc-less styles ("auto") return ``[]``.
    """
    if not style.arc or plan.duration <= _EPS:
        return []
    length = plan.duration
    if music is None:
        _cuts, phases, _notes = _montage._build_pseudo_grid(length, style)
        return phases
    grid_music = music
    if plan.music_start > _EPS:
        grid_music = _montage._window_music(music, plan.music_start, length)
    _cuts, phases, _notes = _montage._build_style_grid(grid_music, length, style)
    return phases


def _section_label(music: MusicAnalysis | None, plan: MontagePlan, t: float) -> str:
    """The music-section label under record time ``t`` ("" without music).

    Sections live in SONG time; a montage cut from the song's strongest
    passage starts at ``plan.music_start``, so record time shifts by that.
    """
    if music is None or not music.sections:
        return ""
    song_t = plan.music_start + t
    for section in music.sections:
        if section.start - _EPS <= song_t < section.end - _EPS:
            return section.label
    if song_t >= music.sections[-1].end - _EPS:
        return music.sections[-1].label
    return ""


def compose_context(
    plan: MontagePlan,
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    style: str = "auto",
    locked: set[int] | frozenset[int] = frozenset(),
) -> dict:
    """The JSON-ready dossier the composer prompt carries.

    ``slots`` has one lean dict per plan entry (the entries ARE the grid:
    one per slot, in record order): 0-based ``slot`` index, ``seconds``
    (the slot's exact duration — dip-shortened slots show their real,
    shorter length), the arc ``phase``, the music-section label under the
    slot, ``drop: true`` when the slot starts on a drop, ``after_dip:
    true`` when it hits out of a black dip. ``dips`` lists the title
    slots (0-based ``dip`` index + start time). ``inventory`` lists every
    usable moment: clip basename, window, score, mean motion magnitude,
    and the vision fields (label/tags/role/hero/group) when present —
    empty fields are omitted so an unseen inventory stays small and
    honest. ``vision`` says whether any annotations exist at all.

    ``locked`` (slot indices an arrangement already cast) marks those
    slots ``"locked": true`` in the dossier — the prompt tells Claude to
    compose around them, and :func:`_apply_cast` ignores any cast for
    them regardless. Empty (the default) leaves the dossier unchanged.
    """
    chosen = _montage.STYLES.get(style, _montage.STYLES["auto"])
    phases = _phases_for(plan, music, chosen)

    drops: list[float] = []
    if music is not None:
        lo, hi = plan.music_start, plan.music_start + plan.duration
        drops = [d - lo for d in music.drops if lo - _EPS <= d <= hi + _EPS]

    slots: list[dict] = []
    for i, entry in enumerate(plan.entries):
        slot: dict = {
            "slot": i,
            "seconds": _r(entry.record_end - entry.record_start),
        }
        phase = _montage._phase_label_at(phases, entry.record_start) if phases else None
        if phase:
            slot["phase"] = phase
        section = _section_label(music, plan, entry.record_start)
        if section:
            slot["music"] = section
        if any(abs(entry.record_start - d) <= _DROP_MATCH for d in drops):
            slot["drop"] = True
        if any(
            abs(entry.record_start - (dip_start + dip_len)) <= 1e-3
            for dip_start, dip_len in plan.dips
        ):
            slot["after_dip"] = True
        if i in locked:
            slot["locked"] = True
        slots.append(slot)

    dips = [
        {"dip": j, "at": _r(start), "seconds": _r(length)}
        for j, (start, length) in enumerate(plan.dips)
    ]

    inventory: list[dict] = []
    for report in reports:
        for m in report.moments:
            item: dict = {
                "clip": Path(report.path).name,
                "start": _r(m.start),
                "end": _r(m.end),
                "score": _r(m.score),
            }
            motion = (
                math.hypot(*m.entry_motion) + math.hypot(*m.exit_motion)
            ) / 2.0
            if motion > _EPS:
                item["motion"] = _r(motion)
            if getattr(m, "label", ""):
                item["label"] = m.label
            if getattr(m, "tags", []):
                item["tags"] = list(m.tags)
            if getattr(m, "role", ""):
                item["role"] = m.role
            if getattr(m, "hero", 0.0) > _EPS:
                item["hero"] = _r(m.hero)
            if getattr(m, "group", ""):
                item["group"] = m.group
            inventory.append(item)

    return {
        "style": chosen.key,
        "duration": _r(plan.duration),
        "slots": slots,
        "dips": dips,
        "inventory": inventory,
        "vision": _has_vision(reports),
    }


def _build_prompt(context: dict, style: str, brief: str) -> str:
    """The one composer prompt: craft brief + editor's brief + dossier."""
    craft = CRAFT_BRIEFS.get(style, CRAFT_BRIEFS["auto"])
    parts = [f"STYLE: {style}\nCRAFT (how this form is cut):\n{craft}"]
    if brief.strip():
        parts.append(
            "THE EDITOR'S BRIEF (what this video is, who it is for):\n"
            + brief.strip()
        )
    if context.get("vision"):
        parts.append(
            "The inventory carries vision labels — cast by MEANING first: "
            "what each shot shows, how the shots chain into a story."
        )
    else:
        parts.append(
            "No vision labels are available for this footage — cast by "
            "score, motion and shot order; do not invent content. (A 'Let "
            "Claude watch your clips' scan would make this far sharper.)"
        )
    if any(slot.get("locked") for slot in context.get("slots") or []):
        parts.append(
            "Slots marked `locked` are already cast by the editor's own "
            "arrangement — their material and order are FINAL. Do not "
            "recast them; compose the remaining slots, the titles and the "
            "story around them."
        )
    parts.append(
        "DOSSIER (slots in record order; dips are black title slots; the "
        "inventory is every usable moment):\n"
        + json.dumps(context, ensure_ascii=False)
    )
    parts.append(
        "Compose the cut:\n"
        "- cast EVERY slot: pick the clip and the `start` second whose "
        "material fills the slot's full `seconds` from `start`;\n"
        "- `start` must lie inside one of that clip's inventory windows "
        "and leave enough material for the slot;\n"
        "- reuse a moment only when the slot count leaves no alternative;\n"
        "- slots marked `drop` want the hero material; slots marked "
        "`after_dip` hit out of black — open them strong;\n"
        "- slots with noticeably more `seconds` than their neighbours are "
        "deliberate HOLDS (the establishing opener, the drop, the final "
        "shot) — cast material that can carry the time;\n"
        "- write one title per dip (`titles`, using the dossier's `dip` "
        "index) that advances the story — short, 2-6 words;\n"
        "- `story` is the one-line arc of the whole cut; `why` is one "
        "short line per act explaining your choices."
    )
    return "\n\n".join(parts)


# --- validation & application ------------------------------------------------------


def _overlapping_moment(
    report: ClipReport, start: float, end: float
) -> Moment | None:
    """The report moment overlapping [start, end] most — None if none do.

    Unlike the director's replacement matcher this does NOT snap to the
    nearest moment: a composer window that touches no sifted moment at all
    is an invalid pick (Claude pointed at material the sift rejected), and
    the slot falls back to the heuristic entry instead.
    """
    best: Moment | None = None
    best_ov = 0.0
    for moment in report.moments:
        ov = min(moment.end, end) - max(moment.start, start)
        if ov > best_ov + _EPS:
            best, best_ov = moment, ov
    return best


def _apply_cast(
    plan: MontagePlan,
    data: dict,
    reports: list[ClipReport],
    locked: set[int] | frozenset[int] = frozenset(),
) -> None:
    """Apply a parsed composer reply to the plan, in place.

    Every valid pick swaps ONLY the slot's source-describing fields
    (clip_path, source window, score, media_start, clip_duration, label);
    the record grid, transitions, dips, fades and SFX stay bit-identical.
    Invalid picks (unknown slot/clip, window outside every moment, not
    enough material) keep the heuristic entry with a per-slot note.
    Claude's titles land in ``plan.title_texts``; story/why in the notes.

    ``locked`` slots (an editor's arrangement cast them) are never
    touched: any cast Claude sends for them is dropped silently — one
    summary note says how many slots the arrangement holds — and a
    missing cast for them is NOT a fallback.
    """
    by_name: dict[str, ClipReport] = {}
    for report in reports:
        by_name.setdefault(report.path, report)
        by_name.setdefault(Path(report.path).name, report)

    picks: dict[int, tuple[str, float]] = {}
    for raw in data.get("cast") or []:
        if not isinstance(raw, dict):
            continue
        try:
            slot = int(raw["slot"])
            clip = str(raw["clip"])
            start = float(raw["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= slot < len(plan.entries) and slot not in picks:
            picks[slot] = (clip, start)

    entries = [replace(e) for e in plan.entries]
    cast_slots: set[int] = set()
    fallbacks: list[tuple[int, str]] = []
    for i, entry in enumerate(entries):
        if i in locked:
            continue  # the arrangement cast this slot — Claude cannot recast it
        pick = picks.get(i)
        if pick is None:
            fallbacks.append((i, "no cast for this slot"))
            continue
        clip, want = pick
        report = by_name.get(clip) or by_name.get(Path(clip).name)
        if report is None:
            fallbacks.append((i, f"no clip named {clip!r} in the footage"))
            continue
        length = entry.record_end - entry.record_start
        moment = _overlapping_moment(report, want, want + length)
        if moment is None:
            fallbacks.append(
                (
                    i,
                    f"{want:.2f}s is outside every good moment of "
                    f"{Path(report.path).name}",
                )
            )
            continue
        # Snap into the moment, exactly duration-preserving; pad toward the
        # clip's end when the moment tail is short — the fill's own rules.
        src_start = min(max(want, moment.start), max(moment.start, moment.end - length))
        src_start = max(0.0, src_start)
        src_end = min(src_start + length, moment.end)
        if src_end - src_start < length - _EPS:
            limit = report.duration if report.duration > _EPS else src_start + length
            src_end = max(src_end, min(src_start + length, limit))
        if src_end - src_start < length - _EPS:
            fallbacks.append(
                (
                    i,
                    f"only {src_end - src_start:.2f}s of source for a "
                    f"{length:.2f}s slot",
                )
            )
            continue
        entries[i] = replace(
            entry,
            clip_path=report.path,
            source_start=src_start,
            source_end=src_end,
            score=moment.score,
            media_start=report.media_start,
            clip_duration=report.duration,
            label=getattr(moment, "label", ""),
        )
        cast_slots.add(i)
    plan.entries = entries

    # Reuse accounting: Claude may repeat material, but only knowingly —
    # the notes say when it happened, and same-clip neighbours are called
    # out as the composer's own choice.
    reused = 0
    seen: list[tuple[str, float, float]] = []
    for i in sorted(cast_slots):
        e = entries[i]
        window = (e.clip_path, e.source_start, e.source_end)
        if any(
            c == window[0]
            and min(hi, window[2]) - max(lo, window[1]) > _EPS
            for c, lo, hi in seen
        ):
            reused += 1
        seen.append(window)
    adjacent = sum(
        1
        for i in range(len(entries) - 1)
        if i in cast_slots
        and i + 1 in cast_slots
        and entries[i].clip_path == entries[i + 1].clip_path
    )

    story = str(data.get("story") or "").strip()
    if story:
        plan.notes.append(f"story: {story}")
    for idx, line in enumerate(data.get("why") or []):
        line = str(line).strip()
        if line:
            plan.notes.append(f"act {idx + 1}: {line}")
    plan.notes.append(
        f"composed by Claude: {len(cast_slots)} of "
        f"{len(entries) - len(locked)} slots cast"
    )
    if locked:
        plan.notes.append(
            f"composer: {len(locked)} slot"
            + ("s" if len(locked) != 1 else "")
            + " locked by your arrangement"
        )
    if reused:
        plan.notes.append(
            f"composer: reused material in {reused} slot"
            + ("s" if reused != 1 else "")
        )
    if adjacent:
        plan.notes.append(
            f"composer: {adjacent} same-clip cut"
            + ("s" if adjacent != 1 else "")
            + " back to back (Claude's explicit choice)"
        )
    for i, reason in fallbacks:
        plan.notes.append(
            f"composer: slot {i + 1} kept the heuristic pick ({reason})"
        )

    # Act titles for the dips: aligned by index, "" where Claude gave none.
    if plan.dips:
        texts = [""] * len(plan.dips)
        for raw in data.get("titles") or []:
            if not isinstance(raw, dict):
                continue
            try:
                dip = int(raw["dip"])
                text = str(raw["text"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= dip < len(texts) and text and not texts[dip]:
                texts[dip] = text
        if any(texts):
            plan.title_texts = texts
            titled = sum(1 for t in texts if t)
            plan.notes.append(
                f"composer: {titled} act title" + ("s" if titled != 1 else "")
                + " on the black dips"
            )


# --- public API --------------------------------------------------------------------


def compose_montage(
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    *,
    style: str = "auto",
    brief: str = "",
    strict: bool = False,
    **plan_kwargs,
) -> MontagePlan:
    """Plan a montage with Claude as the cutter (see the module docstring).

    Stage 1 is a plain :func:`monteur.montage.plan_montage` call —
    ``style`` and every ``plan_kwargs`` entry (order, max_duration, pace,
    transitions, sfx, ...) are forwarded verbatim, so the grid, dips and
    durations are exactly what the heuristic build would produce. Stage 2
    sends ONE completion through :func:`monteur.ai.complete` (craft brief
    + ``brief`` + dossier, :data:`COMPOSE_SCHEMA`). Stage 3 validates the
    cast per slot and swaps only the sources; invalid picks keep the
    heuristic entry with a note.

    An ``arrangement`` in ``plan_kwargs`` (the editor's own scene order,
    see :func:`monteur.montage.plan_montage`) is forwarded verbatim; the
    slots it claims are LOCKED for the composer — flagged in the dossier
    and immune to recasting — so Claude composes the remaining slots,
    titles and story around the editor's order.

    ``strict=False`` (default): an unreachable backend or unparseable
    reply returns the heuristic plan with a ``"composer unavailable"``
    note — never raises. ``strict=True``: those failures raise
    :class:`monteur.ai.MonteurAIError` with the actionable message (the
    Studio's mode — the user explicitly asked for the AI cut).
    """
    plan = plan_montage(reports, music, style=style, **plan_kwargs)
    if not plan.entries:
        return plan

    # An arrangement (forwarded verbatim to plan_montage above) claims the
    # slots 0..k-1 — deterministically, so the SAME k is recomputed here and
    # those slots are locked: flagged in the dossier, immune in _apply_cast.
    arrangement = plan_kwargs.get("arrangement") or []
    locked = frozenset(range(min(len(arrangement), len(plan.entries))))

    context = compose_context(plan, reports, music, style=style, locked=locked)
    prompt = _build_prompt(context, style, brief)
    try:
        raw = ai.complete(prompt, system=_SYSTEM, json_schema=COMPOSE_SCHEMA)
    except ai.MonteurAIError as exc:
        if strict:
            raise
        plan.notes.append(f"composer unavailable: {exc}; heuristic cut")
        return plan
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("the reply is not a JSON object")
    except ValueError as exc:
        if strict:
            raise ai.MonteurAIError(
                f"the composer came back with unparseable JSON: {raw[:200]!r}"
            ) from exc
        plan.notes.append(f"composer unavailable: {exc}; heuristic cut")
        return plan

    _apply_cast(plan, data, reports, locked=locked)
    if not context.get("vision"):
        plan.notes.append(
            'no vision labels — run "Let Claude watch your clips" '
            "(monteur create --see) for a smarter composed cut"
        )
    return plan
