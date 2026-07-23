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
from monteur.montage import (
    MontagePlan,
    _shares_material,  # the shared zero-repeat overlap rule
    plan_montage,
)
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
        # Optional: seconds into the cut where the music should enter. Must
        # be one of the dossier's `music_in_candidates`; anything else is
        # rejected with a note and the engine's own choice stands.
        "music_in": {
            "type": "number",
            "description": (
                "optional: when the music enters (seconds into the cut) — "
                "one of the dossier's music_in_candidates"
            ),
        },
    },
    "required": ["story", "cast", "titles", "why"],
}

#: A composer music_in within this many seconds of a candidate snaps to it;
#: anything further off is an invalid pick (kept the engine's choice).
_MUSIC_IN_MATCH = 0.75

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
    "short": (
        "Hook in the FIRST second: slot 0 takes the boldest, most arresting "
        "image in the footage — the pattern interrupt, not the prettiest or "
        "widest shot. Shorts never establish; the viewer decides in one "
        "second whether to stay. Punch relentlessly on the beat through the "
        "body — motion, faces, impact — every shot earning its slot. End on "
        "a frame that loops back into the opening: cast the last slot from "
        "the hook's own scene (or matching motion) so a replay feels "
        "seamless, and never wind down into a slow fade."
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
    *,
    allow_repeats: bool = True,
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
    the vision fields (label/tags/role/hero/group) and the offline
    ``daylight`` class (day/golden/night, :mod:`monteur.daylight`) when
    present — empty fields are omitted so an unseen inventory stays small
    and honest. ``vision`` says whether any annotations exist at all.
    When daylight classes exist the prompt states the time-coherence law
    (blocks with rare switches) and hands the block ORDER to the composer
    as a story decision to be explained in ``why``.

    ``locked`` (slot indices an arrangement already cast) marks those
    slots ``"locked": true`` in the dossier — the prompt tells Claude to
    compose around them, and :func:`_apply_cast` ignores any cast for
    them regardless. Empty (the default) leaves the dossier unchanged.

    ``allow_repeats=False`` (the plan was built with repeats off) sets
    ``"reuse_forbidden": true`` in the dossier: the prompt then forbids
    reusing a moment and :func:`_apply_cast` rejects reused casts to the
    heuristic fallback. The default (True) leaves the dossier unchanged.

    With music, the dossier also carries the adaptive music window:
    ``music_opening`` (the song's own opening character from
    :func:`monteur.music.intro_profile`, measured at the cut's source
    window), ``music_in`` (the engine's decision, 0 = first frame) and
    ``music_in_candidates`` (the musical entry points the composer may
    choose between — :func:`monteur.montage.music_window_candidates`).
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
            if getattr(m, "daylight", ""):
                item["daylight"] = m.daylight
            if getattr(m, "shot_size", ""):
                item["shot_size"] = m.shot_size
            # The editor's own note for THIS moment (Moments review step) — the
            # strongest steer, in the editor's words, for this exact stretch.
            if getattr(m, "user_note", "").strip():
                item["note"] = m.user_note.strip()
            inventory.append(item)

    # The editor's own per-clip notes (from the Clips review step) — Claude's
    # strongest steer: what THIS shot is and how to use it, in the editor's
    # words. Keyed by clip name so it lines up with the inventory items.
    clip_notes = {
        Path(report.path).name: report.user_note.strip()
        for report in reports
        if getattr(report, "user_note", "").strip()
    }

    context = {
        "style": chosen.key,
        "duration": _r(plan.duration),
        "slots": slots,
        "dips": dips,
        "inventory": inventory,
        "vision": _has_vision(reports),
    }
    if clip_notes:
        context["clip_notes"] = clip_notes
    if not allow_repeats:
        context["reuse_forbidden"] = True
    if music is not None:
        from monteur.music import intro_profile

        profile = intro_profile(music, start=plan.music_start)
        context["music_opening"] = {
            key: profile[key]
            for key in ("label", "rel_energy", "onset_density", "low_presence")
        }
        context["music_in"] = _r(getattr(plan, "music_in", 0.0) or 0.0)
        context["music_in_candidates"] = [
            _r(c["time"])
            for c in _montage.music_window_candidates(
                music, phases, music_start=plan.music_start
            )
        ]
    return context


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
    if any(item.get("daylight") for item in context.get("inventory") or []):
        parts.append(
            "TIME OF DAY: inventory moments carry a `daylight` class (day / "
            "golden / night), measured from the footage. COHERENCE IS THE "
            "LAW: keep the cut in time-of-day blocks — switch classes "
            "rarely and only on purpose; a lone night shot between two day "
            "shots reads like a mistake. The block ORDER is yours to "
            "direct: the natural arc is day -> golden -> night, but the "
            "brief may justify another order (a night teaser as a cold "
            "open is a legitimate choice). If you depart from the natural "
            "arc, say why in `why`."
        )
    if context.get("clip_notes"):
        parts.append(
            "THE EDITOR'S CLIP NOTES: `clip_notes` maps a clip name to the "
            "editor's own note about that shot — what it is, how they want it "
            "used, what to avoid. This is direct intent from the person whose "
            "film this is: weight it ABOVE the machine labels when casting "
            "those clips, and honour it in the story you tell."
        )
    if any(item.get("note") for item in context.get("inventory") or []):
        parts.append(
            "THE EDITOR'S MOMENT NOTES: an inventory moment may carry a `note` "
            "— the editor's own words about THAT exact stretch (what it is, how "
            "to use it, what to avoid). This is the strongest, most specific "
            "steer there is: weight it ABOVE every machine label for that "
            "moment, favour casting the moment where its note asks, and honour "
            "it in the story you tell."
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
    if len(context.get("music_in_candidates") or []) > 1:
        parts.append(
            "MUSIC ENTRY: `music_opening` describes how the song itself "
            "opens; `music_in` is the engine's current choice of when the "
            "music enters (0 = with the first frame — everything before it "
            "is a dry cold open carried by sound design). You may override "
            "it by answering with `music_in` set to one of "
            "`music_in_candidates` (seconds); omit the field to keep the "
            "engine's choice."
        )
    if context.get("reuse_forbidden"):
        reuse_rule = (
            "- repeats are OFF for this cut: NEVER reuse a moment — every "
            "slot must cast distinct material, and a cast repeating "
            "material already used in another slot is rejected (that slot "
            "falls back to the engine's own pick);\n"
        )
    else:
        reuse_rule = "- reuse a moment only when the slot count leaves no alternative;\n"
    parts.append(
        "Compose the cut:\n"
        "- cast EVERY slot: pick the clip and the `start` second whose "
        "material fills the slot's full `seconds` from `start`;\n"
        "- `start` must lie inside one of that clip's inventory windows "
        "and leave enough material for the slot;\n"
        + reuse_rule +
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


def _enforce_no_reuse(
    entries: list,
    heuristic: list,
    reports: list[ClipReport],
    cast_slots: set[int],
    locked: set[int] | frozenset[int],
    fallbacks: list[tuple[int, str]],
) -> tuple[int, list[tuple[int, int]]]:
    """Make the composed cut repeat-free, in place (repeats OFF only).

    Resolves every pair of entries sharing material (:func:`_shares_material`)
    with a clear precedence — the composer's explicit choice is worth
    saving, the engine's picks are fungible:

    * **cast vs cast** — the later cast is rejected to its heuristic
      entry (a per-slot ``fallbacks`` note; the reverted heuristic is
      re-checked against everything on the next pass).
    * **cast vs engine pick** — the ENGINE pick moves: it is re-sourced
      to unused moment material (:func:`monteur.montage._find_unused_window`;
      a shorter span keeps the record slot with the fill's own gap
      semantics). Only when no unused span remains is the cast rejected
      instead.
    * **cast vs locked slot** — the arrangement is final: the cast is
      rejected.
    * **engine vs engine** (reachable only after reverts) — the later
      one is re-sourced; two LOCKED slots sharing material are the
      editor's own arrangement demanding reuse and are left alone.

    Mutates ``entries``, ``cast_slots`` and ``fallbacks``; returns
    ``(re-sourced count, unresolvable pairs)`` — an unresolvable pair
    (no material anywhere) is left as-is but reported, never silent.
    """
    resourced = 0
    stuck: list[tuple[int, int]] = []
    skip: set[tuple[int, int]] = set()

    def first_collision() -> tuple[int, int] | None:
        for a in range(len(entries)):
            for b in range(a + 1, len(entries)):
                if (a, b) in skip:
                    continue
                if a in locked and b in locked:
                    continue  # the editor's own arrangement may reuse
                if _shares_material(entries[a], entries[b]):
                    return a, b
        return None

    def resource(idx: int) -> bool:
        nonlocal resourced
        entry = entries[idx]
        needed = entry.record_end - entry.record_start
        used = [
            (e.clip_path, e.source_start, e.source_end)
            for k, e in enumerate(entries)
            if k != idx
        ]
        found = _montage._find_unused_window(reports, used, needed)
        if found is None:
            return False
        report, moment, start, length = found
        entries[idx] = replace(
            entry,
            clip_path=report.path,
            source_start=start,
            source_end=start + length,
            score=moment.score,
            media_start=report.media_start,
            clip_duration=report.duration,
            label=getattr(moment, "label", ""),
        )
        resourced += 1
        return True

    def reject(idx: int, other: int) -> None:
        entries[idx] = replace(heuristic[idx])
        cast_slots.discard(idx)
        fallbacks.append(
            (idx, f"repeats are off — that material already plays in slot {other + 1}")
        )

    guard = 4 * len(entries) + 8  # bounded by construction; belt and braces
    while guard > 0:
        guard -= 1
        pair = first_collision()
        if pair is None:
            break
        i, j = pair
        ci, cj = i in cast_slots, j in cast_slots
        if ci and cj:
            reject(j, i)  # the later cast loses
        elif ci or cj:
            cast, engine = (i, j) if ci else (j, i)
            if engine in locked or not resource(engine):
                reject(cast, engine)
        else:
            movable = j if j not in locked else i
            if not resource(movable):
                stuck.append((i, j))
                skip.add((i, j))
    return resourced, stuck


def _apply_cast(
    plan: MontagePlan,
    data: dict,
    reports: list[ClipReport],
    locked: set[int] | frozenset[int] = frozenset(),
    music_in_candidates: list[float] | None = None,
    allow_repeats: bool = True,
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

    ``music_in_candidates`` (the dossier's musical entry points) validates
    an optional composer ``music_in``: the value snaps to the nearest
    candidate within :data:`_MUSIC_IN_MATCH` seconds and lands in
    ``plan.music_in`` with a note; anything else keeps the engine's own
    window with a note saying why.

    ``allow_repeats=False`` extends the zero-repeat promise to the
    composer via :func:`_enforce_no_reuse`: entries sharing material
    (same clip, >= :data:`monteur.montage._REUSE_OVERLAP_SHARE` overlap of the shorter
    window) are resolved — a cast reusing another cast's or a locked
    slot's material is rejected to the heuristic fallback with a
    per-slot note; a cast colliding with an engine pick keeps the cast
    and re-sources the engine pick to unused material (rejecting the
    cast only when nothing unused remains). ``True`` (the default)
    keeps deliberate reuse, noted as before.
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

    if not allow_repeats and cast_slots:
        resourced, stuck = _enforce_no_reuse(
            entries, plan.entries, reports, cast_slots, locked, fallbacks
        )
        if resourced:
            plan.notes.append(
                f"composer: {resourced} engine pick"
                + ("s" if resourced != 1 else "")
                + " re-sourced so the cast stays repeat-free"
            )
        for i, j in stuck:
            plan.notes.append(
                f"composer: slots {i + 1} and {j + 1} share material — no "
                "unused footage left to re-source (repeats are off)"
            )
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

    # Optional composer music entry: valid only against the dossier's own
    # candidate list, snapped onto the matched candidate.
    raw_music_in = data.get("music_in")
    if raw_music_in is not None and music_in_candidates:
        try:
            want = float(raw_music_in)
        except (TypeError, ValueError):
            want = None
        matched: float | None = None
        if want is not None:
            nearest = min(music_in_candidates, key=lambda t: abs(t - want))
            if abs(nearest - want) <= _MUSIC_IN_MATCH:
                matched = float(nearest)
        if matched is not None:
            plan.music_in = matched
            if matched > _EPS:
                plan.notes.append(
                    f"composer: music enters at {matched:.1f}s"
                )
            else:
                plan.notes.append("composer: music enters with the first frame")
        else:
            plan.notes.append(
                f"composer: music_in {raw_music_in!r} is not one of the "
                f"candidates — kept "
                f"{(getattr(plan, 'music_in', 0.0) or 0.0):.1f}s"
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
    on_text=None,
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

    ``allow_repeats`` in ``plan_kwargs`` (default False) carries the
    zero-repeat promise through the composer stage: with repeats off the
    dossier says reuse is forbidden and a reused cast is rejected to the
    heuristic entry (:func:`_apply_cast`); with ``allow_repeats=True``
    Claude may still reuse material deliberately, noted as before.

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

    # Repeats off (plan_montage's default) extends the zero-repeat promise
    # to the composer: the dossier says reuse is forbidden and _apply_cast
    # rejects reused casts to the heuristic fallback.
    allow_repeats = bool(plan_kwargs.get("allow_repeats", False))
    context = compose_context(
        plan, reports, music, style=style, locked=locked,
        allow_repeats=allow_repeats,
    )
    prompt = _build_prompt(context, style, brief)
    try:
        # on_text streams Claude's answer as it is written, so the storyboard
        # build can show the cut being composed live instead of a frozen wait.
        raw = ai.complete(
            prompt, system=_SYSTEM, json_schema=COMPOSE_SCHEMA, on_delta=on_text
        )
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

    _apply_cast(
        plan,
        data,
        reports,
        locked=locked,
        music_in_candidates=[
            float(t) for t in context.get("music_in_candidates") or []
        ],
        allow_repeats=allow_repeats,
    )
    if not context.get("vision"):
        plan.notes.append(
            'no vision labels — run "Let Claude watch your clips" '
            "(monteur create --see) for a smarter composed cut"
        )
    return plan
