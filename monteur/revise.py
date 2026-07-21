"""The revision loop: "die zweite Hälfte ist zu hektisch" -> a better cut.

A montage plan used to be fire-and-forget. This module lets the editor
iterate: save the plan (``monteur create --save-plan``), watch the cut, say
what bothers them in one sentence, and rebuild — while the shots they like
stay exactly where they are (``--pin``).

Two pieces:

* :func:`parse_revision` — an OFFLINE German + English keyword matcher, in
  the same deliberately rough spirit as
  :func:`monteur.brief.interpret_brief_offline`. It recognizes obvious
  region, pace, transition and style cues and NEVER guesses: text with no
  recognizable cue yields a neutral :class:`Revision` whose rationale says
  so.
* :func:`revise_plan` — re-plans via :func:`monteur.montage.plan_montage`
  with the caller's original kwargs (plus the revision's overrides) and
  applies the region mechanics described below.

Region pace mechanics (and their honest limits)
-----------------------------------------------
``plan_montage`` has no per-region pace, so a region-scoped pace change is
implemented here, asymmetrically:

* **Calmer** (``pace_scale`` > 1): the plan is rebuilt with the caller's
  unchanged kwargs (with deterministic inputs this reproduces the original
  cut), then adjacent slots INSIDE the region are merged — every
  ``max(2, round(pace_scale))`` contiguous slots become one, keeping the
  EARLIER entry's material and extending its record span to the last merged
  slot (source padded toward the clip's end, exactly like the fill; a short
  clip keeps a shorter source and the gap is noted). Every surviving cut in
  the region is therefore a SUBSET of the original grid positions — still
  on the beats. Limits: the realized factor is whole slots (x1.6 merges
  pairs, i.e. realized x2); dissolves INTO absorbed slots disappear with
  them; merge runs break at dips (a black gap is never absorbed) and at
  pinned entries.
* **Snappier** (``pace_scale`` < 1): a slot canNOT be split without the
  beat grid, so the whole plan is re-run with the GLOBAL pace scaled down
  (base: the median slot length of the original region's entries) and only
  the region is taken from it; entries outside the region are restored
  VERBATIM from the original plan by record-window matching. Interior
  region cuts come from the new beat-grid walk (still on beats); the region
  boundaries stay on original cut positions (a new entry straddling the
  boundary is trimmed to it, its source trimmed 1:1). Limits: the realized
  pace is bounded by the music — a region already cutting on every beat
  cannot get faster; the re-plan doesn't know what the original spent
  where, so footage could appear both in a restored shot and in the re-cut
  region — with repeats off (the default) :func:`_dedupe_repeats`
  re-sources such duplicates afterwards (allow_repeats=True keeps them);
  smash-to-black dips inside the region come from the re-plan, dips outside
  from the original.

A revision with no region applies globally: the pace kwarg is re-derived
from the original plan's median cut length times ``pace_scale`` and the
whole cut is re-planned. ``transitions`` and ``style`` overrides always
apply to the WHOLE cut in every case — plan_montage cannot switch styles
mid-song; the region only scopes the pace.

Pinning
-------
``pinned`` is a list of record-time stamps (seconds). The ORIGINAL entry
whose ``[record_start, record_end)`` contains a stamp is forced into the
revised plan verbatim via :func:`monteur.montage.pin_entry` — exact source
material and record window; whatever the revision planned there is trimmed
or dropped, and a merged/pace region never absorbs a pinned entry. A stamp
that hits no entry (past the end, or inside a black dip) is noted and
ignored. The revision note says what happened, e.g.
``"revision: calmer 6.0-12.0s (pace x1.6): 10 slots -> 5; 2 pinned shots
kept"``.

SFX cues ride along from the re-plan; whoosh cues whose cut vanished in a
merge or splice are dropped (they are cut-synchronous), time-based cues
(ambience, risers, impacts, sub-drops) are kept.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, replace

from monteur.montage import (
    MIN_CUT_INTERVAL,
    STYLES,
    MontageEntry,
    MontagePlan,
    _find_unused_window,
    _shares_material,
    pin_entry,
    plan_montage,
)
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport

# Pace scales for the keyword matcher: "ruhiger" plans ~1.6x longer shots,
# "schneller" ~0.6x. Chosen to be clearly visible without flipping the cut's
# character; the realized change follows the beat grid, not the literal number.
CALM_SCALE = 1.6
SNAPPY_SCALE = 0.6

# Named regions, as fractions of the cut. First hit wins; matched on
# lowercase text. "ende" needs a word boundary so "blenden"/"schwarzblenden"
# don't read as a region (see _ENDE_RE).
_REGION_KEYWORDS: tuple[tuple[tuple[str, ...], tuple[float, float]], ...] = (
    (("zweite hälfte", "second half"), (0.5, 1.0)),
    (("erste hälfte", "first half"), (0.0, 0.5)),
    (("anfang", "intro", "opening"), (0.0, 0.25)),
    (("outro",), (0.75, 1.0)),
)
_ENDE_RE = re.compile(r"\bende\b")
# Absolute regions: "ab 1:30" / "from 1:30" / "ab 90s" / "ab 90 sekunden".
# The unit (or the M:SS colon) is required so "from 3 clips" stays unread.
_FROM_MMSS_RE = re.compile(r"\b(?:ab|from)\s+(\d{1,3}):([0-5]\d)\b")
_FROM_SECONDS_RE = re.compile(
    r"\b(?:ab|from)\s+(\d+(?:[.,]\d+)?)\s*(?:sek(?:\.|unden?)?|sec(?:\.|onds?)?|s)\b",
    re.I,
)

# Pace cues. "zu langsam" (too slow -> faster) is checked on the snappy side
# so it never collides with "langsamer" (slower, please -> calmer).
_CALM_WORDS = (
    "zu hektisch", "hektisch", "ruhiger", "langsamer",
    "too hectic", "hectic", "calmer", "slower",
)
_SNAPPY_WORDS = (
    "zu langsam", "zu lahm", "schneller", "mehr energie",
    "too slow", "faster", "more energy", "snappier",
)

# Transition cues, checked in this order: "schwarzblenden" contains
# "blenden", so smash must win before the dissolve keywords are tried.
_SMASH_WORDS = ("schwarzblende", "smash")
_CUTS_WORDS = ("harte schnitte", "harter schnitt", "hard cuts", "hard cut")
_DISSOLVE_WORDS = ("blende", "dissolve", "überblend")

# Style keywords — mirrors monteur.brief's table (kept local so revise stays
# importable without pulling the AI module chain in).
_STYLE_KEYWORDS = (
    ("travel", ("reise", "travel", "urlaub")),
    ("wedding", ("hochzeit", "wedding")),
    ("music_video", ("musikvideo", "music video", "musik-video")),
    ("trailer", ("trailer", "teaser")),
)

# The plan's own style note ('style "travel": Travel film') — written by
# plan_montage, read back by style_from_plan so a revised travel film stays
# a travel film even though the plan file stores no run flags.
_STYLE_NOTE_RE = re.compile(r'^style "([a-z_]+)"')

_EPS = 1e-6


@dataclass
class Revision:
    """One editorial change request, parsed from a sentence.

    ``region`` is in fractions of the cut ((0.5, 1.0) = the second half);
    ``region_seconds`` is the absolute alternative for "ab 1:30" — (start,
    end) in seconds where end None means "to the end". Only one of the two
    is set; both are resolved against the plan's real duration inside
    :func:`revise_plan` (the duration is unknown at parse time).
    """

    region: tuple[float, float] | None = None
    region_seconds: tuple[float, float | None] | None = None
    pace_scale: float | None = None  # >1 = calmer (longer shots), <1 = snappier
    transitions: str | None = None  # override transitions mode, or None = keep
    style: str | None = None  # override style, or None = keep
    rationale: str = ""  # one line saying how the instruction was read


def parse_revision(text: str) -> Revision:
    """Read a revision request offline — German + English keywords, no AI.

    Deliberately rough, like :func:`monteur.brief.interpret_brief_offline`:
    it recognizes regions ("zweite hälfte"/"second half", "anfang"/"intro",
    "ende"/"outro", "ab 1:30"/"from 90s"), pace words ("ruhiger"/"calmer" ->
    x1.6, "schneller"/"faster" -> x0.6), transition cues ("harte schnitte",
    "blenden", "schwarzblenden"/"smash") and style keywords, and ignores
    everything else. Text with no recognizable cue yields a neutral Revision
    whose rationale starts with "no actionable instruction found:" — it
    never guesses.
    """
    t = text.lower()
    recognized: list[str] = []
    rev = Revision()

    for keywords, span in _REGION_KEYWORDS:
        hit = next((k for k in keywords if k in t), None)
        if hit:
            rev.region = span
            recognized.append(
                f"region {span[0] * 100:.0f}-{span[1] * 100:.0f}% (keyword {hit!r})"
            )
            break
    if rev.region is None and _ENDE_RE.search(t):
        rev.region = (0.75, 1.0)
        recognized.append("region 75-100% (keyword 'ende')")
    if rev.region is None:
        match = _FROM_MMSS_RE.search(t) or _FROM_SECONDS_RE.search(t)
        if match:
            if ":" in match.group(0):
                start = int(match.group(1)) * 60 + int(match.group(2))
            else:
                start = float(match.group(1).replace(",", "."))
            rev.region_seconds = (float(start), None)
            recognized.append(f"region from {start:g}s ({match.group(0)!r})")

    calm = next((w for w in _CALM_WORDS if w in t), None)
    snappy = next((w for w in _SNAPPY_WORDS if w in t), None)
    if calm:
        rev.pace_scale = CALM_SCALE
        recognized.append(f"pace x{CALM_SCALE:g} calmer (keyword {calm!r})")
    elif snappy:
        rev.pace_scale = SNAPPY_SCALE
        recognized.append(f"pace x{SNAPPY_SCALE:g} snappier (keyword {snappy!r})")

    for mode, words in (
        ("smash", _SMASH_WORDS),
        ("cuts", _CUTS_WORDS),
        ("dissolves", _DISSOLVE_WORDS),
    ):
        hit = next((w for w in words if w in t), None)
        if hit:
            rev.transitions = mode
            recognized.append(f"transitions {mode} (keyword {hit!r})")
            break

    for style, keywords in _STYLE_KEYWORDS:
        hit = next((k for k in keywords if k in t), None)
        if hit:
            rev.style = style
            recognized.append(f"style {style} (keyword {hit!r})")
            break

    if recognized:
        rev.rationale = "recognized: " + "; ".join(recognized)
    else:
        rev.rationale = f"no actionable instruction found: {text!r}"
    return rev


def style_from_plan(plan: MontagePlan) -> str:
    """The style a plan was built with, read from its own notes.

    The plan file stores the cut, not the run's flags — but plan_montage
    always writes a ``style "<key>": <name>`` note, so the revision loop
    can keep a travel film a travel film. Falls back to "auto" when no
    (known) style note is present.
    """
    for note in plan.notes:
        match = _STYLE_NOTE_RE.match(note)
        if match and match.group(1) in STYLES:
            return match.group(1)
    return "auto"


# --- region mechanics -----------------------------------------------------------


def _resolve_region(revision: Revision, duration: float) -> tuple[float, float] | None:
    """The revision's region in seconds, clamped to the cut; None if empty."""
    if revision.region is not None:
        lo, hi = revision.region
        r0, r1 = lo * duration, hi * duration
    elif revision.region_seconds is not None:
        start, end = revision.region_seconds
        r0 = start if start is not None else 0.0
        r1 = end if end is not None else duration
    else:
        return None
    r0 = max(0.0, min(r0, duration))
    r1 = max(0.0, min(r1, duration))
    if r1 - r0 <= _EPS:
        return None
    return r0, r1


def _inside(entry: MontageEntry, r0: float, r1: float) -> bool:
    """Whether the entry's record window lies fully inside [r0, r1]."""
    return entry.record_start >= r0 - _EPS and entry.record_end <= r1 + _EPS


def _contains_pin(entry: MontageEntry, pins: list[float]) -> bool:
    return any(entry.record_start - _EPS <= t < entry.record_end - _EPS for t in pins)


def _median_slot(entries: list[MontageEntry]) -> float | None:
    lengths = [e.record_end - e.record_start for e in entries]
    return statistics.median(lengths) if lengths else None


def _merge_region(
    plan: MontagePlan, r0: float, r1: float, scale: float, pins: list[float]
) -> tuple[int, int]:
    """Merge adjacent region slots in place (the calmer mechanic).

    Every ``max(2, round(scale))`` contiguous unpinned slots fully inside
    [r0, r1] become one entry: the FIRST slot's material, the run's full
    record span, the source padded toward the clip's end (a too-short clip
    keeps a shorter source; the gap is noted, in the fill's own wording).
    Runs break at dips (non-contiguous record windows) and at pinned
    entries, so neither is ever absorbed. Returns (slots before, after)
    counted inside the region.
    """
    group = max(2, round(scale))
    merged: list[MontageEntry] = []
    run: list[MontageEntry] = []
    notes: list[str] = []
    before = sum(1 for e in plan.entries if _inside(e, r0, r1))

    def flush() -> None:
        for i in range(0, len(run), group):
            chunk = run[i : i + group]
            first, last = chunk[0], chunk[-1]
            if len(chunk) == 1:
                merged.append(first)
                continue
            span = last.record_end - first.record_start
            limit = (
                first.clip_duration if first.clip_duration > _EPS else float("inf")
            )
            src_end = min(first.source_start + span, limit)
            if src_end - first.source_start < span - _EPS:
                notes.append(
                    f"gap at {first.record_start:.2f}s: only "
                    f"{src_end - first.source_start:.2f}s of source for a "
                    f"{span:.2f}s slot"
                )
            merged.append(replace(first, record_end=last.record_end, source_end=src_end))
        run.clear()

    for entry in plan.entries:
        joinable = (
            _inside(entry, r0, r1)
            and not _contains_pin(entry, pins)
            and (not run or abs(run[-1].record_end - entry.record_start) <= _EPS)
        )
        if joinable:
            run.append(entry)
        else:
            flush()
            merged.append(entry)
    flush()
    merged.sort(key=lambda e: e.record_start)
    plan.entries = merged
    plan.notes.extend(notes)
    after = sum(1 for e in plan.entries if _inside(e, r0, r1))
    return before, after


def _splice_region(
    revised: MontagePlan, original: MontagePlan, r0: float, r1: float
) -> bool:
    """Keep the re-plan only inside the region (the snappier mechanic).

    The fill window is the record span of the ORIGINAL entries fully inside
    [r0, r1]; original entries outside it (including boundary straddlers)
    are restored verbatim, and the re-plan's entries are trimmed to the
    window (source trimmed 1:1 with the record), so the region boundaries
    stay on original cut positions. Dips: the original's outside the
    window, the re-plan's inside. Returns False (nothing changed) when no
    original entry lies fully inside the region.
    """
    inside = [e for e in original.entries if _inside(e, r0, r1)]
    if not inside:
        return False
    win_lo = inside[0].record_start
    win_hi = inside[-1].record_end

    restored = [
        replace(e)
        for e in original.entries
        if e.record_end <= win_lo + _EPS or e.record_start >= win_hi - _EPS
    ]
    filled: list[MontageEntry] = []
    for e in revised.entries:
        lo = max(e.record_start, win_lo)
        hi = min(e.record_end, win_hi)
        if hi - lo <= _EPS:
            continue
        src_lo = min(e.source_end, e.source_start + (lo - e.record_start))
        src_hi = min(e.source_end, e.source_start + (hi - e.record_start))
        filled.append(
            replace(e, record_start=lo, record_end=hi, source_start=src_lo, source_end=src_hi)
        )
    revised.entries = sorted(restored + filled, key=lambda e: e.record_start)
    revised.dips = sorted(
        [
            d
            for d in original.dips
            if d[0] + d[1] <= win_lo + _EPS or d[0] >= win_hi - _EPS
        ]
        + [
            d
            for d in revised.dips
            if win_lo - _EPS <= d[0] and d[0] + d[1] <= win_hi + _EPS
        ]
    )
    return True


def _dedupe_repeats(
    plan: MontagePlan,
    reports: list[ClipReport],
    protected: list[MontageEntry],
) -> tuple[int, int]:
    """Make the revised cut repeat-free again, in place (repeats OFF only).

    ``plan_montage`` itself never repeats footage with repeats off, but
    the revision mechanics can reintroduce repeats around it: a
    snappier-region SPLICE restores original entries next to entries from
    an independent re-plan (which spent its material without knowing what
    the original used where), a calmer-region MERGE pads sources toward
    the clip's end over footage other entries may show, and a PINNED shot
    is forced in verbatim while the re-plan may have cast the same
    material elsewhere. This pass makes the zero-repeat promise hold END
    TO END: pinned entries claim their material first (they are never
    touched), then the entries are walked in record order and any entry
    sharing material with an already-kept one (the planner's own
    :func:`monteur.montage._shares_material` rule) is RE-SOURCED to
    unused moment material (:func:`monteur.montage._find_unused_window`;
    a shorter free span keeps the record slot with the fill's own gap
    semantics). Only when no usable span remains anywhere is the entry
    dropped — an honest hole beats a silent repeat. Returns
    ``(re-sourced, dropped)``; the caller writes the note.
    """
    protected_keys = {
        (e.clip_path, round(e.source_start, 4), round(e.record_start, 4))
        for e in protected
    }
    used: list[MontageEntry] = [
        e
        for e in plan.entries
        if (e.clip_path, round(e.source_start, 4), round(e.record_start, 4))
        in protected_keys
    ]
    resourced = dropped = 0
    kept: list[MontageEntry] = []
    for entry in sorted(plan.entries, key=lambda e: e.record_start):
        if any(entry is p for p in used):
            kept.append(entry)  # pinned: already claimed, never touched
            continue
        if any(_shares_material(entry, other) for other in used + kept):
            needed = entry.record_end - entry.record_start
            windows = [
                (e.clip_path, e.source_start, e.source_end) for e in used + kept
            ]
            found = _find_unused_window(
                reports, windows, needed, min_piece=min(MIN_CUT_INTERVAL, needed)
            )
            if found is None:
                dropped += 1
                continue
            report, moment, start, length = found
            entry = replace(
                entry,
                clip_path=report.path,
                source_start=start,
                source_end=start + length,
                score=moment.score,
                media_start=report.media_start,
                clip_duration=report.duration,
                label=getattr(moment, "label", ""),
            )
            resourced += 1
        kept.append(entry)
    if resourced or dropped:
        plan.entries = sorted(kept, key=lambda e: e.record_start)
    return resourced, dropped


def _prune_stale_whooshes(plan: MontagePlan) -> int:
    """Drop whoosh cues whose cut no longer exists (in place).

    Whooshes are cut-synchronous (centered on a cut); after a merge or
    splice their cut may be gone. Time-based cues (ambience, risers,
    impacts, sub-drops) are kept — they mark musical positions, not cuts.
    """
    if not plan.sfx:
        return 0
    starts = [e.record_start for e in plan.entries]
    kept = [
        cue
        for cue in plan.sfx
        if cue.kind != "whoosh"
        or any(abs(cue.time + cue.duration / 2.0 - s) <= 1e-3 for s in starts)
    ]
    dropped = len(plan.sfx) - len(kept)
    plan.sfx = kept
    return dropped


# --- public API -----------------------------------------------------------------


def revise_plan(
    plan: MontagePlan,
    reports: list[ClipReport],
    music: MusicAnalysis | None,
    revision: Revision,
    pinned: list[float] | None = None,
    **plan_kwargs,
) -> MontagePlan:
    """Rebuild a plan per one revision, keeping pinned shots untouched.

    ``plan_kwargs`` must carry the SAME kwargs the original ``plan_montage``
    run used (order/style/max_duration/pace/transitions/sfx/...); the
    revision's overrides are applied on top, the region mechanics are
    described in the module docstring, and ``pinned`` record-time stamps
    protect individual shots (see Pinning there). The original plan is not
    modified; the revised plan's notes end with a "revision: ..." line
    saying what actually happened.
    """
    kwargs = dict(plan_kwargs)
    if revision.style is not None:
        kwargs["style"] = revision.style
    if revision.transitions is not None:
        kwargs["transitions"] = revision.transitions

    region = _resolve_region(revision, plan.duration)
    region_requested = revision.region is not None or revision.region_seconds is not None
    scale = revision.pace_scale if revision.pace_scale not in (None, 1.0) else None

    extra_notes: list[str] = []
    merge_after = False
    splice_after = False
    if scale is not None and region_requested and region is None:
        extra_notes.append(
            "revision region is empty after clamping to the cut; pace change skipped"
        )
        scale = None
    if scale is not None and region is not None:
        if not any(_inside(e, *region) for e in plan.entries):
            extra_notes.append(
                f"region {region[0]:.1f}-{region[1]:.1f}s contains no whole shot; "
                "pace change skipped"
            )
            scale = None
        elif scale > 1.0:
            merge_after = True  # post-process; the re-plan keeps the caller's pace
        else:
            base = _median_slot(
                [e for e in plan.entries if _inside(e, *region)]
            ) or _median_slot(plan.entries)
            kwargs["pace"] = base * scale
            splice_after = True
    elif scale is not None:
        base = _median_slot(plan.entries)
        if base is None:
            extra_notes.append("the plan has no entries; pace change skipped")
            scale = None
        else:
            kwargs["pace"] = base * scale

    revised = plan_montage(reports, music, **kwargs)

    pins = list(pinned or [])
    pinned_entries: list[MontageEntry] = []
    missed_pins: list[float] = []
    for t in pins:
        hit = next(
            (
                e
                for e in plan.entries
                if e.record_start - _EPS <= t < e.record_end - _EPS
            ),
            None,
        )
        if hit is None:
            missed_pins.append(t)
        elif hit not in pinned_entries:
            pinned_entries.append(hit)

    parts: list[str] = []
    if merge_after:
        before, after = _merge_region(revised, region[0], region[1], scale, pins)
        parts.append(
            f"calmer {region[0]:.1f}-{region[1]:.1f}s (pace x{scale:g}): "
            f"{before} slots -> {after}"
        )
    if splice_after:
        _splice_region(revised, plan, region[0], region[1])
        parts.append(
            f"snappier {region[0]:.1f}-{region[1]:.1f}s (pace x{scale:g}): "
            f"re-cut at ~{kwargs['pace']:.1f}s per cut, rest restored from the "
            "original plan"
        )
    if scale is not None and region is None:
        parts.append(
            f"{'calmer' if scale > 1 else 'snappier'} overall (pace x{scale:g}): "
            f"re-planned at ~{kwargs['pace']:.1f}s per cut"
        )
    if revision.style is not None:
        parts.append(f"style -> {revision.style}")
    if revision.transitions is not None:
        parts.append(f"transitions -> {revision.transitions}")
    if not parts:
        parts.append("no changes requested")

    for entry in pinned_entries:
        pin_entry(revised, entry)

    # Zero-repeat promise, end to end: the splice/merge/pin mechanics can
    # put the same material on screen twice even though every plan_montage
    # call was repeat-free — re-source (or, out of material, drop) the
    # duplicates unless the caller planned with allow_repeats=True.
    if not plan_kwargs.get("allow_repeats"):
        resourced, dedup_dropped = _dedupe_repeats(revised, reports, pinned_entries)
        if resourced or dedup_dropped:
            bits = []
            if resourced:
                bits.append(
                    f"{resourced} repeated shot{'s' if resourced != 1 else ''} "
                    "re-sourced"
                )
            if dedup_dropped:
                bits.append(
                    f"{dedup_dropped} dropped (no unused footage left)"
                )
            extra_notes.append("no repeats: " + ", ".join(bits))

    dropped = _prune_stale_whooshes(revised)
    if dropped:
        extra_notes.append(
            f"{dropped} whoosh cue{'s' if dropped != 1 else ''} dropped "
            "(their cuts were revised away)"
        )

    note = "revision: " + "; ".join(parts)
    if pinned_entries:
        n = len(pinned_entries)
        note += f"; {n} pinned shot{'s' if n != 1 else ''} kept"
    revised.notes.append(note)
    for t in missed_pins:
        revised.notes.append(f"pin at {t:g}s hits no shot; ignored")
    revised.notes.extend(extra_notes)
    return revised
