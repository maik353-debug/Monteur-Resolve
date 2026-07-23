"""Serien-Modus: one tour folder -> N genuinely different vertical Shorts.

The engine on top of :func:`monteur.montage.plan_montage`. From ONE footage
folder (a long ride, many clips, many moments) it produces up to ``count``
"short"-style vertical MontagePlans, each built around a DIFFERENT strong
moment, with **zero moment repeated across the whole series**.

The pipeline, all deterministic and offline:

1. **Seed selection** (:func:`_pick_seeds`). Every usable moment across all
   reports is ranked by strength (its score, plus its hero/highlight
   signals). Seeds are picked greedily for VARIETY: first the strongest
   moment of each distinct clip (a different clip whenever the footage has
   one), then — only if more seeds are still needed than there are clips —
   further moments inside a clip, each kept a minimum time gap from the
   seeds already chosen, preferring an unseen daylight / shot-size look.

2. **Disjoint partition** (:func:`_partition`). Every usable moment is
   assigned to its NEAREST seed — same clip + time proximity first, then
   similarity via the daylight / shot-size / scene-group / tag signals.
   This yields ``count`` DISJOINT groups: no moment sits in two groups.

3. **Build each short** (:func:`plan_series`). For each group a set of
   filtered :class:`~monteur.sift.ClipReport` s is rebuilt — same clip
   identity, paths, duration, segments — carrying ONLY that group's
   moments, and handed to the UNCHANGED :func:`plan_montage` with
   ``style="short"``. The whole magie stack (peak-on-beat, deliberate
   silence, best/secondary drops, loop seam, ducking, frame hygiene,
   eye-trace / shot-grammar, and Auto-Reframe on the vertical canvas)
   applies exactly as for any single short. Zero-repeat holds INSIDE each
   short via plan_montage's own pool, and ACROSS shorts via the disjoint
   groups — each short only ever sees its own group's moments.

Honest degradation: if the footage supports fewer than ``count`` distinct,
well-separated seeds, FEWER shorts come back and every short's note says
so. The series never pads by repeating a moment or a near-identical seed —
the whole point is N *different* shorts. A single clip or a too-short tour
may yield one.

``plan_montage`` and the rest of montage.py are UNTOUCHED — plan_series is a
new caller layered on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from monteur.montage import CANVASES, MontagePlan, plan_montage
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport, Moment

_EPS = 1e-6

# Strength = score, lifted by the moment's hero and audio-highlight signals
# (both 0..1). Small weights: the sift score stays the headline, hero /
# highlight only lift a genuinely stronger candidate and break near ties.
_HERO_WEIGHT = 0.5
_HIGHLIGHT_WEIGHT = 0.25

# Minimum time gap (seconds) between two seeds picked from the SAME clip.
# Seeds on different clips are already maximally separated; this only bounds
# the fallback that draws a second seed out of one long clip.
MIN_SEED_GAP_SECONDS = 8.0

# Default per-short length cap (seconds). A Short wants ~a half-minute; the
# real platform cap (60/90 s) still applies through plan_montage's music.
DEFAULT_SHORT_SECONDS = 30.0

# Partition distances. Same-clip proximity (time gap in seconds, typically
# < a few hundred) must ALWAYS beat a cross-clip assignment, so the
# cross-clip base sits far above any realistic same-clip gap.
_CROSS_CLIP_BASE = 1.0e6
# Cross-clip dissimilarity weights (added when a signal DIFFERS, i.e. a
# less-similar seed is a worse home). Tag overlap pulls a candidate TOWARD
# a seed it shares vocabulary with.
_SIM_BASE = 1.0
_W_DAYLIGHT = 1.0
_W_SHOT = 0.5
_W_GROUP = 1.0
_W_TAG = 0.25


@dataclass(frozen=True)
class SeriesSeed:
    """The strong moment a short is built around (its identity in the tour)."""

    clip_path: str
    start: float
    end: float
    score: float
    label: str = ""


@dataclass
class SeriesShort:
    """One short in the series: its plan, the seed it was built around, a note."""

    plan: MontagePlan
    seed: SeriesSeed
    note: str
    canvas: str = "vertical-uhd"


@dataclass(frozen=True)
class _Cand:
    """A usable moment as a series candidate (keeps its home report index)."""

    report_idx: int
    clip_path: str
    moment: Moment


def _strength(m: Moment) -> float:
    """A moment's series strength: its score lifted by hero / highlight."""
    return m.score + _HERO_WEIGHT * m.hero + _HIGHLIGHT_WEIGHT * m.highlight


def _rank_key(c: _Cand) -> tuple:
    """Stable strength ranking: strongest first, fixed tie-breaks."""
    m = c.moment
    return (-_strength(m), c.clip_path, m.start, m.end)


def _candidates(reports: list[ClipReport]) -> list[_Cand]:
    """Every usable moment across all reports, as flat candidates."""
    cands: list[_Cand] = []
    for idx, r in enumerate(reports):
        for m in r.moments:
            cands.append(_Cand(idx, r.path, m))
    return cands


def _too_close(cand: _Cand, seeds: list[_Cand]) -> bool:
    """True if ``cand`` sits within the min gap of a SAME-CLIP seed."""
    for s in seeds:
        if s.clip_path != cand.clip_path:
            continue
        if abs(cand.moment.start - s.moment.start) < MIN_SEED_GAP_SECONDS - _EPS:
            return True
    return False


def _pick_seeds(ranked: list[_Cand], count: int) -> list[_Cand]:
    """Greedily pick up to ``count`` well-separated seeds for variety.

    Phase 1 takes the strongest moment of each DISTINCT clip (a different
    clip whenever the tour has one), strongest clips first. Phase 2 (only
    when more seeds are wanted than there are clips) draws further seeds
    from within clips, each kept :data:`MIN_SEED_GAP_SECONDS` from every
    seed already chosen and preferring a daylight / shot-size look not yet
    represented. Deterministic throughout (``ranked`` is a stable order).
    """
    seeds: list[_Cand] = []
    seed_clips: set[str] = set()
    # Phase 1 — one seed per clip, strongest first.
    for c in ranked:
        if len(seeds) >= count:
            return seeds
        if c.clip_path in seed_clips:
            continue
        seeds.append(c)
        seed_clips.add(c.clip_path)
    if len(seeds) >= count:
        return seeds
    # Phase 2 — fall back to further seeds inside clips. Look variety
    # (an unseen daylight / shot-size) is a soft preference over strength.
    seen_day = {c.moment.daylight for c in seeds if c.moment.daylight}
    seen_shot = {c.moment.shot_size for c in seeds if c.moment.shot_size}
    remaining = [c for c in ranked if c not in seeds]

    def phase2_key(c: _Cand) -> tuple:
        novelty = 0
        if c.moment.daylight and c.moment.daylight in seen_day:
            novelty += 1
        if c.moment.shot_size and c.moment.shot_size in seen_shot:
            novelty += 1
        return (novelty,) + _rank_key(c)

    for c in sorted(remaining, key=phase2_key):
        if len(seeds) >= count:
            break
        if _too_close(c, seeds):
            continue
        seeds.append(c)
    return seeds


def _dissimilarity(a: Moment, b: Moment) -> float:
    """How UNLIKE two moments look, via the sift/vision signals (>= 0)."""
    d = _SIM_BASE
    if a.daylight and b.daylight and a.daylight != b.daylight:
        d += _W_DAYLIGHT
    if a.shot_size and b.shot_size and a.shot_size != b.shot_size:
        d += _W_SHOT
    if a.group and b.group and a.group != b.group:
        d += _W_GROUP
    overlap = len(set(a.tags) & set(b.tags))
    d -= _W_TAG * overlap
    return max(0.0, d)


def _distance(cand: _Cand, seed: _Cand) -> float:
    """Distance from a candidate to a seed for nearest-seed partitioning.

    Same clip: pure time proximity (seconds) — a candidate belongs with the
    seed it is closest to in the take. Different clip: a large base plus the
    look dissimilarity, so a same-clip home always wins and, across clips,
    the most similar seed wins.
    """
    if cand.clip_path == seed.clip_path:
        return abs(cand.moment.start - seed.moment.start)
    return _CROSS_CLIP_BASE + _dissimilarity(cand.moment, seed.moment)


def _partition(
    cands: list[_Cand], seeds: list[_Cand]
) -> list[list[_Cand]]:
    """Assign every candidate to its nearest seed -> ``len(seeds)`` groups.

    Disjoint by construction: each candidate lands in exactly one group,
    ties broken toward the earlier (stronger) seed. That is the cross-short
    zero-repeat guarantee — a moment is only ever in one group, and a short
    only ever sees its own group.
    """
    groups: list[list[_Cand]] = [[] for _ in seeds]
    for c in cands:
        best_i = 0
        best_d = _distance(c, seeds[0])
        for i in range(1, len(seeds)):
            d = _distance(c, seeds[i])
            if d < best_d - _EPS:  # strictly closer; ties keep the earlier seed
                best_d = d
                best_i = i
        groups[best_i].append(c)
    return groups


def _group_reports(
    group: list[_Cand], reports: list[ClipReport]
) -> list[ClipReport]:
    """Rebuild filtered ClipReports carrying ONLY this group's moments.

    Clip identity (path, duration, media_start, usable_ratio, notes,
    segments) is preserved; the moments are exactly the group's, in each
    source report's original (best-first) order. Reports are emitted in
    source order so plan_montage sees a stable pool.
    """
    by_report: dict[int, set[int]] = {}
    for c in group:
        by_report.setdefault(c.report_idx, set()).add(id(c.moment))
    out: list[ClipReport] = []
    for idx in sorted(by_report):
        src = reports[idx]
        wanted = by_report[idx]
        moments = [m for m in src.moments if id(m) in wanted]
        if not moments:
            continue
        out.append(
            ClipReport(
                path=src.path,
                duration=src.duration,
                segments=list(src.segments),
                moments=moments,
                usable_ratio=src.usable_ratio,
                notes=list(src.notes),
                media_start=src.media_start,
            )
        )
    return out


def _mmss(seconds: float) -> str:
    """Clip time as M:SS (the note's human clock)."""
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _seed_note(index: int, total: int, requested: int, seed: SeriesSeed) -> str:
    """The short's note: which short it is, and the seed it was built around."""
    clock = _mmss(seed.start)
    clip = PurePath(seed.clip_path).name or seed.clip_path
    if seed.label:
        what = f"the {seed.label} moment"
    else:
        what = "the moment"
    note = f"short {index} of {total} — built around {what} at {clock} in {clip}"
    if total < requested:
        note += (
            f" (requested {requested}; the footage yielded {total} distinct "
            "shorts — not enough distinct strong material for more)"
        )
    return note


def plan_series(
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    *,
    count: int,
    canvas: str = "vertical-uhd",
    max_seconds: float | None = DEFAULT_SHORT_SECONDS,
    **plan_kwargs,
) -> list[SeriesShort]:
    """Turn ONE tour into up to ``count`` genuinely different vertical Shorts.

    Ranks every usable moment across ``reports`` by strength, picks up to
    ``count`` well-separated SEED moments (a different clip when possible,
    a minimum time gap within a clip, daylight/look variety), partitions
    EVERY moment into that many DISJOINT groups by nearest seed, and builds
    one "short"-style :class:`~monteur.montage.MontagePlan` per group via
    the unchanged :func:`plan_montage`. Because the groups are disjoint and
    each short only sees its own group, **no moment repeats across the
    series** — the headline promise.

    ``music`` is reused for every short (each short's own window pick finds
    its moment in the song); ``None`` keeps the clips' own sound and then
    ``max_seconds`` must be set (plan_montage needs a length without music).
    ``canvas`` is the delivery frame (default vertical 9:16, on which
    Auto-Reframe applies at render time); it is validated here and carried
    on each :class:`SeriesShort` for the render loop — plan_montage itself
    stays canvas-orthogonal. ``max_seconds`` caps each short's length.
    ``**plan_kwargs`` pass straight to :func:`plan_montage` (``order``,
    ``transitions``, ``allow_repeats``, ``fps`` …); ``style`` is fixed to
    "short" and ``max_duration`` is supplied from ``max_seconds``.

    Returns a list of :class:`SeriesShort` (``plan`` / ``seed`` / ``note`` /
    ``canvas``), strongest seed first. Fewer than ``count`` come back when
    the footage lacks that many distinct strong seeds; an empty list when
    there is no usable material. Deterministic and offline: identical
    ``reports`` + ``music`` + ``count`` give the identical series and order.
    """
    if count <= 0:
        raise ValueError("count must be a positive number of shorts")
    if canvas not in CANVASES:
        valid = ", ".join(sorted(CANVASES))
        raise ValueError(f"unknown canvas {canvas!r}; valid canvases: {valid}")
    # style and max_duration are the series' to set — never accept a
    # conflicting override from the passthrough kwargs.
    plan_kwargs.pop("style", None)
    plan_kwargs.pop("max_duration", None)

    cands = _candidates(reports)
    if not cands:
        return []
    ranked = sorted(cands, key=_rank_key)
    seeds = _pick_seeds(ranked, count)
    if not seeds:
        return []
    groups = _partition(cands, seeds)

    shorts: list[SeriesShort] = []
    total = len(seeds)
    for i, (seed_cand, group) in enumerate(zip(seeds, groups), start=1):
        group_reports = _group_reports(group, reports)
        plan = plan_montage(
            group_reports,
            music,
            style="short",
            max_duration=max_seconds,
            **plan_kwargs,
        )
        seed = SeriesSeed(
            clip_path=seed_cand.clip_path,
            start=seed_cand.moment.start,
            end=seed_cand.moment.end,
            score=seed_cand.moment.score,
            label=seed_cand.moment.label,
        )
        note = _seed_note(i, total, count, seed)
        # Surface the series note on the plan too, so it survives
        # serialization / the render surfaces (only-when-set contract: a
        # normal single-short plan is untouched).
        plan.notes.append(note)
        shorts.append(SeriesShort(plan=plan, seed=seed, note=note, canvas=canvas))
    return shorts


# --- shorts from an existing long-form EDIT (project -> shorts) ---------------
#
# plan_series above turns a raw footage POOL into shorts. When the user already
# has a finished long-form CUT (a Monteur project), the shorts should come out
# of the beats the EDIT actually used, not the whole raw pool — the long form
# is where the good moments were already found. These helpers filter the
# footage reports down to the moments the plan drew from, then reuse the exact
# same plan_series engine. When the edit is too lean to seed the wanted number
# of distinct shorts, they fall back to the full footage pool so the user still
# gets shorts, and report which path was taken.


def used_source_ranges(plan: dict | None) -> dict[str, list[tuple[float, float]]]:
    """Map ``clip_path`` -> the ``(source_start, source_end)`` ranges a plan uses.

    Reads a :func:`monteur.montage.plan_to_dict` dict. Malformed or zero-length
    entries are skipped; an empty/None plan yields ``{}``.
    """
    ranges: dict[str, list[tuple[float, float]]] = {}
    for entry in (plan or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("clip_path")
        try:
            start = float(entry.get("source_start"))
            end = float(entry.get("source_end"))
        except (TypeError, ValueError):
            continue
        if not path or end <= start + _EPS:
            continue
        ranges.setdefault(str(path), []).append((start, end))
    return ranges


def _overlaps_any(moment: Moment, ranges: list[tuple[float, float]]) -> bool:
    """True if the moment's span overlaps any ``(start, end)`` in ``ranges``."""
    for start, end in ranges:
        if min(moment.end, end) - max(moment.start, start) > _EPS:
            return True
    return False


def restrict_to_edit(
    reports: list[ClipReport], plan: dict | None
) -> list[ClipReport]:
    """Keep only the moments a long-form ``plan`` actually used.

    A moment is "used" when it overlaps a source range some plan entry drew
    from its clip (the same overlap match the change list uses). Clip identity
    (path, duration, segments, usable_ratio, notes, media_start) is preserved;
    a report left with no used moment is dropped. Clip paths are matched
    exactly, then by basename, so a plan saved with a differently-spelled path
    (relative vs absolute) still lines up with its report.
    """
    ranges = used_source_ranges(plan)
    if not ranges:
        return []
    by_name: dict[str, list[tuple[float, float]]] = {}
    for path, spans in ranges.items():
        by_name.setdefault(PurePath(path).name, []).extend(spans)
    out: list[ClipReport] = []
    for r in reports:
        spans = ranges.get(r.path) or by_name.get(PurePath(r.path).name) or []
        if not spans:
            continue
        moments = [m for m in r.moments if _overlaps_any(m, spans)]
        if not moments:
            continue
        out.append(
            ClipReport(
                path=r.path,
                duration=r.duration,
                segments=list(r.segments),
                moments=moments,
                usable_ratio=r.usable_ratio,
                notes=list(r.notes),
                media_start=r.media_start,
            )
        )
    return out


def series_from_edit(
    reports: list[ClipReport],
    plan: dict | None,
    music: MusicAnalysis | None = None,
    *,
    count: int,
    canvas: str = "vertical-uhd",
    max_seconds: float | None = DEFAULT_SHORT_SECONDS,
    **plan_kwargs,
) -> tuple[list[SeriesShort], bool]:
    """Shorts extracted from the beats a long-form ``plan`` actually used.

    Filters ``reports`` to the moments the long form drew from and runs the
    unchanged :func:`plan_series` on them, so the shorts are genuine extracts
    of the edit — not a fresh cut of the raw pool. When the edit is too lean to
    seed ``count`` distinct shorts (few beats, one clip), falls back to the
    full footage moment pool so the user still gets shorts.

    Returns ``(shorts, from_edit)`` — ``from_edit`` is ``True`` when the shorts
    came out of the edit, ``False`` when the fallback footage pool was used.
    Deterministic and offline, like :func:`plan_series`.
    """
    used = restrict_to_edit(reports, plan)
    edit_shorts: list[SeriesShort] = []
    if used:
        edit_shorts = plan_series(
            used, music, count=count, canvas=canvas,
            max_seconds=max_seconds, **plan_kwargs,
        )
        if len(edit_shorts) >= count:
            return edit_shorts, True
    # The edit alone couldn't seed the full count — try the whole footage pool
    # and keep whichever yields more distinct shorts (ties favour the edit).
    full_shorts = plan_series(
        reports, music, count=count, canvas=canvas,
        max_seconds=max_seconds, **plan_kwargs,
    )
    if edit_shorts and len(edit_shorts) >= len(full_shorts):
        return edit_shorts, True
    return full_shorts, False
