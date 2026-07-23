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

from monteur.media import (
    _metrics_from_frames,
    _parse_keyframe_pts,
    _phase_shift,
    audio_metrics,
    frame_metrics,
)

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


def test_residual_motion_separates_pan_from_subject_motion():
    # A pure camera pan (the whole frame shifts) leaves ~0 residual once the
    # global shift is aligned out; a subject moving IN a still frame leaves a
    # large residual. Both have high raw `motion`.
    base = textured_frame(seed=7)
    panned = np.roll(base, (0, 6), axis=1)  # content moved right 6 px (a pan)
    subject = base.copy()
    subject[30:60, 30:60] = 255 - subject[30:60, 30:60]  # a patch changes, frame still

    pan_metrics = _metrics_from_frames([base, panned], [0.0, 0.5])
    sub_metrics = _metrics_from_frames([base, subject], [0.0, 0.5])

    # both moved a lot in raw terms
    assert pan_metrics[1].motion > 5.0
    assert sub_metrics[1].motion > 5.0
    # ...but the pan's residual is a small fraction of its motion, while the
    # subject motion is (almost) all residual
    assert pan_metrics[1].residual < 0.5 * pan_metrics[1].motion
    assert sub_metrics[1].residual > 0.8 * sub_metrics[1].motion
    assert sub_metrics[1].residual > pan_metrics[1].residual


def smooth_frame(cx, cy, shape=(90, 160)):
    """A smooth low-frequency 'scene' (a bright blob on a gradient) — like real
    footage, unlike white noise, so a perceptual hash is stable under a small
    nudge and only shifts when the composition really changes."""
    ys, xs = np.mgrid[0 : shape[0], 0 : shape[1]].astype(np.float32)
    grad = (xs / shape[1]) * 120.0
    blob = 130.0 * np.exp(-(((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * 25.0**2)))
    return np.clip(grad + blob, 0, 255).astype(np.float32)


def test_dhash_near_identical_frames_hash_close():
    from monteur.media import _dhash, phash_distance

    base = smooth_frame(80, 45)
    # a small exposure bump + 1px shift keeps the SAME composition -> close hash
    nudged = np.clip(np.roll(base, 1, axis=1) * 1.05, 0, 255).astype(np.float32)
    different = smooth_frame(20, 70)  # the subject is somewhere else entirely

    h_base = _dhash(base, np)
    h_nudged = _dhash(nudged, np)
    h_diff = _dhash(different, np)

    assert phash_distance(h_base, h_base) == 0
    assert phash_distance(h_base, h_nudged) <= 6          # near-identical
    assert phash_distance(h_base, h_diff) > 6             # clearly different


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


# -------------------------------------------------------- keyframe fast path


def make_h264_clip(tmp_path, seconds, gop=None, name="clip.mp4"):
    """Encode a testsrc2 H.264 clip; ``gop`` forces a fixed keyframe interval."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = tmp_path / name
    cmd = [
        exe, "-y", "-f", "lavfi",
        "-i", f"testsrc2=duration={seconds}:size=320x180:rate=30",
    ]
    if gop is not None:
        cmd += ["-g", str(gop), "-keyint_min", str(gop)]
    cmd += ["-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


@needs_ffmpeg
def test_frame_metrics_long_clip_uses_keyframe_sampling(tmp_path):
    """50s clip with a 1s GOP: ~50 keyframe samples at (near-)integer pts,
    NOT the ~100 uniform 2/s samples of the full-decode path."""
    clip = make_h264_clip(tmp_path, seconds=50, gop=30)
    metrics = frame_metrics(str(clip))

    assert 45 <= len(metrics) <= 55  # one keyframe per second, give or take
    for m in metrics:
        assert abs(m.t - round(m.t)) <= 0.1  # exact pts, on the GOP grid
        assert 0.0 <= m.brightness <= 255.0
    # Metrics still classify downstream.
    from monteur.sift import classify_metrics

    segments = classify_metrics(metrics, 50.0)
    assert segments
    assert segments[-1].end == pytest.approx(50.0)


@needs_ffmpeg
def test_frame_metrics_short_clip_keeps_uniform_full_decode(tmp_path):
    """A 6s clip stays on the full-decode path: 2/s samples, uniform spacing."""
    clip = make_h264_clip(tmp_path, seconds=6)
    metrics = frame_metrics(str(clip))
    assert abs(len(metrics) - 12) <= 1  # duration * samples_per_second
    for i, m in enumerate(metrics):
        assert m.t == pytest.approx(i / 2.0)


@needs_ffmpeg
def test_frame_metrics_10s_clip_now_uses_keyframe_path(tmp_path):
    """A 10s clip with a 1s GOP clears the lowered 8s threshold and its ~10
    keyframes beat the max(6, 10/6) sparse-GOP floor: keyframe path, samples
    at (near-)integer pts instead of 20 uniform half-second samples."""
    clip = make_h264_clip(tmp_path, seconds=10, gop=30, name="ten.mp4")
    metrics = frame_metrics(str(clip))
    assert 8 <= len(metrics) <= 12  # one keyframe per second, give or take
    for m in metrics:
        assert abs(m.t - round(m.t)) <= 0.1  # exact pts, on the GOP grid


@needs_ffmpeg
def test_frame_metrics_5s_clip_still_full_decodes(tmp_path):
    """A 5s clip sits below the 8s threshold: uniform 2/s full-decode samples,
    even when its GOP would offer enough keyframes."""
    clip = make_h264_clip(tmp_path, seconds=5, gop=30, name="five.mp4")
    metrics = frame_metrics(str(clip))
    assert abs(len(metrics) - 10) <= 1
    for i, m in enumerate(metrics):
        assert m.t == pytest.approx(i / 2.0)


_PROBE_STDERR = (
    b"  Duration: 00:00:50.00, start: 0.000000, bitrate: 900 kb/s\n"
    b"  Stream #0:0: Video: h264 (High), yuv420p, 320x180, 30 fps, 30 tbr\n"
)


def _fake_runner(keyframe_stdout, keyframe_stderr, full_stdout, calls):
    """A frame_metrics ``runner`` that answers probe/keyframe/full-decode
    commands from canned data and records which kind of command ran."""

    def runner(cmd, capture_output=True):
        if "-skip_frame" in cmd:
            calls.append("keyframe")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=keyframe_stdout, stderr=keyframe_stderr
            )
        if any("fps=" in str(arg) for arg in cmd):
            calls.append("full")
            return subprocess.CompletedProcess(cmd, 0, stdout=full_stdout, stderr=b"")
        calls.append("probe")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=_PROBE_STDERR)

    return runner


def test_frame_metrics_sparse_keyframes_fall_back_to_full_decode(tmp_path, monkeypatch):
    """Only 2 keyframes in a 50s clip (sparse GOP): the fast path must be
    rejected and the full-decode command must run."""
    import monteur.media as media

    monkeypatch.setattr(media, "find_ffmpeg", lambda: "ffmpeg")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    width, height = 4, 3
    frame_bytes = width * height
    calls: list[str] = []
    runner = _fake_runner(
        keyframe_stdout=bytes(frame_bytes * 2),
        keyframe_stderr=b"[Parsed_showinfo_0] n: 0 pts_time:0 x\n"
        b"[Parsed_showinfo_0] n: 1 pts_time:25 x\n",
        full_stdout=bytes(frame_bytes * 100),
        calls=calls,
    )
    metrics = frame_metrics(str(clip), size=(width, height), runner=runner)

    assert calls == ["probe", "keyframe", "full"]  # tried fast path, fell back
    assert len(metrics) == 100  # 50s x 2/s from the full decode
    assert [m.t for m in metrics[:4]] == [0.0, 0.5, 1.0, 1.5]


def test_frame_metrics_frame_pts_mismatch_falls_back(tmp_path, monkeypatch):
    """3 rawvideo frames but only 2 pts_time lines: mistrust and fall back."""
    import monteur.media as media

    monkeypatch.setattr(media, "find_ffmpeg", lambda: "ffmpeg")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    width, height = 4, 3
    frame_bytes = width * height
    calls: list[str] = []
    runner = _fake_runner(
        keyframe_stdout=bytes(frame_bytes * 3),
        keyframe_stderr=b"pts_time:0\npts_time:1\n",
        full_stdout=bytes(frame_bytes * 100),
        calls=calls,
    )
    metrics = frame_metrics(str(clip), size=(width, height), runner=runner)
    assert calls == ["probe", "keyframe", "full"]
    assert len(metrics) == 100


def test_frame_metrics_keyframe_path_uses_exact_pts_and_crlf(tmp_path, monkeypatch):
    """A healthy keyframe stream (with Windows \\r\\n stderr) is used as-is:
    timestamps are the parsed pts, and the full decode never runs."""
    import monteur.media as media

    monkeypatch.setattr(media, "find_ffmpeg", lambda: "ffmpeg")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    width, height = 4, 3
    frame_bytes = width * height
    pts = [round(i + 0.25, 2) for i in range(50)]  # ~1/s over the 50s probe
    stderr = b"\r\n".join(
        f"[Parsed_showinfo_0] n: {i} pts_time:{t} pos: 1".encode()
        for i, t in enumerate(pts)
    ) + b"\r\n"
    calls: list[str] = []
    runner = _fake_runner(
        keyframe_stdout=bytes(frame_bytes * 50),
        keyframe_stderr=stderr,
        full_stdout=b"",
        calls=calls,
    )
    metrics = frame_metrics(str(clip), size=(width, height), runner=runner)
    assert calls == ["probe", "keyframe"]  # no fallback
    assert [m.t for m in metrics] == pytest.approx(pts)


def test_frame_metrics_all_intra_keyframes_are_thinned(tmp_path, monkeypatch):
    """300 keyframes over 50s (all-intra, 6/s) get thinned to ~2/s."""
    import monteur.media as media

    monkeypatch.setattr(media, "find_ffmpeg", lambda: "ffmpeg")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    width, height = 4, 3
    frame_bytes = width * height
    pts = [i / 6.0 for i in range(300)]
    stderr = "".join(f"pts_time:{t:.6f}\n" for t in pts).encode()
    calls: list[str] = []
    runner = _fake_runner(
        keyframe_stdout=bytes(frame_bytes * 300),
        keyframe_stderr=stderr,
        full_stdout=b"",
        calls=calls,
    )
    metrics = frame_metrics(str(clip), size=(width, height), runner=runner)
    assert calls == ["probe", "keyframe"]
    assert len(metrics) == 100  # every 3rd of 300 -> ~2 samples/second
    assert [m.t for m in metrics] == pytest.approx(pts[::3])


def test_parse_keyframe_pts_tolerates_crlf_lines():
    text = (
        "[Parsed_showinfo_0 @ 0x55] n: 0 pts: 0 pts_time:0 duration: 512\r\n"
        "[Parsed_showinfo_0 @ 0x55] n: 1 pts: 512 pts_time:1.001 duration: 512\r\n"
        "[Parsed_showinfo_0 @ 0x55] n: 2 pts: 1024 pts_time:2.5\r\n"
    )
    assert _parse_keyframe_pts(text) == [0.0, 1.001, 2.5]


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
