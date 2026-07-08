"""Music analysis: tempo, beats and energy sections.

Fable cuts montages to music, so it needs to know where the beats fall and
where the song changes gear. Pure-numpy DSP on the decoded waveform — no ML,
tuned for music with a clear pulse (the montage use case).
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


def detect_beats(samples, rate: int) -> tuple[float, list[float]]:
    """Estimate (tempo_bpm, beat_times) from a mono float32 waveform."""
    raise NotImplementedError


def detect_sections(samples, rate: int) -> list[MusicSection]:
    """Split the waveform into low/mid/high energy sections."""
    raise NotImplementedError


def analyze_music(path: str, rate: int = 22050) -> MusicAnalysis:
    """Decode a song and return tempo, beat grid and energy sections."""
    raise NotImplementedError
