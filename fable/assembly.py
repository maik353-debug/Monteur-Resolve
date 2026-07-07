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
it finds there. Because the *best* remaining window wins (not the first
acceptable one), a garbled first attempt does not trap the matcher; the
cleaner retry later in the take scores higher and is chosen. The trade-off
of greediness: if an early line locks onto a spot far too late in the
take, every following line is pushed after it. In practice takes are short
and deliveries orderly enough that this is rare.

Fluffs (restarts and aborts) are detected with two cheap heuristics: a
segment that half-matches a script line (similarity 0.3..0.55) before the
accepted, better match of the same line reads as a restart; a segment that
trails off with "--" or "..." and whose next segment reopens with (almost)
the same first three words reads as an abort-and-retry. Exact recall does
not matter here — fluff counts only nudge the ranking between takes, they
never exclude material.

Known limits: no speaker verification (a line matched in the wrong actor's
mouth still counts), no handling of intentionally out-of-order coverage
(pickups performed in reverse), and heavy improvisation drives similarity
below the threshold even when the performance is usable. The plan is a
proposal, not a verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from statistics import fmean

from fable.model import (
    AUDIO,
    VIDEO,
    Clip,
    Marker,
    Timeline,
    Transcript,
    TranscriptSegment,
    seconds_to_frames,
)
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


# --- Matching ----------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace, drop filler sounds."""
    words = _PUNCT_RE.sub(" ", text.lower()).split()
    return " ".join(w for w in words if w not in _FILLERS)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _window_norms(segments: list[TranscriptSegment]) -> list[str]:
    return [_normalize(s.text) for s in segments]


def _best_window(
    line_norm: str, seg_norms: list[str], from_index: int
) -> tuple[float, int, int]:
    """Best window of consecutive segments starting at or after ``from_index``.

    Returns (similarity, start_index, window_length); (0.0, -1, 0) when
    nothing scores above zero. Strict ``>`` on the running best keeps ties
    on the earliest, shortest window — the least material that says the line.
    """
    best_sim, best_i, best_w = 0.0, -1, 0
    for i in range(from_index, len(seg_norms)):
        joined = ""
        for w in range(1, min(MAX_WINDOW, len(seg_norms) - i) + 1):
            piece = seg_norms[i + w - 1]
            joined = f"{joined} {piece}".strip() if joined else piece
            sim = _similarity(line_norm, joined)
            if sim > best_sim:
                best_sim, best_i, best_w = sim, i, w
    return best_sim, best_i, best_w


def _match_take(
    scene: Scene, dialogue_indexes: list[int], take: TakeSource
) -> tuple[list[LineMatch], TakeScore]:
    """Match one take against a scene's dialogue and score it.

    Greedy in-order pass: script lines are visited in scene order and each
    may only match at or after the window where the previous line matched
    (non-decreasing time within a take). See the module docstring.
    """
    segments = take.transcript.segments
    seg_norms = _window_norms(segments)
    matches: list[LineMatch] = []
    cursor = 0
    search_starts: dict[int, int] = {}  # element_index -> where its search began
    match_windows: dict[int, tuple[float, int]] = {}  # element_index -> (sim, start)

    for elem_idx in dialogue_indexes:
        line_norm = _normalize(scene.elements[elem_idx].text)
        if not line_norm:
            continue
        search_starts[elem_idx] = cursor
        sim, i, w = _best_window(line_norm, seg_norms, cursor)
        if i < 0 or sim < MATCH_THRESHOLD:
            continue
        matches.append(
            LineMatch(
                element_index=elem_idx,
                take=take.name,
                start=segments[i].start,
                end=segments[i + w - 1].end,
                similarity=sim,
                text=" ".join(s.text.strip() for s in segments[i : i + w]),
            )
        )
        match_windows[elem_idx] = (sim, i)
        cursor = i  # non-decreasing: the next line may start here, not earlier

    fluffs = _count_fluffs(scene, segments, seg_norms, search_starts, match_windows)
    n_lines = len(dialogue_indexes)
    coverage = len(matches) / n_lines if n_lines else 0.0
    accuracy = fmean(m.similarity for m in matches) if matches else 0.0
    duration = sum(m.end - m.start for m in matches)
    return matches, TakeScore(take.name, coverage, accuracy, fluffs, duration)


def _count_fluffs(
    scene: Scene,
    segments: list[TranscriptSegment],
    seg_norms: list[str],
    search_starts: dict[int, int],
    match_windows: dict[int, tuple[float, int]],
) -> int:
    """Count restart/abort segments in a take. Deliberately rough.

    (a) A single segment that scores 0.3..0.55 against a script line, sitting
        between where the search for that line began and its accepted (better)
        match, is a restart of that line.
    (b) A segment trailing off with "--" or "..." whose successor reopens with
        (almost) the same first three words is an abort-and-retry.
    Each transcript segment is counted at most once.
    """
    flagged: set[int] = set()
    for elem_idx, (best_sim, best_i) in match_windows.items():
        line_norm = _normalize(scene.elements[elem_idx].text)
        for j in range(search_starts.get(elem_idx, 0), best_i):
            sim = _similarity(line_norm, seg_norms[j])
            if FLUFF_THRESHOLD <= sim < MATCH_THRESHOLD and sim < best_sim:
                flagged.add(j)
    for j in range(len(segments) - 1):
        raw = segments[j].text.rstrip()
        if not (raw.endswith("--") or raw.endswith("...")):
            continue
        head_a = seg_norms[j].split()[:3]
        head_b = seg_norms[j + 1].split()[:3]
        if len(head_a) < 3 or len(head_b) < 3:
            continue
        if _similarity(" ".join(head_a), " ".join(head_b)) >= 0.8:
            flagged.add(j)
    return len(flagged)


def match_takes_to_scene(
    scene: Scene, takes: list[TakeSource]
) -> tuple[list[LineMatch], list[TakeScore]]:
    """Locate each dialogue line of a scene inside each candidate take.

    Returns every line match found across all takes (grouped by take, in the
    order the takes were given) plus one TakeScore per take. Matching strategy
    and its limits are described in the module docstring.
    """
    dialogue_indexes = [
        i for i, e in enumerate(scene.elements) if e.kind == DIALOGUE
    ]
    all_matches: list[LineMatch] = []
    scores: list[TakeScore] = []
    for take in takes:
        matches, score = _match_take(scene, dialogue_indexes, take)
        all_matches.extend(matches)
        scores.append(score)
    return all_matches, scores


# --- Planning ----------------------------------------------------------------


def _norm_scene_number(value: str) -> str:
    """Case-insensitive scene-number comparison key; leading zeros ignored."""
    v = value.strip().lower().lstrip("0")
    return v if v else ("0" if value.strip() else "")


def _candidates_for_scene(scene: Scene, takes: list[TakeSource]) -> list[TakeSource]:
    """Takes hinted at this scene's number, plus all unhinted takes."""
    scene_key = _norm_scene_number(scene.number)
    out: list[TakeSource] = []
    for take in takes:
        if not take.scene_hint.strip():
            out.append(take)
        elif scene_key and _norm_scene_number(take.scene_hint) == scene_key:
            out.append(take)
    return out


def _build_segments(chosen: list[LineMatch]) -> list[Segment]:
    """Merge adjacent matches from the same take into contiguous segments.

    Matches are laid out in script order; consecutive ones from the same take
    fuse when the pause between them is under MERGE_GAP seconds. The pause
    stays inside the segment — natural breathing room survives the cut.
    """
    segments: list[Segment] = []
    for m in sorted(chosen, key=lambda m: m.element_index):
        if (
            segments
            and segments[-1].take == m.take
            and m.start >= segments[-1].start
            and m.start - segments[-1].end < MERGE_GAP
        ):
            seg = segments[-1]
            seg.end = max(seg.end, m.end)
            seg.element_indexes.append(m.element_index)
            seg.text = f"{seg.text} {m.text}".strip()
        else:
            segments.append(
                Segment(
                    take=m.take,
                    start=m.start,
                    end=m.end,
                    element_indexes=[m.element_index],
                    text=m.text,
                )
            )
    return segments


def plan_assembly(
    screenplay: Screenplay,
    takes: list[TakeSource],
    max_takes_per_scene: int = 1,
    forced: dict[int, str] | None = None,
) -> AssemblyPlan:
    """Choose material for every scene and return the full assembly plan.

    Takes with a scene_hint are candidates only for that scene (numbers
    compared case-insensitively, leading zeros ignored); unhinted takes are
    candidates everywhere. Per scene the highest-scoring take supplies its
    matched lines; with max_takes_per_scene > 1, lines it missed are filled
    from the next-best takes. Adjacent lines from one take merge into a
    single segment when the gap between them is under 1.5 seconds.

    ``forced`` maps a scene index to a take name the editor picked manually;
    that take supplies its lines first regardless of score (gap filling from
    other takes still applies when max_takes_per_scene allows it).
    """
    plan = AssemblyPlan()
    for scene_index, scene in enumerate(screenplay.scenes):
        assembly = SceneAssembly(scene_index=scene_index, heading=scene.heading)
        plan.scenes.append(assembly)
        dialogue_indexes = [
            i for i, e in enumerate(scene.elements) if e.kind == DIALOGUE
        ]
        if not dialogue_indexes:
            assembly.notes.append("scene has no dialogue")
            continue

        candidates = _candidates_for_scene(scene, takes)
        if not candidates:
            assembly.unmatched_lines = list(dialogue_indexes)
            label = scene.number or scene.heading or str(scene_index)
            assembly.notes.append(f"no takes matched scene {label}")
            continue

        matches, scores = match_takes_to_scene(scene, candidates)
        assembly.take_scores = sorted(scores, key=lambda s: s.total, reverse=True)
        by_take: dict[str, dict[int, LineMatch]] = {}
        for m in matches:
            by_take.setdefault(m.take, {})[m.element_index] = m

        order = assembly.take_scores
        forced_take = (forced or {}).get(scene_index)
        if forced_take is not None:
            if any(s.take == forced_take for s in order):
                order = sorted(order, key=lambda s: (s.take != forced_take, -s.total))
                assembly.notes.append(f"take {forced_take} pinned by editor")
            else:
                assembly.notes.append(
                    f"pinned take {forced_take} not available for this scene"
                )

        chosen: dict[int, LineMatch] = {}
        takes_used = 0
        for score in order:
            if takes_used >= max_takes_per_scene:
                break
            contributed = 0
            for elem_idx, m in by_take.get(score.take, {}).items():
                if elem_idx not in chosen:
                    chosen[elem_idx] = m
                    contributed += 1
            if contributed:
                takes_used += 1
                assembly.notes.append(
                    f"take {score.take} covers {contributed}/{len(dialogue_indexes)} lines"
                )

        assembly.segments = _build_segments(list(chosen.values()))
        assembly.unmatched_lines = [
            i for i in dialogue_indexes if i not in chosen
        ]
        for i in assembly.unmatched_lines:
            assembly.notes.append(f"line {i} missing everywhere")
    return plan


# --- Timeline rendering -------------------------------------------------------


def assembly_to_timeline(
    plan: AssemblyPlan,
    takes: list[TakeSource],
    fps: float,
    handles: float = 0.5,
    name: str = "Fable Assembly",
) -> Timeline:
    """Render an AssemblyPlan as a Timeline (paired V1/A1 clips per segment).

    Segments land back-to-back from record frame 0, in scene/plan order. Each
    segment's source range is widened by ``handles`` seconds on both sides
    (clamped to 0 and to the take transcript's duration) so the editor has
    trim room, then converted to frames. Every segment yields one video clip
    on V1 and one audio clip on A1 with identical ranges. A marker is dropped
    at each scene boundary carrying the scene heading.
    """
    by_name: dict[str, TakeSource] = {t.name: t for t in takes}
    timeline = Timeline(name=name, fps=fps)
    cursor = 0
    for scene in plan.scenes:
        timeline.markers.append(Marker(frame=cursor, name=scene.heading))
        for seg in scene.segments:
            src_start = max(0.0, seg.start - handles)
            src_end = seg.end + handles
            take = by_name.get(seg.take)
            if take is not None and take.transcript.duration > 0:
                src_end = min(src_end, take.transcript.duration)
            src_end = max(src_end, src_start)
            source_in = seconds_to_frames(src_start, fps)
            source_out = seconds_to_frames(src_end, fps)
            length = source_out - source_in
            for track, kind in (("V1", VIDEO), ("A1", AUDIO)):
                timeline.clips.append(
                    Clip(
                        name=seg.text[:40],
                        track=track,
                        kind=kind,
                        source_in=source_in,
                        source_out=source_out,
                        record_in=cursor,
                        record_out=cursor + length,
                        source_name=seg.take,
                    )
                )
            cursor += length
    return timeline
