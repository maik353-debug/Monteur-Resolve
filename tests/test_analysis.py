"""Tests for monteur.analysis pacing metrics."""

from __future__ import annotations

import math

from monteur.analysis import analyze_timeline, compare, rhythm_signature
from monteur.model import Clip, Timeline

FPS = 25.0


def make_timeline(
    lengths_seconds: list[float],
    fps: float = FPS,
    track: str = "V1",
    kind: str = "video",
    name: str = "cut",
    gap_seconds: float = 0.0,
) -> Timeline:
    """Lay out back-to-back clips of the given second-lengths on one track."""
    timeline = Timeline(name=name, fps=fps)
    position = 0
    for i, seconds in enumerate(lengths_seconds):
        frames = round(seconds * fps)
        timeline.clips.append(
            Clip(
                name=f"shot{i:03d}",
                track=track,
                kind=kind,
                record_in=position,
                record_out=position + frames,
            )
        )
        position += frames + round(gap_seconds * fps)
    return timeline


def test_scalar_stats_on_known_layout() -> None:
    lengths = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    stats = analyze_timeline(make_timeline(lengths))

    assert stats.shot_count == 8
    assert stats.cut_count == 7
    assert math.isclose(stats.avg_shot_seconds, 5.0)
    assert math.isclose(stats.median_shot_seconds, 4.5)
    assert math.isclose(stats.std_shot_seconds, 2.0)
    assert math.isclose(stats.min_shot_seconds, 2.0)
    assert math.isclose(stats.max_shot_seconds, 9.0)
    assert math.isclose(stats.duration_seconds, 40.0)
    assert [s.length for s in stats.shots] == lengths
    assert [s.length for s in stats.longest_shots] == [9.0, 7.0, 5.0, 5.0, 4.0]


def test_gaps_are_not_shots_and_do_not_cut() -> None:
    stats = analyze_timeline(make_timeline([2.0, 2.0, 2.0], gap_seconds=1.0))
    assert stats.shot_count == 3
    assert stats.cut_count == 0
    assert math.isclose(stats.avg_shot_seconds, 2.0)


def test_histogram_bucket_counts() -> None:
    lengths = [0.5, 1.5, 3.0, 6.0, 10.0, 20.0, 40.0, 45.0]
    stats = analyze_timeline(make_timeline(lengths))
    assert stats.histogram == [
        ("0–1s", 1),
        ("1–2s", 1),
        ("2–4s", 1),
        ("4–8s", 1),
        ("8–15s", 1),
        ("15–30s", 1),
        ("30s+", 2),
    ]


def test_sections_fast_then_slow() -> None:
    stats = analyze_timeline(make_timeline([1.0] * 10 + [10.0] * 10))

    assert len(stats.sections) >= 2
    assert stats.sections[0].label == "fast"
    assert stats.sections[-1].label == "slow"
    assert math.isclose(stats.sections[0].start, 0.0)
    assert math.isclose(stats.sections[-1].end, stats.duration_seconds)
    end = 0.0
    for section in stats.sections:
        assert section.start >= end
        assert section.end > section.start
        end = section.end


def test_empty_timeline_returns_zeros() -> None:
    stats = analyze_timeline(Timeline(name="empty", fps=FPS))

    assert stats.shot_count == 0
    assert stats.cut_count == 0
    assert stats.duration_seconds == 0.0
    assert stats.avg_shot_seconds == 0.0
    assert stats.median_shot_seconds == 0.0
    assert stats.min_shot_seconds == 0.0
    assert stats.max_shot_seconds == 0.0
    assert stats.std_shot_seconds == 0.0
    assert stats.shots == []
    assert stats.pacing_curve == []
    assert stats.longest_shots == []
    assert stats.sections == []
    assert len(stats.histogram) == 7
    assert all(count == 0 for _, count in stats.histogram)
    assert rhythm_signature(stats, buckets=8) == [0.0] * 8


def test_compare_deltas_and_verdict() -> None:
    a = analyze_timeline(make_timeline([4.0] * 10, name="cut A"))
    b = analyze_timeline(make_timeline([2.0] * 10, name="cut B"))
    result = compare(a, b)

    for metric in (
        "duration_seconds",
        "shot_count",
        "cut_count",
        "avg_shot_seconds",
        "median_shot_seconds",
        "min_shot_seconds",
        "max_shot_seconds",
        "std_shot_seconds",
    ):
        entry = result[metric]
        assert math.isclose(entry["delta"], entry["b"] - entry["a"])

    assert math.isclose(result["avg_shot_seconds"]["delta"], -2.0)
    assert result["shot_count"] == {"a": 10, "b": 10, "delta": 0}
    assert isinstance(result["verdict"], str)
    assert result["verdict"]
    assert "faster" in result["verdict"]


def test_track_none_falls_back_to_all_video_when_no_v1() -> None:
    timeline = make_timeline([3.0, 3.0], track="V2")
    later = make_timeline([3.0], track="V3")
    later.clips[0].record_in += 150
    later.clips[0].record_out += 150
    timeline.clips.extend(later.clips)
    timeline.clips.append(
        Clip(name="audio", track="A1", kind="audio", record_in=0, record_out=250)
    )

    stats = analyze_timeline(timeline)
    assert stats.shot_count == 3
    assert [s.start for s in stats.shots] == [0.0, 3.0, 6.0]


def test_track_none_prefers_v1_when_present() -> None:
    timeline = make_timeline([2.0, 2.0], track="V1")
    timeline.clips.append(
        Clip(name="title", track="V2", kind="video", record_in=0, record_out=50)
    )
    stats = analyze_timeline(timeline)
    assert stats.shot_count == 2


def test_pacing_curve_uses_min_five_shot_window() -> None:
    stats = analyze_timeline(make_timeline([30.0, 30.0, 30.0, 30.0, 30.0, 1.0]))
    assert len(stats.pacing_curve) == 6
    _, rolling = stats.pacing_curve[-1]
    assert math.isclose(rolling, (30.0 * 4 + 1.0) / 5)


def test_rhythm_signature_shape() -> None:
    stats = analyze_timeline(make_timeline([1.0] * 10 + [10.0] * 10))
    signature = rhythm_signature(stats, buckets=4)
    assert len(signature) == 4
    assert signature[0] < signature[-1]
