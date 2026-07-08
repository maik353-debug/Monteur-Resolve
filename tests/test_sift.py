"""Tests for monteur.sift — synthetic metrics for the heuristics, plus one
ffmpeg-backed integration test."""

from __future__ import annotations

import subprocess

import pytest

from monteur.media import FrameMetric
from monteur.sift import (
    BLURRY,
    DARK,
    SHAKY,
    USABLE,
    analyze_clip,
    classify_metrics,
    find_moments,
)

try:
    import imageio_ffmpeg

    HAVE_FFMPEG = True
except ImportError:
    HAVE_FFMPEG = False


def make_metrics(brightness, sharpness, motion, step=0.5):
    """Build a synthetic clip from per-sample value lists (2 samples/sec)."""
    n = max(len(brightness), len(sharpness), len(motion))

    def pick(values, i):
        return values[i] if i < len(values) else values[-1]

    out = []
    for i in range(n):
        out.append(
            FrameMetric(
                t=i * step,
                brightness=pick(brightness, i),
                sharpness=pick(sharpness, i),
                motion=0.0 if i == 0 else pick(motion, i),
            )
        )
    return out


def assert_tiles(segments, duration):
    assert segments[0].start == 0.0
    for a, b in zip(segments, segments[1:]):
        assert a.end == b.start
    assert segments[-1].end == duration


# ---------------------------------------------------------------- classify


def test_dark_stretch_detected_with_boundaries():
    # 12 samples over 6s; samples 4-7 (t=2.0..3.5) underexposed.
    brightness = [120] * 4 + [20] * 4 + [120] * 4
    metrics = make_metrics(brightness, [300] * 12, [2] * 12)
    segments = classify_metrics(metrics, 6.0)

    assert [s.label for s in segments] == [USABLE, DARK, USABLE]
    assert segments[1].start == 2.0
    assert segments[1].end == 4.0
    assert segments[1].score == 0.0
    assert segments[0].score > 0.0
    assert_tiles(segments, 6.0)


def test_blurry_stretch_uses_relative_threshold():
    # Sharpness 40 amid 300: p90 ~ 300, threshold 75 -> 40 is blurry.
    sharpness = [300] * 4 + [40] * 4 + [300] * 4
    metrics = make_metrics([120] * 12, sharpness, [2] * 12)
    segments = classify_metrics(metrics, 6.0)
    assert [s.label for s in segments] == [USABLE, BLURRY, USABLE]
    assert segments[1].start == 2.0
    assert segments[1].end == 4.0

    # A uniformly low-texture clip is NOT blurry relative to itself.
    flat = make_metrics([120] * 12, [30] * 12, [2] * 12)
    segments = classify_metrics(flat, 6.0)
    assert [s.label for s in segments] == [USABLE]


def test_jitter_is_shaky_but_steady_high_motion_is_usable():
    # Jittery block: high motion alternating strongly -> SHAKY.
    motion = [0, 2, 2, 2, 2, 14, 8, 15, 7, 14, 2, 2]
    metrics = make_metrics([120] * 12, [300] * 12, motion)
    segments = classify_metrics(metrics, 6.0)
    assert [s.label for s in segments] == [USABLE, SHAKY, USABLE]
    assert segments[1].start == 2.5
    assert segments[1].end == 5.0

    # Steady block at the same magnitude: no alternation -> stays USABLE.
    steady = [0, 2, 2, 2, 2, 8, 8, 8, 8, 8, 2, 2]
    metrics = make_metrics([120] * 12, [300] * 12, steady)
    segments = classify_metrics(metrics, 6.0)
    assert [s.label for s in segments] == [USABLE]


def test_single_sample_flicker_is_smoothed():
    brightness = [120] * 6 + [20] + [120] * 5
    metrics = make_metrics(brightness, [300] * 12, [2] * 12)
    segments = classify_metrics(metrics, 6.0)
    assert [s.label for s in segments] == [USABLE]
    assert_tiles(segments, 6.0)


def test_segments_tile_duration_with_mixed_labels():
    brightness = [120] * 3 + [20] * 3 + [120] * 6
    sharpness = [300] * 8 + [40] * 2 + [300] * 2
    metrics = make_metrics(brightness, sharpness, [2] * 12)
    segments = classify_metrics(metrics, 6.0)
    assert len(segments) >= 3
    assert_tiles(segments, 6.0)
    for seg in segments:
        if seg.label != USABLE:
            assert seg.score == 0.0
        else:
            assert 0.0 <= seg.score <= 1.0


def test_classify_empty_metrics():
    assert classify_metrics([], 6.0) == []


# ----------------------------------------------------------------- moments


def moving_vs_static_clip():
    # 24 samples / 12s, all usable: first half static (motion 0), second
    # half moderate steady motion (~clip median).
    motion = [0] * 12 + [2] * 12
    metrics = make_metrics([120] * 24, [300] * 24, motion)
    segments = classify_metrics(metrics, 12.0)
    assert [s.label for s in segments] == [USABLE]
    return segments, metrics


def test_moments_sorted_deduped_and_capped():
    segments, metrics = moving_vs_static_clip()
    moments = find_moments(segments, metrics, min_length=1.0)

    assert 0 < len(moments) <= 12
    scores = [m.score for m in moments]
    assert scores == sorted(scores, reverse=True)
    for m in moments:
        assert m.end - m.start == pytest.approx(1.0)
        assert 0.0 <= m.score <= 1.0
    # No two kept windows overlap.
    for i, a in enumerate(moments):
        for b in moments[i + 1 :]:
            assert a.end <= b.start + 1e-9 or a.start >= b.end - 1e-9


def test_moments_prefer_moderate_motion_over_static():
    segments, metrics = moving_vs_static_clip()
    moments = find_moments(segments, metrics, min_length=1.0)
    # Best windows live in the moving half (t >= 6.0).
    assert moments[0].start >= 6.0 - 1e-9
    static = [m for m in moments if m.end <= 6.0]
    moving = [m for m in moments if m.start >= 6.0]
    assert moving and static
    assert min(m.score for m in moving) > max(m.score for m in static)


def test_moments_respect_min_length():
    # Only a 2s usable stretch: no window fits at min_length=3.
    brightness = [120] * 4 + [20] * 8
    metrics = make_metrics(brightness, [300] * 12, [2] * 12)
    segments = classify_metrics(metrics, 6.0)
    assert segments[0].label == USABLE and segments[0].end == 2.0

    assert find_moments(segments, metrics, min_length=3.0) == []
    shorter = find_moments(segments, metrics, min_length=1.5)
    assert shorter
    for m in shorter:
        assert m.end - m.start == pytest.approx(1.5)
        assert m.start >= 0.0 and m.end <= 2.0 + 1e-9


# ------------------------------------------------------------- integration


@pytest.mark.skipif(not HAVE_FFMPEG, reason="imageio_ffmpeg not installed")
def test_analyze_clip_integration(tmp_path):
    """6s clip: 2s sharp testsrc2 + 2s boxblurred + 2s darkened, concatenated."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    src = "testsrc2=duration=2:size=320x180:rate=30"
    parts = []
    for i, vf in enumerate([None, "boxblur=8", "eq=brightness=-0.45"]):
        out = tmp_path / f"part{i}.mp4"
        cmd = [exe, "-y", "-f", "lavfi", "-i", src]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-pix_fmt", "yuv420p", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        parts.append(out)

    concat_list = tmp_path / "list.txt"
    concat_list.write_text("".join(f"file '{p}'\n" for p in parts))
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [exe, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", str(clip)],
        check=True,
        capture_output=True,
    )

    report = analyze_clip(str(clip))
    assert 5.5 <= report.duration <= 6.5
    assert_tiles(report.segments, report.duration)

    blurry = [s for s in report.segments if s.label == BLURRY]
    dark = [s for s in report.segments if s.label == DARK]
    assert blurry, f"no blurry segment in {report.segments}"
    assert dark, f"no dark segment in {report.segments}"

    b = max(blurry, key=lambda s: s.end - s.start)
    d = max(dark, key=lambda s: s.end - s.start)
    # Blur roughly in the 2..4s third, dark roughly in the 4..6s third.
    assert 1.0 <= b.start <= 3.0 and 3.0 <= b.end <= 5.0
    assert 3.0 <= d.start <= 5.0 and d.end >= 5.0

    assert report.usable_ratio == pytest.approx(0.33, abs=0.15)
    assert report.notes  # the "% unusable" note is present
