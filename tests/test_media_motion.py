"""Tests for monteur.media's global motion (phase correlation) and audio
metrics — synthetic numpy frames plus ffmpeg-backed integration tests.

Sign convention under test (see media._phase_shift): positive dx = scene
content moved right, positive dy = content moved down (image y grows down);
``cur = np.roll(prev, (3, -5), axis=(0, 1))`` recovers (dx=-5, dy=3).
"""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from monteur.media import _phase_shift, audio_metrics, frame_metrics

try:
    import imageio_ffmpeg

    HAVE_FFMPEG = True
except ImportError:
    HAVE_FFMPEG = False

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="imageio_ffmpeg not installed")


# --------------------------------------------------------- phase correlation


def textured_frame(seed=42, shape=(90, 160)):
    rng = np.random.default_rng(seed)
    return (rng.random(shape) * 255).astype(np.float32)


def test_phase_correlation_recovers_rolled_shift():
    prev = textured_frame()
    cur = np.roll(prev, (3, -5), axis=(0, 1))  # content: down 3 px, left 5 px
    dx, dy = _phase_shift(prev, cur)
    assert dx == pytest.approx(-5.0, abs=1.0)
    assert dy == pytest.approx(3.0, abs=1.0)


def test_phase_correlation_unwraps_large_shifts_to_negative():
    prev = textured_frame(seed=7)
    cur = np.roll(prev, (-20, 30), axis=(0, 1))  # content: up 20 px, right 30 px
    dx, dy = _phase_shift(prev, cur)
    assert dx == pytest.approx(30.0, abs=1.0)
    assert dy == pytest.approx(-20.0, abs=1.0)


def test_phase_correlation_weak_texture_reports_no_motion():
    # Uniform (textureless) frames: the correlation has no trustworthy peak,
    # so the shift is reported as (0, 0) rather than garbage.
    flat = np.full((90, 160), 128.0, dtype=np.float32)
    assert _phase_shift(flat, flat.copy()) == (0.0, 0.0)
    brighter = np.full((90, 160), 150.0, dtype=np.float32)
    assert _phase_shift(flat, brighter) == (0.0, 0.0)


@needs_ffmpeg
def test_frame_metrics_dx_consistent_on_scrolling_clip(tmp_path):
    """A static texture scrolling LEFT 2 px/frame -> steadily negative dx."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    rng = np.random.default_rng(7)
    tex = np.kron(rng.random((45, 80)), np.ones((4, 4)))  # 180x320, smooth blocks
    raw = tmp_path / "scroll.raw"
    with open(raw, "wb") as fh:
        for i in range(120):  # 4s @ 30 fps
            frame = np.roll(tex, -2 * i, axis=1)
            fh.write((frame * 255).astype(np.uint8).tobytes())
    clip = tmp_path / "scroll.mp4"
    subprocess.run(
        [exe, "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "320x180",
         "-r", "30", "-i", str(raw), "-pix_fmt", "yuv420p", str(clip)],
        check=True,
        capture_output=True,
    )

    metrics = frame_metrics(str(clip))
    assert metrics[0].dx == 0.0 and metrics[0].dy == 0.0
    dxs = [m.dx for m in metrics[1:]]
    assert dxs
    # 2 px/frame left at 320 wide = 15 px left per 0.5 s sample at 160 wide.
    negative = sum(1 for d in dxs if d < 0)
    assert negative >= 0.8 * len(dxs)
    assert sum(dxs) / len(dxs) == pytest.approx(-15.0, abs=3.0)


# ------------------------------------------------------------- audio metrics


def sine_wav(tmp_path, name, frequency, volume, duration=2):
    """ffmpeg's sine source peaks around 0.125; ``volume`` scales it."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = tmp_path / name
    subprocess.run(
        [exe, "-y", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}",
         "-af", f"volume={volume}", str(out)],
        check=True,
        capture_output=True,
    )
    return out


@needs_ffmpeg
def test_audio_metrics_moderate_sine_has_no_clipping(tmp_path):
    wav = sine_wav(tmp_path, "moderate.wav", frequency=440, volume=2.0)
    metrics = audio_metrics(str(wav))
    assert 3 <= len(metrics) <= 4  # 2 s of 0.5 s windows
    for i, m in enumerate(metrics):
        assert m.t == pytest.approx(i * 0.5)
        assert m.clipping == 0.0
        assert 0.1 < m.rms < 0.3  # ~0.25 peak sine -> rms ~0.18
        assert m.low_ratio < 0.1  # 440 Hz is far above the 150 Hz low band


@needs_ffmpeg
def test_audio_metrics_detects_clipping(tmp_path):
    # volume=20 drives the ~0.125-peak sine far past full scale; the wav
    # encoder saturates, so most samples sit at |x| >= 0.985.
    wav = sine_wav(tmp_path, "clipped.wav", frequency=440, volume=20.0)
    metrics = audio_metrics(str(wav))
    assert metrics
    assert all(m.clipping > 0.001 for m in metrics)
    assert all(m.clipping > 0.5 for m in metrics)  # heavily clipped


@needs_ffmpeg
def test_audio_metrics_low_frequency_dominates_low_ratio(tmp_path):
    wav = sine_wav(tmp_path, "rumble.wav", frequency=50, volume=2.0)
    metrics = audio_metrics(str(wav))
    assert metrics
    assert all(m.low_ratio > 0.6 for m in metrics)


@needs_ffmpeg
def test_audio_metrics_video_without_audio_returns_empty(tmp_path):
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    clip = tmp_path / "mute.mp4"
    subprocess.run(
        [exe, "-y", "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x180:rate=30",
         "-an", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
        capture_output=True,
    )
    assert audio_metrics(str(clip)) == []
