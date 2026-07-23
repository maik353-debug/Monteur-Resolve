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

import copy
import json
import math
from dataclasses import replace
from pathlib import Path

from monteur import ai
from monteur import montage as _montage
from monteur.critique import Scorecard, critique, supersedes
from monteur.montage import (
    MontagePlan,
    _shares_material,  # the shared zero-repeat overlap rule
    plan_montage,
)
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport, Moment

_EPS = 1e-6

#: How many self-critique REVISION passes the composer may run after the
#: first cast (blueprint Wave 4: watch the cut, fix what misses). Each pass
#: costs one more completion, so it only fires when the first cut actually
#: misses a fixable acceptance metric (a picture peak off its beat) — a
#: clean first cut ships immediately. 0 disables the loop (the pre-4 behaviour).
COMPOSE_CRITIQUE_PASSES = 1

#: Reasoning depth for the compose completion. The compose is a structured
#: casting/story task over a dossier that is ALREADY fully analysed — no video
#: is read here — so it does not need a model's deepest extended thinking. On
#: the CLI backend the default is high, which can spend many MINUTES reasoning
#: (and, when that CLI bills, real money); capping it to "medium" cuts the wait
#: and cost sharply while keeping the reasoning that makes the cut good. (The
#: API structured path carries no thinking at all, so this only affects the CLI.)
COMPOSE_EFFORT = "medium"

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
        # Optional: the WHOLE cut's base tempo, in seconds per shot. Fast cuts
        # to music suit SOME videos (energetic montages) but are wrong for
        # others (a landscape / nature trailer must breathe throughout). Claude
        # knows which from the brief, so it sets the baseline here; omit to keep
        # the engine's fast default. `holds` still stretches specific shots on
        # top. The timeline never moves — a slower pace means FEWER, longer
        # shots over the same length, not a longer cut.
        "pace": {
            "type": "number",
            "description": (
                "optional base seconds-per-shot for the whole cut (~1 = fast "
                "music-video energy, ~4-6 = calm / cinematic / landscape). Omit "
                "for the engine's fast default."
            ),
        },
        # Optional: cast slots that should HOLD (breathe) — a hero, an
        # establishing wide, an emotional beat. Each stretches by absorbing
        # the short punchy shots right after it, so ONE shot breathes while
        # the rest of the cut stays fast. Duration/beat-grid/music never move.
        "holds": {
            "type": "array",
            "description": (
                "optional: slots that should breathe — a hero / establishing / "
                "emotional beat held long while the rest of the cut stays punchy"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "seconds": {
                        "type": "number",
                        "description": "optional target hold length (~2-6s)",
                    },
                },
                "required": ["slot"],
            },
        },
        # Optional: base tempo PER ARC PHASE, when the cut's speed should
        # VARY across its length — a trailer that opens on long 6-7s scenes,
        # RACES at the climax, then settles back to long shots at the end.
        # A single `pace` makes the whole cut one speed (which flattens that
        # arc); this sets a different seconds-per-shot for each phase. Any
        # phase omitted falls back to `pace`, then to the engine's default.
        # The timeline still never moves — each phase just holds fewer,
        # longer (or more, shorter) shots over its own stretch.
        "pace_by_phase": {
            "type": "object",
            "description": (
                "optional per-phase seconds-per-shot for a cut whose speed "
                "varies across the arc (e.g. slow opening 6, fast climax 1, "
                "slow outro 6). Each phase omitted falls back to `pace`."
            ),
            "properties": {
                "opening": {"type": "number"},
                "build": {"type": "number"},
                "climax": {"type": "number"},
                "outro": {"type": "number"},
                "hook": {"type": "number"},
                "punch": {"type": "number"},
                "loop": {"type": "number"},
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

#: Default and minimum length (seconds) of a composer-chosen breathing hold.
#: The maximum is montage._MAX_CUT_SECONDS — the same ceiling the engine's own
#: holds respect, so a breath never becomes a dead stare.
_BREATHE_TARGET = 3.5
_BREATHE_MIN = 2.0

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
    "You are a seasoned film editor composing a montage. The beat grid's slot "
    "positions and the black dips and fades are final — your job is the casting "
    "(which clip, from which second, fills each slot), the act titles on the "
    "dips, and the story they add up to. You also own the PACING, and this "
    "matters enormously: fast cuts to music are right for SOME pieces "
    "(energetic montages, hype reels) and completely wrong for others — a "
    "landscape or nature trailer, a mood piece, a cinematic teaser must breathe "
    "throughout, and a couple of long shots among fast ones fool no one. Read "
    "the brief and the footage and decide: set `pace` (base seconds per shot) "
    "to the tempo the CONTENT wants — small for energy, large (4-6s) for calm. "
    "When the speed should VARY across the cut — a trailer that opens on long "
    "6-7s scenes, RACES at the climax, then slows to long shots at the end — "
    "set `pace_by_phase` instead (a different seconds-per-shot per arc phase: "
    "opening, build, climax, outro), and the arc stays crisp. On top of the "
    "base, mark specific slots in `holds` to linger even longer (a hero, the "
    "shot on the drop, the resolving closer). A slower pace means fewer, longer "
    "shots over the same length — the cut never gets longer. Tell ONE story "
    "across the whole cut; every act must escalate or breathe on purpose. Use "
    "only clips and windows from the inventory — never invent material. Write "
    "titles in the language of the editor's brief and labels."
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
            # ...and the editor's star rating (1..5), a direct preference signal
            if getattr(m, "user_rating", 0):
                item["rating"] = int(m.user_rating)
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
    if any(item.get("rating") for item in context.get("inventory") or []):
        parts.append(
            "THE EDITOR'S MOMENT RATINGS: an inventory moment may carry a "
            "`rating` from 1 to 5 — the editor's own star rating for that "
            "stretch. Strongly favour casting the 4- and 5-star moments (the "
            "editor loves them) and lean away from the 1- and 2-star ones; a "
            "rating outweighs the machine `score` for that moment."
        )
    if any(slot.get("locked") for slot in context.get("slots") or []):
        parts.append(
            "Slots marked `locked` are already cast by the editor's own "
            "arrangement — their material and order are FINAL. Do not "
            "recast them; compose the remaining slots, the titles and the "
            "story around them."
        )
    parts.append(
        "PACING — the single biggest quality lever, DECIDE IT DELIBERATELY: the "
        "slot lengths in the dossier are the engine's FAST default (cut to the "
        "music). That is right for an energetic montage and WRONG for a calm "
        "piece. First set `pace` = the base seconds per shot the CONTENT wants: "
        "~1 for hype/energy, ~2-3 for a steady story, ~4-6 for a landscape / "
        "nature / mood / cinematic cut that should breathe THROUGHOUT (do not "
        "leave such a piece on the fast default — a few long shots among fast "
        "ones is not enough). Omit `pace` only when the fast default is genuinely "
        "right. When the tempo should CHANGE across the arc — e.g. a trailer "
        "that opens on long 6-7s scenes, RACES at the climax, then settles back "
        "to long shots at the outro — use `pace_by_phase` instead of one flat "
        "`pace`: give each phase its own seconds-per-shot (opening 6, build 2.5, "
        "climax 1, outro 6); any phase you leave out falls back to `pace`. "
        "Then, ON TOP of the base, use `holds` for the FEW slots that "
        "should linger even longer — an establishing opener, the hero on the "
        "drop, the resolving closer (each a `slot` + optional `seconds`). Both "
        "levers only ever GROW a shot by absorbing the quick shots after it, so "
        "the total length and the beats never move — a slower pace just means "
        "fewer, longer shots. NEVER re-pace or hold a `locked` slot."
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


def _apply_pacing(
    plan: MontagePlan, pace, holds, locked=frozenset(), pace_by_phase=None
) -> None:
    """Re-pace a composed cut to the composer's own tempo — in place.

    Three levers, one mechanism (a shot GROWS by absorbing the quick shots
    right after it, so the timeline never moves — total duration, the beat
    grid, the music sync, the dips and the SFX all stay put; a slower feel
    means FEWER, longer shots over the same length, not a longer cut):

    * ``pace`` (seconds) sets the WHOLE cut's base shot length. Fast cuts to
      music suit energetic montages; a landscape / nature / cinematic piece
      must breathe throughout, and one or two long shots among fast ones do
      nothing — so the composer, which knows the brief, can slow EVERY shot.
      ``None`` keeps the engine's fast default.
    * ``pace_by_phase`` (a ``{phase: seconds}`` map) sets a DIFFERENT base per
      arc phase, for a cut whose speed varies across its length — a trailer
      that opens on long 6-7s scenes, RACES at the climax, then settles back
      to long shots at the outro. A phase not in the map falls back to
      ``pace``. Merging never crosses a phase boundary, so the arc stays
      crisp (a slow opening never bleeds a long shot into the fast build).
    * ``holds`` names specific slots to hold even longer on top of the base
      (a hero, the shot on the drop, the resolving closer).

    Safe by construction: a shot that would cross a black dip, a phase
    boundary, a locked (arrangement) slot, the ``montage._MAX_CUT_SECONDS``
    ceiling, or the source clip's own available footage simply absorbs fewer
    shots (or none). A ``None`` pace with no phase map and no holds is a pure
    no-op.
    """
    entries = plan.entries
    n = len(entries)
    if n < 2:
        return
    dip_starts = [d[0] for d in (plan.dips or [])]
    phases = plan.phases or []

    def _dip_at(t: float) -> bool:
        return any(abs(t - ds) <= _DROP_MATCH for ds in dip_starts)

    def _phase_of(i: int) -> str:
        return _montage._phase_label_at(phases, entries[i].record_start) or ""

    def _clamp(sec: float) -> float:
        return max(_BREATHE_MIN, min(_montage._MAX_CUT_SECONDS, sec))

    def _clamp_pace(sec: float) -> float:
        # a pace target is NOT floored at _BREATHE_MIN: a fast phase (e.g. a 1s
        # climax) is meant to STAY fast — a target under the current shot
        # length just yields no merge — while a holds floor would wrongly bump
        # it up to 2s and slow the peak. Only the ceiling matters.
        return max(0.1, min(_montage._MAX_CUT_SECONDS, sec))

    # a clean {phase: seconds} view of pace_by_phase (bad entries dropped).
    phase_pace: dict[str, float] = {}
    if isinstance(pace_by_phase, dict):
        for label, raw in pace_by_phase.items():
            if raw is None:
                continue
            try:
                phase_pace[str(label)] = _clamp_pace(float(raw))
            except (TypeError, ValueError):
                continue

    base = None
    if pace is not None:
        try:
            base = _clamp_pace(float(pace))
        except (TypeError, ValueError):
            base = None

    # the per-slot target length: each slot's phase pace (if any) else the
    # base pace, then holds override with their (usually longer) target.
    targets: dict[int, float] = {}
    if phase_pace or base is not None:
        for i in range(n):
            if i in locked:
                continue
            t = phase_pace.get(_phase_of(i)) if phase_pace else None
            if t is None:
                t = base
            if t is not None:
                targets[i] = t
    for h in holds or []:
        if not isinstance(h, dict):
            continue
        try:
            s = int(h["slot"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= s < n) or s in locked:
            continue
        raw = h.get("seconds")
        try:
            sec = _clamp(float(raw)) if raw is not None else _clamp(_BREATHE_TARGET)
        except (TypeError, ValueError):
            sec = _clamp(_BREATHE_TARGET)
        targets[s] = max(targets.get(s, 0.0), sec)  # a hold never shortens the base
    if not targets:
        return

    merged: list = []
    held = 0
    i = 0
    while i < n:
        e = entries[i]
        target = targets.get(i)
        if target is not None and (e.record_end - e.record_start) < target - _EPS:
            # Source-footage guard: only when the clip duration is KNOWN. An
            # unknown duration (0.0) is treated as unbounded — same as the
            # planner's own extension rule (montage: "if prev.clip_duration >
            # eps and ...") — otherwise avail goes negative and every merge is
            # wrongly blocked, silently no-op'ing pace/holds on those slots.
            known_dur = (e.clip_duration or 0.0) > _EPS
            avail = (e.clip_duration or 0.0) - e.source_start
            new_end = e.record_end
            j = i + 1
            while (
                (new_end - e.record_start) < target - _EPS
                and j < n
                and j not in locked
                and not _dip_at(entries[j - 1].record_end)  # don't swallow a dip
                and _phase_of(j) == _phase_of(i)  # never merge across the arc
                and (entries[j].record_end - e.record_start)
                <= _montage._MAX_CUT_SECONDS + _EPS
                and (
                    not known_dur
                    or (entries[j].record_end - e.record_start) <= avail + _EPS
                )
            ):
                new_end = entries[j].record_end
                j += 1
            if new_end > e.record_end + _EPS:
                length = new_end - e.record_start
                merged.append(
                    replace(e, record_end=new_end, source_end=e.source_start + length)
                )
                held += 1
                i = j
                continue
        merged.append(e)
        i += 1

    if held:
        plan.entries = merged
        if phase_pace:
            arc = ", ".join(f"{k} ~{v:g}s" for k, v in phase_pace.items())
            note = f"pacing: tempo varies by phase ({arc})"
        elif pace is not None:
            note = f"pacing: cut re-paced to ~{float(pace):g}s shots"
        else:
            note = f"breathing: {held} shot{'s' if held != 1 else ''} held long"
        plan.notes.append(note)


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
            # Carry the CAST moment's picture peak (not the grid pick's stale
            # one) so the self-critique's coincidence metric honestly scores
            # whether Claude's chosen action lands on this slot's cut.
            peak_source=getattr(moment, "peak_time", -1.0),
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


# --- self-critique: watch the cut, fix what misses ----------------------------------


def _valid_cast(data: dict) -> dict[int, dict]:
    """The reply's cast as ``{slot: {slot,clip,start,...}}`` (bad rows dropped)."""
    out: dict[int, dict] = {}
    for raw in data.get("cast") or []:
        if not isinstance(raw, dict):
            continue
        try:
            slot = int(raw["slot"])
            str(raw["clip"])
            float(raw["start"])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(slot, raw)
    return out


def _merge_cast(base: dict, repair: dict) -> dict:
    """A new reply: ``base`` with ``repair``'s picks overriding by slot.

    A revision pass only names the slots it changes; every other slot keeps
    its first-pass pick. Story/why/titles stay the base's (a revision is
    about the cut, not the words) unless the repair supplies its own; the
    pacing levers (``pace``/``holds``/``pace_by_phase``/``music_in``) are
    taken from the repair whenever it sends them.
    """
    cast = dict(_valid_cast(base))
    cast.update(_valid_cast(repair))
    merged = dict(base)
    merged["cast"] = [cast[s] for s in sorted(cast)]
    for key in ("pace", "holds", "pace_by_phase", "music_in", "titles", "why", "story"):
        if repair.get(key) is not None:
            merged[key] = repair[key]
    return merged


def _compose_pass(
    grid: MontagePlan,
    data: dict,
    reports: list[ClipReport],
    locked,
    music_in_candidates: list[float],
    allow_repeats: bool,
) -> tuple[MontagePlan, Scorecard]:
    """Cast ``data`` onto a FRESH copy of the pristine grid and score it.

    Each pass starts from an untouched deep copy of the grid so a revision
    re-casts from the same clean slate (stable slot indices, no compounded
    notes) rather than on top of the previous pass. Scored BEFORE pacing —
    the metrics talk about the CAST's cuts (stable original slot numbers the
    revision prompt can name); the winning cast is paced once, afterwards.
    """
    plan = copy.deepcopy(grid)
    _apply_cast(
        plan,
        data,
        reports,
        locked=locked,
        music_in_candidates=music_in_candidates,
        allow_repeats=allow_repeats,
    )
    return plan, critique(plan)


def _fixable_culprits(card: Scorecard, locked) -> list[int]:
    """Interior, non-locked slots whose picture peak misses its beat.

    These are the slots a re-cast can actually fix (a different moment /
    start whose action lands on the cut). Locked (arrangement) slots are
    the editor's and left out.
    """
    m = card.metrics.get("coincidence")
    if m is None or not m.sample or m.passed:
        return []
    return [i for i in m.culprits if i not in locked]


def _repair_section(card: Scorecard, best_data: dict, locked) -> str:
    """The REVISION block appended to the base prompt for a critique pass.

    Names the failing acceptance metric (peak-on-beat) and the exact slots
    that miss, with each one's current pick, and asks Claude to re-cast only
    those slots to a moment whose action peak lands on the cut. The shot-size
    grammar polish (soft) rides along when it too is off.
    """
    cast = _valid_cast(best_data)
    lines = [
        "REVISION PASS — you already composed the cut below; now WATCH IT BACK "
        "against the house quality bar and fix only what misses. The timeline, "
        "the beats and every other slot stay exactly as they are.",
    ]
    coincidence = card.metrics.get("coincidence")
    culprits = _fixable_culprits(card, locked)
    if coincidence is not None and culprits:
        lines.append(
            f"PEAK-ON-BEAT (the house's first promise): {coincidence.detail}. "
            "The cut is only tight when the shot's strongest movement / impact "
            "lands right ON its cut. These slots miss it — recast EACH to a "
            "moment (or a start second) whose action peaks at the very start of "
            "the shot, or to a calmer moment with no mid-shot spike:"
        )
        for i in culprits:
            pick = cast.get(i)
            if pick is not None:
                lines.append(
                    f"  - slot {i + 1}: now {pick.get('clip')} @ "
                    f"{float(pick.get('start', 0.0)):.2f}s — its peak is off the cut"
                )
            else:
                lines.append(f"  - slot {i + 1}: its peak is off the cut")
    grammar = card.metrics.get("grammar")
    if grammar is not None and grammar.sample and not grammar.passed and grammar.culprits:
        pairs = ", ".join(str(i + 1) for i in grammar.culprits if i not in locked)
        if pairs:
            lines.append(
                f"SHOT GRAMMAR (polish): slots {pairs} repeat the previous shot's "
                "size — the eye wants scale to keep changing; pick a different "
                "framing where you can."
            )
    lines.append(
        "Return ONLY the slots you change in `cast` (slot + clip + start); every "
        "slot you omit keeps its current pick. You may also adjust `holds` / "
        "`pace` / `pace_by_phase`. Keep the story and titles."
    )
    return "\n".join(lines)


def _critique_note(first: Scorecard, best: Scorecard, passes: int) -> str:
    """One honest line narrating the self-critique loop for the plan notes."""
    bits = [f"{passes} revision pass{'es' if passes != 1 else ''}"]
    fm = first.metrics.get("coincidence")
    bm = best.metrics.get("coincidence")
    if fm is not None and bm is not None and fm.sample and bm.sample:
        bits.append(f"peak-on-beat {fm.value * 100:.0f}%→{bm.value * 100:.0f}%")
    verdict = "acceptance met" if best.passed() else "best effort kept"
    return "self-critique: " + ", ".join(bits) + f" — {verdict}"


# --- public API --------------------------------------------------------------------


def compose_montage(
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    *,
    style: str = "auto",
    brief: str = "",
    strict: bool = False,
    on_text=None,
    on_thinking=None,
    critique_passes: int | None = None,
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

    Self-critique (blueprint Wave 4): after the first cast, the cut is
    scored by :func:`monteur.critique.critique`. When Claude cast slots and
    a FIXABLE acceptance metric misses — a picture peak that does not land
    on its beat — up to ``critique_passes`` REVISION completions let the
    composer watch its own cut and re-cast only the flagged slots, and the
    best-scoring cast ships (with a ``self-critique:`` note). A clean first
    cut never spends a second completion. ``critique_passes`` defaults to
    :data:`COMPOSE_CRITIQUE_PASSES`; ``0`` restores the single-pass
    behaviour. Each pass re-casts a fresh copy of the pristine grid, so the
    timeline, beats, dips and locked (arrangement) slots never move.
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
        # on_text streams Claude's answer as it is written and on_thinking its
        # reasoning before that, so the storyboard build shows the cut being
        # thought through and composed live instead of a frozen wait. effort is
        # capped (COMPOSE_EFFORT) so the CLI backend does not reason for minutes
        # on a task that only reads the finished dossier.
        raw = ai.complete(
            prompt, system=_SYSTEM, json_schema=COMPOSE_SCHEMA,
            effort=COMPOSE_EFFORT, on_delta=on_text, on_thinking=on_thinking,
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

    music_in_candidates = [
        float(t) for t in context.get("music_in_candidates") or []
    ]
    # `plan` is the pristine beat grid — every pass re-casts a FRESH copy of it
    # (stable slot indices, no compounded notes), so a revision starts from the
    # same clean slate rather than on top of the last cast.
    grid = plan
    best_plan, best_card = _compose_pass(
        grid, data, reports, locked, music_in_candidates, allow_repeats
    )
    best_data = data
    first_card = best_card
    best_agg = best_card.aggregate()
    passes = 0
    budget = COMPOSE_CRITIQUE_PASSES if critique_passes is None else max(0, critique_passes)

    # Self-critique (blueprint Wave 4): watch the cut, fix what misses. When
    # Claude actually cast slots and a FIXABLE acceptance metric misses — a
    # picture peak that does not land on its beat (the house's first promise) —
    # let the composer see its own cut and re-cast only the flagged slots,
    # keeping the best-scoring cast across the bounded passes. A clean first
    # cut ships immediately and never spends a second completion.
    while (
        passes < budget
        and _valid_cast(best_data)
        and not best_card.passed()
        and _fixable_culprits(best_card, locked)
    ):
        passes += 1
        repair_prompt = prompt + "\n\n" + _repair_section(best_card, best_data, locked)
        try:
            raw = ai.complete(
                repair_prompt, system=_SYSTEM, json_schema=COMPOSE_SCHEMA,
                effort=COMPOSE_EFFORT, on_delta=on_text, on_thinking=on_thinking,
            )
            rdata = json.loads(raw)
            if not isinstance(rdata, dict):
                raise ValueError("not a JSON object")
        except (ai.MonteurAIError, ValueError):
            break  # a failed revision is never fatal — keep the best cut so far
        merged = _merge_cast(best_data, rdata)
        plan_i, card_i = _compose_pass(
            grid, merged, reports, locked, music_in_candidates, allow_repeats
        )
        # keep the better cast — acceptance first (a cut that meets the bar
        # always beats one that misses it), then higher aggregate; ties keep
        # the earlier winner (no churn).
        agg_i = card_i.aggregate()
        if supersedes(card_i, agg_i, best_card, best_agg):
            best_plan, best_card, best_data, best_agg = plan_i, card_i, merged, agg_i

    # Pace the WINNING cast once: the composer's base tempo (fast for energy,
    # slow for a landscape / cinematic piece), varied per phase when the arc
    # should speed up and slow down, plus specific held shots. Runs on the cast
    # entries (original slot indices); the timeline length never moves.
    _apply_pacing(
        best_plan,
        best_data.get("pace"),
        best_data.get("holds") or [],
        locked,
        pace_by_phase=best_data.get("pace_by_phase"),
    )
    if passes:
        best_plan.notes.append(_critique_note(first_card, best_card, passes))
    if not context.get("vision"):
        best_plan.notes.append(
            'no vision labels — run "Let Claude watch your clips" '
            "(monteur create --see) for a smarter composed cut"
        )
    return best_plan
