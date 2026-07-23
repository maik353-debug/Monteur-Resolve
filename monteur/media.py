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


class MediaCancelled(RuntimeError):
    """Raised when a cancellable ffmpeg run is killed mid-flight.

    Deliberately NOT a subclass of :class:`MonteurMediaError` so the
    ``except MonteurMediaError`` handlers that turn a decode failure into a
    soft per-clip note do NOT swallow a cancellation — it must propagate all
    the way up so the caller can tear the whole run down promptly.
    """


# How often the cancellable :func:`_run` wakes to check the cancel flag while
# an ffmpeg subprocess is running. Small enough that a set cancel kills the
# running process within a fraction of a second, large enough not to spin.
_CANCEL_POLL_S = 0.15


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


def _run(cmd: list[str], runner=None, cancel=None) -> subprocess.CompletedProcess:
    """Run ``cmd`` and capture its output.

    ``cancel`` — anything with ``.is_set()`` (e.g. a ``threading.Event``) —
    makes the run KILLABLE: instead of blocking in ``subprocess.run`` until
    ffmpeg finishes, the process is polled and, the moment ``cancel`` is set,
    killed within ~``_CANCEL_POLL_S`` and :class:`MediaCancelled` is raised.

    When ``cancel is None`` (the default) this is byte-identical to the
    original blocking implementation: ``runner`` (or ``subprocess.run``) is
    called with ``capture_output=True`` and its result returned unchanged, so
    every existing caller, test and fixture behaves exactly as before. The
    poll-and-kill path engages ONLY when a real cancel object is passed.
    """
    if cancel is None:
        runner = runner or subprocess.run
        return runner(cmd, capture_output=True)
    # Cancellable path: run ffmpeg under our own control and poll the flag.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_CANCEL_POLL_S)
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                proc.kill()
                proc.wait()  # reap — never leave a zombie behind
                raise MediaCancelled("ffmpeg run cancelled")
            continue  # still running, cancel not set — keep polling
        # communicate() has already drained both pipes on normal completion.
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


@dataclass
class MediaInfo:
    path: str
    duration: float  # seconds
    fps: float
    width: int
    height: int
    has_audio: bool
    start_timecode: str = ""  # embedded start TC (e.g. "01:47:52:08"), "" if none


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*Audio:")
# Embedded start timecode, printed by ffmpeg as a metadata line such as
# "    timecode        : 01:47:52:08" (drop-frame uses ';' before frames).
_TIMECODE_RE = re.compile(r"timecode\s*:\s*(\d{1,2}:\d{2}:\d{2}[:;]\d{1,3})")


def probe(path: str | Path, runner=None, cancel=None) -> MediaInfo:
    """Read duration/fps/size/audio from ffmpeg's stream info."""
    path = Path(path)
    if not path.exists():
        raise MonteurMediaError(f"no such file: {path}")
    result = _run([find_ffmpeg(), "-hide_banner", "-i", str(path)], runner, cancel)
    text = result.stderr.decode("utf-8", "replace")
    m = _DURATION_RE.search(text)
    if not m:
        raise MonteurMediaError(f"ffmpeg could not read {path.name} — not a media file?")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    video = _VIDEO_RE.search(text)
    fps_match = _FPS_RE.search(text)
    tc_match = _TIMECODE_RE.search(text)
    return MediaInfo(
        path=str(path),
        duration=duration,
        fps=float(fps_match.group(1)) if fps_match else 0.0,
        width=int(video.group(1)) if video else 0,
        height=int(video.group(2)) if video else 0,
        has_audio=bool(_AUDIO_RE.search(text)),
        start_timecode=tc_match.group(1) if tc_match else "",
    )


def start_timecode_seconds(info: MediaInfo) -> float:
    """The file's embedded start timecode as seconds, 0.0 when unknown.

    Real camera files (DJI action cams, most pro cameras) carry a
    time-of-day start timecode; NLEs like DaVinci Resolve link media by
    matching source ranges against it. Returns 0.0 when the file has no
    embedded TC, its frame rate is unknown, or the TC cannot be parsed.
    """
    if not info.start_timecode or info.fps <= 0:
        return 0.0
    from monteur.model import parse_timecode

    try:
        frames = parse_timecode(info.start_timecode, info.fps)
    except ValueError:
        return 0.0
    return frames / info.fps


def read_audio(path: str | Path, rate: int = 22050, runner=None, cancel=None):
    """Decode a file's audio to mono float32 at ``rate`` Hz (numpy array)."""
    np = _numpy()
    result = _run(
        [
            find_ffmpeg(), "-hide_banner", "-i", str(path),
            "-vn", "-f", "f32le", "-ac", "1", "-ar", str(rate), "-",
        ],
        runner,
        cancel,
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
    * residual — motion LEFT after the global camera shift (dx, dy) is
      removed: previous is aligned to the current frame by the estimated
      pan and re-differenced, so a pure camera pan reads ~0 while a subject
      moving IN the frame reads high. This is the honest "something is
      happening" signal (camera-independent action).
    """

    t: float  # seconds
    brightness: float
    sharpness: float
    motion: float
    dx: float = 0.0  # global horizontal motion (px/sample, + = content moves right)
    dy: float = 0.0  # global vertical motion (px/sample, + = content moves down)
    residual: float = 0.0  # motion the camera pan does NOT explain (subject action)
    phash: int = 0  # 64-bit perceptual (difference) hash of the frame


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


# Keyframe fast path (see frame_metrics): clips at/above this duration are
# sampled by decoding ONLY keyframes, which skips the (very expensive) full
# decode of long 4K/H.265 material. Even a ~10s 4K clip takes a minute to
# full-decode, so the threshold is low; correctness is still guaranteed by
# the sparse-keyframe fallback below (fewer than max(6, duration/6) keyframes
# rejects the fast path — e.g. a 10s clip with a 1s GOP has ~10 keyframes and
# qualifies, while a 10s clip with a 10s GOP has 1 and falls back). Only
# genuinely short clips keep the uniform full-decode path.
_KEYFRAME_MIN_DURATION = 8.0  # seconds
# All-intra codecs mark every frame a keyframe; above this multiple of the
# requested sample rate the decoded keyframes are thinned back down so the
# metric list stays bounded (the decode cost is already paid at that point).
_KEYFRAME_THIN_FACTOR = 1.5
# showinfo logs one line per frame containing "pts_time:<float>".
_PTS_TIME_RE = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?)")


def _parse_keyframe_pts(stderr_text: str) -> list[float]:
    """Extract per-frame pts_time floats from showinfo's stderr log.

    Tolerates \\r\\n line endings (Windows consoles) — the regex scans the raw
    text, so line-ending flavour does not matter.
    """
    return [float(m) for m in _PTS_TIME_RE.findall(stderr_text)]


def _resize_mean(frame, np, rows: int, cols: int):
    """Block-average ``frame`` down to a ``rows``x``cols`` grid (for hashing)."""
    row_edges = np.linspace(0, frame.shape[0], rows + 1).astype(int)
    col_edges = np.linspace(0, frame.shape[1], cols + 1).astype(int)
    out = np.empty((rows, cols), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            block = frame[row_edges[r]:row_edges[r + 1], col_edges[c]:col_edges[c + 1]]
            out[r, c] = float(block.mean()) if block.size else 0.0
    return out


def _dhash(frame, np) -> int:
    """A 64-bit difference hash: 8x9 block means, row-wise adjacent compares.

    Robust to small exposure/scale/framing changes, so near-identical shots
    hash close together (small Hamming distance). Pure numpy, no PIL.
    """
    small = _resize_mean(frame, np, 8, 9)
    diff = small[:, 1:] > small[:, :-1]  # 8x8 booleans
    bits = 0
    for b in diff.reshape(-1):
        bits = (bits << 1) | int(b)
    return bits


def phash_distance(a: int, b: int) -> int:
    """Hamming distance between two 64-bit perceptual hashes (0 = identical)."""
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def _metrics_from_frames(frames, times) -> list[FrameMetric]:
    """Per-frame quality metrics for decoded grayscale frames at ``times``.

    Shared by the full-decode and keyframe paths so the brightness/sharpness/
    motion/phase-correlation math exists exactly once. ``motion``/``dx``/``dy``
    are measured between consecutive entries of ``frames``, whatever their
    temporal spacing.
    """
    np = _numpy()
    metrics: list[FrameMetric] = []
    previous = None
    for i, t in enumerate(times):
        frame = frames[i].astype(np.float32)
        gy, gx = np.gradient(frame)
        sharpness = float((gx**2 + gy**2).mean())
        motion = float(np.abs(frame - previous).mean()) if previous is not None else 0.0
        dx, dy = _phase_shift(previous, frame) if previous is not None else (0.0, 0.0)
        # residual (subject) motion: align previous to the current frame by the
        # estimated global pan, then what remains is motion the camera does NOT
        # explain. np.roll wraps a thin border, a negligible constant on a
        # 160x90 frame; when there is no pan (dx=dy=0) residual == motion.
        if previous is not None:
            aligned = np.roll(previous, (int(round(dy)), int(round(dx))), axis=(0, 1))
            residual = float(np.abs(frame - aligned).mean())
        else:
            residual = 0.0
        metrics.append(
            FrameMetric(
                t=float(t),
                brightness=float(frame.mean()),
                sharpness=sharpness,
                motion=motion,
                dx=dx,
                dy=dy,
                residual=residual,
                phash=_dhash(frame, np),
            )
        )
        previous = frame
    return metrics


def _full_decode_metrics(
    path: str | Path,
    samples_per_second: float,
    size: tuple[int, int],
    runner=None,
    cancel=None,
) -> list[FrameMetric]:
    """The original uniform-sampling path: decode every frame, fps-filter down."""
    np = _numpy()
    width, height = size
    result = _run(
        [
            find_ffmpeg(), "-hide_banner", "-i", str(path),
            "-vf", f"fps={samples_per_second},scale={width}:{height},format=gray",
            "-f", "rawvideo", "-",
        ],
        runner,
        cancel,
    )
    frame_bytes = width * height
    if result.returncode != 0 or len(result.stdout) < frame_bytes:
        stderr = result.stderr.decode("utf-8", "replace")[-400:]
        raise MonteurMediaError(f"could not decode video from {path}: {stderr}")
    count = len(result.stdout) // frame_bytes
    frames = np.frombuffer(
        result.stdout[: count * frame_bytes], dtype=np.uint8
    ).reshape(count, height, width)
    return _metrics_from_frames(frames, [i / samples_per_second for i in range(count)])


def _keyframe_metrics(
    path: str | Path,
    samples_per_second: float,
    size: tuple[int, int],
    duration: float,
    runner=None,
    cancel=None,
) -> list[FrameMetric] | None:
    """Keyframe-only sampling; returns None whenever the fast path can't be trusted.

    ``-skip_frame nokey`` makes the decoder skip every non-keyframe (cheap:
    inter frames are never reconstructed); showinfo logs each emitted frame's
    exact ``pts_time`` to stderr, so real timestamps come for free. showinfo
    logs at info level, so no ``-loglevel error`` here. Any of the following
    silently defers to the full-decode path (correctness never depends on the
    fast path):

    * ffmpeg failed or produced no complete frame;
    * emitted frame count and parsed pts count disagree;
    * fewer than max(6, duration / 6) keyframes — sparse-GOP codecs would
      leave the clip badly undersampled.
    """
    np = _numpy()
    width, height = size
    result = _run(
        [
            find_ffmpeg(), "-hide_banner", "-skip_frame", "nokey", "-i", str(path),
            "-vf", f"showinfo,scale={width}:{height},format=gray",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-",
        ],
        runner,
        cancel,
    )
    frame_bytes = width * height
    if result.returncode != 0 or len(result.stdout) < frame_bytes:
        return None
    count = len(result.stdout) // frame_bytes
    pts = _parse_keyframe_pts(result.stderr.decode("utf-8", "replace"))
    if len(pts) != count:
        return None
    if count < max(6.0, duration / 6.0):
        return None
    frames = np.frombuffer(
        result.stdout[: count * frame_bytes], dtype=np.uint8
    ).reshape(count, height, width)

    # All-intra material (every frame a keyframe): thin back to roughly the
    # requested rate. The decode cost is already paid; this only bounds the
    # size of the metric list for downstream consumers.
    rate = count / duration if duration > 0 else 0.0
    if rate > _KEYFRAME_THIN_FACTOR * samples_per_second:
        stride = max(1, round(rate / samples_per_second))
        frames = frames[::stride]
        pts = pts[::stride]
    return _metrics_from_frames(frames, pts)


def frame_metrics(
    path: str | Path,
    samples_per_second: float = 2.0,
    size: tuple[int, int] = (160, 90),
    runner=None,
    cancel=None,
) -> list[FrameMetric]:
    """Decode downscaled grayscale frames and compute quality metrics.

    Clips shorter than _KEYFRAME_MIN_DURATION (8 s) are sampled uniformly at
    ``samples_per_second`` by decoding every frame (the original path). Longer
    clips use a keyframe-only fast path: only keyframes are decoded (orders of
    magnitude cheaper on long-GOP 4K/H.265 material), so sample spacing
    follows the codec's GOP — typically ~0.5–2 s — while each sample's
    timestamp is the frame's EXACT pts. On that path motion/dx/dy are measured
    between consecutive KEPT samples rather than fixed 0.5 s steps, which is
    fine because every consumer compares them relative to the clip's own
    median. All-intra codecs (every frame a keyframe) are thinned back to
    roughly ``samples_per_second``. If the keyframe stream looks untrustworthy
    (decode failure, frame/pts count mismatch, or sparse GOPs yielding fewer
    than max(6, duration/6) keyframes) the full-decode path runs instead, so
    results never depend on the fast path. ``runner`` is injected into every
    ffmpeg invocation (probe, keyframe and full decode alike) for tests.
    """
    duration = 0.0
    try:
        duration = probe(path, runner, cancel).duration
    except MonteurMediaError:
        pass  # let the full-decode path produce the canonical error
    if duration >= _KEYFRAME_MIN_DURATION:
        metrics = _keyframe_metrics(
            path, samples_per_second, size, duration, runner, cancel
        )
        if metrics is not None:
            return metrics
    return _full_decode_metrics(path, samples_per_second, size, runner, cancel)


def extract_rgb_frame(
    path: str | Path,
    time_s: float,
    size: tuple[int, int] = (64, 36),
    runner=None,
    cancel=None,
):
    """One downscaled RGB frame at ``time_s`` as a numpy uint8 array (H, W, 3).

    The COLOR sibling of the grayscale metric decode above — used by
    :mod:`monteur.daylight` to judge time of day (warmth needs the R-B
    balance, which grayscale cannot carry). ``-ss`` before ``-i`` seeks on
    keyframes (fast; frame-exactness does not matter for a representative
    sample) and the raw rgb24 frame is read straight from the pipe — no
    temp files. Raises :class:`MonteurMediaError` when ffmpeg is missing
    or the frame cannot be decoded.
    """
    np = _numpy()
    width, height = size
    result = _run(
        [
            find_ffmpeg(), "-hide_banner",
            "-ss", f"{max(0.0, float(time_s)):.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", f"scale={width}:{height}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        runner,
        cancel,
    )
    frame_bytes = width * height * 3
    if result.returncode != 0 or len(result.stdout) < frame_bytes:
        stderr = result.stderr.decode("utf-8", "replace")[-300:]
        raise MonteurMediaError(f"could not extract a frame from {path}: {stderr}")
    return np.frombuffer(result.stdout[:frame_bytes], dtype=np.uint8).reshape(
        height, width, 3
    )


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
    path: str | Path, rate: int = 22050, window: float = 0.5, runner=None, cancel=None
) -> list[AudioMetric]:
    """Per-``window``-second audio quality metrics for a file.

    Files without an audio stream return ``[]`` (probe guard, so the audio
    decode is never attempted on video-only material). Only full windows are
    analysed; a sub-window tail is dropped.
    """
    np = _numpy()
    if not probe(path, runner, cancel).has_audio:
        return []
    samples = read_audio(path, rate=rate, runner=runner, cancel=cancel)
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
