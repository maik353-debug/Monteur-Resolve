"""Trailer distillation: a finished cut IS the curation.

An editor who just finished a 12-minute travel film in Resolve has already
answered the hard question — which footage is worth showing. Every clip in
the cut was hand-picked, and the longer a shot stays on screen, the more
the editor cared about it. Distillation reads that finished timeline
(EDL/FCPXML via :func:`monteur.io.load_timeline`) and turns it into the
same :class:`~monteur.sift.ClipReport` material the montage planner eats,
so the 30/60s trailer for Shorts/teasers comes straight from the cut —
no re-sifting of raw footage.

:func:`timeline_to_reports` converts the timeline into reports:

* Video clips are grouped by ``source_file`` (falling back to
  ``source_name`` — bare EDL reels — then the clip name), one report per
  source, in order of first appearance in the cut.
* Every used source range becomes a :class:`~monteur.sift.Moment`
  (frames -> seconds via ``timeline.fps``; the readers already made the
  ranges file-relative). Overlapping or touching ranges from the same
  file are merged — a shot used twice in the film is ONE moment in the
  trailer pool.
* The score is the editor's prior, not a pixel metric:
  ``0.75 + 0.2 x (moment length / longest moment in the cut)`` — base
  0.75 because everything in the cut was already judged good, up to +0.2
  for screen time relative to the longest shot (the longest shot scores
  0.95). A ``metadata["label"]`` left by an earlier Monteur pass rides
  into ``Moment.label``.
* ``usable_ratio`` is 1.0: the editor already judged the material usable.
* With ``probe_media=True`` (the default) and the source file on disk,
  :func:`monteur.media.probe` supplies the file's real duration and
  embedded start timecode. Otherwise the report is built from the cut
  alone: duration = the furthest source end seen (an honest lower bound,
  raised by ``metadata["media_duration_seconds"]`` when a reader carried
  it) and ``media_start`` from ``metadata["media_start_seconds"]`` when
  present. Sources that don't exist on disk still work — the export
  carries their paths/reels through — and are noted.

:func:`distill` then plans the trailer with
:func:`monteur.montage.plan_montage` (default ``order="chronological"``
so the trailer preserves the film's arc, default ``style="trailer"``),
forwarding ``pace`` / ``transitions`` / ``sfx`` / ``allow_repeats`` etc.
The repetition guard stays active — a 60s trailer distilled from 12
minutes of material never repeats a shot. The returned plan is exported
by the caller (:func:`monteur.montage.montage_to_timeline` is untouched).
"""

from __future__ import annotations

from pathlib import Path

from monteur.media import MonteurMediaError, probe, start_timecode_seconds
from monteur.model import Clip, Timeline, frames_to_seconds
from monteur.montage import CHRONOLOGICAL, MontagePlan, plan_montage
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport, Moment

__all__ = ["timeline_to_reports", "distill"]

# The editor's prior: everything in the cut is at least this good...
_BASE_SCORE = 0.75
# ...plus up to this much for screen time relative to the longest shot.
_SCREEN_TIME_BONUS = 0.2

_EPS = 1e-6


def _meta_float(clip: Clip, key: str) -> float:
    """A positive float from clip metadata, 0.0 when absent or malformed."""
    try:
        value = float(clip.metadata.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _merge_spans(
    spans: list[tuple[float, float, str]]
) -> list[tuple[float, float, str]]:
    """Merge overlapping/touching (start, end, label) spans, sorted.

    A shot used twice in the film (two overlapping source ranges from the
    same file) is one moment; the first non-empty label of the merged
    constituents survives.
    """
    merged: list[list] = []
    for start, end, label in sorted(spans, key=lambda s: (s[0], s[1])):
        if merged and start <= merged[-1][1] + _EPS:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = merged[-1][2] or label
        else:
            merged.append([start, end, label])
    return [(s, e, lab) for s, e, lab in merged]


def timeline_to_reports(
    timeline: Timeline, probe_media: bool = True
) -> list[ClipReport]:
    """Turn a finished cut into :class:`ClipReport` material for planning.

    See the module docstring for the grouping, merge, scoring and probe
    rules. Raises ValueError when the timeline has no video clips (or none
    with a positive source range) — there is nothing to distill.
    """
    fps = timeline.fps
    if fps <= 0:
        raise ValueError(f"timeline {timeline.name!r} has no valid frame rate")
    video = timeline.video_clips()
    if not video:
        raise ValueError(
            f"timeline {timeline.name!r} has no video clips — nothing to distill"
        )

    # Group by source, in order of first appearance in the cut.
    groups: dict[str, dict] = {}
    for clip in video:
        key = clip.source_file or clip.source_name or clip.name or "untitled"
        group = groups.setdefault(
            key,
            {"spans": [], "meta_start": 0.0, "meta_duration": 0.0, "has_file": False},
        )
        if clip.source_file:
            group["has_file"] = True
        start = frames_to_seconds(clip.source_in, fps)
        end = frames_to_seconds(clip.source_out, fps)
        if end - start > _EPS:
            group["spans"].append(
                (start, end, str(clip.metadata.get("label") or ""))
            )
        if not group["meta_start"]:
            group["meta_start"] = _meta_float(clip, "media_start_seconds")
        group["meta_duration"] = max(
            group["meta_duration"], _meta_float(clip, "media_duration_seconds")
        )

    merged: dict[str, list[tuple[float, float, str]]] = {
        key: _merge_spans(group["spans"])
        for key, group in groups.items()
        if group["spans"]
    }
    if not merged:
        raise ValueError(
            f"timeline {timeline.name!r} has no video material — nothing to distill"
        )

    # Screen time relative to the LONGEST shot anywhere in the cut.
    longest = max(end - start for spans in merged.values() for start, end, _ in spans)

    reports: list[ClipReport] = []
    for key, spans in merged.items():
        group = groups[key]
        max_end = max(end for _, end, _ in spans)
        notes: list[str] = []
        info = None
        if probe_media and Path(key).exists():
            try:
                info = probe(key)
            except MonteurMediaError as exc:
                notes.append(
                    f"could not probe {key}: {exc}; duration is a lower bound from the cut"
                )
        if info is not None:
            duration = info.duration
            media_start = start_timecode_seconds(info) or group["meta_start"]
        else:
            duration = max(max_end, group["meta_duration"])
            media_start = group["meta_start"]
            if not group["has_file"]:
                notes.append(
                    f"source {key!r} is a reel/clip name, not a file path — "
                    "relink manually in Resolve"
                )
            elif probe_media and not notes:
                notes.append(
                    f"{key} not found on disk; duration {duration:.1f}s is a "
                    "lower bound from the cut (the export carries the path through)"
                )
        moments = [
            Moment(
                start=start,
                end=end,
                score=min(
                    1.0, _BASE_SCORE + _SCREEN_TIME_BONUS * (end - start) / longest
                ),
                label=label,
            )
            for start, end, label in spans
        ]
        moments.sort(key=lambda m: (-m.score, m.start))  # ClipReport: best first
        reports.append(
            ClipReport(
                path=key,
                duration=duration,
                moments=moments,
                usable_ratio=1.0,  # the editor already judged it usable
                notes=notes,
                media_start=media_start,
            )
        )
    return reports


def distill(
    timeline: Timeline,
    music: MusicAnalysis | None,
    target: float = 60.0,
    style: str = "trailer",
    *,
    probe_media: bool = True,
    **plan_kwargs,
) -> MontagePlan:
    """Distill a finished cut into a trailer plan of about ``target`` seconds.

    ``timeline_to_reports`` turns the cut into moment material (see the
    module docstring), then :func:`monteur.montage.plan_montage` lays the
    trailer onto ``music`` (or a no-music grid when ``music`` is None —
    render that plan with ``montage_to_timeline(..., audio="original")``).

    ``order`` defaults to ``"chronological"`` so the trailer preserves the
    film's arc; pass ``order="best_first"`` (or any other ``plan_montage``
    keyword: ``pace``, ``transitions``, ``sfx``, ``allow_repeats``,
    ``end_on_phrase``, ``cut_lead``, ...) through ``plan_kwargs``. The
    repetition guard stays active by default — a 60s trailer distilled
    from 12 minutes of material never repeats a shot.

    The returned plan carries a leading note
    ``distilled from '<name>': N shots, M unique sources`` and, when some
    sources are not files on disk (bare EDL reels, unmounted media), a
    trailing note that relinking will be manual. Export is the caller's
    job (:func:`monteur.montage.montage_to_timeline` + the io writers).
    """
    reports = timeline_to_reports(timeline, probe_media=probe_media)
    order = plan_kwargs.pop("order", CHRONOLOGICAL)
    plan = plan_montage(
        reports,
        music,
        order=order,
        max_duration=target,
        style=style,
        **plan_kwargs,
    )
    n_shots = len(timeline.video_clips())
    plan.notes.insert(
        0,
        f"distilled from '{timeline.name}': {n_shots} shots, "
        f"{len(reports)} unique sources",
    )
    missing = [r.path for r in reports if not Path(r.path).exists()]
    if missing:
        plan.notes.append(
            f"{len(missing)} of {len(reports)} sources are not files on disk "
            f"(e.g. {missing[0]!r}); relinking in Resolve will be manual"
        )
    return plan
