"""Audio waveform envelopes for the timeline — offline, no API.

The timeline's music lane used to draw only the song's SECTION energy: two
samples a second, which is a shape, not a waveform. At that resolution a beat
is invisible — the lane reads as one block. An editor lines cuts up against
what they can SEE, so the lane needs the real thing: an amplitude envelope
dense enough that the beats stand out as peaks.

The envelope is the per-bucket maximum absolute sample — the standard NLE
waveform look (symmetric around the centre line). It is computed ONCE per file
at :data:`ENVELOPE_RATE` buckets per second and cached in memory; any window and
any zoom level is then resampled from that, so scrubbing the zoom never
re-decodes a three-minute song.

Deliberately cheap: the decode runs at :data:`DECODE_RATE` (4 kHz mono), which
is far below music fidelity but exactly right for an amplitude envelope — we
need how LOUD it is, not which note it is.
"""

from __future__ import annotations

import os
from pathlib import Path

from monteur.media import MonteurMediaError, read_audio

__all__ = ["ENVELOPE_RATE", "DECODE_RATE", "peaks", "clear_cache"]

#: Buckets per second in the cached per-file envelope. 200/s resolves a beat
#: at any sane tempo (a 200 bpm beat is 60 buckets wide) with a small footprint
#: — a 5-minute song is 60k floats.
ENVELOPE_RATE = 200

#: Decode sample rate. An envelope only needs amplitude, so this is 5x below
#: speech-grade on purpose: it makes the ffmpeg pass cheap.
DECODE_RATE = 4000

#: Hard cap on what a caller may ask for, so a silly ``buckets`` cannot make
#: the server build a giant list.
MAX_BUCKETS = 4000

# path -> (mtime, size, envelope). Keyed on mtime+size so a re-rendered song
# is picked up without a restart.
_CACHE: dict[str, tuple[float, int, list[float]]] = {}


def clear_cache() -> None:
    """Forget every cached envelope (tests, and the settings cache-clear)."""
    _CACHE.clear()


def _file_envelope(path: str) -> list[float]:
    """The whole file's envelope at :data:`ENVELOPE_RATE`, cached per file.

    Raises :class:`monteur.media.MonteurMediaError` when the audio cannot be
    decoded (no ffmpeg, no numpy, unreadable file).
    """
    real = os.path.abspath(path)
    try:
        stat = os.stat(real)
    except OSError as exc:
        raise MonteurMediaError(f"could not read {path}: {exc}") from exc
    cached = _CACHE.get(real)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    samples = read_audio(real, rate=DECODE_RATE)
    per_bucket = max(1, DECODE_RATE // ENVELOPE_RATE)
    total = len(samples) // per_bucket
    envelope: list[float] = []
    if total:
        # reshape + max along the row is one vectorised pass; the tail (a
        # partial bucket) is dropped, which is at most 1/200 s of the song.
        import numpy as np

        block = np.asarray(samples[: total * per_bucket]).reshape(total, per_bucket)
        envelope = [float(v) for v in np.abs(block).max(axis=1)]
    _CACHE[real] = (stat.st_mtime, stat.st_size, envelope)
    return envelope


def peaks(
    path: str | Path,
    *,
    start: float = 0.0,
    duration: float = 0.0,
    buckets: int = 1200,
) -> list[float]:
    """Amplitude envelope of ``path`` over a window, as ``buckets`` values 0..1.

    ``start``/``duration`` select the window in SONG seconds (the montage's
    ``music_start`` and its length); ``duration`` <= 0 means "to the end".
    Values are normalised to the loudest peak IN THE WINDOW, so the lane always
    fills its height — a quiet song is still readable. Silence returns zeros
    rather than dividing by zero.

    Never raises for an empty result: a window past the end of the song simply
    yields zeros, so a plan whose music was replaced still draws a lane.
    """
    buckets = max(1, min(int(buckets), MAX_BUCKETS))
    envelope = _file_envelope(str(path))
    if not envelope:
        return [0.0] * buckets

    lo = max(0, int(round(max(0.0, start) * ENVELOPE_RATE)))
    hi = len(envelope) if duration <= 0 else lo + int(round(duration * ENVELOPE_RATE))
    hi = max(lo, min(hi, len(envelope)))
    window = envelope[lo:hi]
    if not window:
        return [0.0] * buckets

    # resample to exactly `buckets` by taking each output bucket's own max —
    # a peak must never be averaged away, or the beats flatten out again.
    out: list[float] = []
    n = len(window)
    for i in range(buckets):
        a = (i * n) // buckets
        b = max(a + 1, ((i + 1) * n) // buckets)
        out.append(max(window[a:b]))
    top = max(out)
    if top <= 0.0:
        return [0.0] * buckets
    return [round(v / top, 4) for v in out]
