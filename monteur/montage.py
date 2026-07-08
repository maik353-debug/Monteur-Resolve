"""Montage builder: best moments + music beats -> a first cut.

Takes the sifted footage (:mod:`monteur.sift`) and an analyzed song
(:mod:`monteur.music`) and lays out a rough cut on the beat grid: calm
sections cut slower (every few beats), high-energy sections cut faster.
The result is a Timeline — video from the footage on V1, the song on A1 —
ready for EDL/FCPXML export into Resolve.

Slotting algorithm
------------------
1. The montage length is ``min(song duration, max_duration)``.
2. A cut grid is walked beat by beat: in a "high" section the next cut is
   1 beat away, in "mid" 2 beats, in "low" 4 beats. If that interval would
   be shorter than :data:`MIN_CUT_INTERVAL` (0.4 s) the beat step is doubled
   until it isn't (no strobing). With no beats at all, a fixed 2 s grid is
   used and noted. Cuts at/after the montage length are dropped; the last
   slot always ends exactly at the montage length.
3. Every moment from every report goes into one pool (keeping its clip
   path). CHRONOLOGICAL sorts the pool by (clip path, moment start) and
   fills slots left to right. BEST_FIRST sorts the pool by score descending
   and visits the slots in section-energy order (highest first, ties by
   record time), so the best material lands on the loudest music; entries
   are re-sorted by record time afterwards.
4. Reuse rules: on the first pass every pool moment serves exactly one
   slot, consuming a slot-length piece from its start. Once the pool runs
   short, unconsumed tails are sliced first — the pool is scanned cyclically
   for the next moment with unused material and the next non-overlapping
   slot-length piece is taken (a long moment thus splits into several
   pieces). Only when *no* moment has unused material left is a moment
   rewound and its footage repeated, and that is noted.
5. Each entry takes ``slot length`` seconds starting at
   ``moment.start + consumed``. If the remaining piece is shorter than the
   slot it is padded by extending toward the clip's end; if even that is
   not enough, the short piece is kept (record stays on the grid) and a
   gap is noted.

Styles
------
``plan_montage(..., style=...)`` picks a :data:`STYLES` entry. "auto"
(the default) keeps the section-energy grid described above. A named style
instead maps a story arc — (share_of_duration, phase) pairs over
opening/build/climax/outro — onto the montage duration and cuts each phase
at its own beat density. Grid points still snap to musical positions:
phase boundaries snap to the nearest phrase start (falling back to
downbeats, then beats, when phrases are unknown), slow phases
(>= 4 beats per cut) place their cuts on downbeats, fast phases walk the
beat grid; with neither beats nor downbeats the fixed 2 s grid is used,
exactly as in "auto".

Drops
-----
With a named style that has a climax phase, the climax start is aligned to
the FIRST drop: boundaries before it are scaled by ``drop / original``,
boundaries after it are scaled toward the end by
``(length - drop) / (length - original)``. Limits: only the first drop is
used, and only when it lies within 5%..95% of the montage — otherwise a
note explains why alignment was skipped. In "auto", every in-range drop
forces a cut exactly on the drop and the slot starting there is reserved
for the unused moment with the highest (highlight, score), so the impact
lands on the strongest material.

Highlights and motion matching
------------------------------
In the phase named by ``style.prefer_highlights_in`` (usually "climax")
the candidate window is re-sorted by (highlight, score) instead of the
plain pool order, so audible peaks (cheers, laughter, action) land on the
musical peak. The ordering mode (CHRONOLOGICAL / BEST_FIRST) still decides
WHICH moments are in play; these refinements — and motion matching — only
break near-ties among the next few candidates: for each slot the next
K = 4 unconsumed pool items are scored with
``0.7 * order_preference + 0.3 * motion_continuity`` where order
preference is ``1 - position / K`` (earlier in the pool = higher) and
motion continuity is the cosine similarity between the previous slot's
exit motion and the candidate's entry motion (neutral 0 unless both
vectors exceed 0.5 px). With neutral motion the earliest candidate always
wins, so behavior without motion data is unchanged.

Finishing
---------
A montage shorter than the song ends on a musical boundary:
``end_on_phrase=True`` (the default) snaps the requested length to the
nearest phrase start within ±12% (ties prefer the shorter cut; downbeats,
then beats, serve as fallbacks; the change is never allowed to exceed
12%, and a full-song montage is left alone). Styles with an outro phase
plan a 0.5 s fade-in and a fade-out of min(2 s, last outro slot) on
:class:`MontagePlan` (``fade_in`` / ``fade_out``); "auto" plans 0.5 s /
1 s. Entries in gentle phases — >= 4 beats per cut, i.e. opening/outro,
or "low" sections in "auto" — carry a dissolve INTO them:
``MontageEntry.transition`` = min(0.5 s, half the slot length), always 0
for the montage's first entry (its fade is ``fade_in``).
:func:`montage_to_timeline` publishes dissolves as clip metadata
(``"transition"`` / ``"transition_frames"``) and the fades as timeline
metadata (``"fade_in_frames"`` / ``"fade_out_frames"``) so the EDL/FCPXML
writers can carry the dissolves into Resolve. Audio fades cannot ride
along in either export format; a plan note reminds the editor to apply
the music fade in Resolve.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from pathlib import PurePath

from monteur.model import AUDIO, VIDEO, Clip, Marker, Timeline, seconds_to_frames
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import ClipReport, Moment

CHRONOLOGICAL = "chronological"  # keep footage order (travel/event films)
BEST_FIRST = "best_first"  # strongest material on the strongest sections

# How many beats between cuts, per section energy label.
BEATS_PER_CUT = {"high": 1, "mid": 2, "low": 4}
# Anti-strobe floor: a cut interval below this doubles the beat step.
MIN_CUT_INTERVAL = 0.4
# Grid interval used when the song has no detected beats.
FALLBACK_INTERVAL = 2.0

# A phase cutting every >= this many beats is "slow": its cuts go on downbeats.
_SLOW_PHASE_STEP = 4
# Drop alignment only when the drop falls inside this share of the montage.
_DROP_ALIGN_MARGIN = 0.05
# Candidate window (K): unconsumed pool items considered per slot.
_CANDIDATE_WINDOW = 4
# Blend weights for near-tie breaking among the candidate window.
_ORDER_WEIGHT = 0.7
_MOTION_WEIGHT = 0.3
# Below this magnitude (px) a motion vector counts as "no motion" (neutral).
_MOTION_MIN_MAGNITUDE = 0.5
# Musical ending: max relative change when snapping the length to a phrase.
_END_SNAP_TOLERANCE = 0.12
# Dissolve INTO a gentle-phase entry: min(this, half the slot length).
_MAX_DISSOLVE = 0.5
# Planned fades (seconds) for styles with an outro phase / for "auto".
_FADE_IN = 0.5
_MAX_FADE_OUT = 2.0
_AUTO_FADE_OUT = 1.0

_EPS = 1e-6


@dataclass(frozen=True)
class MontageStyle:
    """An editorial cutting style: a story arc mapped onto the song."""

    key: str
    name: str
    description: str  # one line an editor understands
    # (share_of_duration, phase label "opening"/"build"/"climax"/"outro").
    # Empty arc = section-energy-driven ("auto"). A label may repeat in
    # consecutive entries; the beat step then ramps toward the next phase's
    # step ("trailer" uses this to accelerate through its split build).
    arc: list[tuple[float, str]]
    beats_per_cut: dict[str, int]  # phase label -> beats between cuts
    prefer_highlights_in: str = "climax"  # phase where highlights win slots


_ARC_STANDARD = [(0.15, "opening"), (0.35, "build"), (0.35, "climax"), (0.15, "outro")]

STYLES: dict[str, MontageStyle] = {
    "auto": MontageStyle(
        key="auto",
        name="Auto (section energy)",
        description=(
            "Follows the song's own energy: calm sections cut every 4 beats, mid "
            "every 2, loud every beat; a drop forces a cut with the strongest moment."
        ),
        arc=[],
        beats_per_cut={},
    ),
    "travel": MontageStyle(
        key="travel",
        name="Travel film",
        description=(
            "Scenic slow opening, steady build, beat-for-beat climax, calm outro "
            "(4/2/1/4 beats per cut over a 15/35/35/15 arc)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 4, "build": 2, "climax": 1, "outro": 4},
    ),
    "wedding": MontageStyle(
        key="wedding",
        name="Wedding film",
        description=(
            "Gentle throughout — never faster than every 2 beats, so faces and "
            "gestures get room to breathe (4/2/2/4)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 4, "build": 2, "climax": 2, "outro": 4},
    ),
    "music_video": MontageStyle(
        key="music_video",
        name="Music video",
        description=(
            "Fast throughout — cuts every 1-2 beats from the first bar for "
            "constant energy (2/1/1/2)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 2, "build": 1, "climax": 1, "outro": 2},
    ),
    "trailer": MontageStyle(
        key="trailer",
        name="Trailer",
        description=(
            "Long tease, accelerating build (every 2 beats, then every beat), "
            "hard climax, snap outro (20/50/20/10 arc)."
        ),
        # The build is split in half so the beat step can ramp 2 -> 1.
        arc=[(0.2, "opening"), (0.25, "build"), (0.25, "build"), (0.2, "climax"), (0.1, "outro")],
        beats_per_cut={"opening": 4, "build": 2, "climax": 1, "outro": 4},
    ),
}


@dataclass
class MontagePlan:
    """The chosen cut points before rendering to a timeline."""

    music_path: str
    duration: float  # seconds, montage length (may be shorter than the song)
    fade_in: float = 0.0  # seconds, intended music/video fade-in
    fade_out: float = 0.0  # seconds, intended music/video fade-out
    entries: list["MontageEntry"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class MontageEntry:
    clip_path: str
    source_start: float  # seconds in the clip
    source_end: float
    record_start: float  # seconds in the montage
    record_end: float
    score: float
    transition: float = 0.0  # seconds of dissolve INTO this entry (0 = cut)


# --- grid -------------------------------------------------------------------


def _label_at(sections: list[MusicSection], t: float) -> str:
    """Energy label of the section containing ``t`` ("mid" if uncovered)."""
    for s in sections:
        if s.start - _EPS <= t < s.end - _EPS:
            return s.label
    if sections and t >= sections[-1].end - _EPS:
        return sections[-1].label
    return "mid"


def _energy_at(sections: list[MusicSection], t: float) -> float:
    """Energy value of the section containing ``t`` (0.5 if uncovered)."""
    for s in sections:
        if s.start - _EPS <= t < s.end - _EPS:
            return s.energy
    if sections and t >= sections[-1].end - _EPS:
        return sections[-1].energy
    return 0.5


def _nth_beat_after(beats: list[float], t: float, n: int) -> float | None:
    """The n-th beat strictly after ``t``, or None if beats run out."""
    i = bisect.bisect_right(beats, t + _EPS)
    j = i + n - 1
    return beats[j] if j < len(beats) else None


def _build_grid(music: MusicAnalysis, length: float) -> tuple[list[float], list[str]]:
    """Cut times ``[0, ..., length]`` walked on the beat grid."""
    notes: list[str] = []
    cuts = [0.0]
    beats = sorted(b for b in music.beats if b > _EPS or abs(b) <= _EPS)
    if not beats:
        notes.append(
            f"no beats detected; falling back to a fixed {FALLBACK_INTERVAL:g}s grid"
        )
        t = FALLBACK_INTERVAL
        while t < length - _EPS:
            cuts.append(t)
            t += FALLBACK_INTERVAL
    else:
        cur = 0.0
        while True:
            step = BEATS_PER_CUT.get(_label_at(music.sections, cur), 2)
            nxt = _nth_beat_after(beats, cur, step)
            # Anti-strobe: double the beat step until the interval is sane.
            while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                step *= 2
                nxt = _nth_beat_after(beats, cur, step)
            if nxt is None or nxt >= length - _EPS:
                break  # beats ran out or past the end: close at `length`
            cuts.append(nxt)
            cur = nxt
    cuts.append(length)
    return cuts, notes


def _nearest(points: list[float], t: float) -> float:
    """Nearest value in a sorted, non-empty list (ties go to the earlier one)."""
    i = bisect.bisect_left(points, t)
    if i <= 0:
        return points[0]
    if i >= len(points):
        return points[-1]
    before, after = points[i - 1], points[i]
    return before if t - before <= after - t else after


def _snap_ending_length(music: MusicAnalysis, length: float) -> tuple[float | None, str]:
    """Musical boundary to end a truncated montage on, or (None, "").

    Looks for the boundary nearest to ``length`` — phrase starts first,
    falling back to downbeats, then beats — but only within
    ±``_END_SNAP_TOLERANCE`` (12%) of the requested length; equidistant
    candidates prefer the shorter montage. Returns (None, "") when no
    boundary qualifies or the nearest one IS the requested length (no
    change needed). The returned time never exceeds the song duration.
    """
    tolerance = _END_SNAP_TOLERANCE * length
    for cand, kind in (
        (music.phrases, "phrase"),
        (music.downbeats, "downbeat"),
        (music.beats, "beat"),
    ):
        pts = sorted(p for p in cand if _EPS < p <= music.duration + _EPS)
        if not pts:
            continue
        i = bisect.bisect_left(pts, length)
        neighbours = ([pts[i - 1]] if i > 0 else []) + ([pts[i]] if i < len(pts) else [])
        best: float | None = None
        for p in neighbours:  # shorter first: a tie keeps the shorter cut
            d = abs(p - length)
            if d <= tolerance + _EPS and (best is None or d < abs(best - length) - _EPS):
                best = p
        if best is not None:
            if abs(best - length) <= _EPS:
                return None, ""  # already on a boundary
            return best, kind
    return None, ""


def _phase_steps(style: MontageStyle) -> list[int]:
    """Beats-per-cut for every arc entry.

    A run of consecutive arc entries with the same label ramps linearly from
    that label's own step to the FOLLOWING phase's step — "trailer" uses this
    to accelerate through its split build (every 2 beats, then every beat).
    """
    labels = [lab for _, lab in style.arc]
    steps: list[int] = []
    i = 0
    while i < len(labels):
        j = i
        while j + 1 < len(labels) and labels[j + 1] == labels[i]:
            j += 1
        own = style.beats_per_cut.get(labels[i], 2)
        if j > i:
            nxt = style.beats_per_cut.get(labels[j + 1], own) if j + 1 < len(labels) else own
            span = j - i
            for r in range(span + 1):
                steps.append(max(1, round(own + (nxt - own) * r / span)))
        else:
            steps.append(own)
        i = j + 1
    return steps


def _build_style_grid(
    music: MusicAnalysis, length: float, style: MontageStyle
) -> tuple[list[float], list[tuple[float, float, str]], list[str]]:
    """Cut grid and phase spans ``(start, end, label)`` for a named style.

    Phase boundaries are the arc shares mapped onto ``length``, snapped to
    the nearest phrase start (falling back to downbeats, then beats). If the
    song has drops and the arc has a climax, the climax start is pinned to
    the first drop and the neighbouring boundaries are scaled proportionally
    (limits: first drop only, and only when it lies within 5%..95% of the
    montage — otherwise a note explains the skip). Slow phases
    (>= ``_SLOW_PHASE_STEP`` beats per cut) cut on downbeats, fast phases
    walk the beat grid with the phase's step; with neither beats nor
    downbeats the fixed 2 s fallback grid is used, exactly as in "auto".
    """
    notes: list[str] = []
    labels = [lab for _, lab in style.arc]
    total_share = sum(share for share, _ in style.arc) or 1.0
    bounds = [0.0]
    acc = 0.0
    for share, _ in style.arc:
        acc += share
        bounds.append(length * acc / total_share)
    bounds[-1] = length

    # Drop = climax: pin the climax start to the first drop.
    pinned: set[int] = set()
    drops = sorted(d for d in music.drops)
    if drops and "climax" in labels:
        drop = drops[0]
        climax_i = labels.index("climax")
        orig = bounds[climax_i]
        if not (_DROP_ALIGN_MARGIN * length <= drop <= (1 - _DROP_ALIGN_MARGIN) * length):
            notes.append(
                f"drop at {drop:.1f}s outside 5-95% of the montage; climax not aligned"
            )
        elif climax_i == 0 or orig <= _EPS or orig >= length - _EPS:
            notes.append("climax phase starts at the montage edge; drop alignment skipped")
        else:
            for i in range(1, climax_i):
                bounds[i] *= drop / orig
            bounds[climax_i] = drop
            for i in range(climax_i + 1, len(bounds) - 1):
                bounds[i] = length - (length - bounds[i]) * (length - drop) / (length - orig)
            pinned.add(climax_i)
            notes.append(f"climax aligned to drop at {drop:.1f}s")

    # Snap the remaining interior boundaries to musical positions:
    # phrases, else downbeats, else beats.
    snap_points: list[float] = []
    snapped_to = ""
    for cand, kind in (
        (music.phrases, "phrase starts"),
        (music.downbeats, "downbeats"),
        (music.beats, "beats"),
    ):
        pts = sorted(p for p in cand if _EPS < p < length - _EPS)
        if pts:
            snap_points, snapped_to = pts, kind
            break
    snapped = 0
    for i in range(1, len(bounds) - 1):
        if i in pinned or not snap_points:
            continue
        bounds[i] = _nearest(snap_points, bounds[i])
        snapped += 1
    for i in range(1, len(bounds)):  # keep boundaries monotonic
        bounds[i] = min(max(bounds[i], bounds[i - 1]), length)

    phases = [(bounds[i], bounds[i + 1], labels[i]) for i in range(len(labels))]

    beats = sorted(b for b in music.beats if b > -_EPS)
    downs = sorted(d for d in music.downbeats if d > -_EPS)
    pulse = beats or downs  # graceful: no beats -> walk downbeats instead
    cuts = [0.0]
    downbeat_cuts = 0
    if not pulse:
        notes.append(
            f"no beats detected; falling back to a fixed {FALLBACK_INTERVAL:g}s grid"
        )
        t = FALLBACK_INTERVAL
        while t < length - _EPS:
            cuts.append(t)
            t += FALLBACK_INTERVAL
    else:
        for (p_start, p_end, _label), step in zip(phases, _phase_steps(style)):
            cur = cuts[-1]
            slow = step >= _SLOW_PHASE_STEP and bool(downs)
            while True:
                if slow:  # slow phase: one cut per downbeat
                    n = 1
                    nxt = _nth_beat_after(downs, cur, n)
                    while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                        n += 1  # anti-strobe: skip to a later downbeat
                        nxt = _nth_beat_after(downs, cur, n)
                else:  # fast phase: walk the beat grid
                    s = step
                    nxt = _nth_beat_after(pulse, cur, s)
                    while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                        s *= 2
                        nxt = _nth_beat_after(pulse, cur, s)
                if nxt is None or nxt >= p_end - _EPS:
                    break
                cuts.append(nxt)
                if slow:
                    downbeat_cuts += 1
                cur = nxt
            if p_end < length - _EPS and p_end > cuts[-1] + _EPS:
                cuts.append(p_end)  # the phase boundary itself is a cut
    cuts.append(length)

    if snapped and snapped_to:
        notes.append(f"{snapped} phase boundaries snapped to {snapped_to}")
    if downbeat_cuts:
        notes.append(f"{downbeat_cuts} cuts on downbeats")
    return cuts, phases, notes


# --- slot filling -------------------------------------------------------------


@dataclass
class _PoolItem:
    clip_path: str
    clip_duration: float
    moment: Moment
    consumed: float = 0.0  # seconds of the moment already placed
    uses: int = 0

    @property
    def remaining(self) -> float:
        return self.moment.end - (self.moment.start + self.consumed)


def _pick_reuse(
    pool: list[_PoolItem], start: int, held: set[int] | frozenset[int] = frozenset()
) -> _PoolItem | None:
    """First pool item (cyclic scan from ``start``) with unconsumed material.

    Indices in ``held`` (reserved for a not-yet-served drop slot) are skipped
    so their material stays fresh for the drop.
    """
    n = len(pool)
    for k in range(n):
        idx = (start + k) % n
        if idx in held:
            continue
        item = pool[idx]
        if item.remaining > _EPS:
            return item
    return None


def _motion_continuity(
    prev_exit: tuple[float, float] | None, entry: tuple[float, float]
) -> float:
    """Cosine similarity between exit and entry motion, in [-1, 1].

    Neutral 0 when there is no previous entry or either vector's magnitude
    is at or below ``_MOTION_MIN_MAGNITUDE`` px (i.e. effectively static).
    """
    if prev_exit is None:
        return 0.0
    ax, ay = prev_exit
    bx, by = entry
    mag_a = math.hypot(ax, ay)
    mag_b = math.hypot(bx, by)
    if mag_a <= _MOTION_MIN_MAGNITUDE or mag_b <= _MOTION_MIN_MAGNITUDE:
        return 0.0
    return (ax * bx + ay * by) / (mag_a * mag_b)


def _phase_label_at(phases: list[tuple[float, float, str]], t: float) -> str | None:
    """Phase label of the arc phase containing ``t`` (None if no phases)."""
    for start, end, label in phases:
        if start - _EPS <= t < end - _EPS:
            return label
    if phases and t >= phases[-1][1] - _EPS:
        return phases[-1][2]
    return None


def _fill(
    slots: list[tuple[float, float]],
    slot_order: list[int],
    pool: list[_PoolItem],
    phases: list[tuple[float, float, str]] | None = None,
    highlight_phase: str | None = None,
    drop_slots: set[int] | frozenset[int] = frozenset(),
) -> tuple[list[MontageEntry], list[str]]:
    """Assign pool moments to slots.

    The first pass still consumes every pool moment exactly once, in pool
    order — the ordering mode decides WHICH moments are in play — with two
    craft refinements that only reorder the next few candidates:

    * Drop slots are reserved up front for the unused moment with the
      highest (highlight, score), so the drop hits the strongest material.
    * For every other slot the next ``_CANDIDATE_WINDOW`` (K = 4) unconsumed
      pool items compete. Inside ``highlight_phase`` they are first re-sorted
      by (highlight, score) so audible peaks win the musical peak. The pick
      maximises ``0.7 * order_preference + 0.3 * motion_continuity`` where
      order preference is ``1 - position / K`` (earlier = higher) and motion
      continuity is the cosine similarity between the previous slot's exit
      motion and the candidate's entry motion (see
      :func:`_motion_continuity`). With neutral motion the earliest
      candidate always wins, so behavior without motion data is unchanged.

    Reuse (pool exhausted) is unchanged — cyclic scan for unconsumed tails,
    then rewind — except a drop slot still grabs the best remaining material.
    """
    entries: list[MontageEntry] = []
    notes: list[str] = []
    n = len(pool)
    rewound = False
    unused = list(range(n))  # pool indices not yet placed, in pool order
    reserved: dict[int, int] = {}  # slot index -> pool index held for a drop
    for drop_slot in sorted(drop_slots):
        if not unused:
            break
        pos = max(
            range(len(unused)),
            key=lambda p: (
                pool[unused[p]].moment.highlight,
                pool[unused[p]].moment.score,
                -unused[p],  # ties: earliest in pool order
            ),
        )
        reserved[drop_slot] = unused.pop(pos)
    held = set(reserved.values())  # kept out of reuse until their drop is served

    by_slot: dict[int, _PoolItem] = {}
    for visit, slot_idx in enumerate(slot_order):
        rec_start, rec_end = slots[slot_idx]
        slot_len = rec_end - rec_start
        if slot_idx in reserved:
            item = pool[reserved[slot_idx]]  # drop slot: strongest moment
            held.discard(reserved[slot_idx])
        elif unused:
            # First pass: choose among the next K unconsumed pool items.
            window = unused[:_CANDIDATE_WINDOW]
            if (
                highlight_phase
                and phases
                and _phase_label_at(phases, rec_start) == highlight_phase
            ):
                window = sorted(
                    window,
                    key=lambda i: (-pool[i].moment.highlight, -pool[i].moment.score, i),
                )
            prev = by_slot.get(slot_idx - 1)
            prev_exit = prev.moment.exit_motion if prev is not None else None
            best = max(
                enumerate(window),
                key=lambda pi: _ORDER_WEIGHT * (1.0 - pi[0] / _CANDIDATE_WINDOW)
                + _MOTION_WEIGHT
                * _motion_continuity(prev_exit, pool[pi[1]].moment.entry_motion),
            )[1]
            unused.remove(best)
            item = pool[best]
        else:
            item = None
            if slot_idx in drop_slots:  # late drop slot: best remaining tail
                leftovers = [
                    it for i, it in enumerate(pool) if i not in held and it.remaining > _EPS
                ]
                if leftovers:
                    item = max(
                        leftovers, key=lambda it: (it.moment.highlight, it.moment.score)
                    )
            if item is None:
                item = _pick_reuse(pool, visit % n, held)
            if item is None:  # everything consumed: rewind and repeat footage
                idx = visit % n
                for k in range(n):  # don't rewind a held (drop-reserved) moment
                    if (idx + k) % n not in held:
                        idx = (idx + k) % n
                        break
                item = pool[idx]
                item.consumed = 0.0
                rewound = True
        by_slot[slot_idx] = item
        moment = item.moment
        src_start = moment.start + item.consumed
        src_end = min(src_start + slot_len, moment.end)
        if src_end - src_start < slot_len - _EPS:
            # Pad the short piece by extending toward the clip's end.
            src_end = max(src_end, min(src_start + slot_len, item.clip_duration))
        if src_end - src_start < slot_len - _EPS:
            notes.append(
                f"gap at {rec_start:.2f}s: only {src_end - src_start:.2f}s of "
                f"source for a {slot_len:.2f}s slot"
            )
        item.consumed = src_end - moment.start
        item.uses += 1
        entries.append(
            MontageEntry(
                clip_path=item.clip_path,
                source_start=src_start,
                source_end=src_end,
                record_start=rec_start,
                record_end=rec_end,
                score=moment.score,
            )
        )
    if len(slot_order) > n:
        msg = f"material ran short: {len(slot_order)} slots for {n} moments; moments reused"
        if rewound:
            msg += " (some footage repeats)"
        notes.append(msg)
    return entries, notes


# --- public API ---------------------------------------------------------------


def plan_montage(
    reports: list[ClipReport],
    music: MusicAnalysis,
    order: str = CHRONOLOGICAL,
    max_duration: float | None = None,
    style: str = "auto",
    end_on_phrase: bool = True,
) -> MontagePlan:
    """Distribute the best moments across the song, in a cutting style.

    ``style`` selects a :data:`STYLES` entry. "auto" (the default) keeps the
    section-energy beat grid; a named style cuts on its story arc instead
    (see the module docstring for the grid, drop, highlight and motion
    rules). Unknown styles raise ValueError listing the valid ones.

    ``end_on_phrase`` (default True) gives a truncated montage a musical
    ending: when ``max_duration`` makes it shorter than the song, the length
    is snapped to the nearest phrase start (fallback: downbeats, then beats)
    within ±12% of the request — ties prefer the shorter cut, larger changes
    are never made, and a full-song montage is left alone. The plan also
    carries the intended fades (``fade_in`` / ``fade_out``) and per-entry
    dissolves for gentle phases (see the module docstring's Finishing
    section).
    """
    if style not in STYLES:
        valid = ", ".join(sorted(STYLES))
        raise ValueError(f"unknown style {style!r}; valid styles: {valid}")
    chosen = STYLES[style]

    length = music.duration if max_duration is None else min(music.duration, max_duration)
    end_note: str | None = None
    if end_on_phrase and max_duration is not None and length < music.duration - _EPS and length > _EPS:
        snapped_length, boundary_kind = _snap_ending_length(music, length)
        if snapped_length is not None:
            end_note = f"length snapped to {boundary_kind} at {snapped_length:.1f}s"
            length = snapped_length
    plan = MontagePlan(music_path=music.path, duration=max(length, 0.0))
    plan.notes.append(f'style "{chosen.key}": {chosen.name}')
    if end_note:
        plan.notes.append(end_note)
    if length <= _EPS:
        plan.notes.append("montage length is zero; nothing planned")
        return plan

    phases: list[tuple[float, float, str]] = []
    highlight_phase: str | None = None
    drop_starts: list[float] = []
    if chosen.arc:
        cuts, phases, grid_notes = _build_style_grid(music, length, chosen)
        highlight_phase = chosen.prefer_highlights_in
    else:
        cuts, grid_notes = _build_grid(music, length)
        # Auto style: every in-range drop forces a cut exactly on the drop;
        # the slot starting there is reserved for the strongest moment.
        for d in sorted({d for d in music.drops if _EPS < d < length - _EPS}):
            if not any(abs(c - d) <= _EPS for c in cuts):
                bisect.insort(cuts, d)
            drop_starts.append(d)
            grid_notes.append(f"cut forced at drop {d:.1f}s; strongest moment assigned")
    plan.notes.extend(grid_notes)
    slots = list(zip(cuts, cuts[1:]))
    drop_slots = {
        i
        for i, (s, _) in enumerate(slots)
        if any(abs(s - d) <= _EPS for d in drop_starts)
    }
    pool = [_PoolItem(r.path, r.duration, m) for r in reports for m in r.moments]
    if not slots or not pool:
        plan.notes.append("no slots or no moments; nothing planned")
        return plan

    if order == CHRONOLOGICAL:
        pool.sort(key=lambda it: (it.clip_path, it.moment.start))
        slot_order = list(range(len(slots)))
    elif order == BEST_FIRST:
        pool.sort(key=lambda it: (-it.moment.score, it.clip_path, it.moment.start))
        slot_order = sorted(
            range(len(slots)),
            key=lambda i: (-_energy_at(music.sections, slots[i][0]), slots[i][0]),
        )
    else:
        raise ValueError(f"unknown order: {order!r}")

    entries, fill_notes = _fill(slots, slot_order, pool, phases, highlight_phase, drop_slots)
    entries.sort(key=lambda e: e.record_start)
    plan.entries = entries
    plan.notes.extend(fill_notes)
    used = sum(1 for it in pool if it.uses)
    plan.notes.append(f"{len(slots)} slots filled, {used} of {len(pool)} moments used")
    _plan_finishing(plan, entries, music, chosen, phases)
    return plan


def _plan_finishing(
    plan: MontagePlan,
    entries: list[MontageEntry],
    music: MusicAnalysis,
    style: MontageStyle,
    phases: list[tuple[float, float, str]],
) -> None:
    """Set the plan's fades and gentle-phase dissolves (in place).

    Styles with an outro phase get ``fade_in`` = 0.5 s and ``fade_out`` =
    min(2 s, last outro slot length); "auto" gets 0.5 s / 1 s. Every entry
    in a gentle phase (>= ``_SLOW_PHASE_STEP`` beats per cut; "low"
    sections in "auto") except the montage's very first entry gets
    ``transition`` = min(0.5 s, half its slot length) — a dissolve INTO
    that entry. Notes the dissolve count and reminds that the music
    fade-out must be applied in Resolve (the export formats can't carry it).
    """
    if not entries:
        return
    arc_labels = [lab for _, lab in style.arc]
    if "outro" in arc_labels:
        plan.fade_in = _FADE_IN
        last = entries[-1]
        plan.fade_out = min(_MAX_FADE_OUT, last.record_end - last.record_start)
    elif not style.arc:  # "auto"
        plan.fade_in = _FADE_IN
        plan.fade_out = _AUTO_FADE_OUT

    dissolves = 0
    for entry in entries[1:]:  # the first entry's fade is fade_in, not a dissolve
        if style.arc:
            label = _phase_label_at(phases, entry.record_start)
            gentle = label is not None and style.beats_per_cut.get(label, 2) >= _SLOW_PHASE_STEP
        else:
            gentle = _label_at(music.sections, entry.record_start) == "low"
        if gentle:
            entry.transition = min(_MAX_DISSOLVE, (entry.record_end - entry.record_start) / 2.0)
            if entry.transition > _EPS:
                dissolves += 1
    if dissolves:
        plan.notes.append(f"{dissolves} dissolves in gentle phases")
    if plan.fade_out > _EPS:
        plan.notes.append(f"music fade-out: {plan.fade_out:.1f}s (apply in Resolve)")


def montage_to_timeline(plan: MontagePlan, fps: float, name: str = "Monteur Montage") -> Timeline:
    """Render a MontagePlan as a Timeline (footage on V1, music on A1).

    Entries with a dissolve (``transition`` > 0) carry it in the video
    clip's metadata (``"transition"`` = ``"dissolve"``,
    ``"transition_frames"`` = the length in frames) so the EDL/FCPXML
    writers can emit it; the plan's fades land in ``timeline.metadata``
    as ``"fade_in_frames"`` / ``"fade_out_frames"``.
    """
    timeline = Timeline(name=name, fps=fps)
    for entry in plan.entries:
        stem = PurePath(entry.clip_path).stem
        rec_in = seconds_to_frames(entry.record_start, fps)
        rec_out = seconds_to_frames(entry.record_end, fps)
        src_in = seconds_to_frames(entry.source_start, fps)
        src_len = entry.source_end - entry.source_start
        rec_len = entry.record_end - entry.record_start
        if abs(src_len - rec_len) < _EPS:
            # Keep source and record durations frame-exact together.
            src_out = src_in + (rec_out - rec_in)
        else:
            src_out = seconds_to_frames(entry.source_end, fps)
        clip = Clip(
            name=stem,
            track="V1",
            kind=VIDEO,
            source_in=src_in,
            source_out=src_out,
            record_in=rec_in,
            record_out=rec_out,
            source_name=stem,
        )
        transition_frames = round(entry.transition * fps)
        if transition_frames > 0:
            clip.metadata["transition"] = "dissolve"
            clip.metadata["transition_frames"] = transition_frames
        timeline.clips.append(clip)
    if plan.fade_in > _EPS:
        timeline.metadata["fade_in_frames"] = seconds_to_frames(plan.fade_in, fps)
    if plan.fade_out > _EPS:
        timeline.metadata["fade_out_frames"] = seconds_to_frames(plan.fade_out, fps)
    music_stem = PurePath(plan.music_path).stem
    duration_frames = seconds_to_frames(plan.duration, fps)
    timeline.clips.append(
        Clip(
            name=music_stem,
            track="A1",
            kind=AUDIO,
            source_in=0,
            source_out=duration_frames,
            record_in=0,
            record_out=duration_frames,
            source_name=music_stem,
        )
    )
    timeline.markers.append(Marker(frame=0, name=f"Cut to {music_stem}"))
    return timeline
