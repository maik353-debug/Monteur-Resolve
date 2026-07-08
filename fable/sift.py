"""Footage sifting: which parts of which clips are worth using?

Fable scans every clip's frames (via :mod:`fable.media`) and classifies
stretches as usable or problematic (too dark, blurry, shaky), then ranks the
best moments — so the editor (or the montage builder) starts from the good
material instead of watching everything.

Heuristics (all deliberately simple and documented as approximate):

* DARK — sample brightness (mean luma) below 40/255.
* BLURRY — sample sharpness below 25% of the clip's *own* 90th-percentile
  sharpness. The threshold is relative because gradient-variance sharpness
  depends heavily on scene texture: a talking head against a plain wall may
  peak at 40 while a leafy exterior peaks at 400.  A clip is only "blurry"
  where it is much softer than its own best material.
* SHAKY — sample motion above 3x the clip's median motion AND locally
  jittery: the mean absolute motion difference over a 3-sample window
  exceeds half the clip's mean motion. This is a rough proxy for handheld
  shake (large, alternating frame-to-frame differences); steady high motion
  (a pan, a fast subject) does not alternate and stays USABLE.

A single odd sample inside a run of another label is smoothed away, so
one-frame flickers and cut transitions do not fragment segments.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from fable.media import (
    FableMediaError,
    FrameMetric,
    frame_metrics,
    list_media,
    probe,
)

USABLE = "usable"
DARK = "dark"
BLURRY = "blurry"
SHAKY = "shaky"

# Tunable heuristic constants (see module docstring).
_DARK_BRIGHTNESS = 40.0  # mean luma below this = underexposed
_BLURRY_P90_FRACTION = 0.25  # blurry if sharpness < 25% of clip's p90
_SHAKY_MOTION_FACTOR = 3.0  # shaky candidates: motion > 3x clip median
_JITTER_FACTOR = 0.5  # ...and local jitter > 0.5x clip mean motion
_BRIGHT_FULL = 120.0  # brightness at (or above) this = fully adequate
_MODERATE_MOTION_BAND = (0.5, 2.5)  # x clip median: "something happens"
_MOMENT_MOTION_BONUS = 0.25  # weight of the moderate-motion bonus
_MAX_MOMENTS = 12  # cap per clip


@dataclass
class ClipSegment:
    start: float  # seconds
    end: float
    label: str  # USABLE | DARK | BLURRY | SHAKY
    score: float  # 0..1 quality within the clip (usable segments only)


@dataclass
class Moment:
    """A candidate moment for the cut."""

    start: float
    end: float
    score: float  # 0..1


@dataclass
class ClipReport:
    path: str
    duration: float
    segments: list[ClipSegment] = field(default_factory=list)
    moments: list[Moment] = field(default_factory=list)  # best first
    usable_ratio: float = 0.0  # share of the clip classified usable
    notes: list[str] = field(default_factory=list)


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1); coarse but dependency-free."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(q * (len(ordered) - 1)))
    return ordered[index]


def _sharpness_ranks(metrics: list[FrameMetric]) -> list[float]:
    """Per-sample sharpness percentile rank within the clip, 0..1.

    Ties get their average rank, so a clip with uniform sharpness ranks
    everything 0.5 rather than 0 (uniform ≠ bad).
    """
    n = len(metrics)
    if n <= 1:
        return [0.5] * n
    ordered = sorted(m.sharpness for m in metrics)
    ranks = []
    for m in metrics:
        lo = bisect_left(ordered, m.sharpness)
        hi = bisect_right(ordered, m.sharpness)
        ranks.append(((lo + hi - 1) / 2) / (n - 1))
    return ranks


def _brightness_adequacy(brightness: float) -> float:
    """0 at the dark threshold, 1 at normal exposure (~120 mean luma)."""
    span = _BRIGHT_FULL - _DARK_BRIGHTNESS
    return min(1.0, max(0.0, (brightness - _DARK_BRIGHTNESS) / span))


def _motion_stats(metrics: list[FrameMetric]) -> tuple[float, float]:
    """(median, mean) motion, skipping sample 0 (always 0 by construction)."""
    motions = [m.motion for m in metrics[1:]]
    if not motions:
        return 0.0, 0.0
    return statistics.median(motions), sum(motions) / len(motions)


def _jitter(metrics: list[FrameMetric], i: int) -> float:
    """Mean |motion delta| over the 3-sample window centred on ``i``."""
    diffs = []
    if i > 0:
        diffs.append(abs(metrics[i].motion - metrics[i - 1].motion))
    if i < len(metrics) - 1:
        diffs.append(abs(metrics[i + 1].motion - metrics[i].motion))
    return sum(diffs) / len(diffs) if diffs else 0.0


def classify_metrics(metrics: list[FrameMetric], duration: float) -> list[ClipSegment]:
    """Label stretches of a clip from its frame metrics."""
    if not metrics:
        return []
    n = len(metrics)

    blur_threshold = _BLURRY_P90_FRACTION * _percentile(
        [m.sharpness for m in metrics], 0.9
    )
    median_motion, mean_motion = _motion_stats(metrics)

    # Per-sample labels, first matching problem wins (dark > blurry > shaky).
    labels: list[str] = []
    for i, m in enumerate(metrics):
        if m.brightness < _DARK_BRIGHTNESS:
            labels.append(DARK)
        elif m.sharpness < blur_threshold:
            labels.append(BLURRY)
        elif (
            m.motion > _SHAKY_MOTION_FACTOR * median_motion
            and _jitter(metrics, i) > _JITTER_FACTOR * mean_motion
        ):
            labels.append(SHAKY)
        else:
            labels.append(USABLE)

    # Smooth: a single odd sample inside a run of another label flips.
    smoothed = labels[:]
    for i in range(1, n - 1):
        if labels[i - 1] == labels[i + 1] != labels[i]:
            smoothed[i] = labels[i - 1]
    labels = smoothed

    # Merge consecutive same-label samples into segments tiling 0..duration.
    ranks = _sharpness_ranks(metrics)
    segments: list[ClipSegment] = []
    run_start = 0
    for i in range(1, n + 1):
        if i < n and labels[i] == labels[run_start]:
            continue
        start = 0.0 if not segments else metrics[run_start].t
        end = duration if i == n else metrics[i].t
        label = labels[run_start]
        score = 0.0
        if label == USABLE:
            per_sample = [
                ranks[j] * _brightness_adequacy(metrics[j].brightness)
                for j in range(run_start, i)
            ]
            score = sum(per_sample) / len(per_sample)
        segments.append(ClipSegment(start=start, end=end, label=label, score=score))
        run_start = i
    return segments


def find_moments(
    segments: list[ClipSegment], metrics: list[FrameMetric], min_length: float = 1.0
) -> list[Moment]:
    """Rank the best usable moments, longest-window-first scoring.

    Slides a ``min_length`` window (step = half the window) across each
    USABLE segment. A window scores by mean sharpness rank plus a bonus for
    moderate, steady motion — motion between 0.5x and 2.5x the clip median
    means *something happens* in frame and beats a static tripod shot, while
    extreme motion (handled by SHAKY upstream) earns nothing. Overlapping
    windows are deduplicated (the better one wins) and the result is capped
    at 12 per clip, sorted best-first.
    """
    if not metrics or min_length <= 0:
        return []
    eps = 1e-9
    ranks = _sharpness_ranks(metrics)
    median_motion, _ = _motion_stats(metrics)
    band_lo = _MODERATE_MOTION_BAND[0] * median_motion
    band_hi = _MODERATE_MOTION_BAND[1] * median_motion
    step = min_length / 2

    candidates: list[Moment] = []
    for seg in segments:
        if seg.label != USABLE:
            continue
        start = seg.start
        while start + min_length <= seg.end + eps:
            end = start + min_length
            idx = [
                j for j, m in enumerate(metrics) if start - eps <= m.t < end - eps
            ]
            if idx:
                mean_rank = sum(ranks[j] for j in idx) / len(idx)
                if median_motion > 0:
                    moderate = sum(
                        1 for j in idx if band_lo <= metrics[j].motion <= band_hi
                    ) / len(idx)
                else:
                    moderate = 0.0
                score = min(
                    1.0,
                    (1 - _MOMENT_MOTION_BONUS) * mean_rank
                    + _MOMENT_MOTION_BONUS * moderate,
                )
                candidates.append(Moment(start=start, end=end, score=score))
            start += step

    # Best first; dedupe overlaps greedily so the better window survives.
    candidates.sort(key=lambda m: (-m.score, m.start))
    kept: list[Moment] = []
    for cand in candidates:
        if len(kept) >= _MAX_MOMENTS:
            break
        if all(
            cand.end <= other.start + eps or cand.start >= other.end - eps
            for other in kept
        ):
            kept.append(cand)
    kept.sort(key=lambda m: (-m.score, m.start))
    return kept


def _reraise_if_ffmpeg_missing(exc: FableMediaError) -> None:
    if "ffmpeg not found" in str(exc):
        raise exc


def analyze_clip(path: str) -> ClipReport:
    """Full report for one clip (decodes frames via fable.media).

    Clips that are too short or fail to decode come back as a report with an
    explanatory note instead of raising; only a missing ffmpeg re-raises.
    """
    path = str(path)
    try:
        info = probe(path)
    except FableMediaError as exc:
        _reraise_if_ffmpeg_missing(exc)
        return ClipReport(path=path, duration=0.0, notes=[f"could not analyze: {exc}"])

    report = ClipReport(path=path, duration=info.duration)
    if info.duration < 1.0:
        report.notes.append("clip shorter than 1s — skipped")
        return report

    try:
        metrics = frame_metrics(path)
    except FableMediaError as exc:
        _reraise_if_ffmpeg_missing(exc)
        report.notes.append(f"could not decode frames: {exc}")
        return report

    report.segments = classify_metrics(metrics, info.duration)
    report.moments = find_moments(report.segments, metrics)
    usable_time = sum(
        s.end - s.start for s in report.segments if s.label == USABLE
    )
    report.usable_ratio = usable_time / info.duration if info.duration else 0.0

    unusable_time = info.duration - usable_time
    if unusable_time > 1e-6:
        by_label: dict[str, float] = {}
        for seg in report.segments:
            if seg.label != USABLE:
                by_label[seg.label] = by_label.get(seg.label, 0.0) + (
                    seg.end - seg.start
                )
        worst = max(by_label, key=by_label.__getitem__)
        wording = {DARK: "too dark", BLURRY: "blurry", SHAKY: "shaky"}[worst]
        pct = round(100 * unusable_time / info.duration)
        report.notes.append(f"{pct}% unusable: mostly {wording}")
    if not report.moments:
        report.notes.append("no usable stretch ≥ 1s — clip skipped")
    return report


def sift_directory(directory: str) -> list[ClipReport]:
    """Reports for every video file in a directory.

    Individual clip failures become a note in that clip's report; only a
    missing ffmpeg (which dooms every clip) aborts the run.
    """
    reports: list[ClipReport] = []
    for media_path in list_media(directory):
        try:
            reports.append(analyze_clip(str(media_path)))
        except FableMediaError as exc:
            _reraise_if_ffmpeg_missing(exc)
            reports.append(
                ClipReport(
                    path=str(media_path), duration=0.0, notes=[f"skipped: {exc}"]
                )
            )
    return reports
