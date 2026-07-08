"""Tests for monteur.sift — synthetic metrics for the heuristics, plus one
ffmpeg-backed integration test."""

from __future__ import annotations

import subprocess

import pytest

from monteur.media import AudioMetric, FrameMetric
from monteur.sift import (
    BLURRY,
    DARK,
    SHAKY,
    USABLE,
    Moment,
    analyze_clip,
    apply_audio,
    audio_flags,
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


# ------------------------------------------------------------------- audio


def make_audio(rms, clipping=None, low_ratio=None, step=0.5):
    """Build synthetic audio windows from per-window value lists."""
    clipping = clipping or [0.0]
    low_ratio = low_ratio or [0.2]
    n = max(len(rms), len(clipping), len(low_ratio))

    def pick(values, i):
        return values[i] if i < len(values) else values[-1]

    return [
        AudioMetric(
            t=i * step,
            rms=pick(rms, i),
            clipping=pick(clipping, i),
            low_ratio=pick(low_ratio, i),
        )
        for i in range(n)
    ]


def test_audio_flags_wind_note():
    audio = make_audio(rms=[0.05] * 8, low_ratio=[0.8] * 8)
    notes, _ = audio_flags(audio)
    assert "audio: likely wind noise" in notes
    assert "audio: mostly silent" not in notes


def test_audio_flags_silence_note_and_no_wind_when_silent():
    # Near-silence: silent note fires; the rumble-heavy spectrum of noise
    # floor does NOT read as wind (rms below the floor).
    audio = make_audio(rms=[0.002] * 8, low_ratio=[0.9] * 8)
    notes, bursts = audio_flags(audio)
    assert "audio: mostly silent" in notes
    assert not any("wind" in n for n in notes)
    assert bursts == [0.0] * 8  # noise-floor wobble is not a highlight


def test_audio_flags_clipping_note_counts_windows():
    clipping = [0.0, 0.0, 0.05, 0.02, 0.9, 0.0, 0.0, 0.0]
    audio = make_audio(rms=[0.2] * 8, clipping=clipping)
    notes, _ = audio_flags(audio)
    assert "audio: clipping in 3 windows" in notes


def test_audio_flags_clean_audio_has_no_notes():
    audio = make_audio(rms=[0.1] * 8)
    notes, bursts = audio_flags(audio)
    assert notes == []
    assert bursts == [0.0] * 8


def test_audio_flags_empty():
    assert audio_flags([]) == ([], [])


def test_highlight_for_loud_burst_window():
    # Windows 4 and 5 (t=2.0, 2.5) are loud bursts: > 1.8x median rms 0.05.
    rms = [0.05] * 12
    rms[4] = rms[5] = 0.3
    audio = make_audio(rms=rms)
    loud = Moment(start=2.0, end=3.0, score=0.5)
    quiet = Moment(start=4.0, end=5.0, score=0.5)
    notes = apply_audio([loud, quiet], audio)
    assert loud.highlight == pytest.approx(1.0)  # min(1, 1.0 * 3)
    assert quiet.highlight == 0.0
    assert notes == []


def test_highlight_scales_with_burst_share():
    # A 2 s moment covering 4 windows with 1 burst: min(1, 0.25 * 3) = 0.75.
    rms = [0.05] * 16
    rms[4] = 0.5
    audio = make_audio(rms=rms)
    moment = Moment(start=2.0, end=4.0, score=0.5)
    apply_audio([moment], audio)
    assert moment.highlight == pytest.approx(0.75)


def test_apply_audio_without_audio_keeps_highlight_zero():
    moment = Moment(start=0.0, end=1.0, score=0.5)
    assert apply_audio([moment], []) == []
    assert moment.highlight == 0.0


def test_moments_carry_entry_exit_motion():
    # 24 usable samples with dx = sample index, dy = -1: each 2 s moment's
    # entry/exit motion is the mean dx/dy of its first/last two samples.
    metrics = make_metrics([120] * 24, [300] * 24, [2] * 24)
    for i, m in enumerate(metrics):
        m.dx = float(i)
        m.dy = -1.0
    segments = classify_metrics(metrics, 12.0)
    moments = find_moments(segments, metrics, min_length=2.0)
    assert moments
    eps = 1e-9
    for mom in moments:
        idx = [j for j, m in enumerate(metrics) if mom.start - eps <= m.t < mom.end - eps]
        assert len(idx) == 4
        expected_entry = (idx[0] + idx[1]) / 2
        expected_exit = (idx[-2] + idx[-1]) / 2
        assert mom.entry_motion == pytest.approx((expected_entry, -1.0))
        assert mom.exit_motion == pytest.approx((expected_exit, -1.0))
        assert mom.entry_motion != mom.exit_motion


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

    # The clip has no audio stream: no audio notes, highlights stay 0.0.
    assert not any(n.startswith("audio:") for n in report.notes)
    assert all(m.highlight == 0.0 for m in report.moments)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="imageio_ffmpeg not installed")
def test_analyze_clip_flags_silent_audio(tmp_path):
    """testsrc2 video + digital-silence audio -> 'audio: mostly silent'."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    clip = tmp_path / "silent.mp4"
    subprocess.run(
        [exe, "-y", "-f", "lavfi", "-i", "testsrc2=duration=4:size=320x180:rate=30",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest",
         "-pix_fmt", "yuv420p", str(clip)],
        check=True,
        capture_output=True,
    )
    report = analyze_clip(str(clip))
    assert "audio: mostly silent" in report.notes
    assert report.moments
    assert all(m.highlight == 0.0 for m in report.moments)  # silence: no bursts
