"""Director's Notes: Claude reviews a planned cut against editing craft.

The montage planner is good at mechanics — beats, grids, slots — but it
cannot *watch* the result. This module closes that loop: it condenses a
:class:`~monteur.montage.MontagePlan` (plus the sifted footage and the
music analysis) into a compact text dossier, hands it to Claude in the
role of a seasoned film editor (a Schnittmeister), and gets back a
structured review — verdict, score, praise, and concrete issues, each
optionally with a replacement shot picked from the unused material.

Three functions, three responsibilities:

* :func:`review_context` — the dossier: overview (style, duration, tempo,
  sections, drops), one line per slot (record window, clip, source window,
  what the vision pass saw there, the music section under it), and the
  "bench" — the strongest UNUSED moments, the only material Claude may
  propose as replacements. Deliberately lean: basenames only, floats
  rounded, empty fields omitted — this goes into a prompt.
* :func:`direct_cut` — the AI call. Goes through the one seam
  :func:`monteur.ai.complete`, so it works over an API key OR the Claude
  Code CLI — with Claude Code there is **no extra API cost**: the review
  is text-only (no images are sent), which is exactly what the CLI
  backend can do. A :class:`monteur.ai.MonteurAIError` passes through
  unchanged; everything else about the reply is validated defensively
  (missing keys get sensible defaults, the score is clamped to 0–100,
  issues pointing at slots that don't exist are dropped).
* :func:`apply_review` — pure plan surgery, no AI: every issue that
  carries a ``replacement`` swaps that slot's SOURCE (clip, source
  window, and the source-describing fields that ride with it) while the
  record grid, transitions, dips and SFX stay bit-identical. Pinned
  slots are never touched.

The vision fields in the dossier (label/role/hero/group) come from the
cached ``monteur see`` pass (:mod:`monteur.vision`): when vision ran, the
review is proportionally smarter — Claude sees WHAT is in each slot, not
just how it scored. Without vision the review still works, judged on
structure, pacing and scores alone.

Slot indexes are 0-based everywhere in the data (`review_context`'s
``slot`` field, the review's ``issues[].slots``); human-facing text (the
CLI printout, `apply_review`'s notes, the Studio cards) shows them
1-based, matching the numbered shot list in the Studio's revise block.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from monteur import ai
from monteur.montage import MontagePlan
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport, Moment

_EPS = 1e-6

#: How many unused moments the bench may carry (the dossier must stay lean).
BENCH_LIMIT = 20

#: The structured-output contract for :func:`direct_cut`. The API backend
#: enforces it; the CLI backend gets it as an instruction — either way the
#: parsed dict is re-validated by :func:`_validate_review`.
REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "description": "one-line editorial verdict"},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "praise": {"type": "array", "items": {"type": "string"}},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slots": {"type": "array", "items": {"type": "integer"}},
                    "kind": {"type": "string"},
                    "problem": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "replacement": {
                        "type": ["object", "null"],
                        "properties": {
                            "clip": {"type": "string"},
                            "start": {"type": "number"},
                            "end": {"type": "number"},
                        },
                        "required": ["clip", "start", "end"],
                    },
                },
                "required": ["slots", "kind", "problem", "suggestion", "replacement"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "score", "praise", "issues", "summary"],
}

#: The craft rules the review judges against. Kept explicit and short —
#: this is the editorial contract, not a personality.
_SYSTEM = (
    "You are a seasoned film editor — a Schnittmeister — reviewing a "
    "colleague's planned montage before it renders. Judge the cut against "
    "craft:\n"
    "- the opening must establish place and mood before the cut accelerates;\n"
    "- the strongest or hero moment belongs on the drop;\n"
    "- never two shots of the same scene group back to back;\n"
    "- image energy should track music energy — calm sections carry calmer "
    "imagery, loud sections carry the motion;\n"
    "- time of day must stay coherent: day, golden-hour and night shots "
    "belong in blocks with rare, deliberate switches — a lone night shot "
    "inside a day block is a flag unless it is clearly a chosen story "
    "beat (a teaser, a flash-forward);\n"
    "- vary the motifs: one location or subject must not dominate;\n"
    "- the outro should decay, not spike;\n"
    "- repetition is acceptable only when it is rhythmically intentional.\n"
    "Judge only what the dossier shows; when slots carry no vision labels, "
    "judge structure, pacing and scores without inventing content. Be "
    "concrete, cite slot indexes, and propose a replacement only when a "
    "bench moment clearly beats what is in the slot."
)


def _r(value: float) -> float:
    """Round for the dossier — prompts don't need 15 decimals."""
    return round(float(value), 2)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _report_index(reports: list[ClipReport]) -> dict[str, ClipReport]:
    """Reports keyed by full path AND basename (first one wins)."""
    index: dict[str, ClipReport] = {}
    for report in reports:
        index.setdefault(report.path, report)
        index.setdefault(Path(report.path).name, report)
    return index


def _entry_moment(entry, report: ClipReport | None) -> Moment | None:
    """The report moment the entry's source window overlaps most (or None)."""
    if report is None:
        return None
    best: Moment | None = None
    best_ov = 0.0
    for moment in report.moments:
        ov = _overlap(moment.start, moment.end, entry.source_start, entry.source_end)
        if ov > best_ov + _EPS:
            best, best_ov = moment, ov
    return best


def _section_label(music: MusicAnalysis | None, plan: MontagePlan, t: float) -> str:
    """The music-section label under record time ``t`` ("" without music).

    Sections live in SONG time; a montage cut from the song's strongest
    passage starts at ``plan.music_start``, so record time shifts by that.
    """
    if music is None or not music.sections:
        return ""
    song_t = plan.music_start + t
    for section in music.sections:
        if section.start - _EPS <= song_t < section.end - _EPS:
            return section.label
    if song_t >= music.sections[-1].end - _EPS:
        return music.sections[-1].label
    return ""


def review_context(
    plan: MontagePlan,
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
) -> dict:
    """A compact, JSON-ready dossier of the planned cut — the prompt payload.

    ``overview`` carries the run's shape (style from the plan's own notes,
    duration, tempo, section list, drop times, smash-to-black dips, entry
    count). ``slots`` has one lean dict per entry: 0-based ``slot`` index,
    record/source windows, clip BASENAME, score, the vision annotations
    (label from the entry itself; role/hero/group — plus the offline
    ``daylight`` class — enriched by matching the entry's clip + source
    overlap against the reports' moments), the music
    section under the slot and the dissolve length when there is one.
    ``bench`` lists up to :data:`BENCH_LIMIT` of the strongest moments NO
    entry uses — the only material a review may propose as replacements.

    Empty/zero fields are omitted and floats are rounded, so an
    un-annotated plan stays small and honest ("no label" is absent, not
    ``""``).
    """
    from monteur.revise import style_from_plan

    overview: dict = {
        "style": style_from_plan(plan),
        "duration": _r(plan.duration),
        "entries": len(plan.entries),
    }
    if music is not None:
        lo, hi = plan.music_start, plan.music_start + plan.duration
        overview["tempo"] = round(float(music.tempo), 1)
        overview["sections"] = [
            {
                "label": s.label,
                "start": _r(max(s.start, lo) - lo),
                "end": _r(min(s.end, hi) - lo),
            }
            for s in music.sections
            if min(s.end, hi) - max(s.start, lo) > _EPS
        ]
        drops = [_r(d - lo) for d in music.drops if lo - _EPS <= d <= hi + _EPS]
        if drops:
            overview["drops"] = drops
    if plan.dips:
        overview["dips"] = [[_r(start), _r(length)] for start, length in plan.dips]

    index = _report_index(reports)
    used: list[tuple[ClipReport, Moment]] = []
    slots: list[dict] = []
    for i, entry in enumerate(plan.entries):
        report = index.get(entry.clip_path) or index.get(Path(entry.clip_path).name)
        moment = _entry_moment(entry, report)
        slot: dict = {
            "slot": i,
            "record": [_r(entry.record_start), _r(entry.record_end)],
            "clip": Path(entry.clip_path).name,
            "source": [_r(entry.source_start), _r(entry.source_end)],
            "score": _r(entry.score),
        }
        if entry.transition > _EPS:
            slot["dissolve"] = _r(entry.transition)
        label = entry.label or (moment.label if moment is not None else "")
        if label:
            slot["label"] = label
        if moment is not None:
            if moment.role:
                slot["role"] = moment.role
            if moment.hero > _EPS:
                slot["hero"] = _r(moment.hero)
            if moment.group:
                slot["group"] = moment.group
            if getattr(moment, "daylight", ""):
                slot["daylight"] = moment.daylight
            if report is not None:
                used.append((report, moment))
        section = _section_label(music, plan, entry.record_start)
        if section:
            slot["music"] = section
        slots.append(slot)

    # The bench: strongest moments no entry draws from. "Used" means the
    # entry's source window overlaps the moment in the same clip.
    def _is_used(report: ClipReport, moment: Moment) -> bool:
        for entry in plan.entries:
            same = entry.clip_path == report.path or (
                Path(entry.clip_path).name == Path(report.path).name
            )
            if same and _overlap(
                moment.start, moment.end, entry.source_start, entry.source_end
            ) > _EPS:
                return True
        return False

    candidates: list[tuple[ClipReport, Moment]] = [
        (report, moment)
        for report in reports
        for moment in report.moments
        if not _is_used(report, moment)
    ]
    candidates.sort(key=lambda rm: (-rm[1].score, -rm[1].hero, rm[0].path, rm[1].start))
    bench: list[dict] = []
    for report, moment in candidates[:BENCH_LIMIT]:
        item: dict = {
            "clip": Path(report.path).name,
            "start": _r(moment.start),
            "end": _r(moment.end),
            "score": _r(moment.score),
        }
        if moment.label:
            item["label"] = moment.label
        if moment.role:
            item["role"] = moment.role
        if moment.hero > _EPS:
            item["hero"] = _r(moment.hero)
        if moment.group:
            item["group"] = moment.group
        if getattr(moment, "daylight", ""):
            item["daylight"] = moment.daylight
        bench.append(item)

    return {"overview": overview, "slots": slots, "bench": bench}


def _validate_review(data, slot_count: int) -> dict:
    """Defensively normalise a parsed review dict.

    Missing keys become sensible defaults, the score is clamped to 0–100
    (a missing/non-numeric score reads as a neutral 50), praise entries are
    coerced to strings, and issues are dropped when they carry no slots or
    reference a slot index outside the plan. A malformed ``replacement``
    (missing keys, non-numeric window, empty window) degrades to ``None``
    — the issue's words survive, only the automatic swap is off the table.
    """
    if not isinstance(data, dict):
        data = {}
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))
    praise = [str(p).strip() for p in data.get("praise") or [] if str(p).strip()]
    issues: list[dict] = []
    for raw in data.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        try:
            slots = [int(s) for s in raw.get("slots") or []]
        except (TypeError, ValueError):
            continue
        if not slots or any(s < 0 or s >= slot_count for s in slots):
            continue
        replacement = raw.get("replacement")
        rep: dict | None = None
        if isinstance(replacement, dict):
            try:
                rep = {
                    "clip": str(replacement["clip"]),
                    "start": float(replacement["start"]),
                    "end": float(replacement["end"]),
                }
            except (KeyError, TypeError, ValueError):
                rep = None
            if rep is not None and rep["end"] - rep["start"] <= _EPS:
                rep = None
        issues.append(
            {
                "slots": slots,
                "kind": str(raw.get("kind") or ""),
                "problem": str(raw.get("problem") or ""),
                "suggestion": str(raw.get("suggestion") or ""),
                "replacement": rep,
            }
        )
    return {
        "verdict": str(data.get("verdict") or ""),
        "score": score,
        "praise": praise,
        "issues": issues,
        "summary": str(data.get("summary") or ""),
    }


def direct_cut(
    plan: MontagePlan,
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    notes: str = "",
) -> dict:
    """Ask Claude for director's notes on the planned cut.

    Builds the :func:`review_context` dossier, sends it through
    :func:`monteur.ai.complete` with :data:`REVIEW_SCHEMA` and the
    Schnittmeister system prompt, and returns the validated review dict::

        {"verdict": str, "score": int 0-100, "praise": [str],
         "issues": [{"slots": [int], "kind": str, "problem": str,
                     "suggestion": str,
                     "replacement": {"clip", "start", "end"} | None}],
         "summary": str}

    ``notes`` is optional context from the editor (what the video is for,
    who watches it). Raises :class:`monteur.ai.MonteurAIError` unchanged
    when no backend is reachable, the request fails, or the reply is not
    parseable JSON; a structurally odd but parseable reply is repaired by
    :func:`_validate_review` instead of raising.
    """
    context = review_context(plan, reports, music)
    prompt = (
        "Here is the dossier of a planned montage: the overview, every slot "
        "in record order, and the bench — the strongest unused moments, the "
        "ONLY material you may propose as replacements.\n\n"
        + json.dumps(context, ensure_ascii=False)
    )
    if notes.strip():
        prompt += (
            "\n\nCONTEXT FROM THE EDITOR (what the video is for):\n"
            + notes.strip()
        )
    prompt += (
        "\n\nReview the cut. In `issues`, `slots` are the 0-based `slot` "
        "indexes from the dossier (adjacent slots may share one issue); "
        "`kind` is a short snake_case category (e.g. same_scene, "
        "weak_opening, drop_mismatch, energy_mismatch, repetition, "
        "weak_outro); a `replacement` must come from the bench — `clip` is "
        "the bench entry's clip name, start/end inside its window — and "
        "only when it clearly improves the slot, otherwise null. `score` is "
        "0-100 (100 = ship it). Keep praise honest and short."
    )
    raw = ai.complete(prompt, system=_SYSTEM, json_schema=REVIEW_SCHEMA)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ai.MonteurAIError(
            f"the director's review came back as unparseable JSON: {raw[:200]!r}"
        ) from exc
    return _validate_review(data, len(plan.entries))


def _pick_moment(report: ClipReport, start: float, end: float) -> Moment | None:
    """The report moment best matching a requested window.

    Preference order: largest overlap, then nearest centre, then higher
    score — so an out-of-range request still snaps to the closest real
    material instead of failing.
    """
    best: Moment | None = None
    best_key: tuple | None = None
    centre = (start + end) / 2.0
    for moment in report.moments:
        key = (
            -_overlap(moment.start, moment.end, start, end),
            abs((moment.start + moment.end) / 2.0 - centre),
            -moment.score,
        )
        if best_key is None or key < best_key:
            best, best_key = moment, key
    return best


def apply_review(
    plan: MontagePlan,
    review: dict,
    reports: list[ClipReport],
    pinned: list[float] | None = None,
) -> tuple[MontagePlan, list[str]]:
    """Apply the review's replacement suggestions — pure plan surgery, no AI.

    Only issues that carry a ``replacement`` act; each targets the FIRST
    slot in its ``slots`` list. The replacement clip is matched by
    basename against ``reports``, the requested window is snapped into the
    best-matching moment's available range and trimmed/padded to the
    slot's record length exactly like the planner's fill does (source
    padded toward the clip's end; a too-short clip keeps a shorter source
    with a note). The swap touches ONLY the source-describing fields
    (clip_path, source_start/source_end, media_start, clip_duration,
    label, score); the record window, the transition, and every other
    entry stay bit-identical — like :func:`monteur.revise.revise_plan`,
    the original plan object is never modified.

    ``pinned`` (optional) takes the same record-time stamps the revision
    loop uses: a slot containing a stamp is skipped with a note.

    Returns ``(new_plan, notes)`` where ``notes`` are human-readable lines
    (1-based slot numbers) saying what was applied or skipped; the same
    lines are appended to the new plan's notes prefixed ``director:``.
    """
    pins = [float(t) for t in (pinned or [])]
    by_name: dict[str, ClipReport] = {}
    for report in reports:
        by_name.setdefault(Path(report.path).name, report)

    entries = [replace(e) for e in plan.entries]
    notes: list[str] = []
    applied = 0
    issues = review.get("issues") if isinstance(review, dict) else None
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        rep = issue.get("replacement")
        if not isinstance(rep, dict):
            continue
        try:
            idx = int((issue.get("slots") or [None])[0])
            clip = str(rep["clip"])
            want_start = float(rep["start"])
            want_end = float(rep["end"])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if idx < 0 or idx >= len(entries):
            notes.append(f"slot {idx + 1}: not in this plan — skipped")
            continue
        entry = entries[idx]
        if any(
            entry.record_start - _EPS <= t < entry.record_end - _EPS for t in pins
        ):
            notes.append(f"slot {idx + 1}: pinned — left untouched")
            continue
        report = by_name.get(clip) or by_name.get(Path(clip).name)
        if report is None:
            notes.append(f"slot {idx + 1}: no clip named {clip!r} in the footage — skipped")
            continue

        length = entry.record_end - entry.record_start
        moment = _pick_moment(report, want_start, want_end)
        if moment is not None:
            avail_lo, avail_hi = moment.start, moment.end
        else:  # a clip the sift found nothing in: fall back to the whole file
            avail_lo = 0.0
            avail_hi = report.duration if report.duration > _EPS else want_end
        src_start = min(max(want_start, avail_lo), max(avail_lo, avail_hi - length))
        src_end = min(src_start + length, avail_hi)
        if src_end - src_start < length - _EPS:
            # Pad toward the clip's end, exactly like the planner's fill.
            limit = report.duration if report.duration > _EPS else src_start + length
            src_end = max(src_end, min(src_start + length, limit))
        if src_end - src_start < length - _EPS:
            notes.append(
                f"slot {idx + 1}: only {src_end - src_start:.2f}s of source for a "
                f"{length:.2f}s slot"
            )

        entries[idx] = replace(
            entry,
            clip_path=report.path,
            source_start=src_start,
            source_end=src_end,
            score=moment.score if moment is not None else entry.score,
            media_start=report.media_start,
            clip_duration=report.duration,
            label=(moment.label if moment is not None else ""),
        )
        applied += 1
        why = str(issue.get("kind") or "").replace("_", " ") or "director's suggestion"
        notes.append(
            f"slot {idx + 1}: {Path(entry.clip_path).name} "
            f"{entry.source_start:.2f}-{entry.source_end:.2f}s -> "
            f"{Path(report.path).name} {src_start:.2f}-{src_end:.2f}s ({why})"
        )

    if not applied and not notes:
        notes.append("no replacement suggestions to apply")
    new_plan = replace(
        plan,
        entries=entries,
        notes=list(plan.notes) + [f"director: {note}" for note in notes],
        dips=list(plan.dips),
        sfx=list(plan.sfx),
    )
    return new_plan, notes
