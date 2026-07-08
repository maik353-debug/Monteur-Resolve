"""Music analysis: tempo, beats and energy sections.

Monteur cuts montages to music, so it needs to know where the beats fall and
where the song changes gear. Pure-numpy DSP on the decoded waveform — no ML,
tuned for music with a clear pulse (the montage use case).

How it works
------------
* Beats: an onset envelope is built from spectral flux (STFT with ~46 ms
  windows, ~11.6 ms hop; half-wave-rectified positive difference of
  log-magnitude spectra, summed over frequency with the low band — the
  kick/bass register below ~200 Hz — weighted up, because the PULSE lives
  there while snares/hats/vocals carry the syncopation). Tempo comes from
  the autocorrelation of that envelope over lags corresponding to 60..200
  BPM with HARMONIC scoring — a lag's score includes its double and half
  lag, so a track whose bar structure autocorrelates stronger than its
  beat no longer halves the tempo — and a mild preference for the 90..150
  BPM octave. Beats are then tracked by dynamic programming (Ellis 2007):
  each frame's score is its onset strength plus the best predecessor score
  minus a log-squared penalty for deviating from the beat period, and the
  best final beat is backtracked. Unlike greedy peak-snapping, the DP
  path is globally optimal: one loud off-beat (a syncopated snare) cannot
  pull the grid off the pulse, weak beats are carried through by the
  regularity term, and gradual tempo drift is followed. Each beat is then
  refined to sub-frame precision by parabolic interpolation of the onset
  envelope (~3 ms instead of the 11.6 ms frame grid), and the reported
  tempo is re-estimated from the median inter-beat interval.
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

_EPS = 1e-6


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
# The pulse lives in the kick/bass register: a SEPARATE low-band onset
# envelope (bins below this frequency, normalised to its own peak) is added
# to the full-band flux. A kick concentrates its whole energy in a handful
# of low bins, so it dominates that envelope; a broadband snare/hat/vocal
# spreads thin there. This locks the beat phase to the kick even when the
# off-beat hits are louder overall.
_LOW_FLUX_HZ = 150.0
_LOW_FLUX_WEIGHT = 1.0
# Harmonic tempo scoring: score(lag) credits the 2x and 3x lags — a true
# beat period is reinforced by its bar structure ABOVE it. Crediting the
# half lag would be wrong: that would let bar-level lags borrow the beat's
# own strength and halve the tempo.
_HARMONIC_WEIGHTS = ((2, 0.5), (3, 0.33))
# Log-Gaussian tempo prior (Ellis): candidates are weighted by how far (in
# octaves) their BPM sits from the centre. Resolves octave ambiguity toward
# danceable tempi without hard cutoffs.
_TEMPO_PRIOR_BPM = 120.0
_TEMPO_PRIOR_OCTAVES = 1.0
# DP beat tracking (Ellis 2007): transition penalty weight. Higher = stiffer
# grid (trusts the tempo), lower = looser (follows onsets). The penalty is
# tightness * log2(interval/period)^2 against an envelope normalised to unit
# standard deviation.
_DP_TIGHTNESS = 3.0


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

    # Half-wave-rectified positive difference. Full-band LOG flux carries
    # the timing precision; the low-band flux is computed on LINEAR
    # magnitudes — log compression would flatten the difference between a
    # kick (its whole energy in a few low bins) and broadband noise leaking
    # into the band — and self-normalised, then added so kicks outvote loud
    # off-beat snares/hats when the beat phase is decided.
    diff = np.maximum(log_mags[1:] - log_mags[:-1], 0.0)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / rate)
    full = diff.sum(axis=1)
    low_lin = mags[:, freqs <= _LOW_FLUX_HZ]
    low = np.maximum(low_lin[1:] - low_lin[:-1], 0.0).sum(axis=1)
    full_peak, low_peak = full.max(), low.max()
    flux = (full / full_peak) if full_peak > 0 else full
    if low_peak > 0:
        flux = flux + _LOW_FLUX_WEIGHT * (low / low_peak)

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

    Searches 60..200 BPM with HARMONIC scoring: a candidate lag is scored by
    its own autocorrelation plus _HARMONIC_WEIGHT x the autocorrelation at
    its double and half lag (a true beat period is supported by its bar and
    half-beat structure; a spurious bar-level peak is not supported below).
    Candidates inside the 90..150 BPM octave get a mild _OCTAVE_BONUS.
    Returns 0.0 when no periodicity is found.
    """
    lag_min = max(int(np.floor(60.0 / _MAX_BPM / frame_period)), 1)
    lag_max = int(np.ceil(60.0 / _MIN_BPM / frame_period))
    if env.size < 2 * lag_min + 2 or lag_max <= lag_min:
        return 0.0

    # Compute out to 2*lag_max so every candidate's double lag is scored.
    acf = _autocorrelate(env, min(2 * lag_max, env.size - 1))
    if acf.size <= lag_min:
        return 0.0

    hi = min(lag_max, acf.size - 1)
    base = np.maximum(acf, 0.0)

    def harmonic_score(lag: int) -> float:
        score = float(base[lag])
        for mult, weight in _HARMONIC_WEIGHTS:
            m = mult * lag
            if m < base.size:
                # The multiple may sit a frame off exact; take the local best.
                lo = max(m - 1, 1)
                score += weight * float(base[lo : min(m + 2, base.size)].max())
        bpm = 60.0 / (lag * frame_period)
        octaves_off = np.log2(bpm / _TEMPO_PRIOR_BPM)
        prior = float(np.exp(-0.5 * (octaves_off / _TEMPO_PRIOR_OCTAVES) ** 2))
        return score * prior

    scores = [harmonic_score(lag) for lag in range(lag_min, hi + 1)]
    if not scores or max(scores) <= 0:
        return 0.0
    best_lag = lag_min + int(np.argmax(scores))

    # Parabolic interpolation around the integer ACF peak.
    if 1 <= best_lag < acf.size - 1:
        a, b, c = acf[best_lag - 1], acf[best_lag], acf[best_lag + 1]
        denom = a - 2 * b + c
        if denom < 0:
            shift = 0.5 * (a - c) / denom
            if abs(shift) <= 1:
                return best_lag + shift
    return float(best_lag)


def _track_beats(
    env: np.ndarray, period: float, frame_period: float, first_time: float
) -> list[float]:
    """Track beats by dynamic programming (Ellis 2007).

    Every frame t gets the score ``env[t] + max over predecessors tau of
    (score[tau] - tightness * log2((t - tau) / period)^2)`` with tau in
    [t - 2*period, t - period/2]; the beat sequence is backtracked from the
    best late frame. The result is the globally optimal trade-off between
    landing on onsets and keeping a steady pulse — one loud syncopated hit
    cannot pull the grid off the beat, silent beats are carried through,
    and gradual tempo drift is followed. Beats are refined to sub-frame
    precision by parabolic interpolation of the envelope.
    """
    n = env.size
    if n == 0 or period <= 0:
        return []

    # Normalise so the tightness constant is independent of track loudness.
    std = float(env.std())
    e = env / std if std > 0 else env

    lo = max(int(round(period / 2)), 1)
    hi = min(int(round(period * 2)), n - 1)
    if hi < lo:
        return []

    score = e.copy()
    backlink = np.full(n, -1, dtype=np.int64)
    offsets = np.arange(lo, hi + 1)
    # log-squared deviation of each candidate interval from the period
    penalty = _DP_TIGHTNESS * np.log2(offsets / period) ** 2
    for t in range(lo, n):
        prev = t - offsets
        valid = prev >= 0
        if not np.any(valid):
            continue
        cand = score[prev[valid]] - penalty[valid]
        best = int(np.argmax(cand))
        best_val = float(cand[best])
        if best_val > 0:  # starting fresh at t (score e[t]) beats a bad chain
            score[t] += best_val
            backlink[t] = int(prev[valid][best])

    # Backtrack from the best-scoring frame in the final period's window.
    tail_start = max(n - int(round(period)) - 1, 0)
    t = tail_start + int(np.argmax(score[tail_start:]))
    beats_frames: list[int] = []
    while t >= 0:
        beats_frames.append(t)
        t = int(backlink[t])
    beats_frames.reverse()
    if len(beats_frames) < 2:
        return []

    def refine(frame: int) -> float:
        """Sub-frame peak position via parabolic interpolation."""
        if 1 <= frame < n - 1:
            a, b, c = env[frame - 1], env[frame], env[frame + 1]
            denom = a - 2 * b + c
            if denom < 0:
                shift = 0.5 * (a - c) / denom
                if abs(shift) <= 1:
                    return frame + float(shift)
        return float(frame)

    return [first_time + refine(f) * frame_period for f in beats_frames]


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
    beats = _track_beats(env, lag, frame_period, first_time)

    # Half-tempo trap: alternating accents (hat/snare every OTHER beat) make
    # the 2-beat lag autocorrelate stronger than the beat itself. Detect it
    # from the tracked grid: when the onsets BETWEEN tracked beats are about
    # as strong as the beats themselves, the real pulse is twice as fast —
    # re-track at half the period.
    if len(beats) >= 8 and 60.0 / (lag / 2 * frame_period) <= _MAX_BPM * 1.1:
        def strength(times: list[float]) -> float:
            frames = np.clip(
                np.round((np.asarray(times) - first_time) / frame_period).astype(int),
                1, env.size - 2,
            )
            # max over ±1 frame: tolerate half-frame placement error
            return float(np.median(np.maximum.reduce(
                [env[frames - 1], env[frames], env[frames + 1]]
            )))

        mids = [(a + b) / 2 for a, b in zip(beats, beats[1:])]
        if strength(mids) >= 0.5 * strength(beats):
            doubled = _track_beats(env, lag / 2, frame_period, first_time)
            if len(doubled) > len(beats):
                beats = doubled

    if len(beats) >= 4:
        # The tracked grid is the better tempo witness than the ACF lag:
        # median inter-beat interval, robust against edge irregularities.
        intervals = np.diff(beats)
        tempo = 60.0 / float(np.median(intervals))
    else:
        tempo = 60.0 / (lag * frame_period)
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
# Energy windowing
# ----------------------------------------------------------------------------

_WINDOW_DROP_LEAD = 0.15  # lead-in before a drop, as a fraction of the window


def best_energy_window(music: MusicAnalysis, length: float) -> float:
    """Start offset (seconds) of the most energetic ``length``-second window.

    Used when a montage is cut SHORTER than the song, to place the cut against
    the song's strongest passage instead of its intro.

    Approximation
    -------------
    The fine RMS energy envelope built during analysis is not retained on
    :class:`MusicAnalysis`, so this works from the coarse ``music.sections``
    (each a stretch carrying a single 0..1 energy) rather than recomputing it:

    * When ``music.drops`` is non-empty the window is placed to CONTAIN the
      first drop with a short lead-in (``drop - 15% of length``) so the build
      into the drop rides along — a drop is the strongest moment a section
      summary can point at. The start is clamped to ``[0, duration - length]``,
      which always keeps the drop inside the window.
    * Otherwise the window whose section-energy-weighted average over
      ``[start, start + length]`` is highest is chosen. That average is
      piecewise-linear in ``start`` with breakpoints at section boundaries, so
      only boundary-derived candidate starts need scoring; ties resolve to the
      earliest start (deterministic).

    ``length >= music.duration`` (or a non-positive length) returns 0.0 — the
    whole song is used, exactly as before windowing existed.
    """
    duration = music.duration
    if length <= 0.0 or length >= duration - _EPS:
        return 0.0
    max_start = duration - length

    if music.drops:
        drop = min(music.drops)
        start = drop - _WINDOW_DROP_LEAD * length
        return float(min(max(start, 0.0), max_start))

    sections = music.sections
    if not sections:
        return 0.0

    # The optimum aligns a window edge with a section boundary.
    candidates = {0.0, max_start}
    for s in sections:
        candidates.add(s.start)
        candidates.add(s.end - length)
    starts = sorted(
        min(max(c, 0.0), max_start)
        for c in candidates
        if -_EPS <= c <= max_start + _EPS
    )

    def window_energy(start: float) -> float:
        end = start + length
        total = 0.0
        for s in sections:
            lo = max(start, s.start)
            hi = min(end, s.end)
            if hi > lo:
                total += s.energy * (hi - lo)
        return total / length

    best_start, best_energy = 0.0, -1.0
    for start in starts:
        e = window_energy(start)
        if e > best_energy + _EPS:  # strict: ties keep the earlier start
            best_energy, best_start = e, start
    return float(best_start)


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
