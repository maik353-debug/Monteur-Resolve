"""Tests for the Serien-Modus engine (monteur.series.plan_series).

One tour folder -> N genuinely different vertical Shorts. The headline
promise is ZERO moments repeated across the whole series; the rest is
honest degradation, valid short-style plans, and determinism. Synthetic
ClipReports / MusicAnalysis are built directly (no real media), exactly
like the montage tests.
"""

from __future__ import annotations

import pytest

from monteur.music import MusicAnalysis, MusicSection
from monteur.series import (
    DEFAULT_SHORT_SECONDS,
    SeriesShort,
    plan_series,
    restrict_to_edit,
    series_from_edit,
    used_source_ranges,
)
from monteur.sift import ClipReport, Moment


def make_music() -> MusicAnalysis:
    """24 beats at 0.5s (120 bpm) over 12s; low/mid/high sections."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=12.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(24)],
        sections=[
            MusicSection(0.0, 4.0, 0.2, "low"),
            MusicSection(4.0, 8.0, 0.5, "mid"),
            MusicSection(8.0, 12.0, 0.9, "high"),
        ],
    )


def make_tour(n_clips: int = 6, per_clip: int = 3) -> list[ClipReport]:
    """A long ride: many clips, several well-separated moments each.

    Moments sit far apart (30 s grid) so a drop hold can never bleed one
    short's source range into another's. Scores descend by clip and by
    position, so the strength ranking is deterministic and legible.
    """
    reports: list[ClipReport] = []
    for c in range(n_clips):
        moments = [
            Moment(
                20.0 + p * 30.0,
                24.0 + p * 30.0,
                0.9 - 0.05 * c - 0.02 * p,
                label=("tunnel" if p == 0 else f"c{c}p{p}"),
                daylight=("day", "golden", "night")[p % 3],
                shot_size=("wide", "medium", "close")[c % 3],
            )
            for p in range(per_clip)
        ]
        reports.append(
            ClipReport(path=f"/footage/clip{c:02d}.mp4", duration=180.0, moments=moments)
        )
    return reports


def _source_ranges(short: SeriesShort) -> list[tuple[str, float, float]]:
    """Every entry's source span (clip, start, end) in the short's plan."""
    return [
        (e.clip_path, e.source_start, e.source_end) for e in short.plan.entries
    ]


def _overlaps(a: tuple[str, float, float], b: tuple[str, float, float]) -> bool:
    """True if two (clip, start, end) source ranges share frames."""
    if a[0] != b[0]:
        return False
    return a[1] < b[2] - 1e-6 and b[1] < a[2] - 1e-6


# --- the headline promise -----------------------------------------------------


def test_zero_moment_repeat_across_series():
    """No source range appears (or overlaps) in two shorts — the promise."""
    shorts = plan_series(make_tour(6, 3), make_music(), count=3)
    assert len(shorts) == 3
    ranges_per_short = [_source_ranges(s) for s in shorts]
    # Each short actually cut something.
    assert all(ranges for ranges in ranges_per_short)
    # No source range is shared or overlapping across any two shorts.
    for i in range(len(ranges_per_short)):
        for j in range(i + 1, len(ranges_per_short)):
            for a in ranges_per_short[i]:
                for b in ranges_per_short[j]:
                    assert not _overlaps(a, b), (
                        f"shorts {i} and {j} share footage: {a} vs {b}"
                    )


def test_distinct_seeds_across_series():
    """Every short is built around a DIFFERENT seed moment."""
    shorts = plan_series(make_tour(6, 3), make_music(), count=4)
    seeds = [(s.seed.clip_path, s.seed.start) for s in shorts]
    assert len(seeds) == len(set(seeds))


# --- honest degradation -------------------------------------------------------


def test_degrades_when_material_is_thin():
    """Fewer distinct seeds than asked -> M < N shorts, and a note says so."""
    # Two clips, one moment each: at most 2 distinct seeds however many asked.
    reports = [
        ClipReport(path="/footage/a.mp4", duration=60.0, moments=[Moment(10.0, 13.0, 0.9)]),
        ClipReport(path="/footage/b.mp4", duration=60.0, moments=[Moment(10.0, 13.0, 0.8)]),
    ]
    shorts = plan_series(reports, make_music(), count=5)
    assert len(shorts) == 2
    assert all("requested 5" in s.note for s in shorts)
    # And still zero-repeat.
    a = {(e.clip_path, e.source_start) for e in shorts[0].plan.entries}
    b = {(e.clip_path, e.source_start) for e in shorts[1].plan.entries}
    assert not (a & b)


def test_single_clip_close_moments_yields_one():
    """A single clip whose moments cluster together degrades to one short."""
    # Three moments within the min seed gap of each other -> one seed only.
    reports = [
        ClipReport(
            path="/footage/solo.mp4",
            duration=30.0,
            moments=[Moment(2.0, 4.0, 0.9), Moment(4.0, 6.0, 0.8), Moment(6.0, 8.0, 0.7)],
        )
    ]
    shorts = plan_series(reports, make_music(), count=3)
    assert len(shorts) == 1
    assert "short 1 of 1" in shorts[0].note


def test_empty_and_no_moments():
    """No reports / no moments -> an empty series (nothing to build)."""
    assert plan_series([], make_music(), count=3) == []
    empty = [ClipReport(path="/footage/x.mp4", duration=10.0, moments=[])]
    assert plan_series(empty, make_music(), count=3) == []


# --- each short is a valid short-style vertical plan --------------------------


def test_each_short_is_a_valid_short_plan():
    """Every short is a hook/punch/loop 'short'-style plan on the song."""
    shorts = plan_series(make_tour(6, 3), make_music(), count=3, canvas="vertical-uhd")
    for s in shorts:
        assert s.canvas == "vertical-uhd"
        assert s.plan.entries, "a short with no cuts is not a short"
        assert s.plan.music_path == "/music/song.wav"
        # The 'short' style arc opens on a hook phase.
        assert s.plan.phases and s.plan.phases[0][2] == "hook"
        assert s.plan.duration > 0


def test_no_music_keeps_original_sound():
    """music=None cuts to the clips' own sound (max_seconds supplies length)."""
    shorts = plan_series(make_tour(4, 2), None, count=2, max_seconds=20.0)
    assert len(shorts) == 2
    for s in shorts:
        assert s.plan.music_path == ""
        assert s.plan.entries


# --- determinism --------------------------------------------------------------


def test_deterministic_same_series():
    """Same reports + music + count -> identical series and order."""
    a = plan_series(make_tour(6, 3), make_music(), count=3)
    b = plan_series(make_tour(6, 3), make_music(), count=3)
    assert len(a) == len(b)
    for sa, sb in zip(a, b):
        assert sa.note == sb.note
        assert (sa.seed.clip_path, sa.seed.start) == (sb.seed.clip_path, sb.seed.start)
        assert _source_ranges(sa) == _source_ranges(sb)


# --- variety: seeds spread across clips before doubling up on one -------------


def test_seeds_prefer_distinct_clips():
    """With enough clips, every seed is on a different clip."""
    shorts = plan_series(make_tour(6, 3), make_music(), count=5)
    clips = [s.seed.clip_path for s in shorts]
    assert len(clips) == len(set(clips)) == 5


def test_seeds_fall_back_within_a_clip_when_needed():
    """More shorts than clips: extra seeds come from within a clip, gap-apart."""
    # One clip, moments 40 s apart -> well past the min seed gap.
    reports = [
        ClipReport(
            path="/footage/long.mp4",
            duration=200.0,
            moments=[Moment(10.0 + i * 40.0, 13.0 + i * 40.0, 0.9 - 0.05 * i) for i in range(3)],
        )
    ]
    shorts = plan_series(reports, make_music(), count=3)
    assert len(shorts) == 3
    starts = sorted(s.seed.start for s in shorts)
    # Every pair of seeds is at least the min gap apart.
    assert all(starts[i + 1] - starts[i] >= 8.0 for i in range(len(starts) - 1))


# --- guard rails --------------------------------------------------------------


def test_count_must_be_positive():
    with pytest.raises(ValueError):
        plan_series(make_tour(), make_music(), count=0)


def test_unknown_canvas_rejected():
    with pytest.raises(ValueError):
        plan_series(make_tour(), make_music(), count=2, canvas="square")


def test_default_short_length_cap_is_sane():
    assert 10.0 <= DEFAULT_SHORT_SECONDS <= 90.0


# --- shorts from an existing long-form EDIT (project -> shorts) ---------------


def _edit_plan(*spans: tuple[str, float, float]) -> dict:
    """A minimal plan_to_dict with the given (clip_path, src_start, src_end) uses."""
    return {
        "monteur_plan": 1,
        "entries": [
            {"clip_path": c, "source_start": a, "source_end": b}
            for c, a, b in spans
        ],
    }


def test_used_source_ranges_reads_a_plan():
    plan = _edit_plan(
        ("/footage/clip00.mp4", 20.0, 24.0),
        ("/footage/clip00.mp4", 80.0, 84.0),
        ("/footage/clip02.mp4", 20.0, 24.0),
    )
    ranges = used_source_ranges(plan)
    assert ranges["/footage/clip00.mp4"] == [(20.0, 24.0), (80.0, 84.0)]
    assert ranges["/footage/clip02.mp4"] == [(20.0, 24.0)]


def test_used_source_ranges_skips_junk():
    plan = _edit_plan(("/a.mp4", 5.0, 5.0))  # zero length
    plan["entries"].append({"clip_path": "", "source_start": 1, "source_end": 2})
    plan["entries"].append({"clip_path": "/b.mp4", "source_start": "x", "source_end": 2})
    assert used_source_ranges(plan) == {}
    assert used_source_ranges(None) == {}


def test_restrict_keeps_only_used_moments():
    reports = make_tour(6, 3)  # each clip: moments at 20, 50, 80 s
    # the edit used clip00's first moment and clip02's second moment
    plan = _edit_plan(
        ("/footage/clip00.mp4", 20.0, 24.0),
        ("/footage/clip02.mp4", 50.0, 54.0),
    )
    kept = restrict_to_edit(reports, plan)
    paths = {r.path for r in kept}
    assert paths == {"/footage/clip00.mp4", "/footage/clip02.mp4"}
    c0 = next(r for r in kept if r.path == "/footage/clip00.mp4")
    assert [m.start for m in c0.moments] == [20.0]  # only the used beat
    c2 = next(r for r in kept if r.path == "/footage/clip02.mp4")
    assert [m.start for m in c2.moments] == [50.0]


def test_restrict_matches_by_basename_when_paths_drift():
    reports = make_tour(2, 2)  # absolute /footage/clipNN.mp4
    plan = _edit_plan(("clip00.mp4", 20.0, 24.0))  # relative path in the plan
    kept = restrict_to_edit(reports, plan)
    assert {r.path for r in kept} == {"/footage/clip00.mp4"}


def test_restrict_empty_for_no_plan():
    assert restrict_to_edit(make_tour(), None) == []
    assert restrict_to_edit(make_tour(), {"entries": []}) == []


def test_series_from_edit_extracts_the_edits_beats():
    reports = make_tour(6, 3)
    # the edit used one strong beat in each of four distinct clips
    plan = _edit_plan(
        ("/footage/clip00.mp4", 20.0, 24.0),
        ("/footage/clip01.mp4", 20.0, 24.0),
        ("/footage/clip02.mp4", 20.0, 24.0),
        ("/footage/clip03.mp4", 20.0, 24.0),
    )
    shorts, from_edit = series_from_edit(reports, plan, make_music(), count=3)
    assert from_edit is True
    assert len(shorts) == 3
    # every seed is a beat the edit actually used
    used = used_source_ranges(plan)
    for s in shorts:
        spans = used.get(s.seed.clip_path, [])
        assert any(
            min(s.seed.end, b) - max(s.seed.start, a) > 1e-6 for a, b in spans
        ), f"seed {s.seed.clip_path}@{s.seed.start} not from the edit"


def test_series_from_edit_falls_back_when_the_edit_is_too_lean():
    reports = make_tour(6, 3)  # rich footage
    plan = _edit_plan(("/footage/clip00.mp4", 20.0, 24.0))  # edit used ONE beat
    shorts, from_edit = series_from_edit(reports, plan, make_music(), count=3)
    # one beat can't seed three distinct shorts -> fall back to the full pool
    assert from_edit is False
    assert len(shorts) == 3


def test_series_from_edit_no_plan_uses_the_pool():
    reports = make_tour(6, 3)
    shorts, from_edit = series_from_edit(reports, None, make_music(), count=3)
    assert from_edit is False
    assert len(shorts) == 3
