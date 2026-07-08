"""Media decoding for Monteur's automatic features.

Everything that touches actual video/audio goes through here: locating an
ffmpeg binary, probing files, decoding audio to a numpy array, and decoding
downscaled grayscale frames with per-frame metrics (brightness, sharpness,
motion). The heavy lifting is ffmpeg's; Monteur only reads the streams.

Requires the optional media extra: ``pip install 'monteur[media]'``
(numpy + imageio-ffmpeg, which bundles an ffmpeg binary for every platform).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MEDIA_EXTENSIONS = {
    ".mov", ".mp4", ".mxf", ".mkv", ".avi", ".m4v", ".mts", ".webm",
}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff", ".ogg"}


class MonteurMediaError(RuntimeError):
    """Raised when ffmpeg/numpy are unavailable or decoding fails."""


def _numpy():
    try:
        import numpy
    except ImportError as exc:
        raise MonteurMediaError(
            "media features need numpy: pip install 'monteur[media]'"
        ) from exc
    return numpy


def find_ffmpeg() -> str:
    """Locate ffmpeg: $FFMPEG_BINARY, PATH, or the bundled imageio binary."""
    import os

    env = os.environ.get("FFMPEG_BINARY")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    raise MonteurMediaError(
        "ffmpeg not found — install it (https://ffmpeg.org) or run: "
        "pip install 'monteur[media]'"
    )


def _run(cmd: list[str], runner=None) -> subprocess.CompletedProcess:
    runner = runner or subprocess.run
    result = runner(cmd, capture_output=True)
    return result


@dataclass
class MediaInfo:
    path: str
    duration: float  # seconds
    fps: float
    width: int
    height: int
    has_audio: bool


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*Audio:")


def probe(path: str | Path, runner=None) -> MediaInfo:
    """Read duration/fps/size/audio from ffmpeg's stream info."""
    path = Path(path)
    if not path.exists():
        raise MonteurMediaError(f"no such file: {path}")
    result = _run([find_ffmpeg(), "-hide_banner", "-i", str(path)], runner)
    text = result.stderr.decode("utf-8", "replace")
    m = _DURATION_RE.search(text)
    if not m:
        raise MonteurMediaError(f"ffmpeg could not read {path.name} — not a media file?")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    video = _VIDEO_RE.search(text)
    fps_match = _FPS_RE.search(text)
    return MediaInfo(
        path=str(path),
        duration=duration,
        fps=float(fps_match.group(1)) if fps_match else 0.0,
        width=int(video.group(1)) if video else 0,
        height=int(video.group(2)) if video else 0,
        has_audio=bool(_AUDIO_RE.search(text)),
    )


def read_audio(path: str | Path, rate: int = 22050, runner=None):
    """Decode a file's audio to mono float32 at ``rate`` Hz (numpy array)."""
    np = _numpy()
    result = _run(
        [
            find_ffmpeg(), "-hide_banner", "-i", str(path),
            "-vn", "-f", "f32le", "-ac", "1", "-ar", str(rate), "-",
        ],
        runner,
    )
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", "replace")[-400:]
        raise MonteurMediaError(f"could not decode audio from {path}: {stderr}")
    return np.frombuffer(result.stdout, dtype=np.float32)


@dataclass
class FrameMetric:
    """Cheap per-sample-frame quality signals.

    * brightness — mean luma 0..255; very low = underexposed
    * sharpness — variance of the image gradient; low = soft/blurry
      (relative within one clip, not comparable across clips)
    * motion — mean absolute pixel difference to the previous sample;
      high + erratic = shake, moderate + steady = action
    """

    t: float  # seconds
    brightness: float
    sharpness: float
    motion: float
    dx: float = 0.0  # global horizontal motion (px/sample, + = content moves right)
    dy: float = 0.0  # global vertical motion (px/sample, + = content moves down)


# Phase-correlation tuning (see _phase_shift): a correlation peak weaker than
# _PC_MIN_PEAK_FACTOR x the mean correlation surface means there is too
# little texture to trust the estimate, and no motion is reported instead.
_PC_MIN_PEAK_FACTOR = 3.0
_PC_EPS = 1e-9  # avoids division by zero when normalising the cross-spectrum


def _phase_shift(prev, cur) -> tuple[float, float]:
    """Global (dx, dy) shift between two grayscale frames (phase correlation).

    Sign convention: positive ``dx`` means scene content moved RIGHT from
    ``prev`` to ``cur``; positive ``dy`` means content moved DOWN (image y
    grows downward). A camera panning right therefore yields negative dx.
    Concretely, ``cur = np.roll(prev, (3, -5), axis=(0, 1))`` (content down 3
    px, left 5 px) comes back as ``(dx=-5.0, dy=3.0)``.

    Pure-numpy phase correlation: normalise the cross-power spectrum of the
    two Hann-windowed frames (window reduces wrap-around edge artifacts) and
    locate the inverse-FFT peak; peaks past half the frame size unwrap to
    negative shifts. If the peak is weak (< _PC_MIN_PEAK_FACTOR x the mean of
    the correlation surface) the shift is unreliable — flat or textureless
    frames — and (0.0, 0.0) is returned.
    """
    np = _numpy()
    height, width = prev.shape
    hann = np.outer(np.hanning(height), np.hanning(width))
    f1 = np.fft.fft2(prev * hann)
    f2 = np.fft.fft2(cur * hann)
    cross = f1 * np.conj(f2)
    r = np.abs(np.fft.ifft2(cross / (np.abs(cross) + _PC_EPS)))
    peak = float(r.max())
    if peak < _PC_MIN_PEAK_FACTOR * float(r.mean()):
        return 0.0, 0.0
    py, px = np.unravel_index(int(r.argmax()), r.shape)
    if py > height // 2:
        py -= height
    if px > width // 2:
        px -= width
    # The correlation peak sits at MINUS the content displacement.
    return float(-px), float(-py)


def frame_metrics(
    path: str | Path,
    samples_per_second: float = 2.0,
    size: tuple[int, int] = (160, 90),
    runner=None,
) -> list[FrameMetric]:
    """Decode downscaled grayscale frames and compute quality metrics."""
    np = _numpy()
    width, height = size
    result = _run(
        [
            find_ffmpeg(), "-hide_banner", "-i", str(path),
            "-vf", f"fps={samples_per_second},scale={width}:{height},format=gray",
            "-f", "rawvideo", "-",
        ],
        runner,
    )
    frame_bytes = width * height
    if result.returncode != 0 or len(result.stdout) < frame_bytes:
        stderr = result.stderr.decode("utf-8", "replace")[-400:]
        raise MonteurMediaError(f"could not decode video from {path}: {stderr}")
    count = len(result.stdout) // frame_bytes
    frames = np.frombuffer(
        result.stdout[: count * frame_bytes], dtype=np.uint8
    ).reshape(count, height, width)

    metrics: list[FrameMetric] = []
    previous = None
    for i in range(count):
        frame = frames[i].astype(np.float32)
        gy, gx = np.gradient(frame)
        sharpness = float((gx**2 + gy**2).mean())
        motion = float(np.abs(frame - previous).mean()) if previous is not None else 0.0
        dx, dy = _phase_shift(previous, frame) if previous is not None else (0.0, 0.0)
        metrics.append(
            FrameMetric(
                t=i / samples_per_second,
                brightness=float(frame.mean()),
                sharpness=sharpness,
                motion=motion,
                dx=dx,
                dy=dy,
            )
        )
        previous = frame
    return metrics


# Audio-metric tuning (approximate; see AudioMetric).
_CLIP_LEVEL = 0.985  # |sample| at/above this counts as (near-)digital clipping
_LOW_BAND_HZ = 150.0  # ceiling of the "low" band; wind/handling rumble lives below


@dataclass
class AudioMetric:
    """Cheap per-window audio quality signals.

    * rms — root-mean-square level of the window (0..~1 for sane audio)
    * clipping — fraction of samples with |x| >= _CLIP_LEVEL (≈ full scale);
      any noticeable amount means the recording distorted
    * low_ratio — share of spectral energy below _LOW_BAND_HZ; near 1 with
      audible level usually means wind or handling rumble, not speech
    """

    t: float  # window start, seconds
    rms: float
    clipping: float
    low_ratio: float


def audio_metrics(
    path: str | Path, rate: int = 22050, window: float = 0.5, runner=None
) -> list[AudioMetric]:
    """Per-``window``-second audio quality metrics for a file.

    Files without an audio stream return ``[]`` (probe guard, so the audio
    decode is never attempted on video-only material). Only full windows are
    analysed; a sub-window tail is dropped.
    """
    np = _numpy()
    if not probe(path, runner).has_audio:
        return []
    samples = read_audio(path, rate=rate, runner=runner)
    win = max(1, int(round(window * rate)))
    count = len(samples) // win
    freqs = np.fft.rfftfreq(win, d=1.0 / rate)
    low_band = freqs < _LOW_BAND_HZ
    metrics: list[AudioMetric] = []
    for i in range(count):
        chunk = samples[i * win : (i + 1) * win].astype(np.float64)
        energy = np.abs(np.fft.rfft(chunk)) ** 2
        total = float(energy.sum())
        metrics.append(
            AudioMetric(
                t=i * window,
                rms=float(np.sqrt(np.mean(chunk**2))),
                clipping=float(np.mean(np.abs(chunk) >= _CLIP_LEVEL)),
                low_ratio=float(energy[low_band].sum() / total) if total > 0 else 0.0,
            )
        )
    return metrics


def list_media(directory: str | Path) -> list[Path]:
    """Video files in a directory, sorted by name."""
    directory = Path(directory)
    if not directory.is_dir():
        raise MonteurMediaError(f"not a directory: {directory}")
    return sorted(
        p for p in directory.iterdir() if p.suffix.lower() in MEDIA_EXTENSIONS
    )
