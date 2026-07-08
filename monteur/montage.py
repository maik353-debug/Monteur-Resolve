"""Montage builder: best moments + music beats -> a first cut.

Takes the sifted footage (:mod:`monteur.sift`) and an analyzed song
(:mod:`monteur.music`) and lays out a rough cut on the beat grid: calm
sections cut slower (every few beats), high-energy sections cut faster.
The result is a Timeline — video from the footage on V1, the song on A1 —
ready for EDL/FCPXML export into Resolve.

Slotting algorithm
------------------
1. The montage length is ``min(song duration, max_duration)``.
2. A cut grid is walked beat by beat: in a "high" section the next cut is
   1 beat away, in "mid" 2 beats, in "low" 4 beats. If that interval would
   be shorter than :data:`MIN_CUT_INTERVAL` (0.4 s) the beat step is doubled
   until it isn't (no strobing). With no beats at all, a fixed 2 s grid is
   used and noted. Cuts at/after the montage length are dropped; the last
   slot always ends exactly at the montage length.
3. Every moment from every report goes into one pool (keeping its clip
   path). CHRONOLOGICAL sorts the pool by (clip path, moment start) and
   fills slots left to right. BEST_FIRST sorts the pool by score descending
   and visits the slots in section-energy order (highest first, ties by
   record time), so the best material lands on the loudest music; entries
   are re-sorted by record time afterwards.
4. Reuse rules: on the first pass every pool moment serves exactly one
   slot, consuming a slot-length piece from its start. Once the pool runs
   short, unconsumed tails are sliced first — the pool is scanned cyclically
   for the next moment with unused material and the next non-overlapping
   slot-length piece is taken (a long moment thus splits into several
   pieces). Only when *no* moment has unused material left is a moment
   rewound and its footage repeated, and that is noted.
5. Each entry takes ``slot length`` seconds starting at
   ``moment.start + consumed``. If the remaining piece is shorter than the
   slot it is padded by extending toward the clip's end; if even that is
   not enough, the short piece is kept (record stays on the grid) and a
   gap is noted.

Styles
------
``plan_montage(..., style=...)`` picks a :data:`STYLES` entry. "auto"
(the default) keeps the section-energy grid described above. A named style
instead maps a story arc — (share_of_duration, phase) pairs over
opening/build/climax/outro — onto the montage duration and cuts each phase
at its own beat density. Grid points still snap to musical positions:
phase boundaries snap to the nearest phrase start (falling back to
downbeats, then beats, when phrases are unknown), slow phases
(>= 4 beats per cut) place their cuts on downbeats, fast phases walk the
beat grid; with neither beats nor downbeats the fixed 2 s grid is used,
exactly as in "auto".

Drops
-----
With a named style that has a climax phase, the climax start is aligned to
the FIRST drop: boundaries before it are scaled by ``drop / original``,
boundaries after it are scaled toward the end by
``(length - drop) / (length - original)``. Limits: only the first drop is
used, and only when it lies within 5%..95% of the montage — otherwise a
note explains why alignment was skipped. In "auto", every in-range drop
forces a cut exactly on the drop and the slot starting there is reserved
for the unused moment with the highest (highlight, score), so the impact
lands on the strongest material.

Highlights and motion matching
------------------------------
In the phase named by ``style.prefer_highlights_in`` (usually "climax")
the candidate window is re-sorted by (highlight, score) instead of the
plain pool order, so audible peaks (cheers, laughter, action) land on the
musical peak. The ordering mode (CHRONOLOGICAL / BEST_FIRST) still decides
WHICH moments are in play; these refinements — and motion matching — only
break near-ties among the next few candidates: for each slot the next
K = 4 unconsumed pool items are scored with
``0.7 * order_preference + 0.3 * motion_continuity`` where order
preference is ``1 - position / K`` (earlier in the pool = higher) and
motion continuity is the cosine similarity between the previous slot's
exit motion and the candidate's entry motion (neutral 0 unless both
vectors exceed 0.5 px). With neutral motion the earliest candidate always
wins, so behavior without motion data is unchanged.

Energy-motion matching adds ``_ENERGY_MATCH_WEIGHT x (1 - |slot_energy -
candidate_motion|)`` to the same blend: slot energy comes from the song's
sections ("auto") or the arc phase's nominal energy (:data:`_PHASE_ENERGY`),
candidate motion is the moment's mean entry/exit motion magnitude
normalised to the pool's fastest moment. Loud passages meet moving
footage, calm passages calm footage; the full weight only tips the scale
at the energy extremes (a climax slot picks the moving shot over a static
one a single order position earlier), everywhere else it just leans.

Semantic casting
----------------
:mod:`monteur.vision` can annotate moments with what is IN the picture: a
one-line ``label``, a story ``role`` (opener/build/climax/closer), a
``hero`` strength (0..1, the poster shot) and a scene-similarity ``group``.
When at least one pool moment carries a role, hero or group, the slot
filling reads them — always as mild bonuses on the candidate blend above,
never as hard filters; moments without annotations behave exactly as
before. A slot in an arc phase prefers the matching role (opening ->
opener, build -> build, climax -> climax, outro -> closer), and the
montage's FIRST slot prefers an opener and its LAST slot a closer in every
style; a fitting role adds :data:`_ROLE_WEIGHT` (0.2) — enough to flip one
order position, never two. Drop-slot reservation adds :data:`_HERO_WEIGHT`
(0.5) x ``hero`` to the (highlight, score) key and climax-phase candidates
get the same hero bonus, so the real hero shot wins the drop even against
slightly better motion continuity. A candidate whose group matches an
already-filled neighbouring slot loses :data:`_GROUP_PENALTY` (0.25), so
two takes of the same scene never sit back to back while an alternative
exists. Labels ride along: ``MontageEntry.label`` feeds the video clip's
``"label"`` metadata and the title-slot markers ("0.4s of black — next:
<label>"), and a plan note reports what the casting actually did (e.g.
"semantic casting: 9 of 14 slots matched to roles, hero shot on the drop").

Finishing
---------
A montage shorter than the song ends on a musical boundary:
``end_on_phrase=True`` (the default) snaps the requested length to the
nearest phrase start within ±12% (ties prefer the shorter cut; downbeats,
then beats, serve as fallbacks; the change is never allowed to exceed
12%, and a full-song montage is left alone). Styles with an outro phase
plan a 0.5 s fade-in and a fade-out of min(2 s, last outro slot) on
:class:`MontagePlan` (``fade_in`` / ``fade_out``); "auto" plans 0.5 s /
1 s. Entries in gentle phases — >= 4 beats per cut, i.e. opening/outro,
or "low" sections in "auto" — carry a dissolve INTO them:
``MontageEntry.transition`` = min(0.5 s, half the slot length), always 0
for the montage's first entry (its fade is ``fade_in``).
:func:`montage_to_timeline` publishes dissolves as clip metadata
(``"transition"`` / ``"transition_frames"``) and the fades as timeline
metadata (``"fade_in_frames"`` / ``"fade_out_frames"``) so the EDL/FCPXML
writers can carry the dissolves into Resolve. Audio fades cannot ride
along in either export format; a plan note reminds the editor to apply
the music fade in Resolve.

Repetition guard
----------------
Stretching little footage over a long song repeats material painfully
(36 clips over a 2:30 song "extrem viel wiederholt"). ``plan_montage``
merges each clip's overlapping moments, sums the deduplicated material,
and caps the montage length at ``unique_material x _REPEAT_TOLERANCE``
(1.5) when the requested length exceeds it — the end_on_phrase snap then
refines the capped length, and the strongest-window logic works from it.
A note explains the cap; ``allow_repeats=True`` (CLI ``--allow-repeats``)
disables it. The cap never lengthens a montage and never applies when
the request is already below it.

Cut-ahead lead
--------------
Editors place cuts 1-2 frames BEFORE the beat so the incoming shot is
already on screen when the beat lands — a cut exactly ON the beat reads
late. ``cut_lead`` (default ``_DEFAULT_CUT_LEAD`` = 0.04 s, ~1 frame at
25 fps; 0 disables) shifts every interior cut point earlier by that
amount after the grid is built, clamped so ordering is preserved, no
slot drops below ``_LEAD_MIN_SLOT`` (0.25 s, or its own original length
if shorter), the first cut stays at 0 and the final boundary stays at
the montage length.

No-music plans and audio modes
------------------------------
``plan_montage(reports, music=None, max_duration=...)`` plans a cut with
no song at all (ride-POV videos where the clips' own engine sound IS the
soundtrack): the grid falls back to fixed intervals per style phase
(``beats_per_cut x _PSEUDO_BEAT`` = 0.75 s — slow phases every ~3 s,
fast every ~0.75 s), with no drops/phrases/sections; ``music_path`` is
"" and ``song_duration``/``music_start`` are 0. :func:`montage_to_timeline`
takes ``audio=``: "music" (song on A1, today's behavior), "mix" (song on
A1 plus each entry's own audio on A2) or "original" (no song clip; each
entry's own audio on A1). A no-music plan only renders with "original".

SFX layer (film mode)
---------------------
``plan_montage(..., sfx=True)`` plans a sound-design layer on top of the
finished cut — for films where the effects carry the edit instead of (or
alongside) the music, e.g. ride-POV cuts with ``audio="original"``.
Monteur cannot render audio, so the deliverable is CUES: each
:class:`SfxCue` says when (``time``/``duration``), what (``kind``), what
to search for in an SFX library (``query``) and why (``note``). Placement
reads what the plan already knows:

* an **ambience** bed at 0 under the opening phase (the first
  ``_SFX_AUTO_OPENING`` = 4 s for "auto"); its query comes from the
  opening entries' vision labels ("mountain pass ambience"), falling back
  to the honest generic "outdoor ambience",
* a **riser** ENDING exactly on every act change (label changes only —
  the trailer's split build ramps inside one act and gets no riser),
  ``duration = min(2 s, prior phase / 2)``,
* an **impact** ON the climax start and ON every drop-forced cut in
  "auto",
* a **sub-drop** under every smash-to-black dip (a title slot wants a
  boom),
* **whooshes** (0.6 s, centered on the cut) on up to 3 of the fastest
  cuts, each keeping 1 s clearance from every other cue.

Density is capped at ~1 cue per ``_SFX_SECONDS_PER_CUE`` (5 s) of cut:
whooshes are dropped first, then risers (the riser INTO the climax
survives longest, then earlier act changes); ambience/impact/sub-drop are
the backbone and always stay. Cues are sorted by time and reported in the
notes. :func:`montage_to_timeline` exports each cue as a Green timeline
marker ("SFX: <kind>" / "<query> — <note>"), which the EDL/FCPXML writers
and the Resolve bridge already carry. ``sfx=False`` (the default) plans
exactly as before.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field, replace
from pathlib import PurePath

from monteur.model import AUDIO, VIDEO, Clip, Marker, Timeline, seconds_to_frames
from monteur.music import MusicAnalysis, MusicSection, best_energy_window
from monteur.sift import ClipReport, Moment

CHRONOLOGICAL = "chronological"  # keep footage order (travel/event films)
BEST_FIRST = "best_first"  # strongest material on the strongest sections

# How many beats between cuts, per section energy label.
BEATS_PER_CUT = {"high": 1, "mid": 2, "low": 4}
# Anti-strobe floor: a cut interval below this doubles the beat step.
MIN_CUT_INTERVAL = 0.4
# Grid interval used when the song has no detected beats.
FALLBACK_INTERVAL = 2.0

# A phase cutting every >= this many beats is "slow": its cuts go on downbeats.
_SLOW_PHASE_STEP = 4
# Downbeat detection assumes 4/4; slow phases cut every (step / this) downbeats.
_BEATS_PER_BAR = 4
# Repetition guard: a montage longer than (unique material x this factor)
# repeats footage too visibly and is capped (unless allow_repeats=True).
_REPEAT_TOLERANCE = 1.5
# No-music plans have no beat grid; each phase cuts on a fixed interval of
# (beats_per_cut x this nominal pseudo-beat) seconds — slow phases every ~3s,
# fast phases every ~0.75s.
_PSEUDO_BEAT = 0.75
# Cut-ahead lead (seconds, ~1 frame at 25 fps): interior cuts are shifted this
# far BEFORE the beat so the incoming shot is on screen when the beat lands.
_DEFAULT_CUT_LEAD = 0.04
# Lead shifting never squeezes a slot below this (seconds).
_LEAD_MIN_SLOT = 0.25
# Audio modes for montage_to_timeline.
_AUDIO_MODES = ("music", "mix", "original")
# Transition modes for plan_montage: how clips hand over to each other.
# "auto" = the style's own habits (gentle-phase dissolves; the trailer
# smashes to black), "cuts" = hard cuts only, "dissolves" = dissolve on
# every cut, "smash" = black title-slot gaps at act/section changes.
TRANSITION_MODES = ("auto", "cuts", "dissolves", "smash")
# Canvas presets for montage_to_timeline: shape x resolution.
CANVASES: dict[str, tuple[int, int]] = {
    "hd": (1920, 1080),  # 16:9 in HD
    "uhd": (3840, 2160),  # 16:9 in 4K
    "vertical": (1080, 1920),  # Shorts / Reels / TikTok 9:16 in HD
    "vertical-uhd": (2160, 3840),  # 9:16 in 4K
    "cine": (1920, 804),  # 2.39:1 cinemascope in HD
    "cine-uhd": (3840, 1608),  # 2.39:1 in 4K
}
# Drop alignment only when the drop falls inside this share of the montage.
_DROP_ALIGN_MARGIN = 0.05
# Candidate window (K): unconsumed pool items considered per slot.
_CANDIDATE_WINDOW = 4
# Blend weights for near-tie breaking among the candidate window.
_ORDER_WEIGHT = 0.7
_MOTION_WEIGHT = 0.3
# Below this magnitude (px) a motion vector counts as "no motion" (neutral).
_MOTION_MIN_MAGNITUDE = 0.5
# Semantic casting (vision annotations on moments; see the module docstring).
# The bonuses are sized against the candidate blend above: one order-
# preference step is _ORDER_WEIGHT / _CANDIDATE_WINDOW = 0.175 and the
# motion term peaks at ±_MOTION_WEIGHT = 0.3.
# A candidate whose role fits the slot (its arc phase, or the montage's
# first/last slot) gains this much: flips ONE order position, never two —
# a mild preference, not a filter.
# Energy-motion matching: a slot's music energy should meet footage with
# matching motion — loud passages get moving shots, calm passages calm ones.
# Sized like _ROLE_WEIGHT: enough to flip ONE order position, never two.
_ENERGY_MATCH_WEIGHT = 0.2
# Nominal music energy per arc phase (arc styles have no section data).
_PHASE_ENERGY = {"opening": 0.35, "build": 0.65, "climax": 1.0, "outro": 0.3}
_ROLE_WEIGHT = 0.2
# Hero bonus: this x moment.hero on drop-reserved and climax-phase slots.
# A full hero (1.0) outweighs the motion term plus one order step, so the
# real hero shot wins the drop even against better motion continuity.
_HERO_WEIGHT = 0.5
# A candidate whose scene group matches a neighbouring filled slot loses
# this much — two takes of the same scene back to back read like a jump
# cut; an alternative one order step behind wins instead.
_GROUP_PENALTY = 0.25
# A drop-slot moment at/above this hero level is called out in the notes.
_HERO_NOTE_LEVEL = 0.5
# Which vision role each arc phase asks for.
_ROLE_FOR_PHASE = {
    "opening": "opener",
    "build": "build",
    "climax": "climax",
    "outro": "closer",
}
# Musical ending: max relative change when snapping the length to a phrase.
_END_SNAP_TOLERANCE = 0.12
# Dissolve INTO a gentle-phase entry: min(this, half the slot length).
_MAX_DISSOLVE = 0.5
# Planned fades (seconds) for styles with an outro phase / for "auto".
_FADE_IN = 0.5
_MAX_FADE_OUT = 2.0
_AUTO_FADE_OUT = 1.0
# Smash to black: black-gap length at act changes, and the minimum slot
# length the shortened outgoing clip must keep.
_DIP_SECONDS = 0.4
_DIP_MIN_REMAINDER = 0.25

# SFX layer (plan_montage(..., sfx=True)) — see the module docstring.
# Density cap: at most ~one cue per this many seconds of cut, so the plan
# never drowns in cues. Whooshes are dropped first, then risers; ambience,
# impacts and sub-drops are the backbone and always survive.
_SFX_SECONDS_PER_CUE = 5.0
# Riser length: min(this, half the phase it builds out of) — it must grow
# out of the prior act, not drown it.
_SFX_RISER_MAX = 2.0
# Impact hits ring out about this long (a length suggestion for the search,
# not a trim instruction).
_SFX_IMPACT_LENGTH = 1.0
# Whoosh length, centered on its cut, and how many at most (the montage's
# fastest cuts get them).
_SFX_WHOOSH_LENGTH = 0.6
_SFX_MAX_WHOOSHES = 3
# A whoosh keeps this much clearance (seconds) from every other cue, so two
# effects never pile onto the same moment.
_SFX_WHOOSH_CLEARANCE = 1.0
# "auto" has no opening phase; the ambience bed covers this many seconds.
_SFX_AUTO_OPENING = 4.0
# Label words too generic to search an SFX library with.
_SFX_STOPWORDS = frozenset(
    "the a an and of in on at to with into over under from through".split()
)

_EPS = 1e-6


@dataclass(frozen=True)
class MontageStyle:
    """An editorial cutting style: a story arc mapped onto the song."""

    key: str
    name: str
    description: str  # one line an editor understands
    # (share_of_duration, phase label "opening"/"build"/"climax"/"outro").
    # Empty arc = section-energy-driven ("auto"). A label may repeat in
    # consecutive entries; the beat step then ramps toward the next phase's
    # step ("trailer" uses this to accelerate through its split build).
    arc: list[tuple[float, str]]
    beats_per_cut: dict[str, int]  # phase label -> beats between cuts
    prefer_highlights_in: str = "climax"  # phase where highlights win slots
    # Smash to black: act changes cut to a short black gap (a title slot)
    # instead of running clip-to-clip — the classic trailer breath.
    smash_to_black: bool = False


_ARC_STANDARD = [(0.15, "opening"), (0.35, "build"), (0.35, "climax"), (0.15, "outro")]

STYLES: dict[str, MontageStyle] = {
    "auto": MontageStyle(
        key="auto",
        name="Auto (section energy)",
        description=(
            "Follows the song's own energy: calm sections cut every 4 beats, mid "
            "every 2, loud every beat; a drop forces a cut with the strongest moment."
        ),
        arc=[],
        beats_per_cut={},
    ),
    "travel": MontageStyle(
        key="travel",
        name="Travel film",
        description=(
            "Scenic slow opening, steady build, beat-for-beat climax, calm outro "
            "(4/2/1/4 beats per cut over a 15/35/35/15 arc)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 4, "build": 2, "climax": 1, "outro": 4},
    ),
    "wedding": MontageStyle(
        key="wedding",
        name="Wedding film",
        description=(
            "Gentle throughout — never faster than every 2 beats, so faces and "
            "gestures get room to breathe (4/2/2/4)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 4, "build": 2, "climax": 2, "outro": 4},
    ),
    "music_video": MontageStyle(
        key="music_video",
        name="Music video",
        description=(
            "Fast throughout — cuts every 1-2 beats from the first bar for "
            "constant energy (2/1/1/2)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 2, "build": 1, "climax": 1, "outro": 2},
    ),
    "trailer": MontageStyle(
        key="trailer",
        name="Trailer",
        description=(
            "Long tease, accelerating build (every 2 beats, then every beat), "
            "hard climax, snap outro (20/50/20/10 arc)."
        ),
        # The build is split in half so the beat step can ramp 2 -> 1.
        arc=[(0.2, "opening"), (0.25, "build"), (0.25, "build"), (0.2, "climax"), (0.1, "outro")],
        beats_per_cut={"opening": 4, "build": 2, "climax": 1, "outro": 4},
        smash_to_black=True,
    ),
}


@dataclass
class MontagePlan:
    """The chosen cut points before rendering to a timeline."""

    music_path: str
    duration: float  # seconds, montage length (may be shorter than the song)
    music_start: float = 0.0  # seconds into the song where the cut begins
    song_duration: float = 0.0  # seconds, full length of the source song (0 = unknown)
    fade_in: float = 0.0  # seconds, intended music/video fade-in
    fade_out: float = 0.0  # seconds, intended music/video fade-out
    entries: list["MontageEntry"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # (start, length) of black gaps on V1 (smash-to-black title slots).
    dips: list[tuple[float, float]] = field(default_factory=list)
    # Planned sound-design cues (plan_montage(..., sfx=True); empty otherwise).
    sfx: list["SfxCue"] = field(default_factory=list)


@dataclass
class MontageEntry:
    clip_path: str
    source_start: float  # seconds in the clip (file-relative, 0-based)
    source_end: float
    record_start: float  # seconds in the montage
    record_end: float
    score: float
    transition: float = 0.0  # seconds of dissolve INTO this entry (0 = cut)
    media_start: float = 0.0  # seconds: the file's embedded start timecode (0 if none)
    clip_duration: float = 0.0  # seconds: the source file's real duration (0 if unknown)
    label: str = ""  # one-line vision label of the chosen moment ("" if unseen)


@dataclass
class SfxCue:
    """One planned sound-design cue — Monteur plans it, the editor drops it in.

    Monteur cannot render audio, so the deliverable is the CUE: when the
    effect goes, what kind it is, what to type into an SFX library (the
    ``query`` pastes straight into Artlist & co.) and why it is there.
    """

    time: float        # seconds in the cut
    duration: float    # suggested length of the effect
    kind: str          # "riser" | "impact" | "whoosh" | "sub-drop" | "ambience"
    query: str         # ready-to-paste SFX search terms ("whoosh transition fast")
    note: str          # one line WHY this cue is here ("act change into climax")


# --- grid -------------------------------------------------------------------


def _mmss(seconds: float) -> str:
    """Format a position as M:SS (e.g. 61.0 -> "1:01")."""
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _window_music(music: MusicAnalysis, start: float, length: float) -> MusicAnalysis:
    """A view of ``music`` over ``[start, start + length]`` in montage time.

    Every musical position (beats, downbeats, phrases, drops) is shifted by
    ``-start`` and clipped to the window; sections are cropped to the window,
    shifted, and re-tiled so they still cover ``[0, length]`` exactly. The
    montage grid can then be built as usual — in montage-relative time — while
    only the song's strongest passage is in play.
    """
    end = start + length

    def shift(times: list[float]) -> list[float]:
        return [t - start for t in sorted(times) if start - _EPS <= t <= end + _EPS]

    sections: list[MusicSection] = []
    for s in music.sections:
        lo = max(s.start, start)
        hi = min(s.end, end)
        if hi - lo > _EPS:
            sections.append(MusicSection(lo - start, hi - start, s.energy, s.label))
    if sections:  # guarantee exact tiling of [0, length]
        sections[0].start = 0.0
        sections[-1].end = length
        for prev, nxt in zip(sections, sections[1:]):
            nxt.start = prev.end

    return MusicAnalysis(
        path=music.path,
        duration=length,
        tempo=music.tempo,
        beats=shift(music.beats),
        sections=sections,
        downbeats=shift(music.downbeats),
        phrases=shift(music.phrases),
        drops=shift(music.drops),
    )


def _label_at(sections: list[MusicSection], t: float) -> str:
    """Energy label of the section containing ``t`` ("mid" if uncovered)."""
    for s in sections:
        if s.start - _EPS <= t < s.end - _EPS:
            return s.label
    if sections and t >= sections[-1].end - _EPS:
        return sections[-1].label
    return "mid"


def _energy_at(sections: list[MusicSection], t: float) -> float:
    """Energy value of the section containing ``t`` (0.5 if uncovered)."""
    for s in sections:
        if s.start - _EPS <= t < s.end - _EPS:
            return s.energy
    if sections and t >= sections[-1].end - _EPS:
        return sections[-1].energy
    return 0.5


def _nth_beat_after(beats: list[float], t: float, n: int) -> float | None:
    """The n-th beat strictly after ``t``, or None if beats run out."""
    i = bisect.bisect_right(beats, t + _EPS)
    j = i + n - 1
    return beats[j] if j < len(beats) else None


def _build_grid(
    music: MusicAnalysis,
    length: float,
    steps: dict[str, int] | None = None,
) -> tuple[list[float], list[str]]:
    """Cut times ``[0, ..., length]`` walked on the beat grid.

    ``steps`` overrides :data:`BEATS_PER_CUT` (used by the pace control).
    """
    lookup = steps or BEATS_PER_CUT
    notes: list[str] = []
    cuts = [0.0]
    beats = sorted(b for b in music.beats if b > _EPS or abs(b) <= _EPS)
    if not beats:
        notes.append(
            f"no beats detected; falling back to a fixed {FALLBACK_INTERVAL:g}s grid"
        )
        t = FALLBACK_INTERVAL
        while t < length - _EPS:
            cuts.append(t)
            t += FALLBACK_INTERVAL
    else:
        cur = 0.0
        while True:
            step = lookup.get(_label_at(music.sections, cur), 2)
            nxt = _nth_beat_after(beats, cur, step)
            # Anti-strobe: double the beat step until the interval is sane.
            while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                step *= 2
                nxt = _nth_beat_after(beats, cur, step)
            if nxt is None or nxt >= length - _EPS:
                break  # beats ran out or past the end: close at `length`
            cuts.append(nxt)
            cur = nxt
    cuts.append(length)
    return cuts, notes


def _nearest(points: list[float], t: float) -> float:
    """Nearest value in a sorted, non-empty list (ties go to the earlier one)."""
    i = bisect.bisect_left(points, t)
    if i <= 0:
        return points[0]
    if i >= len(points):
        return points[-1]
    before, after = points[i - 1], points[i]
    return before if t - before <= after - t else after


def _snap_ending_length(music: MusicAnalysis, length: float) -> tuple[float | None, str]:
    """Musical boundary to end a truncated montage on, or (None, "").

    Looks for the boundary nearest to ``length`` — phrase starts first,
    falling back to downbeats, then beats — but only within
    ±``_END_SNAP_TOLERANCE`` (12%) of the requested length; equidistant
    candidates prefer the shorter montage. Returns (None, "") when no
    boundary qualifies or the nearest one IS the requested length (no
    change needed). The returned time never exceeds the song duration.
    """
    tolerance = _END_SNAP_TOLERANCE * length
    for cand, kind in (
        (music.phrases, "phrase"),
        (music.downbeats, "downbeat"),
        (music.beats, "beat"),
    ):
        pts = sorted(p for p in cand if _EPS < p <= music.duration + _EPS)
        if not pts:
            continue
        i = bisect.bisect_left(pts, length)
        neighbours = ([pts[i - 1]] if i > 0 else []) + ([pts[i]] if i < len(pts) else [])
        best: float | None = None
        for p in neighbours:  # shorter first: a tie keeps the shorter cut
            d = abs(p - length)
            if d <= tolerance + _EPS and (best is None or d < abs(best - length) - _EPS):
                best = p
        if best is not None:
            if abs(best - length) <= _EPS:
                return None, ""  # already on a boundary
            return best, kind
    return None, ""


def _phase_steps(style: MontageStyle) -> list[int]:
    """Beats-per-cut for every arc entry.

    A run of consecutive arc entries with the same label ramps linearly from
    that label's own step to the FOLLOWING phase's step — "trailer" uses this
    to accelerate through its split build (every 2 beats, then every beat).
    """
    labels = [lab for _, lab in style.arc]
    steps: list[int] = []
    i = 0
    while i < len(labels):
        j = i
        while j + 1 < len(labels) and labels[j + 1] == labels[i]:
            j += 1
        own = style.beats_per_cut.get(labels[i], 2)
        if j > i:
            nxt = style.beats_per_cut.get(labels[j + 1], own) if j + 1 < len(labels) else own
            span = j - i
            for r in range(span + 1):
                steps.append(max(1, round(own + (nxt - own) * r / span)))
        else:
            steps.append(own)
        i = j + 1
    return steps


def _build_style_grid(
    music: MusicAnalysis, length: float, style: MontageStyle
) -> tuple[list[float], list[tuple[float, float, str]], list[str]]:
    """Cut grid and phase spans ``(start, end, label)`` for a named style.

    Phase boundaries are the arc shares mapped onto ``length``, snapped to
    the nearest phrase start (falling back to downbeats, then beats). If the
    song has drops and the arc has a climax, the climax start is pinned to
    the first drop and the neighbouring boundaries are scaled proportionally
    (limits: first drop only, and only when it lies within 5%..95% of the
    montage — otherwise a note explains the skip). Slow phases
    (>= ``_SLOW_PHASE_STEP`` beats per cut) cut on downbeats, fast phases
    walk the beat grid with the phase's step; with neither beats nor
    downbeats the fixed 2 s fallback grid is used, exactly as in "auto".
    """
    notes: list[str] = []
    labels = [lab for _, lab in style.arc]
    total_share = sum(share for share, _ in style.arc) or 1.0
    bounds = [0.0]
    acc = 0.0
    for share, _ in style.arc:
        acc += share
        bounds.append(length * acc / total_share)
    bounds[-1] = length

    # Drop = climax: pin the climax start to the first drop.
    pinned: set[int] = set()
    drops = sorted(d for d in music.drops)
    if drops and "climax" in labels:
        drop = drops[0]
        climax_i = labels.index("climax")
        orig = bounds[climax_i]
        if not (_DROP_ALIGN_MARGIN * length <= drop <= (1 - _DROP_ALIGN_MARGIN) * length):
            notes.append(
                f"drop at {drop:.1f}s outside 5-95% of the montage; climax not aligned"
            )
        elif climax_i == 0 or orig <= _EPS or orig >= length - _EPS:
            notes.append("climax phase starts at the montage edge; drop alignment skipped")
        else:
            for i in range(1, climax_i):
                bounds[i] *= drop / orig
            bounds[climax_i] = drop
            for i in range(climax_i + 1, len(bounds) - 1):
                bounds[i] = length - (length - bounds[i]) * (length - drop) / (length - orig)
            pinned.add(climax_i)
            notes.append(f"climax aligned to drop at {drop:.1f}s")

    # Snap the remaining interior boundaries to musical positions:
    # phrases, else downbeats, else beats.
    snap_points: list[float] = []
    snapped_to = ""
    for cand, kind in (
        (music.phrases, "phrase starts"),
        (music.downbeats, "downbeats"),
        (music.beats, "beats"),
    ):
        pts = sorted(p for p in cand if _EPS < p < length - _EPS)
        if pts:
            snap_points, snapped_to = pts, kind
            break
    snapped = 0
    for i in range(1, len(bounds) - 1):
        if i in pinned or not snap_points:
            continue
        bounds[i] = _nearest(snap_points, bounds[i])
        snapped += 1
    for i in range(1, len(bounds)):  # keep boundaries monotonic
        bounds[i] = min(max(bounds[i], bounds[i - 1]), length)

    phases = [(bounds[i], bounds[i + 1], labels[i]) for i in range(len(labels))]

    beats = sorted(b for b in music.beats if b > -_EPS)
    downs = sorted(d for d in music.downbeats if d > -_EPS)
    pulse = beats or downs  # graceful: no beats -> walk downbeats instead
    cuts = [0.0]
    downbeat_cuts = 0
    if not pulse:
        notes.append(
            f"no beats detected; falling back to a fixed {FALLBACK_INTERVAL:g}s grid"
        )
        t = FALLBACK_INTERVAL
        while t < length - _EPS:
            cuts.append(t)
            t += FALLBACK_INTERVAL
    else:
        for (p_start, p_end, _label), step in zip(phases, _phase_steps(style)):
            cur = cuts[-1]
            slow = step >= _SLOW_PHASE_STEP and bool(downs)
            while True:
                if slow:  # slow phase: cut on downbeats (every bar-multiple)
                    n = max(1, round(step / _BEATS_PER_BAR))
                    nxt = _nth_beat_after(downs, cur, n)
                    while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                        n += 1  # anti-strobe: skip to a later downbeat
                        nxt = _nth_beat_after(downs, cur, n)
                else:  # fast phase: walk the beat grid
                    s = step
                    nxt = _nth_beat_after(pulse, cur, s)
                    while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                        s *= 2
                        nxt = _nth_beat_after(pulse, cur, s)
                if nxt is None or nxt >= p_end - _EPS:
                    break
                cuts.append(nxt)
                if slow:
                    downbeat_cuts += 1
                cur = nxt
            if p_end < length - _EPS and p_end > cuts[-1] + _EPS:
                cuts.append(p_end)  # the phase boundary itself is a cut
    cuts.append(length)

    if snapped and snapped_to:
        notes.append(f"{snapped} phase boundaries snapped to {snapped_to}")
    if downbeat_cuts:
        notes.append(f"{downbeat_cuts} cuts on downbeats")
    return cuts, phases, notes


def _build_pseudo_grid(
    length: float,
    style: MontageStyle,
    auto_steps: dict[str, int] | None = None,
) -> tuple[list[float], list[tuple[float, float, str]], list[str]]:
    """Cut grid and phase spans for a NO-MUSIC plan (fixed intervals).

    With no song there is no beat grid to walk, so each arc phase cuts on a
    fixed interval of ``beats_per_cut x _PSEUDO_BEAT`` (0.75 s) seconds —
    slow phases (4 beats per cut) every ~3 s, fast phases every ~0.75 s.
    Phase boundaries are the raw arc shares mapped onto ``length`` (nothing
    musical to snap to). "auto" has no arc; it cuts on a flat "mid" interval
    (2 x _PSEUDO_BEAT = 1.5 s), or on the paced "high" step when
    ``auto_steps`` is given (see :func:`_apply_pace`).
    """
    notes = [f"no music: fixed intervals from a {_PSEUDO_BEAT:g}s pseudo-beat"]
    cuts = [0.0]
    phases: list[tuple[float, float, str]] = []
    if style.arc:
        labels = [lab for _, lab in style.arc]
        total_share = sum(share for share, _ in style.arc) or 1.0
        bounds = [0.0]
        acc = 0.0
        for share, _ in style.arc:
            acc += share
            bounds.append(length * acc / total_share)
        bounds[-1] = length
        phases = [(bounds[i], bounds[i + 1], labels[i]) for i in range(len(labels))]
        for (p_start, p_end, _label), step in zip(phases, _phase_steps(style)):
            interval = max(step * _PSEUDO_BEAT, MIN_CUT_INTERVAL)
            t = cuts[-1] + interval
            while t < p_end - _EPS:
                cuts.append(t)
                t += interval
            if p_end < length - _EPS and p_end > cuts[-1] + _EPS:
                cuts.append(p_end)  # the phase boundary itself is a cut
    else:
        # "auto" cuts on one flat interval: the "mid" default, but the paced
        # "high" step when a pace is set — with a single interval, the pace
        # IS the interval (rounded to whole pseudo-beats).
        step = auto_steps["high"] if auto_steps else BEATS_PER_CUT["mid"]
        interval = max(step * _PSEUDO_BEAT, MIN_CUT_INTERVAL)
        t = interval
        while t < length - _EPS:
            cuts.append(t)
            t += interval
    cuts.append(length)
    return cuts, phases, notes


def _pulse_interval(music: MusicAnalysis) -> float:
    """Seconds per beat: median beat spacing, else 60/tempo, else the pseudo-beat."""
    beats = sorted(b for b in music.beats if b > -_EPS)
    if len(beats) >= 2:
        gaps = sorted(b - a for a, b in zip(beats, beats[1:]) if b - a > _EPS)
        if gaps:
            return gaps[len(gaps) // 2]
    if music.tempo > _EPS:
        return 60.0 / music.tempo
    return _PSEUDO_BEAT


def _apply_pace(
    style: MontageStyle, pace: float, beat: float
) -> tuple[MontageStyle, dict[str, int], str]:
    """Scale a style's cutting speed to ``pace`` seconds per clip.

    ``pace`` is the approximate clip length the FASTEST phase should cut at;
    slower phases keep their proportion to it. Returns the adjusted style,
    the adjusted "auto" step table (for arc-less styles) and a plan note.
    The requested pace is rounded to whole beats (minimum one), so the
    realized interval follows the music, not the literal number.
    """
    desired = max(1, round(pace / beat))
    steps = {k: max(1, round(v * desired)) for k, v in BEATS_PER_CUT.items()}
    if style.beats_per_cut:
        base = min(style.beats_per_cut.values())
        factor = desired / max(1, base)
        style = replace(
            style,
            beats_per_cut={
                k: max(1, round(v * factor)) for k, v in style.beats_per_cut.items()
            },
        )
    note = (
        f"cut pace ~{pace:g}s: fastest cuts every {desired} "
        f"beat{'s' if desired != 1 else ''} (~{desired * beat:.1f}s)"
    )
    return style, steps, note


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/touching [start, end] intervals (sorted result).

    Used by the repetition guard so moments that overlap WITHIN one clip are
    not double-counted as unique material.
    """
    merged: list[tuple[float, float]] = []
    for start, end in sorted((s, e) for s, e in intervals if e - s > _EPS):
        if merged and start <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _unique_material(reports: list[ClipReport]) -> float:
    """Total seconds of deduplicated moment material across all reports.

    Each clip's moment intervals are merged first (moments can overlap within
    a clip), then the merged spans are summed.
    """
    total = 0.0
    for report in reports:
        for start, end in _merge_intervals([(m.start, m.end) for m in report.moments]):
            total += end - start
    return total


def _apply_cut_lead(cuts: list[float], lead: float) -> list[float]:
    """Shift every INTERIOR cut point ``lead`` seconds earlier.

    Editors place cuts 1-2 frames before the beat so the incoming shot is
    already on screen when the beat lands. The first cut stays at 0 and the
    final boundary stays at the montage length; each shifted cut is clamped
    so ordering is preserved and no slot is squeezed below
    ``min(_LEAD_MIN_SLOT, its original length)``.
    """
    if lead <= _EPS or len(cuts) <= 2:
        return list(cuts)
    shifted = [cuts[0]]
    for i in range(1, len(cuts) - 1):
        floor = shifted[-1] + min(_LEAD_MIN_SLOT, cuts[i] - cuts[i - 1])
        shifted.append(min(max(cuts[i] - lead, floor), cuts[i]))
    shifted.append(cuts[-1])
    return shifted


# --- slot filling -------------------------------------------------------------


@dataclass
class _PoolItem:
    clip_path: str
    clip_duration: float
    moment: Moment
    media_start: float = 0.0  # seconds: the file's embedded start timecode
    consumed: float = 0.0  # seconds of the moment already placed
    uses: int = 0

    @property
    def remaining(self) -> float:
        return self.moment.end - (self.moment.start + self.consumed)

    # Vision annotations (see monteur.vision). getattr keeps Moment objects
    # from before the vision fields existed working: the defaults mean "not
    # seen", which disables all semantic casting for that moment.

    @property
    def role(self) -> str:
        return getattr(self.moment, "role", "")

    @property
    def hero(self) -> float:
        return getattr(self.moment, "hero", 0.0)

    @property
    def group(self) -> str:
        return getattr(self.moment, "group", "")

    @property
    def label(self) -> str:
        return getattr(self.moment, "label", "")


def _pick_reuse(
    pool: list[_PoolItem], start: int, held: set[int] | frozenset[int] = frozenset()
) -> _PoolItem | None:
    """First pool item (cyclic scan from ``start``) with unconsumed material.

    Indices in ``held`` (reserved for a not-yet-served drop slot) are skipped
    so their material stays fresh for the drop.
    """
    n = len(pool)
    for k in range(n):
        idx = (start + k) % n
        if idx in held:
            continue
        item = pool[idx]
        if item.remaining > _EPS:
            return item
    return None


def _motion_continuity(
    prev_exit: tuple[float, float] | None, entry: tuple[float, float]
) -> float:
    """Cosine similarity between exit and entry motion, in [-1, 1].

    Neutral 0 when there is no previous entry or either vector's magnitude
    is at or below ``_MOTION_MIN_MAGNITUDE`` px (i.e. effectively static).
    """
    if prev_exit is None:
        return 0.0
    ax, ay = prev_exit
    bx, by = entry
    mag_a = math.hypot(ax, ay)
    mag_b = math.hypot(bx, by)
    if mag_a <= _MOTION_MIN_MAGNITUDE or mag_b <= _MOTION_MIN_MAGNITUDE:
        return 0.0
    return (ax * bx + ay * by) / (mag_a * mag_b)


def _phase_label_at(phases: list[tuple[float, float, str]], t: float) -> str | None:
    """Phase label of the arc phase containing ``t`` (None if no phases)."""
    for start, end, label in phases:
        if start - _EPS <= t < end - _EPS:
            return label
    if phases and t >= phases[-1][1] - _EPS:
        return phases[-1][2]
    return None


def _wanted_roles(
    slot_idx: int,
    n_slots: int,
    phases: list[tuple[float, float, str]] | None,
    rec_start: float,
) -> set[str]:
    """Vision roles a slot asks for.

    The slot's arc phase maps through :data:`_ROLE_FOR_PHASE`; on top of
    that the montage's FIRST slot always asks for an "opener" and its LAST
    slot for a "closer", in every style (also the arc-less "auto").
    """
    wanted: set[str] = set()
    if slot_idx == 0:
        wanted.add("opener")
    if slot_idx == n_slots - 1:
        wanted.add("closer")
    if phases:
        role = _ROLE_FOR_PHASE.get(_phase_label_at(phases, rec_start) or "")
        if role:
            wanted.add(role)
    return wanted


def _fill(
    slots: list[tuple[float, float]],
    slot_order: list[int],
    pool: list[_PoolItem],
    phases: list[tuple[float, float, str]] | None = None,
    highlight_phase: str | None = None,
    drop_slots: set[int] | frozenset[int] = frozenset(),
    semantic: bool = False,
    slot_energies: list[float] | None = None,
) -> tuple[list[MontageEntry], list[str]]:
    """Assign pool moments to slots.

    The first pass still consumes every pool moment exactly once, in pool
    order — the ordering mode decides WHICH moments are in play — with two
    craft refinements that only reorder the next few candidates:

    * Drop slots are reserved up front for the unused moment with the
      highest (highlight, score), so the drop hits the strongest material.
    * For every other slot the next ``_CANDIDATE_WINDOW`` (K = 4) unconsumed
      pool items compete. Inside ``highlight_phase`` they are first re-sorted
      by (highlight, score) so audible peaks win the musical peak. The pick
      maximises ``0.7 * order_preference + 0.3 * motion_continuity`` where
      order preference is ``1 - position / K`` (earlier = higher) and motion
      continuity is the cosine similarity between the previous slot's exit
      motion and the candidate's entry motion (see
      :func:`_motion_continuity`). With neutral motion the earliest
      candidate always wins, so behavior without motion data is unchanged.

    ``slot_energies`` (one value 0..1 per slot, from the song's sections or
    the arc's nominal phase energy) adds an energy-motion matching term to
    the blend: ``_ENERGY_MATCH_WEIGHT x (1 - |slot_energy - motion|)`` where
    motion is the candidate's mean entry/exit motion magnitude normalised to
    the pool's fastest moment. Loud passages meet moving footage, calm
    passages calm footage. With an all-static pool the term is equal for
    every candidate, so behavior without motion data is unchanged.

    Reuse (pool exhausted) is unchanged — cyclic scan for unconsumed tails,
    then rewind — except a drop slot still grabs the best remaining material.

    ``semantic=True`` (any pool moment carries vision annotations) layers
    the semantic-casting bonuses onto the candidate blend: a fitting role
    adds ``_ROLE_WEIGHT``, climax-phase candidates add ``_HERO_WEIGHT`` x
    hero (drop reservation weighs hero the same way), and a candidate whose
    scene group matches an already-filled neighbouring slot loses
    ``_GROUP_PENALTY`` (see the module docstring). A note reports what the
    casting actually changed. With all-default annotations every bonus is
    zero, so behavior is exactly the unannotated one.
    """
    entries: list[MontageEntry] = []
    notes: list[str] = []
    n = len(pool)
    # Energy-motion matching: candidate motion magnitudes normalised to the
    # pool's fastest moment (empty = all static = term disabled).
    motion_norm: list[float] = []
    if slot_energies is not None:
        mags = [
            (math.hypot(*it.moment.entry_motion) + math.hypot(*it.moment.exit_motion)) / 2.0
            for it in pool
        ]
        peak = max(mags, default=0.0)
        if peak > _EPS:
            motion_norm = [m / peak for m in mags]
    rewound = False
    unused = list(range(n))  # pool indices not yet placed, in pool order
    reserved: dict[int, int] = {}  # slot index -> pool index held for a drop
    for drop_slot in sorted(drop_slots):
        if not unused:
            break
        pos = max(
            range(len(unused)),
            key=lambda p: (
                # Hero shots belong on the drop: hero weighs in next to the
                # audio highlight (identical to before when hero is 0).
                pool[unused[p]].moment.highlight + _HERO_WEIGHT * pool[unused[p]].hero,
                pool[unused[p]].moment.score,
                -unused[p],  # ties: earliest in pool order
            ),
        )
        reserved[drop_slot] = unused.pop(pos)
    held = set(reserved.values())  # kept out of reuse until their drop is served

    by_slot: dict[int, _PoolItem] = {}
    same_scene_avoided = 0
    for visit, slot_idx in enumerate(slot_order):
        rec_start, rec_end = slots[slot_idx]
        slot_len = rec_end - rec_start
        if slot_idx in reserved:
            item = pool[reserved[slot_idx]]  # drop slot: strongest moment
            held.discard(reserved[slot_idx])
        elif unused:
            # First pass: choose among the next K unconsumed pool items.
            window = unused[:_CANDIDATE_WINDOW]
            if (
                highlight_phase
                and phases
                and _phase_label_at(phases, rec_start) == highlight_phase
            ):
                window = sorted(
                    window,
                    key=lambda i: (-pool[i].moment.highlight, -pool[i].moment.score, i),
                )
            prev = by_slot.get(slot_idx - 1)
            prev_exit = prev.moment.exit_motion if prev is not None else None
            # Semantic casting: mild per-candidate adjustments (see the
            # module docstring). Bonuses (role fit, climax hero) and the
            # scene-variety penalty are kept apart so we can honestly note
            # when the penalty actually diverted a pick.
            sem_bonus: dict[int, float] = {}
            sem_penalty: dict[int, float] = {}
            if semantic:
                wanted = _wanted_roles(slot_idx, len(slots), phases, rec_start)
                in_climax = bool(phases) and _phase_label_at(phases, rec_start) == "climax"
                neighbour_groups = {
                    by_slot[j].group
                    for j in (slot_idx - 1, slot_idx + 1)
                    if j in by_slot and by_slot[j].group
                }
                for idx in window:
                    bonus = 0.0
                    if pool[idx].role and pool[idx].role in wanted:
                        bonus += _ROLE_WEIGHT
                    if in_climax:
                        bonus += _HERO_WEIGHT * pool[idx].hero
                    sem_bonus[idx] = bonus
                    if pool[idx].group and pool[idx].group in neighbour_groups:
                        sem_penalty[idx] = _GROUP_PENALTY

            def _blend(pos: int, idx: int) -> float:
                score = (
                    _ORDER_WEIGHT * (1.0 - pos / _CANDIDATE_WINDOW)
                    + _MOTION_WEIGHT
                    * _motion_continuity(prev_exit, pool[idx].moment.entry_motion)
                    + sem_bonus.get(idx, 0.0)
                )
                if motion_norm and slot_energies is not None:
                    score += _ENERGY_MATCH_WEIGHT * (
                        1.0 - abs(slot_energies[slot_idx] - motion_norm[idx])
                    )
                return score

            best = max(
                enumerate(window),
                key=lambda pi: _blend(*pi) - sem_penalty.get(pi[1], 0.0),
            )[1]
            if sem_penalty:
                unguarded = max(enumerate(window), key=lambda pi: _blend(*pi))[1]
                if unguarded != best:
                    same_scene_avoided += 1
            unused.remove(best)
            item = pool[best]
        else:
            item = None
            if slot_idx in drop_slots:  # late drop slot: best remaining tail
                leftovers = [
                    it for i, it in enumerate(pool) if i not in held and it.remaining > _EPS
                ]
                if leftovers:
                    item = max(
                        leftovers,
                        key=lambda it: (
                            it.moment.highlight + _HERO_WEIGHT * it.hero,
                            it.moment.score,
                        ),
                    )
            if item is None:
                item = _pick_reuse(pool, visit % n, held)
            if item is None:  # everything consumed: rewind and repeat footage
                idx = visit % n
                for k in range(n):  # don't rewind a held (drop-reserved) moment
                    if (idx + k) % n not in held:
                        idx = (idx + k) % n
                        break
                item = pool[idx]
                item.consumed = 0.0
                rewound = True
        by_slot[slot_idx] = item
        moment = item.moment
        src_start = moment.start + item.consumed
        src_end = min(src_start + slot_len, moment.end)
        if src_end - src_start < slot_len - _EPS:
            # Pad the short piece by extending toward the clip's end.
            src_end = max(src_end, min(src_start + slot_len, item.clip_duration))
        if src_end - src_start < slot_len - _EPS:
            notes.append(
                f"gap at {rec_start:.2f}s: only {src_end - src_start:.2f}s of "
                f"source for a {slot_len:.2f}s slot"
            )
        item.consumed = src_end - moment.start
        item.uses += 1
        entries.append(
            MontageEntry(
                clip_path=item.clip_path,
                source_start=src_start,
                source_end=src_end,
                record_start=rec_start,
                record_end=rec_end,
                score=moment.score,
                media_start=item.media_start,
                clip_duration=item.clip_duration,
                label=item.label,
            )
        )
    if len(slot_order) > n:
        msg = f"material ran short: {len(slot_order)} slots for {n} moments; moments reused"
        if rewound:
            msg += " (some footage repeats)"
        notes.append(msg)
    if semantic:
        pieces: list[str] = []
        if any(it.role for it in pool):
            matched = sum(
                1
                for idx, it in by_slot.items()
                if it.role and it.role in _wanted_roles(idx, len(slots), phases, slots[idx][0])
            )
            pieces.append(f"{matched} of {len(slots)} slots matched to roles")
        heroes = sum(
            1
            for idx in drop_slots
            if idx in by_slot and by_slot[idx].hero >= _HERO_NOTE_LEVEL
        )
        if heroes:
            pieces.append(
                "hero shot on the drop" if heroes == 1 else f"hero shots on {heroes} drops"
            )
        if same_scene_avoided:
            pieces.append(
                f"{same_scene_avoided} same-scene cut"
                + ("s" if same_scene_avoided != 1 else "")
                + " avoided"
            )
        if pieces:
            notes.append("semantic casting: " + ", ".join(pieces))
    return entries, notes


# --- public API ---------------------------------------------------------------


def plan_montage(
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    order: str = CHRONOLOGICAL,
    max_duration: float | None = None,
    style: str = "auto",
    end_on_phrase: bool = True,
    allow_repeats: bool = False,
    cut_lead: float = _DEFAULT_CUT_LEAD,
    pace: float | None = None,
    transitions: str = "auto",
    *,
    sfx: bool = False,
) -> MontagePlan:
    """Distribute the best moments across the song, in a cutting style.

    ``style`` selects a :data:`STYLES` entry. "auto" (the default) keeps the
    section-energy beat grid; a named style cuts on its story arc instead
    (see the module docstring for the grid, drop, highlight and motion
    rules). Unknown styles raise ValueError listing the valid ones.

    Moments annotated by :mod:`monteur.vision` (role / hero / group / label)
    steer the fill — roles gravitate to their arc phases, hero shots to the
    drop, same-scene takes apart (the module docstring's Semantic casting
    section has the weights). Unannotated moments plan exactly as before.

    ``music=None`` plans a cut with no song at all (ride-POV videos whose
    own sound is the point): ``max_duration`` is then required, the grid
    uses fixed per-phase intervals (``beats_per_cut x _PSEUDO_BEAT``; see
    :func:`_build_pseudo_grid`) and the plan carries ``music_path`` "" —
    render it with ``montage_to_timeline(..., audio="original")``.

    ``allow_repeats`` (default False) controls the repetition guard: a
    montage longer than the deduplicated moment material x
    ``_REPEAT_TOLERANCE`` (1.5) is capped to that product, with a note (see
    the module docstring). The cap runs before the phrase snap and the
    strongest-window choice, never lengthens the montage, and never applies
    when the request is already below it.

    ``pace`` (seconds, optional) sets how fast the montage cuts: it is the
    approximate clip length of the FASTEST phase, rounded to whole beats;
    slower phases scale proportionally, so the style's arc dynamics are
    kept. ``None`` (the default) keeps each style's own pacing. Values
    that are not positive raise ValueError. The anti-strobe floor
    (:data:`MIN_CUT_INTERVAL`) still applies to very small paces.

    ``transitions`` picks how clips hand over (:data:`TRANSITION_MODES`):
    ``"auto"`` (default) keeps each style's habits — dissolves in gentle
    phases, and the trailer smashes to black at act changes; ``"cuts"``
    is hard cuts only; ``"dissolves"`` dissolves on every cut;
    ``"smash"`` forces black title-slot gaps at act changes (for "auto"
    style: at the song's section changes). Unknown values raise
    ValueError listing the four.

    ``cut_lead`` (default 0.04 s, ~1 frame at 25 fps; 0 disables) shifts
    every interior cut earlier so the incoming shot lands ON the beat
    instead of starting there — cuts exactly on the beat read late (see
    :func:`_apply_cut_lead` for the clamping rules).

    ``end_on_phrase`` (default True) gives a truncated montage a musical
    ending: when the montage is shorter than the song, the length is
    snapped to the nearest phrase start (fallback: downbeats, then beats)
    within ±12% of the request — ties prefer the shorter cut, larger changes
    are never made, and a full-song montage is left alone. The plan also
    carries the intended fades (``fade_in`` / ``fade_out``) and per-entry
    dissolves for gentle phases (see the module docstring's Finishing
    section).

    ``sfx`` (keyword-only, default False) additionally plans a sound-design
    layer: ``plan.sfx`` is filled with :class:`SfxCue` entries — ambience
    under the opening, risers into act changes, impacts on the climax/drop
    cuts, sub-drops under smash-to-black dips, whooshes on the fastest cuts
    (the module docstring's SFX layer section has the exact rules and the
    density cap). False leaves ``plan.sfx`` empty and everything else
    byte-identical to before.
    """
    if style not in STYLES:
        valid = ", ".join(sorted(STYLES))
        raise ValueError(f"unknown style {style!r}; valid styles: {valid}")
    chosen = STYLES[style]
    if music is None and max_duration is None:
        raise ValueError("without music, pass max_duration")
    if pace is not None and pace <= 0:
        raise ValueError("pace must be positive (approximate seconds per clip)")
    if transitions not in TRANSITION_MODES:
        valid = ", ".join(TRANSITION_MODES)
        raise ValueError(
            f"unknown transitions {transitions!r}; valid modes: {valid}"
        )

    if music is None:
        requested = max_duration
    else:
        requested = (
            music.duration if max_duration is None else min(music.duration, max_duration)
        )

    # Repetition guard: don't stretch little footage over a long montage.
    # Runs BEFORE the phrase snap and best_energy_window so both refine the
    # capped length.
    length = requested
    repeat_note: str | None = None
    unique_material = _unique_material(reports)
    supported = unique_material * _REPEAT_TOLERANCE
    if not allow_repeats and unique_material > _EPS and requested > supported + _EPS:
        length = supported
        repeat_note = (
            f"footage supports about {supported:.0f}s — capped the cut to "
            f"{length:.0f}s (was {requested:.0f}s); pass allow_repeats=True / "
            f"--allow-repeats to use the full length"
        )

    end_note: str | None = None
    if (
        music is not None
        and end_on_phrase
        and _EPS < length < music.duration - _EPS
    ):
        snapped_length, boundary_kind = _snap_ending_length(music, length)
        if snapped_length is not None:
            end_note = f"length snapped to {boundary_kind} at {snapped_length:.1f}s"
            length = snapped_length

    # A montage cut shorter than the song uses the song's strongest passage,
    # not its intro: shift the whole grid onto [music_start, music_start+length].
    music_start = 0.0
    if music is not None and _EPS < length < music.duration - _EPS:
        music_start = best_energy_window(music, length)
    if music is None:
        grid_music = MusicAnalysis(path="", duration=max(length, 0.0), tempo=0.0)
    elif music_start > _EPS:
        grid_music = _window_music(music, music_start, length)
    else:
        grid_music = music

    plan = MontagePlan(
        music_path=music.path if music is not None else "",
        duration=max(length, 0.0),
        music_start=music_start,
        song_duration=music.duration if music is not None else 0.0,
    )
    plan.notes.append(f'style "{chosen.key}": {chosen.name}')
    if repeat_note:
        plan.notes.append(repeat_note)
    if end_note:
        plan.notes.append(end_note)
    if music_start > _EPS:
        plan.notes.append(
            f"using the song's strongest {length:.0f}s (from {_mmss(music_start)})"
        )
    if length <= _EPS:
        plan.notes.append("montage length is zero; nothing planned")
        return plan

    # Cut pace: scale every phase's beat step so the fastest phase cuts at
    # ~`pace` seconds per clip (the style's own pacing when pace is None).
    auto_steps: dict[str, int] | None = None
    if pace is not None:
        beat = _pulse_interval(grid_music) if music is not None else _PSEUDO_BEAT
        chosen, auto_steps, pace_note = _apply_pace(chosen, pace, beat)
        plan.notes.append(pace_note)

    phases: list[tuple[float, float, str]] = []
    highlight_phase: str | None = None
    drop_starts: list[float] = []
    if music is None:
        cuts, phases, grid_notes = _build_pseudo_grid(length, chosen, auto_steps)
        if chosen.arc:
            highlight_phase = chosen.prefer_highlights_in
    elif chosen.arc:
        cuts, phases, grid_notes = _build_style_grid(grid_music, length, chosen)
        highlight_phase = chosen.prefer_highlights_in
    else:
        cuts, grid_notes = _build_grid(grid_music, length, auto_steps)
        # Auto style: every in-range drop forces a cut exactly on the drop;
        # the slot starting there is reserved for the strongest moment.
        for d in sorted({d for d in grid_music.drops if _EPS < d < length - _EPS}):
            if not any(abs(c - d) <= _EPS for c in cuts):
                bisect.insort(cuts, d)
            drop_starts.append(d)
            grid_notes.append(f"cut forced at drop {d:.1f}s; strongest moment assigned")
    plan.notes.extend(grid_notes)
    # Cut-ahead lead: interior cuts move slightly BEFORE their beat so the
    # incoming shot is on screen when the beat lands. Drop-slot matching
    # below tolerates the shift (slots start cut_lead before their drop).
    cuts = _apply_cut_lead(cuts, cut_lead)
    slots = list(zip(cuts, cuts[1:]))
    drop_slots = {
        i
        for i, (s, _) in enumerate(slots)
        if any(abs(s - d) <= cut_lead + _EPS for d in drop_starts)
    }
    pool = [
        _PoolItem(r.path, r.duration, m, media_start=r.media_start)
        for r in reports
        for m in r.moments
    ]
    if not slots or not pool:
        plan.notes.append("no slots or no moments; nothing planned")
        return plan

    if order == CHRONOLOGICAL:
        pool.sort(key=lambda it: (it.clip_path, it.moment.start))
        slot_order = list(range(len(slots)))
    elif order == BEST_FIRST:
        pool.sort(key=lambda it: (-it.moment.score, it.clip_path, it.moment.start))
        slot_order = sorted(
            range(len(slots)),
            key=lambda i: (-_energy_at(grid_music.sections, slots[i][0]), slots[i][0]),
        )
    else:
        raise ValueError(f"unknown order: {order!r}")

    # Semantic casting kicks in only when the vision pass annotated at least
    # one pool moment (labels alone still ride along, but change nothing).
    semantic = any(it.role or it.hero > _EPS or it.group for it in pool)
    # Energy-motion matching: what the music does in each slot, 0..1. Arc
    # styles use the phase's nominal energy; the arc-less "auto" style reads
    # the song's sections (no-music auto plans have neither and skip it).
    slot_energies: list[float] | None = None
    if phases:
        slot_energies = [
            _PHASE_ENERGY.get(_phase_label_at(phases, s) or "", 0.5) for s, _ in slots
        ]
    elif music is not None and grid_music.sections:
        slot_energies = [_energy_at(grid_music.sections, s) for s, _ in slots]
    entries, fill_notes = _fill(
        slots, slot_order, pool, phases, highlight_phase, drop_slots,
        semantic=semantic, slot_energies=slot_energies,
    )
    entries.sort(key=lambda e: e.record_start)
    plan.entries = entries
    plan.notes.extend(fill_notes)
    used = sum(1 for it in pool if it.uses)
    plan.notes.append(f"{len(slots)} slots filled, {used} of {len(pool)} moments used")
    _plan_finishing(plan, entries, grid_music, chosen, phases, transitions)
    if sfx:
        _plan_sfx(plan, phases, drop_starts)
    return plan


def _plan_finishing(
    plan: MontagePlan,
    entries: list[MontageEntry],
    music: MusicAnalysis,
    style: MontageStyle,
    phases: list[tuple[float, float, str]],
    transitions: str = "auto",
) -> None:
    """Set the plan's fades, dissolves and smash-to-black dips (in place).

    Styles with an outro phase get ``fade_in`` = 0.5 s and ``fade_out`` =
    min(2 s, last outro slot length); "auto" gets 0.5 s / 1 s — fades
    apply in every transition mode.

    ``transitions`` = "auto": every entry in a gentle phase (>=
    ``_SLOW_PHASE_STEP`` beats per cut; "low" sections in "auto") except
    the montage's very first entry gets ``transition`` = min(0.5 s, half
    its slot length) — a dissolve INTO that entry — and a style with
    ``smash_to_black`` (the trailer) dips to black at act changes.
    "dissolves" dissolves into EVERY entry, "cuts" plans neither
    dissolves nor dips, "smash" forces the dips (at act changes; for the
    arc-less "auto" style at the song's section changes) without
    dissolves. Notes the dissolve count and reminds that the music
    fade-out must be applied in Resolve (the export formats can't carry it).
    """
    if not entries:
        return
    arc_labels = [lab for _, lab in style.arc]
    if "outro" in arc_labels:
        plan.fade_in = _FADE_IN
        last = entries[-1]
        plan.fade_out = min(_MAX_FADE_OUT, last.record_end - last.record_start)
    elif not style.arc:  # "auto"
        plan.fade_in = _FADE_IN
        plan.fade_out = _AUTO_FADE_OUT

    dissolves = 0
    for entry in entries[1:]:  # the first entry's fade is fade_in, not a dissolve
        if transitions == "dissolves":
            gentle = True
        elif transitions != "auto":
            gentle = False  # "cuts" and "smash" plan no dissolves
        elif style.arc:
            label = _phase_label_at(phases, entry.record_start)
            gentle = label is not None and style.beats_per_cut.get(label, 2) >= _SLOW_PHASE_STEP
        else:
            gentle = _label_at(music.sections, entry.record_start) == "low"
        if gentle:
            entry.transition = min(_MAX_DISSOLVE, (entry.record_end - entry.record_start) / 2.0)
            if entry.transition > _EPS:
                dissolves += 1
    if dissolves:
        plan.notes.append(
            f"{dissolves} dissolves"
            + (" in gentle phases" if transitions == "auto" else " on every cut")
        )
    if transitions == "cuts":
        plan.notes.append("transitions: hard cuts only")

    # Smash to black: at every act change, the outgoing clip gives up its
    # last _DIP_SECONDS to a black gap — the incoming act then HITS out of
    # black. Each gap is a natural title slot (exported as a marker).
    smash = transitions == "smash" or (
        transitions == "auto" and style.smash_to_black
    )
    if smash:
        if phases:
            bounds = [p_start for p_start, _, _ in phases[1:]]
        else:  # arc-less "auto": the song's section changes are the acts
            bounds = [s.start for s in music.sections[1:]]
        for bound in bounds:
            outgoing = min(
                entries, key=lambda e: abs(e.record_end - bound), default=None
            )
            if outgoing is None:
                continue
            # Tolerate the cut-lead shift; anything further off means the
            # boundary landed inside a slot, not on a cut — no dip there.
            if abs(outgoing.record_end - bound) > 0.25 + _EPS:
                continue
            slot = outgoing.record_end - outgoing.record_start
            if slot - _DIP_SECONDS < _DIP_MIN_REMAINDER:
                continue
            outgoing.record_end -= _DIP_SECONDS
            outgoing.source_end -= _DIP_SECONDS
            plan.dips.append((outgoing.record_end, _DIP_SECONDS))
        if plan.dips:
            plan.notes.append(
                f"{len(plan.dips)} smash-cuts to black at act changes "
                f"({_DIP_SECONDS:g}s each) — title slots, exported as markers"
            )

    if plan.fade_in > _EPS or plan.fade_out > _EPS:
        plan.notes.append(
            f"fades to black: {plan.fade_in:g}s in, {plan.fade_out:g}s out "
            "(in the FCPXML export; fade the music itself in Resolve)"
        )


def _ambience_query(entries: list[MontageEntry], span: float) -> str:
    """Search terms for the opening ambience bed.

    Built from the vision labels of the entries inside the opening span:
    the first two distinct meaningful words (stopwords and sub-3-letter
    words dropped) plus "ambience" — a label "over the mountain pass"
    makes "mountain pass ambience". Entries carry only the label, not the
    vision tags, so the label's own words are the honest source; without
    labels (no --see) the generic "outdoor ambience" is used.
    """
    words: list[str] = []
    for entry in entries:
        if entry.record_start >= span - _EPS:
            break
        for raw in entry.label.lower().split():
            word = raw.strip(".,!?;:()[]'\"-")
            if len(word) < 3 or word in _SFX_STOPWORDS or word in words:
                continue
            words.append(word)
            if len(words) == 2:
                return f"{words[0]} {words[1]} ambience"
    if words:
        return f"{words[0]} ambience"
    return "outdoor ambience"


def _plan_sfx(
    plan: MontagePlan,
    phases: list[tuple[float, float, str]],
    drop_starts: list[float],
) -> None:
    """Plan the sound-design cue layer onto a filled plan (in place).

    Reads only what the plan already knows — the arc ``phases``, the
    "auto" style's drop-forced cut times (``drop_starts``), the dips and
    the entries — and fills ``plan.sfx`` per the module docstring's SFX
    layer section: ambience at 0, risers ending on act changes, impacts
    on the climax start and drop cuts, sub-drops under the dips, whooshes
    centered on the fastest cuts. The density cap (~1 cue per
    ``_SFX_SECONDS_PER_CUE``) trims whooshes first, then risers
    (into-the-climax survives longest, then earlier act changes);
    ambience/impact/sub-drop cues always stay, even if the cut is so
    short they alone exceed the cap. The result is sorted by time and
    reported in the notes.
    """
    if not plan.entries or plan.duration <= _EPS:
        return
    duration = plan.duration

    # 1. Opening ambience: a bed under the first shots, sized to the opening
    #    phase (arc styles) or the first few seconds ("auto" has no phases).
    opening = phases[0][1] - phases[0][0] if phases else min(_SFX_AUTO_OPENING, duration)
    essential: list[SfxCue] = []
    if opening > _EPS:
        essential.append(
            SfxCue(
                time=0.0,
                duration=opening,
                kind="ambience",
                query=_ambience_query(plan.entries, opening),
                note="opening",
            )
        )

    # 2. Risers into act changes: a build ENDING exactly on the boundary.
    #    Only real act changes count — the trailer's split build ramps
    #    inside one act and gets no riser there.
    riser_items: list[tuple[str, SfxCue]] = []  # (incoming phase, cue)
    for (p_start, p_end, p_label), (_, _, n_label) in zip(phases, phases[1:]):
        if n_label == p_label or not (_EPS < p_end < duration - _EPS):
            continue
        length = min(_SFX_RISER_MAX, (p_end - p_start) / 2.0)
        if length <= _EPS:
            continue
        riser_items.append(
            (
                n_label,
                SfxCue(
                    time=p_end - length,
                    duration=length,
                    kind="riser",
                    query="riser build up",
                    note=f"{p_label} -> {n_label}",
                ),
            )
        )

    # 3. Impacts: ON the climax start (arc styles; when a drop pinned the
    #    climax this IS the drop) and ON every drop-forced cut in "auto".
    climax_start = next((s for s, _, lab in phases if lab == "climax"), None)
    if climax_start is not None and _EPS < climax_start < duration - _EPS:
        essential.append(
            SfxCue(
                time=climax_start,
                duration=min(_SFX_IMPACT_LENGTH, duration - climax_start),
                kind="impact",
                query="cinematic impact hit",
                note="climax start",
            )
        )
    for drop in drop_starts:
        essential.append(
            SfxCue(
                time=drop,
                duration=min(_SFX_IMPACT_LENGTH, duration - drop),
                kind="impact",
                query="cinematic impact hit",
                note="cut on the drop",
            )
        )

    # 4. Sub-drops under the smash-to-black dips: the black wants a boom,
    #    and the title (the dip IS a title slot) lands on it.
    for dip_start, dip_len in plan.dips:
        essential.append(
            SfxCue(
                time=dip_start,
                duration=dip_len,
                kind="sub-drop",
                query="sub drop boom",
                note="title slot",
            )
        )

    # Density cap: ~1 cue per _SFX_SECONDS_PER_CUE seconds of cut. Risers
    # are trimmed to the room left by the backbone (into-the-climax riser
    # first, then earlier act changes); whooshes only fill what remains.
    max_cues = max(1, math.ceil(duration / _SFX_SECONDS_PER_CUE))
    room = max_cues - len(essential)
    if len(riser_items) > room:
        riser_items.sort(key=lambda it: (it[0] != "climax", it[1].time))
        riser_items = riser_items[: max(0, room)]
    cues = essential + [cue for _, cue in riser_items]

    # 5. Whooshes on the fastest cuts (shortest slots), centered on the cut,
    #    each clear of every already-placed cue so effects never pile up.
    def _distance(cue: SfxCue, t: float) -> float:
        return max(cue.time - t, t - (cue.time + cue.duration), 0.0)

    room = min(max_cues - len(cues), _SFX_MAX_WHOOSHES)
    for entry in sorted(
        plan.entries, key=lambda e: (e.record_end - e.record_start, e.record_start)
    ):
        if room <= 0:
            break
        cut = entry.record_start
        if not (_EPS < cut < duration - _EPS):
            continue
        if any(_distance(c, cut) < _SFX_WHOOSH_CLEARANCE - _EPS for c in cues):
            continue
        cues.append(
            SfxCue(
                time=max(0.0, cut - _SFX_WHOOSH_LENGTH / 2.0),
                duration=_SFX_WHOOSH_LENGTH,
                kind="whoosh",
                query="whoosh transition fast",
                note="fast cut",
            )
        )
        room -= 1

    cues.sort(key=lambda c: c.time)
    plan.sfx = cues
    plan.notes.append(
        f"sfx layer: {len(cues)} cues planned "
        "(markers on the timeline; queries for your SFX library)"
    )


def montage_to_timeline(
    plan: MontagePlan,
    fps: float,
    name: str = "Monteur Montage",
    audio: str = "music",
    canvas: str = "hd",
) -> Timeline:
    """Render a MontagePlan as a Timeline (footage on V1, sound per ``audio``).

    ``audio`` picks what plays under the pictures:

    * ``"music"`` (default) — the song on A1, exactly as before.
    * ``"mix"`` — the song on A1 PLUS one A2 audio clip per video entry
      carrying the clip's own sound (same source range and source_name as
      the video entry), e.g. engine sound recorded straight into the clips.
    * ``"original"`` — NO song clip; each entry's own audio on A1 (the
      ride-POV mode, and the only valid mode for a no-music plan).

    Any other value raises ValueError listing the three; ``"music"``/
    ``"mix"`` raise ValueError when the plan has no ``music_path``.

    ``canvas`` picks the timeline's shape and resolution from
    :data:`CANVASES`: ``"hd"`` (default, 1920x1080) / ``"uhd"``
    (3840x2160) for 16:9, ``"vertical"`` / ``"vertical-uhd"`` for 9:16
    Shorts/Reels, ``"cine"`` / ``"cine-uhd"`` for 2.39:1 cinemascope.
    Unknown values raise ValueError listing the presets. Footage keeps
    its own aspect ratio — reframe in Resolve after import.

    A plan with ``dips`` (smash-to-black title slots) leaves black gaps on
    V1 and drops a "Title slot" marker on each gap. Entries with a vision
    ``label`` carry it as clip metadata (``"label"``); when the entry right
    after a dip has one, the marker's note names it ("0.4s of black —
    next: <label>") instead of the generic title reminder.

    Entries with a dissolve (``transition`` > 0) carry it in the video
    clip's metadata (``"transition"`` = ``"dissolve"``,
    ``"transition_frames"`` = the length in frames) so the EDL/FCPXML
    writers can emit it; the plan's fades land in ``timeline.metadata``
    as ``"fade_in_frames"`` / ``"fade_out_frames"``.

    A plan with an SFX layer (``plan.sfx``, from ``plan_montage(...,
    sfx=True)``) gets one Green marker per cue at the cue's start frame —
    name ``"SFX: <kind>"``, note ``"<query> — <note>"`` — so the planned
    sound design shows up right on the timeline in Resolve.
    """
    if audio not in _AUDIO_MODES:
        valid = ", ".join(_AUDIO_MODES)
        raise ValueError(f"unknown audio mode {audio!r}; valid modes: {valid}")
    if audio in ("music", "mix") and not plan.music_path:
        raise ValueError(
            f'plan has no music; audio mode {audio!r} needs a song — '
            'use audio="original"'
        )
    if canvas not in CANVASES:
        valid = ", ".join(sorted(CANVASES))
        raise ValueError(f"unknown canvas {canvas!r}; valid canvases: {valid}")
    width, height = CANVASES[canvas]
    timeline = Timeline(name=name, fps=fps, width=width, height=height)
    own_audio_track = {"mix": "A2", "original": "A1"}.get(audio)
    for entry in plan.entries:
        stem = PurePath(entry.clip_path).stem
        rec_in = seconds_to_frames(entry.record_start, fps)
        rec_out = seconds_to_frames(entry.record_end, fps)
        src_in = seconds_to_frames(entry.source_start, fps)
        src_len = entry.source_end - entry.source_start
        rec_len = entry.record_end - entry.record_start
        if abs(src_len - rec_len) < _EPS:
            # Keep source and record durations frame-exact together.
            src_out = src_in + (rec_out - rec_in)
        else:
            src_out = seconds_to_frames(entry.source_end, fps)
        clip = Clip(
            name=stem,
            track="V1",
            kind=VIDEO,
            source_in=src_in,
            source_out=src_out,
            record_in=rec_in,
            record_out=rec_out,
            source_name=stem,
            source_file=entry.clip_path,
        )
        # Real source metadata for the exporters: the file's embedded start
        # timecode and true duration. Resolve refuses to link media whose
        # claimed source ranges don't match the actual file, so the FCPXML/EDL
        # writers shift source positions by media_start at write time
        # (source_in/source_out stay file-relative here).
        clip.metadata["media_start_seconds"] = entry.media_start
        clip.metadata["media_duration_seconds"] = entry.clip_duration
        if entry.label:
            # The vision label travels with the clip so exports and the web
            # UI can say WHAT each cut shows, not just where it came from.
            clip.metadata["label"] = entry.label
        transition_frames = round(entry.transition * fps)
        if transition_frames > 0:
            clip.metadata["transition"] = "dissolve"
            clip.metadata["transition_frames"] = transition_frames
        timeline.clips.append(clip)
        if own_audio_track:
            # The entry's own sound (DJI Mic engine audio etc.): same source
            # range and source_name as the video entry, on A2 ("mix") or A1
            # ("original").
            timeline.clips.append(
                Clip(
                    name=stem,
                    track=own_audio_track,
                    kind=AUDIO,
                    source_in=src_in,
                    source_out=src_out,
                    record_in=rec_in,
                    record_out=rec_out,
                    source_name=stem,
                    source_file=entry.clip_path,
                    metadata={
                        "media_start_seconds": entry.media_start,
                        "media_duration_seconds": entry.clip_duration,
                    },
                )
            )
    if plan.fade_in > _EPS:
        timeline.metadata["fade_in_frames"] = seconds_to_frames(plan.fade_in, fps)
    if plan.fade_out > _EPS:
        timeline.metadata["fade_out_frames"] = seconds_to_frames(plan.fade_out, fps)
    if audio != "original":
        music_stem = PurePath(plan.music_path).stem
        duration_frames = seconds_to_frames(plan.duration, fps)
        # The music clip starts at the song offset the cut was built against,
        # so a short montage plays the song's strongest passage rather than
        # its intro.
        music_in = seconds_to_frames(plan.music_start, fps)
        # Keep the source range inside the song: if independent rounding of the
        # offset and the length would read one frame past the end, shift the
        # start back so the clip length stays exact and never over-reads the
        # media.
        if plan.song_duration > 0:
            song_end = seconds_to_frames(plan.song_duration, fps)
            if music_in + duration_frames > song_end:
                music_in = max(0, song_end - duration_frames)
        timeline.clips.append(
            Clip(
                name=music_stem,
                track="A1",
                kind=AUDIO,
                source_in=music_in,
                source_out=music_in + duration_frames,
                record_in=0,
                record_out=duration_frames,
                source_name=music_stem,
                source_file=plan.music_path,
                # Music has no embedded start timecode we can probe here, so
                # no media_start_seconds; the real song length still lets the
                # FCPXML writer claim an honest asset duration.
                metadata={"media_duration_seconds": plan.song_duration},
            )
        )
        timeline.markers.append(Marker(frame=0, name=f"Cut to {music_stem}"))
    for dip_start, dip_len in plan.dips:
        # When the vision pass labeled the shot that hits out of the black,
        # the title-slot marker says what comes next — a real title cue.
        incoming = next(
            (
                e
                for e in plan.entries
                if abs(e.record_start - (dip_start + dip_len)) <= 1e-3
            ),
            None,
        )
        if incoming is not None and incoming.label:
            note = f"{dip_len:g}s of black — next: {incoming.label}"
        else:
            note = f"{dip_len:g}s of black — drop a title here"
        timeline.markers.append(
            Marker(
                frame=seconds_to_frames(dip_start, fps),
                name="Title slot",
                note=note,
                color="Blue",
            )
        )
    for cue in plan.sfx:
        # The planned sound-design layer rides along as Green markers: the
        # editor sees WHERE each effect goes and gets the search query to
        # paste into the SFX library right in the marker note.
        timeline.markers.append(
            Marker(
                frame=seconds_to_frames(cue.time, fps),
                name=f"SFX: {cue.kind}",
                note=f"{cue.query} — {cue.note}",
                color="Green",
            )
        )
    return timeline
