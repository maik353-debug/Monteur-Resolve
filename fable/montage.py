"""Montage builder: best moments + music beats -> a first cut.

Takes the sifted footage (:mod:`fable.sift`) and an analyzed song
(:mod:`fable.music`) and lays out a rough cut on the beat grid: calm
sections cut slower (every few beats), high-energy sections cut faster.
The result is a Timeline — video from the footage on V1, the song on A1 —
ready for EDL/FCPXML export into Resolve.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fable.model import Timeline
from fable.music import MusicAnalysis
from fable.sift import ClipReport

CHRONOLOGICAL = "chronological"  # keep footage order (travel/event films)
BEST_FIRST = "best_first"  # strongest material on the strongest sections


@dataclass
class MontagePlan:
    """The chosen cut points before rendering to a timeline."""

    music_path: str
    duration: float  # seconds, montage length (may be shorter than the song)
    entries: list["MontageEntry"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class MontageEntry:
    clip_path: str
    source_start: float  # seconds in the clip
    source_end: float
    record_start: float  # seconds in the montage
    record_end: float
    score: float


def plan_montage(
    reports: list[ClipReport],
    music: MusicAnalysis,
    order: str = CHRONOLOGICAL,
    max_duration: float | None = None,
) -> MontagePlan:
    """Distribute the best moments across the song's beat grid."""
    raise NotImplementedError


def montage_to_timeline(plan: MontagePlan, fps: float, name: str = "Fable Montage") -> Timeline:
    """Render a MontagePlan as a Timeline (footage on V1, music on A1)."""
    raise NotImplementedError
