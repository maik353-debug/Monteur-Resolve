"""Pacing and rhythm analytics for timelines.

The public contract is :class:`PacingStats` (produced by
:func:`analyze_timeline`) and :func:`compare` for A/B-ing two versions of a
cut. All durations are seconds; shot lists are chronological.

A quick glossary for editors:

* **ASL (average shot length)** — total shot time divided by the number of
  shots. Lower ASL means faster cutting.
* **Pacing curve** — a rolling average of shot lengths across the timeline,
  so you can see *where* a cut speeds up or slows down, not just its
  overall tempo.
* **Sections** — contiguous stretches of the cut labelled "fast",
  "medium" or "slow" relative to the cut's own average tempo.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace

from fable.model import Timeline, frames_to_seconds

_HISTOGRAM_BUCKETS: list[tuple[str, float, float]] = [
    ("0–1s", 0.0, 1.0),
    ("1–2s", 1.0, 2.0),
    ("2–4s", 2.0, 4.0),
    ("4–8s", 4.0, 8.0),
    ("8–15s", 8.0, 15.0),
    ("15–30s", 15.0, 30.0),
    ("30s+", 30.0, float("inf")),
]

_FAST_FACTOR = 0.75
_SLOW_FACTOR = 1.5
_MIN_WINDOW_SHOTS = 5
_WINDOW_SECONDS = 15.0


@dataclass
class Shot:
    name: str
    start: float  # timeline position, seconds
    length: float  # seconds


@dataclass
class Section:
    """A contiguous stretch of the timeline with a pacing character."""

    start: float
    end: float
    avg_shot_length: float
    label: str  # "fast" | "medium" | "slow"


@dataclass
class PacingStats:
    timeline_name: str
    fps: float
    duration_seconds: float
    shot_count: int
    cut_count: int
    avg_shot_seconds: float
    median_shot_seconds: float
    min_shot_seconds: float
    max_shot_seconds: float
    std_shot_seconds: float
    shots: list[Shot] = field(default_factory=list)
    # (timeline position seconds, rolling average shot length seconds)
    pacing_curve: list[tuple[float, float]] = field(default_factory=list)
    # (bucket label like "0-1s", count)
    histogram: list[tuple[str, int]] = field(default_factory=list)
    longest_shots: list[Shot] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)


def analyze_timeline(timeline: Timeline, track: str | None = None) -> PacingStats:
    """Compute pacing statistics for a timeline's video shots.

    Every clip on the analyzed track counts as one shot; gaps between clips
    are ignored (silence between shots is not a shot). When ``track`` is
    None the "V1" track is used if it exists, otherwise every video clip on
    the timeline, in record order.

    Metrics, in editor terms:

    * ``avg_shot_seconds`` (ASL), ``median_shot_seconds``, ``min``/``max``
      and ``std_shot_seconds`` (population standard deviation — how much
      shot lengths vary; a small number means a very regular rhythm).
    * ``cut_count`` — the number of hard boundaries where one shot ends
      exactly where (or after) the next begins; a shot sitting alone after
      a gap does not add a cut.
    * ``pacing_curve`` — one point per shot: ``(shot start, rolling
      average shot length)``. The rolling window around each shot is all
      shots starting within +/-15 seconds of it, widened to at least 5
      shots (centered) when the neighbourhood is sparse. Fifteen seconds
      is roughly a beat of screen time — wide enough to smooth single
      outliers, narrow enough to show scene-level tempo changes — and the
      5-shot floor keeps the curve meaningful in slow passages where few
      shots fall inside 15 seconds.
    * ``histogram`` — shot lengths sorted into fixed buckets
      (0-1s, 1-2s, 2-4s, 4-8s, 8-15s, 15-30s, 30s+); all seven buckets are
      always present, even when empty.
    * ``longest_shots`` — the five longest shots, longest first.
    * ``sections`` — each shot is judged by its pacing-curve value against
      the cut's overall ASL: below 0.75x the average is "fast", above
      1.5x is "slow", in between is "medium"; adjacent shots with the
      same label are merged into one section.

    An empty timeline returns zeros and empty lists (the histogram keeps
    its seven zero-count buckets).
    """
    clips = _select_shot_clips(timeline, track)
    fps = timeline.fps
    shots = [
        Shot(
            name=c.name,
            start=frames_to_seconds(c.record_in, fps),
            length=frames_to_seconds(c.duration, fps),
        )
        for c in clips
    ]
    lengths = [s.length for s in shots]
    cut_count = sum(
        1 for prev, nxt in zip(clips, clips[1:]) if nxt.record_in <= prev.record_out
    )

    if lengths:
        avg = statistics.fmean(lengths)
        median = statistics.median(lengths)
        shortest = min(lengths)
        longest = max(lengths)
        std = statistics.pstdev(lengths)
    else:
        avg = median = shortest = longest = std = 0.0

    curve = _pacing_curve(shots)
    return PacingStats(
        timeline_name=timeline.name,
        fps=fps,
        duration_seconds=timeline.duration_seconds,
        shot_count=len(shots),
        cut_count=cut_count,
        avg_shot_seconds=avg,
        median_shot_seconds=median,
        min_shot_seconds=shortest,
        max_shot_seconds=longest,
        std_shot_seconds=std,
        shots=shots,
        pacing_curve=curve,
        histogram=_histogram(lengths),
        longest_shots=sorted(shots, key=lambda s: s.length, reverse=True)[:5],
        sections=_sections(shots, curve, avg),
    )


def compare(a: PacingStats, b: PacingStats) -> dict:
    """Compare two cuts; returns per-metric deltas keyed by metric name.

    For each scalar metric the result maps the metric name to
    ``{"a": value_in_a, "b": value_in_b, "delta": b - a}``, so a negative
    delta on ``avg_shot_seconds`` means cut *b* is cut faster than *a*.
    The extra ``"verdict"`` key holds a one-sentence plain-English summary
    of how *b* differs from *a*: overall tempo (faster/slower cutting),
    rhythm variance (tighter/looser) and runtime (shorter/longer).
    """
    metrics = (
        "duration_seconds",
        "shot_count",
        "cut_count",
        "avg_shot_seconds",
        "median_shot_seconds",
        "min_shot_seconds",
        "max_shot_seconds",
        "std_shot_seconds",
    )
    result: dict = {}
    for metric in metrics:
        x = getattr(a, metric)
        y = getattr(b, metric)
        result[metric] = {"a": x, "b": y, "delta": y - x}
    result["verdict"] = _verdict(a, b)
    return result


def rhythm_signature(stats: PacingStats, buckets: int = 20) -> list[float]:
    """Reduce a cut to its rhythmic "shape": ``buckets`` numbers, one per
    equal slice of the runtime, each the average length of the shots that
    *start* in that slice (0.0 where no shot starts).

    Because the signature is normalized to the cut's own runtime, two cuts
    of different lengths can be compared value-by-value to see whether
    they speed up and slow down in the same places.
    """
    if buckets <= 0:
        raise ValueError("buckets must be positive")
    duration = stats.duration_seconds
    sums = [0.0] * buckets
    counts = [0] * buckets
    if duration > 0:
        for shot in stats.shots:
            index = min(int(shot.start / duration * buckets), buckets - 1)
            sums[index] += shot.length
            counts[index] += 1
    return [sums[i] / counts[i] if counts[i] else 0.0 for i in range(buckets)]


@dataclass
class ScenePacing:
    """Pacing of one scene (a marker-delimited stretch of the timeline)."""

    heading: str
    start: float  # seconds
    end: float
    stats: PacingStats


def analyze_scenes(timeline: Timeline, track: str | None = None) -> list[ScenePacing]:
    """Per-scene pacing, using timeline markers as scene boundaries.

    Every marker starts a new scene named after the marker; material before
    the first marker becomes "Opening". A timeline without markers returns a
    single scene spanning the whole cut. Clips are assigned to the scene in
    which they start; scene stats are computed as if the scene were its own
    timeline (positions relative to the scene start).
    """
    fps = timeline.fps
    markers = sorted(timeline.markers, key=lambda m: m.frame)
    boundaries: list[tuple[str, int]] = [
        (m.name or f"Scene {i + 1}", m.frame) for i, m in enumerate(markers)
    ]
    if not boundaries:
        boundaries = [(timeline.name or "Full cut", 0)]
    elif boundaries[0][1] > 0:
        boundaries.insert(0, ("Opening", 0))

    clips = _select_shot_clips(timeline, track)
    scenes: list[ScenePacing] = []
    end_frame = timeline.duration
    for i, (heading, start_frame) in enumerate(boundaries):
        next_frame = boundaries[i + 1][1] if i + 1 < len(boundaries) else end_frame
        scene_clips = [
            replace(
                c,
                record_in=c.record_in - start_frame,
                record_out=c.record_out - start_frame,
            )
            for c in clips
            if start_frame <= c.record_in < next_frame
        ]
        sub = Timeline(name=heading, fps=fps, clips=scene_clips)
        scenes.append(
            ScenePacing(
                heading=heading,
                start=frames_to_seconds(start_frame, fps),
                end=frames_to_seconds(next_frame, fps),
                stats=analyze_timeline(sub, track=None),
            )
        )
    return scenes


def _select_shot_clips(timeline: Timeline, track: str | None) -> list:
    if track is not None:
        return timeline.track_clips(track)
    if "V1" in timeline.tracks():
        return timeline.track_clips("V1")
    return timeline.video_clips()


def _pacing_curve(shots: list[Shot]) -> list[tuple[float, float]]:
    curve: list[tuple[float, float]] = []
    n = len(shots)
    for i, shot in enumerate(shots):
        indices = [
            j for j in range(n) if abs(shots[j].start - shot.start) <= _WINDOW_SECONDS
        ]
        if len(indices) < _MIN_WINDOW_SHOTS:
            half = _MIN_WINDOW_SHOTS // 2
            lo = max(0, min(i - half, n - _MIN_WINDOW_SHOTS))
            hi = min(n, lo + _MIN_WINDOW_SHOTS)
            indices = sorted(set(indices) | set(range(lo, hi)))
        window = [shots[j].length for j in indices]
        curve.append((shot.start, statistics.fmean(window)))
    return curve


def _histogram(lengths: list[float]) -> list[tuple[str, int]]:
    return [
        (label, sum(1 for length in lengths if lo <= length < hi))
        for label, lo, hi in _HISTOGRAM_BUCKETS
    ]


def _sections(
    shots: list[Shot], curve: list[tuple[float, float]], avg: float
) -> list[Section]:
    if not shots:
        return []
    labels: list[str] = []
    for _, rolling in curve:
        if avg <= 0:
            labels.append("medium")
        elif rolling < _FAST_FACTOR * avg:
            labels.append("fast")
        elif rolling > _SLOW_FACTOR * avg:
            labels.append("slow")
        else:
            labels.append("medium")

    sections: list[Section] = []
    run_start = 0
    for i in range(1, len(shots) + 1):
        if i == len(shots) or labels[i] != labels[run_start]:
            run = shots[run_start:i]
            sections.append(
                Section(
                    start=run[0].start,
                    end=run[-1].start + run[-1].length,
                    avg_shot_length=statistics.fmean(s.length for s in run),
                    label=labels[run_start],
                )
            )
            run_start = i
    return sections


def _verdict(a: PacingStats, b: PacingStats) -> str:
    parts: list[str] = []

    d_avg = b.avg_shot_seconds - a.avg_shot_seconds
    if _significant(d_avg, a.avg_shot_seconds):
        parts.append(
            "cut faster overall (shorter average shot)"
            if d_avg < 0
            else "cut slower overall (longer average shot)"
        )
    else:
        parts.append("paced about the same overall")

    d_std = b.std_shot_seconds - a.std_shot_seconds
    if _significant(d_std, a.std_shot_seconds):
        parts.append(
            "with a tighter, more even rhythm"
            if d_std < 0
            else "with a looser, more varied rhythm"
        )

    d_dur = b.duration_seconds - a.duration_seconds
    if _significant(d_dur, a.duration_seconds):
        parts.append(
            f"and runs about {abs(d_dur):.0f}s "
            + ("shorter" if d_dur < 0 else "longer")
        )

    return f"Cut B is {parts[0]}" + ("" if len(parts) == 1 else ", " + ", ".join(parts[1:])) + "."


def _significant(delta: float, base: float, rel: float = 0.05, floor: float = 1e-9) -> bool:
    if abs(delta) <= floor:
        return False
    if base <= 0:
        return True
    return abs(delta) / base >= rel
