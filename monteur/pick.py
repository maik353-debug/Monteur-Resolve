"""Pick the right song for the footage — before a single cut is made.

The montage engine can cut to any song; whether the RESULT feels right is
mostly decided earlier, by whether the song fits the material. This module
ranks candidate songs (e.g. a folder of Artlist downloads) against sifted
footage::

    monteur pick-music footage/ ~/Music/candidates/

Five signals, each 0..1, weighted into one score per song:

* Beat clarity (0.30) — how regular the tracked beat grid is (coefficient
  of variation of the inter-beat intervals). Monteur cuts ON beats; a song
  with a woolly pulse produces a woolly montage no matter what.
* Length fit (0.30) — the song's duration against the footage's unique
  material (deduplicated moment seconds, same measure the repetition guard
  uses). Ideal: the song fits inside the unique material itself, so nothing
  has to repeat (repeats are off by default and the planner shortens the
  cut rather than recycle footage). Longer songs taper off; Monteur can cut
  a montage SHORTER than the song (best_energy_window), so moderate
  oversize is only a mild penalty. When ``target_duration`` is given it
  replaces the material measure: the song must simply be at least that
  long.
* Tempo fit (0.20) — fast footage wants a fast song. The footage's mean
  moment motion (normalised at _MOTION_FAST_PX px/frame ~ "fast") maps to a
  preferred BPM inside 90..150; the song is scored on a Gaussian around it.
* Drop (0.10) — a detected drop gives the montage its climax anchor.
* Dynamic arc (0.10) — songs with BOTH calm and loud sections carry an
  opening -> build -> climax arc; wall-of-sound tracks can't.

Every score comes with human-readable reasons, so the ranking is an
argument, not an oracle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from monteur.montage import _unique_material
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport

# Motion magnitude (px/frame at analysis resolution) treated as "fast
# footage" — the same order of magnitude the sift motion metrics produce
# for action-cam material.
_MOTION_FAST_PX = 8.0
# Tempo preference: static footage -> _TEMPO_CALM_BPM, fast -> _TEMPO_FAST_BPM.
_TEMPO_CALM_BPM = 90.0
_TEMPO_FAST_BPM = 150.0
_TEMPO_WIDTH_BPM = 25.0  # Gaussian width of the tempo-fit score
# Beat clarity: a CV of inter-beat intervals at/above this scores 0.
_CLARITY_CV_BAD = 0.10

_WEIGHTS = {
    "clarity": 0.30,
    "length": 0.30,
    "tempo": 0.20,
    "drop": 0.10,
    "arc": 0.10,
}

_EPS = 1e-6

AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")


@dataclass
class SongRating:
    path: str
    score: float  # 0..1 weighted total
    parts: dict = field(default_factory=dict)  # signal -> 0..1
    reasons: list[str] = field(default_factory=list)
    tempo: float = 0.0
    duration: float = 0.0


def _footage_motion(reports: list[ClipReport]) -> float:
    """Mean moment motion magnitude, normalised 0..1 at _MOTION_FAST_PX."""
    mags = [
        (math.hypot(*m.entry_motion) + math.hypot(*m.exit_motion)) / 2.0
        for r in reports
        for m in r.moments
    ]
    if not mags:
        return 0.5  # nothing known: sit in the middle
    mean = sum(mags) / len(mags)
    return min(mean / _MOTION_FAST_PX, 1.0)


def _clarity(music: MusicAnalysis) -> tuple[float, str]:
    if music.tempo <= 0 or len(music.beats) < 8:
        return 0.0, "no reliable beat found — hard to cut to"
    intervals = [b - a for a, b in zip(music.beats, music.beats[1:])]
    mean = sum(intervals) / len(intervals)
    if mean <= _EPS:
        return 0.0, "degenerate beat grid"
    var = sum((iv - mean) ** 2 for iv in intervals) / len(intervals)
    cv = math.sqrt(var) / mean
    score = max(0.0, 1.0 - cv / _CLARITY_CV_BAD)
    if score >= 0.8:
        return score, f"clear steady pulse ({music.tempo:.0f} BPM)"
    if score > 0.3:
        return score, f"usable but loose pulse ({music.tempo:.0f} BPM)"
    return score, "irregular pulse — cuts will feel arbitrary"


def _length_fit(
    music: MusicAnalysis, unique: float, target: float | None
) -> tuple[float, str]:
    if target:
        if music.duration + _EPS >= target:
            return 1.0, f"covers the {target:.0f}s target"
        score = max(0.0, music.duration / target)
        return score, f"only {music.duration:.0f}s — shorter than the {target:.0f}s target"
    if unique <= _EPS:
        return 0.5, "footage material unknown"
    # The no-repeat rule: a montage never outgrows the unique material
    # (repeats off shortens the cut instead of recycling footage), so a
    # song is a perfect length fit only when it fits INSIDE that material.
    if music.duration <= unique + _EPS:
        return 1.0, (
            f"{music.duration:.0f}s fits your {unique:.0f}s of material — "
            "nothing has to repeat"
        )
    # Oversize tapers: 2x the material ~ 0.5. Monteur can still cut the
    # song short, so this is a soft penalty, not a veto.
    ratio = music.duration / unique
    score = max(0.0, 1.0 - 0.5 * (ratio - 1.0))
    return score, (
        f"{music.duration:.0f}s is long for your {unique:.0f}s of material — "
        "expect a shortened cut (or allow repeats)"
    )


def _tempo_fit(music: MusicAnalysis, motion: float) -> tuple[float, str]:
    if music.tempo <= 0:
        return 0.0, "no tempo"
    preferred = _TEMPO_CALM_BPM + (_TEMPO_FAST_BPM - _TEMPO_CALM_BPM) * motion
    # Octave-fold the song tempo toward the preferred value: cutting every
    # 2 beats of a 170 BPM song feels like 85 BPM.
    tempo = music.tempo
    while tempo > preferred * 1.5:
        tempo /= 2.0
    while tempo < preferred / 1.5:
        tempo *= 2.0
    score = math.exp(-0.5 * ((tempo - preferred) / _TEMPO_WIDTH_BPM) ** 2)
    feel = "fast" if motion > 0.6 else ("calm" if motion < 0.3 else "medium")
    return score, (
        f"{music.tempo:.0f} BPM vs your {feel} footage "
        f"(ideal ~{preferred:.0f} BPM)"
    )


def _drop(music: MusicAnalysis) -> tuple[float, str]:
    if music.drops:
        return 1.0, f"drop at {music.drops[0]:.0f}s — a natural climax anchor"
    return 0.0, "no drop — the climax has nothing to land on"


def _arc(music: MusicAnalysis) -> tuple[float, str]:
    labels = {s.label for s in music.sections}
    if "low" in labels and "high" in labels:
        return 1.0, "quiet and loud passages — carries a story arc"
    if len(labels) > 1:
        return 0.6, "some dynamic movement"
    return 0.2, "flat dynamics — every section sounds the same"


def rate_song(
    music: MusicAnalysis,
    reports: list[ClipReport],
    target_duration: float | None = None,
) -> SongRating:
    """Score one analyzed song against sifted footage."""
    unique = _unique_material(reports)
    motion = _footage_motion(reports)
    parts: dict = {}
    reasons: list[str] = []
    for key, fn in (
        ("clarity", lambda: _clarity(music)),
        ("length", lambda: _length_fit(music, unique, target_duration)),
        ("tempo", lambda: _tempo_fit(music, motion)),
        ("drop", lambda: _drop(music)),
        ("arc", lambda: _arc(music)),
    ):
        score, reason = fn()
        parts[key] = score
        reasons.append(reason)
    total = sum(_WEIGHTS[k] * parts[k] for k in _WEIGHTS)
    return SongRating(
        path=music.path,
        score=total,
        parts=parts,
        reasons=reasons,
        tempo=music.tempo,
        duration=music.duration,
    )


def list_songs(music_dir: str | Path) -> list[Path]:
    """Audio files in a folder, sorted by name."""
    root = Path(music_dir)
    return sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    ) if root.is_dir() else []


def rank_songs(
    reports: list[ClipReport],
    music_dir: str | Path,
    target_duration: float | None = None,
    progress=None,
) -> list[SongRating]:
    """Analyze every song in ``music_dir`` and rank them best-first.

    ``progress(index, total, name)`` is called before each song's analysis.
    Songs that fail to decode get a 0-score rating whose first reason is the
    error — visible in the ranking, never a crash.
    """
    from monteur.music import analyze_music

    songs = list_songs(music_dir)
    ratings: list[SongRating] = []
    for i, path in enumerate(songs, start=1):
        if progress:
            progress(i, len(songs), path.name)
        try:
            music = analyze_music(str(path))
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the ranking
            ratings.append(
                SongRating(path=str(path), score=0.0, reasons=[f"could not analyze: {exc}"])
            )
            continue
        ratings.append(rate_song(music, reports, target_duration))
    ratings.sort(key=lambda r: -r.score)
    return ratings
