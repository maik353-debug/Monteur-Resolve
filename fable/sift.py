"""Footage sifting: which parts of which clips are worth using?

Fable scans every clip's frames (via :mod:`fable.media`) and classifies
stretches as usable or problematic (too dark, blurry, shaky), then ranks the
best moments — so the editor (or the montage builder) starts from the good
material instead of watching everything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fable.media import FrameMetric

USABLE = "usable"
DARK = "dark"
BLURRY = "blurry"
SHAKY = "shaky"


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


@dataclass
class ClipReport:
    path: str
    duration: float
    segments: list[ClipSegment] = field(default_factory=list)
    moments: list[Moment] = field(default_factory=list)  # best first
    usable_ratio: float = 0.0  # share of the clip classified usable
    notes: list[str] = field(default_factory=list)


def classify_metrics(metrics: list[FrameMetric], duration: float) -> list[ClipSegment]:
    """Label stretches of a clip from its frame metrics."""
    raise NotImplementedError


def find_moments(
    segments: list[ClipSegment], metrics: list[FrameMetric], min_length: float = 1.0
) -> list[Moment]:
    """Rank the best usable moments, longest-window-first scoring."""
    raise NotImplementedError


def analyze_clip(path: str) -> ClipReport:
    """Full report for one clip (decodes frames via fable.media)."""
    raise NotImplementedError


def sift_directory(directory: str) -> list[ClipReport]:
    """Reports for every video file in a directory."""
    raise NotImplementedError
