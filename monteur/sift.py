"""Footage sifting: which parts of which clips are worth using?

Monteur scans every clip's frames (via :mod:`monteur.media`) and classifies
stretches as usable or problematic (too dark, blurry, shaky), then ranks the
best moments — so the editor (or the montage builder) starts from the good
material instead of watching everything.

Heuristics (all deliberately simple and documented as approximate):

* DARK — split into two ideas so a flat/Log colour profile is not thrown
  away as "underexposed":

  - genuinely near-BLACK (mean luma below _NEAR_BLACK_BRIGHTNESS, ~18/255):
    no recoverable image — that IS unusable; OR
  - dramatically darker than the clip's *own* median brightness (below
    _DARK_RELATIVE_FRACTION x median): a real shadow within a normally-lit
    clip.

  A merely DIM sample (low absolute luma but not near-black and not far
  below the clip's norm) is NOT dark — it is a flat/Log look and stays
  usable. For a clip detected as flat/low-contrast (small brightness spread
  but real detail; see :func:`_is_flat_log`) the relative check is relaxed
  entirely, because the whole point of such material is to be low-contrast.
* BLURRY — sample sharpness below 25% of the clip's *own* 90th-percentile
  sharpness. The threshold is relative because gradient-variance sharpness
  depends heavily on scene texture: a talking head against a plain wall may
  peak at 40 while a leafy exterior peaks at 400.  A clip is only "blurry"
  where it is much softer than its own best material.
* SHAKY — sample motion above 3x the clip's median motion AND locally
  jittery: the mean absolute motion difference over a 3-sample window
  exceeds half the clip's mean motion. This is a rough proxy for handheld
  shake (large, alternating frame-to-frame differences); steady high motion
  (a pan, a fast subject) does not alternate and stays USABLE.

A single odd sample inside a run of another label is smoothed away, so
one-frame flickers and cut transitions do not fragment segments.

Audio heuristics (also approximate, thresholds are module constants):

* UNRELIABLE — constant-level camera-mic audio (steady wind, engine or hiss)
  carries no editorial signal, yet it defeats the naive checks below (it
  reads as wind, or as clipping in nearly every window). A clip's audio is
  judged unreliable when it is not genuinely silent AND either its rms
  dynamic range is very low (coefficient of variation below
  _AUDIO_LOW_DYNAMIC_CV) OR clipping is present in a LARGE fraction of
  windows (> _AUDIO_UNRELIABLE_CLIP_FRACTION). Unreliable audio suppresses
  the clipping/wind/silent notes, emits one calm note instead, and does not
  drive highlight scoring (every highlight falls back to 0).
* CLIPPING — occasional distortion (a SMALL fraction of windows above
  _AUDIO_CLIP_WINDOW_MIN); the note counts affected windows.
* WIND — median low-band (< 150 Hz) energy share above 0.6 while the clip is
  not silent: wind/handling rumble piles energy at the bottom of the spectrum.
* SILENT — median window rms below 0.01 (≈ -40 dBFS). Genuine silence takes
  precedence over the "constant tone" test, so a truly quiet clip is still
  reported as silent rather than as unreliable.
* HIGHLIGHT — cheers, laughter and action read as loudness bursts: windows
  louder than 1.8x the clip's median rms. A moment's highlight is
  min(1, burst_share_in_window * 3).
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path

from monteur.media import (
    MonteurMediaError,
    AudioMetric,
    FrameMetric,
    audio_metrics,
    frame_metrics,
    list_media,
    probe,
)

USABLE = "usable"
DARK = "dark"
BLURRY = "blurry"
SHAKY = "shaky"

# Tunable heuristic constants (see module docstring).
# Exposure: "dark" splits into near-black (unusable) vs. dim/flat (a Log look,
# usable). A sample is DARK only if near-black OR far below the clip's median.
_NEAR_BLACK_BRIGHTNESS = 18.0  # mean luma below this = genuinely near-black (unusable)
_DARK_RELATIVE_FRACTION = 0.5  # ...or below this fraction of the clip's median brightness
# Flat/Log detection: a low-contrast clip that nevertheless carries real detail
# is intentionally graded — relax the (already unlikely) relative dark check.
_FLAT_CONTRAST_RANGE = 45.0  # brightness p90-p10 below this = low-contrast clip
_FLAT_MIN_DETAIL = 8.0  # ...and clip p90 sharpness at/above this = real detail (Log look)
_FLAT_MAX_MEDIAN_BRIGHTNESS = 110.0  # ...and a dim-ish median (a bright uniform clip is not "Log")
# A clip whose best sharpness is below this floor is featureless (truly
# black/blank): only then may it be reduced to zero moments.
_DETAIL_SHARPNESS_FLOOR = 4.0
_BLURRY_P90_FRACTION = 0.25  # blurry if sharpness < 25% of clip's p90
_SHAKY_MOTION_FACTOR = 3.0  # shaky candidates: motion > 3x clip median
_JITTER_FACTOR = 0.5  # ...and local jitter > 0.5x clip mean motion
_BRIGHT_FULL = 120.0  # brightness at (or above) this = fully adequate
_MODERATE_MOTION_BAND = (0.5, 2.5)  # x clip median: "something happens"
_MOMENT_MOTION_BONUS = 0.25  # weight of the moderate-motion bonus
_MAX_MOMENTS = 12  # cap per clip

# Audio heuristics (all approximate, see module docstring).
_AUDIO_CLIP_WINDOW_MIN = 0.001  # a window "clips" above this clipping fraction
_AUDIO_WIND_LOW_RATIO = 0.6  # median low_ratio above this = rumble-dominated
_AUDIO_SILENCE_RMS = 0.01  # median rms below this = mostly silent (~-40 dBFS)
# Unreliable (constant-level camera-mic: wind/engine/hiss) audio detection:
_AUDIO_UNRELIABLE_CLIP_FRACTION = 0.30  # clipping in more than this share of windows = not real clipping
_AUDIO_LOW_DYNAMIC_CV = 0.08  # rms coefficient of variation below this = constant-level tone
_AUDIO_MIN_RELIABLE_WINDOWS = 4  # need at least this many windows to judge dynamic range
_HIGHLIGHT_BURST_FACTOR = 1.8  # burst: window rms > 1.8x clip median rms
_HIGHLIGHT_GAIN = 3.0  # highlight = min(1, burst_share * gain)
_MOTION_EDGE_SAMPLES = 2  # samples averaged for entry/exit motion stability


@dataclass
class ClipSegment:
    start: float  # seconds
    end: float
    label: str  # USABLE | DARK | BLURRY | SHAKY
    score: float  # 0..1 quality within the clip (usable segments only)


@dataclass
class Moment:
    """A candidate moment for the cut."""

    start: float
    end: float
    score: float  # 0..1
    entry_motion: tuple[float, float] = (0.0, 0.0)  # (dx, dy) at the window start
    exit_motion: tuple[float, float] = (0.0, 0.0)  # (dx, dy) at the window end
    highlight: float = 0.0  # 0..1 audio-highlight strength inside the window


@dataclass
class ClipReport:
    path: str
    duration: float
    segments: list[ClipSegment] = field(default_factory=list)
    moments: list[Moment] = field(default_factory=list)  # best first
    usable_ratio: float = 0.0  # share of the clip classified usable
    notes: list[str] = field(default_factory=list)


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1); coarse but dependency-free."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(q * (len(ordered) - 1)))
    return ordered[index]


def _sharpness_ranks(metrics: list[FrameMetric]) -> list[float]:
    """Per-sample sharpness percentile rank within the clip, 0..1.

    Ties get their average rank, so a clip with uniform sharpness ranks
    everything 0.5 rather than 0 (uniform ≠ bad).
    """
    n = len(metrics)
    if n <= 1:
        return [0.5] * n
    ordered = sorted(m.sharpness for m in metrics)
    ranks = []
    for m in metrics:
        lo = bisect_left(ordered, m.sharpness)
        hi = bisect_right(ordered, m.sharpness)
        ranks.append(((lo + hi - 1) / 2) / (n - 1))
    return ranks


def _brightness_adequacy(brightness: float) -> float:
    """0 at near-black, 1 at normal exposure (~120 mean luma).

    Anchored at _NEAR_BLACK_BRIGHTNESS (not an absolute "dark" cutoff) so that
    dim-but-usable / flat-Log material still scores above zero.
    """
    span = _BRIGHT_FULL - _NEAR_BLACK_BRIGHTNESS
    return min(1.0, max(0.0, (brightness - _NEAR_BLACK_BRIGHTNESS) / span))


def _brightness_spread(metrics: list[FrameMetric]) -> float:
    """Robust contrast proxy: 90th minus 10th percentile of brightness."""
    values = [m.brightness for m in metrics]
    return _percentile(values, 0.9) - _percentile(values, 0.1)


def _clip_detail(metrics: list[FrameMetric]) -> float:
    """The clip's best sharpness (90th percentile) — its detail ceiling."""
    return _percentile([m.sharpness for m in metrics], 0.9)


def _is_flat_log(metrics: list[FrameMetric]) -> bool:
    """True for an intentional flat/Log look: low contrast, dim-ish, but detailed.

    Such material must not be judged against the relative-darkness rule (it is
    low-contrast on purpose); only genuine near-black still counts as dark.
    """
    if len(metrics) < 2:
        return False
    median_brightness = statistics.median(m.brightness for m in metrics)
    return (
        _brightness_spread(metrics) < _FLAT_CONTRAST_RANGE
        and _clip_detail(metrics) >= _FLAT_MIN_DETAIL
        and median_brightness < _FLAT_MAX_MEDIAN_BRIGHTNESS
    )


def _has_visual_detail(metrics: list[FrameMetric]) -> bool:
    """True if the clip has any real detail (best sharpness above the noise floor).

    A clip with detail must never be reduced to zero moments; only a truly
    black / featureless clip may be skipped entirely.
    """
    return bool(metrics) and _clip_detail(metrics) >= _DETAIL_SHARPNESS_FLOOR


def _motion_stats(metrics: list[FrameMetric]) -> tuple[float, float]:
    """(median, mean) motion, skipping sample 0 (always 0 by construction)."""
    motions = [m.motion for m in metrics[1:]]
    if not motions:
        return 0.0, 0.0
    return statistics.median(motions), sum(motions) / len(motions)


def _jitter(metrics: list[FrameMetric], i: int) -> float:
    """Mean |motion delta| over the 3-sample window centred on ``i``."""
    diffs = []
    if i > 0:
        diffs.append(abs(metrics[i].motion - metrics[i - 1].motion))
    if i < len(metrics) - 1:
        diffs.append(abs(metrics[i + 1].motion - metrics[i].motion))
    return sum(diffs) / len(diffs) if diffs else 0.0


def classify_metrics(metrics: list[FrameMetric], duration: float) -> list[ClipSegment]:
    """Label stretches of a clip from its frame metrics."""
    if not metrics:
        return []
    n = len(metrics)

    blur_threshold = _BLURRY_P90_FRACTION * _percentile(
        [m.sharpness for m in metrics], 0.9
    )
    median_motion, mean_motion = _motion_stats(metrics)

    # Exposure: a sample is DARK only if genuinely near-black OR dramatically
    # darker than this clip's own median brightness (a real shadow). For a
    # flat/Log clip the relative test is dropped — the material is low-contrast
    # on purpose — leaving only the near-black floor.
    median_brightness = statistics.median(m.brightness for m in metrics)
    flat_log = _is_flat_log(metrics)
    dark_relative = _DARK_RELATIVE_FRACTION * median_brightness

    # Per-sample labels, first matching problem wins (dark > blurry > shaky).
    # A dim AND featureless sample (low brightness with essentially no image
    # detail) is unusable "mud" — botched night footage, not Log. Log footage
    # is dim but detailed (sharpness above the floor), so it is NOT caught here.
    labels: list[str] = []
    for i, m in enumerate(metrics):
        near_black = m.brightness < _NEAR_BLACK_BRIGHTNESS
        relatively_dark = (not flat_log) and m.brightness < dark_relative
        featureless_dim = (
            m.brightness < _FLAT_MAX_MEDIAN_BRIGHTNESS
            and m.sharpness < _DETAIL_SHARPNESS_FLOOR
        )
        if near_black or relatively_dark or featureless_dim:
            labels.append(DARK)
        elif m.sharpness < blur_threshold:
            labels.append(BLURRY)
        elif (
            m.motion > _SHAKY_MOTION_FACTOR * median_motion
            and _jitter(metrics, i) > _JITTER_FACTOR * mean_motion
        ):
            labels.append(SHAKY)
        else:
            labels.append(USABLE)

    # Smooth: a single odd sample inside a run of another label flips.
    smoothed = labels[:]
    for i in range(1, n - 1):
        if labels[i - 1] == labels[i + 1] != labels[i]:
            smoothed[i] = labels[i - 1]
    labels = smoothed

    # Merge consecutive same-label samples into segments tiling 0..duration.
    ranks = _sharpness_ranks(metrics)
    segments: list[ClipSegment] = []
    run_start = 0
    for i in range(1, n + 1):
        if i < n and labels[i] == labels[run_start]:
            continue
        start = 0.0 if not segments else metrics[run_start].t
        end = duration if i == n else metrics[i].t
        label = labels[run_start]
        score = 0.0
        if label == USABLE:
            per_sample = [
                ranks[j] * _brightness_adequacy(metrics[j].brightness)
                for j in range(run_start, i)
            ]
            score = sum(per_sample) / len(per_sample)
        segments.append(ClipSegment(start=start, end=end, label=label, score=score))
        run_start = i
    return segments


def _edge_motion(
    metrics: list[FrameMetric], idx: list[int], head: bool
) -> tuple[float, float]:
    """Mean (dx, dy) over the first/last _MOTION_EDGE_SAMPLES samples of idx."""
    picked = idx[:_MOTION_EDGE_SAMPLES] if head else idx[-_MOTION_EDGE_SAMPLES:]
    return (
        sum(metrics[j].dx for j in picked) / len(picked),
        sum(metrics[j].dy for j in picked) / len(picked),
    )


def find_moments(
    segments: list[ClipSegment], metrics: list[FrameMetric], min_length: float = 1.0
) -> list[Moment]:
    """Rank the best usable moments, longest-window-first scoring.

    Slides a ``min_length`` window (step = half the window) across each
    USABLE segment. A window scores by mean sharpness rank plus a bonus for
    moderate, steady motion — motion between 0.5x and 2.5x the clip median
    means *something happens* in frame and beats a static tripod shot, while
    extreme motion (handled by SHAKY upstream) earns nothing. Overlapping
    windows are deduplicated (the better one wins) and the result is capped
    at 12 per clip, sorted best-first.
    """
    if not metrics or min_length <= 0:
        return []
    eps = 1e-9
    ranks = _sharpness_ranks(metrics)
    median_motion, _ = _motion_stats(metrics)
    band_lo = _MODERATE_MOTION_BAND[0] * median_motion
    band_hi = _MODERATE_MOTION_BAND[1] * median_motion
    step = min_length / 2

    candidates: list[Moment] = []
    for seg in segments:
        if seg.label != USABLE:
            continue
        start = seg.start
        while start + min_length <= seg.end + eps:
            end = start + min_length
            idx = [
                j for j, m in enumerate(metrics) if start - eps <= m.t < end - eps
            ]
            if idx:
                mean_rank = sum(ranks[j] for j in idx) / len(idx)
                if median_motion > 0:
                    moderate = sum(
                        1 for j in idx if band_lo <= metrics[j].motion <= band_hi
                    ) / len(idx)
                else:
                    moderate = 0.0
                score = min(
                    1.0,
                    (1 - _MOMENT_MOTION_BONUS) * mean_rank
                    + _MOMENT_MOTION_BONUS * moderate,
                )
                candidates.append(
                    Moment(
                        start=start,
                        end=end,
                        score=score,
                        entry_motion=_edge_motion(metrics, idx, head=True),
                        exit_motion=_edge_motion(metrics, idx, head=False),
                    )
                )
            start += step

    # Best first; dedupe overlaps greedily so the better window survives.
    candidates.sort(key=lambda m: (-m.score, m.start))
    kept: list[Moment] = []
    for cand in candidates:
        if len(kept) >= _MAX_MOMENTS:
            break
        if all(
            cand.end <= other.start + eps or cand.start >= other.end - eps
            for other in kept
        ):
            kept.append(cand)
    kept.sort(key=lambda m: (-m.score, m.start))
    return kept


def _audio_dynamic_range(audio: list[AudioMetric]) -> float:
    """rms coefficient of variation (stdev / mean) across the windows.

    Near 0 for a constant-level tone (engine, wind, camera hiss); well above
    _AUDIO_LOW_DYNAMIC_CV for real scenes where level rises and falls.
    """
    rms = [a.rms for a in audio]
    mean_rms = sum(rms) / len(rms)
    if mean_rms <= 0:
        return 0.0
    return statistics.pstdev(rms) / mean_rms


def _audio_unreliable(audio: list[AudioMetric], median_rms: float) -> bool:
    """True for constant-level camera-mic audio (see module docstring).

    Genuine silence is handled elsewhere and is NOT treated as unreliable, so
    a truly quiet clip is still reported as silent rather than as a tone.
    """
    if median_rms < _AUDIO_SILENCE_RMS:  # genuinely silent takes precedence
        return False
    if len(audio) < _AUDIO_MIN_RELIABLE_WINDOWS:
        return False
    # The signature of camera-mic noise (wind/engine/hiss) is a CONSTANT level (low dynamic
    # range) — NOT merely "lots of clipping". A genuinely loud, DYNAMIC event
    # (fireworks, applause) can clip heavily yet varies over time; that is real
    # audio worth flagging and worth scoring for highlights, so it must stay
    # "reliable". Heavy clipping only counts as unreliable when it comes with a
    # near-constant level.
    # Reaching here means near-constant level (low CV): steady camera-mic tone,
    # whether or not it also clips. That is the unreliable case.
    return _audio_dynamic_range(audio) < _AUDIO_LOW_DYNAMIC_CV


def audio_flags(audio: list[AudioMetric]) -> tuple[list[str], list[float]]:
    """(notes, per-window burst flags) for a clip's audio metrics.

    If the audio is UNRELIABLE (constant-level camera-mic tone — very low
    dynamic range, or clipping in most windows; see :func:`_audio_unreliable`)
    the clipping/wind/silent notes are suppressed in favour of one calm note,
    and every burst flag is 0 so audio does not drive highlight scoring.

    Otherwise the notes are (thresholds are the approximate module constants):

    * ``audio: clipping in N windows`` — N windows with a clipping fraction
      above _AUDIO_CLIP_WINDOW_MIN (a small, genuine fraction).
    * ``audio: likely wind noise`` — median low_ratio above
      _AUDIO_WIND_LOW_RATIO while the median rms sits above the silence
      floor (quiet clips have meaningless spectra).
    * ``audio: mostly silent`` — median rms below _AUDIO_SILENCE_RMS.

    Burst flags (aligned with ``audio``) mark loudness bursts: windows whose
    rms exceeds _HIGHLIGHT_BURST_FACTOR x the clip's median rms and the
    silence floor — cheers, laughter and action all read as such bursts.
    """
    if not audio:
        return [], []
    median_rms = statistics.median(a.rms for a in audio)
    if _audio_unreliable(audio, median_rms):
        note = "audio: constant camera-mic audio (wind, engine or hiss) — audio signals ignored"
        return [note], [0.0] * len(audio)

    notes: list[str] = []
    median_low = statistics.median(a.low_ratio for a in audio)
    clipped = sum(1 for a in audio if a.clipping > _AUDIO_CLIP_WINDOW_MIN)
    if clipped:
        notes.append(f"audio: clipping in {clipped} windows")
    if median_low > _AUDIO_WIND_LOW_RATIO and median_rms >= _AUDIO_SILENCE_RMS:
        notes.append("audio: likely wind noise")
    if median_rms < _AUDIO_SILENCE_RMS:
        notes.append("audio: mostly silent")
    bursts = [
        1.0
        if a.rms > _HIGHLIGHT_BURST_FACTOR * median_rms and a.rms > _AUDIO_SILENCE_RMS
        else 0.0
        for a in audio
    ]
    return notes, bursts


def apply_audio(moments: list[Moment], audio: list[AudioMetric]) -> list[str]:
    """Set each moment's highlight from the audio; return the audio notes.

    highlight = min(1, burst_share * _HIGHLIGHT_GAIN), where burst_share is
    the share of audio windows starting inside [start, end) that are
    loudness bursts (see :func:`audio_flags`). Empty ``audio`` (no audio
    stream) leaves every highlight at 0.0 and returns no notes.
    """
    notes, bursts = audio_flags(audio)
    eps = 1e-9
    for moment in moments:
        idx = [
            i for i, a in enumerate(audio) if moment.start - eps <= a.t < moment.end - eps
        ]
        if idx:
            share = sum(bursts[i] for i in idx) / len(idx)
            moment.highlight = min(1.0, share * _HIGHLIGHT_GAIN)
    return notes


def _reraise_if_ffmpeg_missing(exc: MonteurMediaError) -> None:
    if "ffmpeg not found" in str(exc):
        raise exc


def analyze_clip(path: str) -> ClipReport:
    """Full report for one clip (decodes frames via monteur.media).

    Clips that are too short or fail to decode come back as a report with an
    explanatory note instead of raising; only a missing ffmpeg re-raises.
    """
    path = str(path)
    try:
        info = probe(path)
    except MonteurMediaError as exc:
        _reraise_if_ffmpeg_missing(exc)
        return ClipReport(path=path, duration=0.0, notes=[f"could not analyze: {exc}"])

    report = ClipReport(path=path, duration=info.duration)
    if info.duration < 1.0:
        report.notes.append("clip shorter than 1s — skipped")
        return report

    try:
        metrics = frame_metrics(path)
    except MonteurMediaError as exc:
        _reraise_if_ffmpeg_missing(exc)
        report.notes.append(f"could not decode frames: {exc}")
        return report

    report.segments = classify_metrics(metrics, info.duration)
    report.moments = find_moments(report.segments, metrics)

    # One calm note for intentionally flat/Log material (never per-segment).
    if _is_flat_log(metrics):
        report.notes.append(
            "flat / low-contrast (log?) footage — exposure check relaxed"
        )

    # HARD RULE: a clip with any real detail is never skipped to zero. If the
    # per-label pass produced no moments (e.g. everything read dark/blurry) but
    # the clip clearly has detail, surface its best stretches anyway.
    if not report.moments and _has_visual_detail(metrics):
        whole = [ClipSegment(start=0.0, end=info.duration, label=USABLE, score=0.0)]
        report.moments = find_moments(whole, metrics)

    # Audio features are best-effort: no audio stream or a failed audio
    # decode silently skips them (highlights stay 0.0, no audio notes).
    try:
        audio = audio_metrics(path) if info.has_audio else []
    except MonteurMediaError as exc:
        _reraise_if_ffmpeg_missing(exc)
        audio = []
    report.notes.extend(apply_audio(report.moments, audio))

    usable_time = sum(
        s.end - s.start for s in report.segments if s.label == USABLE
    )
    report.usable_ratio = usable_time / info.duration if info.duration else 0.0

    unusable_time = info.duration - usable_time
    if unusable_time > 1e-6:
        by_label: dict[str, float] = {}
        for seg in report.segments:
            if seg.label != USABLE:
                by_label[seg.label] = by_label.get(seg.label, 0.0) + (
                    seg.end - seg.start
                )
        worst = max(by_label, key=by_label.__getitem__)
        wording = {DARK: "too dark", BLURRY: "blurry", SHAKY: "shaky"}[worst]
        pct = round(100 * unusable_time / info.duration)
        report.notes.append(f"{pct}% unusable: mostly {wording}")
    if not report.moments:
        report.notes.append("no usable stretch ≥ 1s — clip skipped")
    return report


def _call_progress(progress, index, total, name, stage, report):
    """Invoke a progress callback, swallowing any exception it raises.

    A broken callback (a UI that throws, a closed stream) must never abort
    the sift, so every call is guarded.
    """
    if progress is None:
        return
    try:
        progress(index, total, name, stage, report)
    except Exception:  # noqa: BLE001 — a broken callback must not abort sifting
        pass


def sift_directory(directory: str, progress=None) -> list[ClipReport]:
    """Reports for every video file in a directory.

    Individual clip failures become a note in that clip's report; only a
    missing ffmpeg (which dooms every clip) aborts the run.

    ``progress`` is an optional callback invoked around each clip so callers
    (e.g. the CLI) can show per-clip feedback while the slow frame/audio
    decode runs. Its signature is::

        progress(index: int, total: int, name: str, stage: str,
                 report: ClipReport | None)

    * ``index`` — 1-based position of the clip being analysed (1..total).
    * ``total`` — number of clips in the directory.
    * ``name`` — the clip's file name (no directory).
    * ``stage`` — ``"start"`` just BEFORE the clip is analysed (``report`` is
      ``None``), then ``"done"`` just AFTER (``report`` is the finished
      :class:`ClipReport`).

    The callback is called exactly twice per clip: once with
    ``stage="start"`` and once with ``stage="done"``. Any exception the
    callback raises is swallowed so a broken callback cannot abort the sift.
    ``progress=None`` (the default) disables all feedback and keeps the
    function fully backwards compatible.
    """
    media = list_media(directory)
    total = len(media)
    reports: list[ClipReport] = []
    for i, media_path in enumerate(media, start=1):
        name = Path(media_path).name
        _call_progress(progress, i, total, name, "start", None)
        try:
            report = analyze_clip(str(media_path))
        except MonteurMediaError as exc:
            _reraise_if_ffmpeg_missing(exc)
            report = ClipReport(
                path=str(media_path), duration=0.0, notes=[f"skipped: {exc}"]
            )
        reports.append(report)
        _call_progress(progress, i, total, name, "done", report)
    return reports
