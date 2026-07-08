"""Music analysis: tempo, beats and energy sections.

Fable cuts montages to music, so it needs to know where the beats fall and
where the song changes gear. Pure-numpy DSP on the decoded waveform — no ML,
tuned for music with a clear pulse (the montage use case).

How it works
------------
* Beats: an onset envelope is built from spectral flux (STFT with ~46 ms
  windows, ~11.6 ms hop; half-wave-rectified positive difference of
  log-magnitude spectra summed over frequency). Tempo comes from the
  autocorrelation of that envelope over lags corresponding to 60..200 BPM,
  with a mild preference for the 90..150 BPM octave. The beat grid is the
  phase that maximises onset energy at grid points, then beats are tracked
  sequentially, snapping each predicted beat to the local onset peak within
  ±15% of the period so the grid stays locked to real hits under tempo drift.
* Sections: RMS energy in ~0.5 s windows, smoothed with a ~4 s moving
  average and normalised to the track's 95th percentile, then thresholded
  into "low" / "mid" / "high" stretches that tile the whole duration.

Caveat: this works best on percussive/pulsed music — anything with clear
transients (drums, clicks, plucked instruments). Ambient pads, drones or
beatless textures produce no usable onsets, so they won't yield a reliable
tempo or beat grid (expect a 0 BPM estimate or a jittery grid); the energy
sections remain meaningful for such material.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class MusicSection:
    """A stretch of the song with a consistent energy level."""

    start: float  # seconds
    end: float
    energy: float  # 0..1 relative to the song's own loudest part
    label: str  # "low" | "mid" | "high"


@dataclass
class MusicAnalysis:
    path: str
    duration: float  # seconds
    tempo: float  # BPM estimate
    beats: list[float] = field(default_factory=list)  # beat times, seconds
    sections: list[MusicSection] = field(default_factory=list)


# ----------------------------------------------------------------------------
# Beat detection
# ----------------------------------------------------------------------------

_MIN_BPM = 60.0
_MAX_BPM = 200.0
_PREF_LOW = 90.0  # preferred tempo octave: 90..150 BPM
_PREF_HIGH = 150.0
_SNAP_FRACTION = 0.15  # snap window around each predicted beat, ±15% of period


def _stft_params(rate: int) -> tuple[int, int]:
    """Window (~46 ms, power of two) and hop (window/4, ~11.6 ms)."""
    target = 0.046 * rate
    n_fft = int(2 ** round(np.log2(max(target, 64.0))))
    return n_fft, max(n_fft // 4, 1)


def _onset_envelope(samples: np.ndarray, rate: int) -> tuple[np.ndarray, float, float]:
    """Spectral-flux onset envelope.

    Returns (envelope, frame_period_seconds, first_frame_time_seconds).
    envelope[i] is the onset strength at time first_frame_time + i * period.
    """
    n_fft, hop = _stft_params(rate)
    x = np.asarray(samples, dtype=np.float32)
    if x.size < 2 * n_fft:
        return np.zeros(0, dtype=np.float64), hop / rate, 0.0

    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop]
    window = np.hanning(n_fft).astype(np.float32)
    mags = np.abs(np.fft.rfft(frames * window, axis=1))
    log_mags = np.log1p(1000.0 * mags)

    # Half-wave-rectified positive difference, summed over frequency.
    flux = np.maximum(log_mags[1:] - log_mags[:-1], 0.0).sum(axis=1)

    # Light smoothing (~3 frames) and max-normalisation.
    kernel = np.hanning(5)[1:-1]
    kernel /= kernel.sum()
    flux = np.convolve(flux, kernel, mode="same")
    peak = flux.max()
    if peak > 0:
        flux = flux / peak

    # flux[i] compares frames i and i+1; stamp it at frame i+1's centre.
    first_time = (hop + n_fft / 2) / rate
    return flux.astype(np.float64), hop / rate, first_time


def _autocorrelate(env: np.ndarray, max_lag: int) -> np.ndarray:
    """Mean-removed autocorrelation, normalised per-lag by overlap length."""
    centred = env - env.mean()
    acf = np.zeros(max_lag + 1)
    n = centred.size
    for lag in range(1, max_lag + 1):
        if lag >= n:
            break
        overlap = n - lag
        acf[lag] = float(np.dot(centred[:-lag], centred[lag:])) / overlap
    return acf


def _pick_tempo_lag(env: np.ndarray, frame_period: float) -> float:
    """Beat period in frames from the onset-envelope autocorrelation.

    Searches 60..200 BPM; if the strongest peak sits outside 90..150 BPM but
    its double or half tempo lands inside that octave with comparable
    autocorrelation support, prefer the in-octave tempo. Returns 0.0 when no
    periodicity is found.
    """
    lag_min = max(int(np.floor(60.0 / _MAX_BPM / frame_period)), 1)
    lag_max = int(np.ceil(60.0 / _MIN_BPM / frame_period))
    if env.size < 2 * lag_min + 2 or lag_max <= lag_min:
        return 0.0

    # Compute out to 2*lag_max so the half-tempo octave candidate is scored.
    acf = _autocorrelate(env, min(2 * lag_max, env.size - 1))
    if acf.size <= lag_min:
        return 0.0

    hi = min(lag_max, acf.size - 1)
    search = acf[lag_min : hi + 1]
    if search.size == 0 or search.max() <= 0:
        return 0.0
    best_lag = lag_min + int(np.argmax(search))
    best_val = acf[best_lag]

    def refined(lag: int) -> float:
        """Parabolic interpolation around an integer ACF peak."""
        if 1 <= lag < acf.size - 1:
            a, b, c = acf[lag - 1], acf[lag], acf[lag + 1]
            denom = a - 2 * b + c
            if denom < 0:
                shift = 0.5 * (a - c) / denom
                if abs(shift) <= 1:
                    return lag + shift
        return float(lag)

    def bpm_of(lag: float) -> float:
        return 60.0 / (lag * frame_period)

    chosen = float(best_lag)
    if not (_PREF_LOW <= bpm_of(chosen) <= _PREF_HIGH):
        # Mild octave preference: try double and half tempo.
        for factor in (0.5, 2.0):
            cand = int(round(best_lag * factor))
            if cand < 1 or cand >= acf.size:
                continue
            # Allow the candidate peak to sit a frame off the exact octave.
            lo = max(cand - 1, 1)
            local = lo + int(np.argmax(acf[lo : cand + 2]))
            if (
                _PREF_LOW <= bpm_of(float(local)) <= _PREF_HIGH
                and acf[local] >= 0.4 * best_val
            ):
                chosen = float(local)
                break

    return refined(int(round(chosen)))


def _track_beats(
    env: np.ndarray, period: float, frame_period: float, first_time: float
) -> list[float]:
    """Phase-lock a beat grid to the onset envelope, then track sequentially."""
    n = env.size
    if n == 0 or period <= 0:
        return []

    # Best phase: offset in [0, period) maximising summed onset energy.
    best_phase, best_score = 0.0, -1.0
    for phase in np.arange(0.0, period, 1.0):
        idx = np.arange(phase, n, period).astype(int)
        score = float(env[idx].sum())
        if score > best_score:
            best_score, best_phase = score, float(phase)

    half_window = max(int(round(_SNAP_FRACTION * period)), 1)

    def snap(predicted: float) -> int:
        centre = int(round(predicted))
        lo = max(centre - half_window, 0)
        hi = min(centre + half_window + 1, n)
        if lo >= hi:
            return min(max(centre, 0), n - 1)
        return lo + int(np.argmax(env[lo:hi]))

    beats_frames: list[int] = []
    current = float(snap(best_phase))
    while current < n:
        beats_frames.append(int(current))
        predicted = current + period
        if predicted >= n:
            break
        current = float(snap(predicted))
        if current <= beats_frames[-1]:  # never move backwards
            current = beats_frames[-1] + period

    return [first_time + f * frame_period for f in beats_frames]


def detect_beats(samples, rate: int) -> tuple[float, list[float]]:
    """Estimate (tempo_bpm, beat_times) from a mono float32 waveform."""
    x = np.asarray(samples, dtype=np.float32)
    if x.size < 2 * rate:  # under 2 s: not enough evidence for a tempo
        return 0.0, []
    if not np.any(np.abs(x) > 1e-6):  # silence
        return 0.0, []

    env, frame_period, first_time = _onset_envelope(x, rate)
    if env.size == 0 or env.max() <= 0:
        return 0.0, []

    lag = _pick_tempo_lag(env, frame_period)
    if lag <= 0:
        return 0.0, []
    tempo = 60.0 / (lag * frame_period)
    beats = _track_beats(env, lag, frame_period, first_time)
    return float(tempo), beats


# ----------------------------------------------------------------------------
# Section detection
# ----------------------------------------------------------------------------

_SECTION_WINDOW_S = 0.5
_SECTION_SMOOTH_S = 4.0
_SECTION_MIN_LEN_S = 4.0
_LOW_THRESHOLD = 0.35
_HIGH_THRESHOLD = 0.7


def _label_for(energy: float) -> str:
    if energy < _LOW_THRESHOLD:
        return "low"
    if energy < _HIGH_THRESHOLD:
        return "mid"
    return "high"


def _merge_same_label(sections: list[MusicSection]) -> list[MusicSection]:
    merged: list[MusicSection] = []
    for sec in sections:
        if merged and merged[-1].label == sec.label:
            prev = merged[-1]
            total = (prev.end - prev.start) + (sec.end - sec.start)
            if total > 0:
                prev.energy = (
                    prev.energy * (prev.end - prev.start)
                    + sec.energy * (sec.end - sec.start)
                ) / total
            prev.end = sec.end
        else:
            merged.append(sec)
    return merged


def _absorb_short_sections(sections: list[MusicSection]) -> list[MusicSection]:
    """Merge sections shorter than the minimum length into a neighbour."""
    sections = list(sections)
    while len(sections) > 1:
        durations = [s.end - s.start for s in sections]
        shortest = int(np.argmin(durations))
        if durations[shortest] >= _SECTION_MIN_LEN_S:
            break
        sec = sections[shortest]
        # Prefer the neighbour whose energy is closest to ours.
        candidates = []
        if shortest > 0:
            candidates.append(shortest - 1)
        if shortest < len(sections) - 1:
            candidates.append(shortest + 1)
        target = min(candidates, key=lambda i: abs(sections[i].energy - sec.energy))
        neighbour = sections[target]
        total = (neighbour.end - neighbour.start) + (sec.end - sec.start)
        if total > 0:
            neighbour.energy = (
                neighbour.energy * (neighbour.end - neighbour.start)
                + sec.energy * (sec.end - sec.start)
            ) / total
        neighbour.start = min(neighbour.start, sec.start)
        neighbour.end = max(neighbour.end, sec.end)
        del sections[shortest]
        sections = _merge_same_label(sections)
    return sections


def detect_sections(samples, rate: int) -> list[MusicSection]:
    """Split the waveform into low/mid/high energy sections."""
    x = np.asarray(samples, dtype=np.float32)
    duration = x.size / rate
    if x.size == 0:
        return []

    win = max(int(_SECTION_WINDOW_S * rate), 1)
    n_windows = int(np.ceil(x.size / win))
    padded = np.zeros(n_windows * win, dtype=np.float64)
    padded[: x.size] = x.astype(np.float64)
    rms = np.sqrt((padded.reshape(n_windows, win) ** 2).mean(axis=1))

    # ~4 s moving average (edge-corrected so borders aren't dragged down).
    smooth_n = max(int(round(_SECTION_SMOOTH_S / _SECTION_WINDOW_S)), 1)
    kernel = np.ones(smooth_n)
    counts = np.convolve(np.ones(n_windows), kernel, mode="same")
    smoothed = np.convolve(rms, kernel, mode="same") / counts

    # Normalise to the track's 95th percentile.
    p95 = float(np.percentile(smoothed, 95))
    if p95 > 1e-9:
        energy = np.clip(smoothed / p95, 0.0, 1.0)
    else:
        energy = np.zeros(n_windows)

    # One section per window, then merge.
    sections = []
    for i in range(n_windows):
        start = i * _SECTION_WINDOW_S
        end = min((i + 1) * _SECTION_WINDOW_S, duration)
        e = float(energy[i])
        sections.append(MusicSection(start=start, end=end, energy=e, label=_label_for(e)))

    sections = _merge_same_label(sections)
    sections = _absorb_short_sections(sections)

    # Guarantee exact tiling of [0, duration].
    sections[0].start = 0.0
    sections[-1].end = duration
    for prev, nxt in zip(sections, sections[1:]):
        nxt.start = prev.end
    return sections


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def analyze_music(path: str, rate: int = 22050) -> MusicAnalysis:
    """Decode a song and return tempo, beat grid and energy sections."""
    from fable.media import FableMediaError, read_audio

    try:
        samples = read_audio(path, rate)
    except FableMediaError as exc:
        raise FableMediaError(f"could not analyze music in {path}: {exc}") from exc

    tempo, beats = detect_beats(samples, rate)
    sections = detect_sections(samples, rate)
    return MusicAnalysis(
        path=str(path),
        duration=len(samples) / rate,
        tempo=tempo,
        beats=beats,
        sections=sections,
    )
