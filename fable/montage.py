"""Montage builder: best moments + music beats -> a first cut.

Takes the sifted footage (:mod:`fable.sift`) and an analyzed song
(:mod:`fable.music`) and lays out a rough cut on the beat grid: calm
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
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import PurePath

from fable.model import AUDIO, VIDEO, Clip, Marker, Timeline, seconds_to_frames
from fable.music import MusicAnalysis, MusicSection
from fable.sift import ClipReport, Moment

CHRONOLOGICAL = "chronological"  # keep footage order (travel/event films)
BEST_FIRST = "best_first"  # strongest material on the strongest sections

# How many beats between cuts, per section energy label.
BEATS_PER_CUT = {"high": 1, "mid": 2, "low": 4}
# Anti-strobe floor: a cut interval below this doubles the beat step.
MIN_CUT_INTERVAL = 0.4
# Grid interval used when the song has no detected beats.
FALLBACK_INTERVAL = 2.0

_EPS = 1e-6


@dataclass
class MontagePlan:
    """The chosen cut points before rendering to a timeline."""

    music_path: str
    duration: float  # seconds, montage length (may be shorter than the song)
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


def _pick_reuse(pool: list[_PoolItem], start: int) -> _PoolItem | None:
    """First pool item (cyclic scan from ``start``) with unconsumed material."""
    n = len(pool)
    for k in range(n):
        item = pool[(start + k) % n]
        if item.remaining > _EPS:
            return item
    return None


def _fill(
    slots: list[tuple[float, float]],
    slot_order: list[int],
    pool: list[_PoolItem],
) -> tuple[list[MontageEntry], list[str]]:
    entries: list[MontageEntry] = []
    notes: list[str] = []
    n = len(pool)
    rewound = False
    for visit, slot_idx in enumerate(slot_order):
        rec_start, rec_end = slots[slot_idx]
        slot_len = rec_end - rec_start
        if visit < n:
            item = pool[visit]  # first pass: every moment used once
        else:
            item = _pick_reuse(pool, visit % n)
            if item is None:  # everything consumed: rewind and repeat footage
                item = pool[visit % n]
                item.consumed = 0.0
                rewound = True
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
) -> MontagePlan:
    """Distribute the best moments across the song's beat grid."""
    length = music.duration if max_duration is None else min(music.duration, max_duration)
    plan = MontagePlan(music_path=music.path, duration=max(length, 0.0))
    if length <= _EPS:
        plan.notes.append("montage length is zero; nothing planned")
        return plan

    cuts, grid_notes = _build_grid(music, length)
    plan.notes.extend(grid_notes)
    slots = list(zip(cuts, cuts[1:]))
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

    entries, fill_notes = _fill(slots, slot_order, pool)
    entries.sort(key=lambda e: e.record_start)
    plan.entries = entries
    plan.notes.extend(fill_notes)
    used = sum(1 for it in pool if it.uses)
    plan.notes.append(f"{len(slots)} slots filled, {used} of {len(pool)} moments used")
    return plan


def montage_to_timeline(plan: MontagePlan, fps: float, name: str = "Fable Montage") -> Timeline:
    """Render a MontagePlan as a Timeline (footage on V1, music on A1)."""
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
        timeline.clips.append(
            Clip(
                name=stem,
                track="V1",
                kind=VIDEO,
                source_in=src_in,
                source_out=src_out,
                record_in=rec_in,
                record_out=rec_out,
                source_name=stem,
            )
        )
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
