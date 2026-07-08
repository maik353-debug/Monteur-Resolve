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
        metrics.append(
            FrameMetric(
                t=i / samples_per_second,
                brightness=float(frame.mean()),
                sharpness=sharpness,
                motion=motion,
            )
        )
        previous = frame
    return metrics


def list_media(directory: str | Path) -> list[Path]:
    """Video files in a directory, sorted by name."""
    directory = Path(directory)
    if not directory.is_dir():
        raise MonteurMediaError(f"not a directory: {directory}")
    return sorted(
        p for p in directory.iterdir() if p.suffix.lower() in MEDIA_EXTENSIONS
    )
