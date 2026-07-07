"""Auto-assembly: from screenplay + take transcripts to a first cut.

The engine aligns each take's transcript against the screenplay's dialogue,
scores takes (coverage, accuracy, fluffs), picks the best material per
dialogue passage, and lays it out as a Timeline that Resolve can import.

The editor reviews the plan — Fable proposes, the editor decides.

How the matching works (and where it stops working)
---------------------------------------------------

Text matching is fuzzy, not word-perfect. Both the script line and the
transcript are normalized first: lower-cased, punctuation stripped,
whitespace collapsed, and filler sounds ("uh", "um", "äh", "ähm", "eh",
"hm") removed. The remaining text is compared with difflib's
SequenceMatcher, which yields a 0..1 similarity — 1.0 is a verbatim
delivery, anything below 0.55 is treated as "this line was not said here".
That threshold is deliberately forgiving: actors paraphrase, transcribers
mishear, and a slightly loose match is more useful than no match.

A script line rarely maps to exactly one transcript segment, so the engine
tries every window of 1 up to 6 consecutive segments and keeps the
best-scoring one. Six segments is a practical ceiling — beyond that, the
windows grow so long that unrelated speech starts inflating scores.

Dialogue is performed in order, and the matcher leans on that: within one
take, matches must be non-decreasing in time. The pass is a simple greedy
walk — for each script line (in scene order) it searches only from the
position of the previous accepted match onward and takes the best window
it finds there. Because the *best* window from that point wins (not the
first acceptable one), a garbled first attempt does not trap the matcher;
the cleaner retry later in the take scores higher and is chosen. The
trade-off of greediness: if an early line locks onto a spot far too late
in the take, every following line is pushed after it. In practice takes
are short and deliveries orderly enough that this is rare.

Fluffs (restarts and aborts) are detected with two cheap heuristics:
a segment that half-matches a script line (similarity 0.3..0.55) shortly
before a better match of the same line reads as a restart; a segment that
trails off with "--" or "..." and whose next segment reopens with (almost)
the same first three words reads as an abort-and-retry. Exact recall does
not matter here — fluff counts only nudge the ranking between takes, they
never exclude material.

Known limits: no speaker verification (a line matched in the wrong actor's
mouth still counts), no handling of intentionally out-of-order coverage
(pickups shot in reverse), and heavy improvisation drives similarity below
the threshold even when the performance is usable. The plan is a proposal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from statistics import fmean

from fable.model import AUDIO, VIDEO, Clip, Marker, Timeline, Transcript, seconds_to_frames
from fable.screenplay import DIALOGUE, Scene, Screenplay

MATCH_THRESHOLD = 0.55  # below this, a line counts as unmatched in a take
FLUFF_THRESHOLD = 0.3  # 0.3..0.55 before a better match of the same line = restart
MAX_WINDOW = 6  # a script line rarely spans more than ~6 transcript segments
MERGE_GAP = 1.5  # seconds: adjacent matches closer than this fuse into one segment

_FILLERS = frozenset({"uh", "um", "äh", "ähm", "eh", "hm"})
_PUNCT_RE = re.compile(r"[^\w\s]|_", re.UNICODE)


@dataclass
class TakeSource:
    """One recorded take: a clip name plus its transcript."""

    name: str  # clip/file name, becomes the source in the timeline
    transcript: Transcript
    scene_hint: str = ""  # e.g. "12", parsed from file name like S12_T03
    take_hint: str = ""  # e.g. "3"


@dataclass
class LineMatch:
    """A screenplay dialogue line located inside one take."""

    element_index: int  # index into scene.elements
    take: str  # TakeSource.name
    start: float  # seconds in the take's source
    end: float
    similarity: float  # 0..1 text match quality
    text: str = ""  # what was actually said


@dataclass
class TakeScore:
    take: str
    coverage: float  # 0..1 share of the scene's dialogue this take covers
    accuracy: float  # 0..1 mean similarity of matched lines
    fluffs: int  # restarts/aborts detected in the transcript
    duration: float  # seconds of usable matched material

    @property
    def total(self) -> float:
        return self.coverage * 0.6 + self.accuracy * 0.3 - min(self.fluffs, 10) * 0.02


@dataclass
class Segment:
    """A contiguous piece of one take chosen for the cut."""

    take: str
    start: float  # seconds in source
    end: float
    element_indexes: list[int] = field(default_factory=list)
    text: str = ""


@dataclass
class SceneAssembly:
    scene_index: int
    heading: str
    take_scores: list[TakeScore] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    unmatched_lines: list[int] = field(default_factory=list)  # element indexes
    notes: list[str] = field(default_factory=list)


@dataclass
class AssemblyPlan:
    scenes: list[SceneAssembly] = field(default_factory=list)

    def coverage(self) -> float:
        matched = sum(
            len(seg.element_indexes) for s in self.scenes for seg in s.segments
        )
        missing = sum(len(s.unmatched_lines) for s in self.scenes)
        total = matched + missing
        return matched / total if total else 0.0


def _normalize(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace, drop filler sounds."""
    words = _PUNCT_RE.sub(" ", text.lower()).split()
    return " ".join(w for w in words if w not in _FILLERS)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _best_window(
    line_norm: str, segments: list, from_index: int
) -> tuple[float, int, int]:
    """Best-matching window of consecutive segments at or after ``from_index``.

    Returns (similarity, start_index, window_length); (0.0, -1, 0) if nothing
    scores above zero. Strict ``>`` keeps ties on the earliest, shortest window.
    """
    best = (0.0, -1, 0)
    for i in range(from_index, len(segments)):
        joined = ""
        for w in range(1, min(MAX_WINDOW, len(segments) - i) + 1:
            pass
    return best
