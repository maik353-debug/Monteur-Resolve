"""Music analysis: tempo, beats and energy sections.

Monteur cuts montages to music, so it needs to know where the beats fall and
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
* Downbeats: 4/4 is assumed. Beats are grouped into bars by trying the four
  possible phase offsets and keeping the one whose beats carry the most
  low-frequency (< ~150 Hz) onset energy — kick/bass emphasis on "the one".
* Phrases: pop-structure heuristic — phrases are 8 bars when the track has
  at least 16 bars, otherwise 4 bars, always aligned to the first downbeat.
* Drops: a strong jump of the smoothed RMS envelope into a sustained loud
  stretch preceded by a quieter build, optionally snapped to the nearest
  downbeat.

Caveats
-------
* This works best on percussive/pulsed music — anything with clear
  transients (drums, clicks, plucked instruments). Ambient pads, drones or
  beatless textures produce no usable onsets, so they won't yield a reliable
  tempo or beat grid (expect a 0 BPM estimate or a jittery grid); the energy
  sections remain meaningful for such material.
* Downbeats assume 4/4 time. Waltzes (3/4), shuffles counted in 6/8 and
  odd meters will get a wrong-but-regular bar grid.
* Phrases are a pure 4/8-bar counting heuristic anchored at the first
  downbeat; music with pickup bars, truncated phrases or non-square forms
  will drift from the real phrase boundaries.
* Drops need a real dynamic arc (quiet build -> sustained loud payoff). A
  track that is constantly loud — or constantly quiet — has no drops by
  this definition, and none are reported.
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
    downbeats: list[float] = field(default_factory=list)  # bar starts ("the one")
    phrases: list[float] = field(default_factory=list)  # phrase starts (4/8 bars)
    drops: list[float] = field(default_factory=list)  # drop/chorus impact points


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


def _smoothed_energy(x: np.ndarray, rate: int) -> np.ndarray:
    """Normalised smoothed RMS envelope, one value per ~0.5 s window.

    RMS in ~0.5 s windows, ~4 s moving average (edge-corrected so borders
    aren't dragged down), normalised to the track's 95th percentile and
    clipped to 0..1. energy[i] describes window [i*0.5s, (i+1)*0.5s).
    """
    win = max(int(_SECTION_WINDOW_S * rate), 1)
    n_windows = int(np.ceil(x.size / win))
    padded = np.zeros(n_windows * win, dtype=np.float64)
    padded[: x.size] = x.astype(np.float64)
    rms = np.sqrt((padded.reshape(n_windows, win) ** 2).mean(axis=1))

    smooth_n = max(int(round(_SECTION_SMOOTH_S / _SECTION_WINDOW_S)), 1)
    kernel = np.ones(smooth_n)
    counts = np.convolve(np.ones(n_windows), kernel, mode="same")
    smoothed = np.convolve(rms, kernel, mode="same") / counts

    p95 = float(np.percentile(smoothed, 95))
    if p95 > 1e-9:
        return np.clip(smoothed / p95, 0.0, 1.0)
    return np.zeros(n_windows)


def detect_sections(samples, rate: int) -> list[MusicSection]:
    """Split the waveform into low/mid/high energy sections."""
    x = np.asarray(samples, dtype=np.float32)
    duration = x.size / rate
    if x.size == 0:
        return []

    energy = _smoothed_energy(x, rate)
    n_windows = energy.size

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
# Downbeats, phrases and drops
# ----------------------------------------------------------------------------

_BEATS_PER_BAR = 4  # 4/4 assumed throughout
_LOW_BAND_HZ = 150.0  # "kick/bass" band used to find "the one"
_DOWNBEAT_WIN_S = 0.12  # window around each beat for low-band energy
_MIN_BEATS_FOR_BARS = 8  # under two bars of beats there is no phase evidence

_PHRASE_BARS_LONG = 8  # preferred phrase length when the track is long enough
_PHRASE_BARS_SHORT = 4
_PHRASE_LONG_MIN_BARS = 16  # need >= 16 downbeats to trust 8-bar phrases

_DROP_RISE = 0.3  # required energy rise (normalised units)
_DROP_RISE_WINDOW_S = 2.0  # ...within at most this long
_DROP_SUSTAIN_S = 4.0  # the payoff must stay loud this long
_DROP_SUSTAIN_LEVEL = 0.65
_DROP_BUILD_S = 4.0  # the preceding stretch must be quieter on average
_DROP_BUILD_MAX = 0.55
_DROP_MERGE_S = 8.0  # candidates closer than this are one drop


def detect_downbeats(samples, rate: int, beats: list[float]) -> list[float]:
    """Pick the bar starts ("the one") from a beat grid. Assumes 4/4.

    Beats are grouped into bars of four by testing the four possible phase
    offsets; the winner is the offset whose beats carry the highest mean
    low-frequency onset energy (RMS of the < ~150 Hz band just after each
    beat, minus the RMS just before it, half-wave rectified) — kick and bass
    tend to land hardest on the downbeat. Returns every 4th beat starting at
    the best offset. Fewer than 8 beats (two bars) -> [].
    """
    if len(beats) < _MIN_BEATS_FOR_BARS:
        return []
    x = np.asarray(samples, dtype=np.float32)
    if x.size == 0:
        return []

    # Low-pass below _LOW_BAND_HZ via FFT masking (fine for song-length audio).
    spectrum = np.fft.rfft(x.astype(np.float64))
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    spectrum[freqs > _LOW_BAND_HZ] = 0.0
    low = np.fft.irfft(spectrum, n=x.size)

    win = max(int(_DOWNBEAT_WIN_S * rate), 1)

    def rms(lo: int, hi: int) -> float:
        seg = low[max(lo, 0) : max(hi, 0)]
        return float(np.sqrt(np.mean(seg**2))) if seg.size else 0.0

    scores = np.empty(len(beats))
    for i, t in enumerate(beats):
        centre = int(round(t * rate))
        after = rms(centre, centre + win)
        before = rms(centre - win, centre)
        scores[i] = max(after - before, 0.0)

    best = max(
        range(_BEATS_PER_BAR),
        key=lambda o: float(scores[o::_BEATS_PER_BAR].mean()),
    )
    return [float(t) for t in beats[best::_BEATS_PER_BAR]]


def detect_phrases(downbeats: list[float]) -> list[float]:
    """Phrase-start times: a 4/8-bar counting heuristic over the downbeats.

    Pop phrases are 4 or 8 bars; 8-bar phrases are preferred when there are
    at least 16 downbeats, else 4-bar. Phrases are anchored so the first
    phrase starts at the first downbeat; the result is a subset of
    ``downbeats``. Fewer than 4 downbeats -> [].
    """
    if len(downbeats) < _PHRASE_BARS_SHORT:
        return []
    bars = (
        _PHRASE_BARS_LONG
        if len(downbeats) >= _PHRASE_LONG_MIN_BARS
        else _PHRASE_BARS_SHORT
    )
    return [float(t) for t in downbeats[::bars]]


def detect_drops(
    samples,
    rate: int,
    sections: list[MusicSection] | None = None,
    downbeats: list[float] | None = None,
    beats: list[float] | None = None,
) -> list[float]:
    """Find drop moments: a sharp energy jump into a sustained loud stretch.

    Uses the same smoothed RMS envelope as :func:`detect_sections` (0.5 s
    windows, ~4 s smoothing, normalised to the 95th percentile). A candidate
    is a window whose energy rose by >= 0.3 within the last 2 s, where the
    following 4 s stay >= 0.65 and the preceding 4 s averaged <= 0.55 (the
    quieter build). Candidates closer than 8 s are merged, keeping the
    strongest rise (ties resolved to the earliest, so a long smeared ramp
    reports its onset). If ``downbeats`` are given, each drop is snapped to
    the nearest downbeat when it lies within one beat period (taken from
    ``beats`` if given, else a quarter of the downbeat spacing).

    ``sections`` is accepted so callers holding a finished analysis can pass
    it along, but it is not needed: drops recompute the fine-grained
    envelope that the merged sections no longer carry. Returns sorted times;
    no qualifying jump -> [].
    """
    del sections  # accepted for API symmetry; see docstring
    x = np.asarray(samples, dtype=np.float32)
    if x.size == 0:
        return []

    energy = _smoothed_energy(x, rate)
    n = energy.size
    rise_k = max(int(round(_DROP_RISE_WINDOW_S / _SECTION_WINDOW_S)), 1)
    sustain_k = max(int(round(_DROP_SUSTAIN_S / _SECTION_WINDOW_S)), 1)
    build_k = max(int(round(_DROP_BUILD_S / _SECTION_WINDOW_S)), 1)

    candidates: list[tuple[float, float]] = []  # (time, rise)
    for i in range(1, n):
        rise = float(energy[i] - energy[max(i - rise_k, 0) : i].min())
        if rise < _DROP_RISE:
            continue
        following = energy[i + 1 : i + 1 + sustain_k]
        # Require at least half the sustain window to still exist (track end).
        if following.size < sustain_k // 2 or float(following.min()) < _DROP_SUSTAIN_LEVEL:
            continue
        preceding = energy[max(i - build_k, 0) : i]
        if preceding.size < build_k // 2 or float(preceding.mean()) > _DROP_BUILD_MAX:
            continue
        candidates.append(((i + 0.5) * _SECTION_WINDOW_S, rise))

    if not candidates:
        return []

    # Merge candidates closer than _DROP_MERGE_S: group, keep strongest rise
    # (earliest of near-equal rises, so smeared ramps report their onset).
    drops: list[float] = []
    group: list[tuple[float, float]] = []

    def flush() -> None:
        strongest = max(r for _, r in group)
        for t, r in group:  # in time order
            if r >= 0.95 * strongest:
                drops.append(t)
                return

    for cand in candidates:
        if group and cand[0] - group[-1][0] >= _DROP_MERGE_S:
            flush()
            group = []
        group.append(cand)
    flush()

    # Snap to the nearest downbeat within +/- one beat.
    if downbeats:
        if beats and len(beats) > 1:
            beat_period = float(np.median(np.diff(beats)))
        elif len(downbeats) > 1:
            beat_period = float(np.median(np.diff(downbeats))) / _BEATS_PER_BAR
        else:
            beat_period = 0.0
        if beat_period > 0:
            grid = np.asarray(downbeats, dtype=np.float64)
            snapped = []
            for t in drops:
                nearest = float(grid[int(np.argmin(np.abs(grid - t)))])
                snapped.append(nearest if abs(nearest - t) <= beat_period else t)
            drops = snapped

    return sorted(drops)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def analyze_music(path: str, rate: int = 22050) -> MusicAnalysis:
    """Decode a song; return tempo, beats, sections, downbeats, phrases, drops."""
    from monteur.media import MonteurMediaError, read_audio

    try:
        samples = read_audio(path, rate)
    except MonteurMediaError as exc:
        raise MonteurMediaError(f"could not analyze music in {path}: {exc}") from exc

    tempo, beats = detect_beats(samples, rate)
    sections = detect_sections(samples, rate)
    downbeats = detect_downbeats(samples, rate, beats)
    phrases = detect_phrases(downbeats)
    drops = detect_drops(
        samples, rate, sections=sections, downbeats=downbeats, beats=beats
    )
    return MusicAnalysis(
        path=str(path),
        duration=len(samples) / rate,
        tempo=tempo,
        beats=beats,
        sections=sections,
        downbeats=downbeats,
        phrases=phrases,
        drops=drops,
    )
