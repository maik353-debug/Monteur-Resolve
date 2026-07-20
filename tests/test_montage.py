"""Tests for the montage builder (monteur.montage).

MusicAnalysis / ClipReport objects are constructed directly — the analysis
modules are implemented separately and are not exercised here.
"""

from __future__ import annotations

import pytest

from monteur.montage import (
    BEST_FIRST,
    CHRONOLOGICAL,
    montage_to_timeline,
    plan_montage,
)
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import ClipReport, Moment


def make_music() -> MusicAnalysis:
    """24 beats at 0.5s spacing (120 bpm) over 12s; low/mid/high sections."""
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


def make_reports() -> list[ClipReport]:
    a = ClipReport(
        path="/footage/a.mp4",
        duration=30.0,
        moments=[Moment(1.0, 6.0, 0.9), Moment(10.0, 12.0, 0.5), Moment(20.0, 23.0, 0.7)],
        usable_ratio=0.8,
    )
    b = ClipReport(
        path="/footage/b.mp4",
        duration=25.0,
        moments=[Moment(2.0, 5.0, 0.95), Moment(8.0, 10.0, 0.6), Moment(15.0, 19.0, 0.8)],
        usable_ratio=0.7,
    )
    return [a, b]


def slot_length(entry) -> float:
    return entry.record_end - entry.record_start


# --- grid ---------------------------------------------------------------------


def test_grid_density_follows_section_energy():
    # Faster where loud: each section's cuts average at least twice the next
    # louder section's, no cut is ever faster than the section's base step,
    # and (the rhythm canon) a section opens on a hold with a breath later —
    # not one metronomic interval.
    plan = plan_montage(make_reports(), make_music(), cut_lead=0.0)
    assert plan.duration == 12.0
    low = [slot_length(e) for e in plan.entries if e.record_start < 4.0]
    mid = [slot_length(e) for e in plan.entries if 4.0 <= e.record_start < 8.0]
    high = [slot_length(e) for e in plan.entries if e.record_start >= 8.0]
    assert low and mid and high
    avg = lambda xs: sum(xs) / len(xs)
    assert avg(low) > avg(mid) > avg(high)
    # base steps are the floor: low >= 4 beats, mid >= 2, high >= 1
    assert min(low) >= 2.0 - 1e-9
    assert min(mid) >= 1.0 - 1e-9
    assert min(high) >= 0.5 - 1e-9
    # the exact grid: mid and high open on a hold, high breathes mid-run
    assert low == pytest.approx([2.0, 2.0])
    assert mid == pytest.approx([2.0, 1.0, 1.0])
    assert high == pytest.approx([1.0, 0.5, 0.5, 0.5, 1.0, 0.5])
    assert len(plan.entries) == 11
    assert any("rhythm" in n for n in plan.notes)


def test_grid_is_contiguous_and_ends_on_duration():
    plan = plan_montage(make_reports(), make_music())
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(12.0)


def test_no_beats_falls_back_to_fixed_grid():
    music = MusicAnalysis(path="/music/song.wav", duration=12.0, tempo=0.0, beats=[])
    plan = plan_montage(make_reports(), music, cut_lead=0.0)
    assert all(slot_length(e) == pytest.approx(2.0) for e in plan.entries)
    assert len(plan.entries) == 6
    assert any("no beats" in note for note in plan.notes)


def test_max_duration_truncates_cleanly():
    plan = plan_montage(make_reports(), make_music(), max_duration=5.0)
    assert plan.duration == 5.0
    # a 5s cut is placed against the song's strongest passage (the high tail
    # at 8-12s), not its low-energy intro, so music_start jumps into the window
    assert plan.music_start == pytest.approx(7.0)
    assert any("using the song's strongest 5s (from 0:07)" in n for n in plan.notes)
    # grid still tiles the montage window contiguously and ends on the length
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(5.0)


def test_anti_strobe_doubles_dense_grid():
    music = MusicAnalysis(
        path="/music/fast.wav",
        duration=12.0,
        tempo=300.0,
        beats=[i * 0.2 for i in range(60)],
        sections=[MusicSection(0.0, 12.0, 0.9, "high")],
    )
    plan = plan_montage(make_reports(), music, cut_lead=0.0)
    assert plan.entries
    for e in plan.entries:
        assert slot_length(e) >= 0.4 - 1e-9


# --- ordering -----------------------------------------------------------------


def test_chronological_orders_by_record_time_and_clip_order():
    plan = plan_montage(make_reports(), make_music(), order=CHRONOLOGICAL)
    starts = [e.record_start for e in plan.entries]
    assert starts == sorted(starts)
    # first pass follows (clip path, moment start) order
    first_pass = plan.entries[:6]
    assert [e.clip_path for e in first_pass] == ["/footage/a.mp4"] * 3 + ["/footage/b.mp4"] * 3
    a_sources = [e.source_start for e in first_pass[:3]]
    assert a_sources == sorted(a_sources)


def test_best_first_puts_top_moment_in_high_section():
    plan = plan_montage(make_reports(), make_music(), order=BEST_FIRST)
    # entries come back sorted by record time
    starts = [e.record_start for e in plan.entries]
    assert starts == sorted(starts)
    top_entries = [e for e in plan.entries if e.score == 0.95]
    assert top_entries  # b.mp4's 2.0-5.0 moment
    # its first (fresh) piece starts at the moment start and sits in "high"
    first_use = min(top_entries, key=lambda e: e.source_start)
    assert first_use.source_start == 2.0
    assert 8.0 <= first_use.record_start < 12.0  # landed in the "high" section


# --- reuse --------------------------------------------------------------------


def test_every_moment_used_once_before_reuse_and_repeat_noted():
    music = MusicAnalysis(
        path="/music/song.wav",
        duration=12.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(24)],
        sections=[MusicSection(0.0, 12.0, 0.9, "high")],
    )
    report = ClipReport(
        path="/footage/short.mp4",
        duration=2.0,
        moments=[Moment(0.0, 0.5, 0.9), Moment(1.0, 1.5, 0.8)],
    )
    plan = plan_montage([report], music, order=CHRONOLOGICAL, allow_repeats=True, cut_lead=0.0)
    # beat-based slots over 12s (0.5s base with the section hold + breaths)
    assert len(plan.entries) == 19
    # both moments used once before anything repeats
    assert {plan.entries[0].source_start, plan.entries[1].source_start} == {0.0, 1.0}
    reused = [e for e in plan.entries[2:] if e.source_start in (0.0, 1.0)]
    assert reused, "footage should repeat once the pool is exhausted"
    assert any("reused" in note or "repeat" in note for note in plan.notes)


def test_long_moment_sliced_into_fresh_pieces_before_repeating():
    music = MusicAnalysis(
        path="/music/song.wav",
        duration=4.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(8)],
        sections=[MusicSection(0.0, 4.0, 0.9, "high")],
    )
    report = ClipReport(
        path="/footage/long.mp4",
        duration=10.0,
        moments=[Moment(0.0, 4.0, 0.9), Moment(5.0, 5.5, 0.5)],
    )
    plan = plan_montage([report], music, order=CHRONOLOGICAL)
    assert len(plan.entries) == 6
    # all pieces from the long moment are non-overlapping slices
    long_pieces = [
        (e.source_start, e.source_end) for e in plan.entries if e.source_start < 4.0
    ]
    long_pieces.sort()
    for (s1, e1), (s2, e2) in zip(long_pieces, long_pieces[1:]):
        assert s2 >= e1 - 1e-9


# --- timeline rendering ---------------------------------------------------------


# --- styles ---------------------------------------------------------------------


def make_arc_music(drops: list[float] | None = None) -> MusicAnalysis:
    """40s track: beats every 0.5s, downbeats every 2s, phrases every 8s."""
    return MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
        drops=drops or [],
    )


def make_long_reports() -> list[ClipReport]:
    """Plenty of 2s moments so style plans have material for many slots."""
    return [
        ClipReport(
            path="/footage/long.mp4",
            duration=120.0,
            moments=[Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(20)],
        )
    ]


def test_unknown_style_raises_with_valid_list():
    with pytest.raises(ValueError) as excinfo:
        plan_montage(make_reports(), make_music(), style="vlog")
    message = str(excinfo.value)
    for key in ("auto", "travel", "wedding", "music_video", "trailer"):
        assert key in message


def test_default_auto_style_matches_previous_behavior():
    baseline = plan_montage(make_reports(), make_music())
    explicit = plan_montage(make_reports(), make_music(), style="auto")
    assert [
        (e.clip_path, e.source_start, e.source_end, e.record_start, e.record_end)
        for e in baseline.entries
    ] == [
        (e.clip_path, e.source_start, e.source_end, e.record_start, e.record_end)
        for e in explicit.entries
    ]


def test_travel_phase_beat_densities():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel", cut_lead=0.0)
    assert plan.duration == 40.0
    # phases after phrase snapping: opening 0-8, build 8-16, climax 16-32,
    # outro 32-40. Each phase averages its own density — faster where the
    # arc peaks — while the rhythm canon varies lengths WITHIN the phases.
    opening = [slot_length(e) for e in plan.entries if e.record_start < 8.0]
    build = [slot_length(e) for e in plan.entries if 8.0 <= e.record_start < 16.0]
    climax = [slot_length(e) for e in plan.entries if 16.0 <= e.record_start < 32.0]
    outro = [slot_length(e) for e in plan.entries if e.record_start >= 32.0]
    avg = lambda xs: sum(xs) / len(xs)
    assert avg(opening) > avg(build) > avg(climax)
    assert avg(outro) > avg(climax)
    # the fastest cuts are the climax base (1 beat = 0.5s), nothing faster
    assert min(climax) == pytest.approx(0.5)
    assert min(slot_length(e) for e in plan.entries) >= 0.5 - 1e-9
    # the exact phase textures: opening hold, build accelerando, climax
    # pattern with 2-beat accents, outro decelerando
    assert opening == pytest.approx([4.0, 2.0, 2.0])
    assert build == pytest.approx([2.0, 1.5, 1.5, 1.0, 1.0, 0.5, 0.5])
    assert outro == pytest.approx([2.0, 2.0, 4.0])
    assert len(plan.entries) == 3 + 7 + 26 + 3
    # grid stays contiguous and closes on the montage length
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(40.0)
    assert any("travel" in note for note in plan.notes)


def test_phase_boundaries_snap_to_phrase_starts():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel", cut_lead=0.0)
    by_start = {round(e.record_start, 6): e for e in plan.entries}
    # raw boundaries 6.0 / 20.0 / 34.0 snap to the phrase grid (multiples of 8)
    assert {8.0, 16.0, 32.0} <= set(by_start)
    # 6.0 is still inside the opening: its slot runs to the snapped 8.0 bound
    assert by_start[6.0].record_end == pytest.approx(8.0)
    assert slot_length(by_start[6.0]) == pytest.approx(2.0)
    # the climax density (1-beat base) starts at the snapped 16.0, not 20.0
    assert slot_length(by_start[16.0]) == pytest.approx(0.5)
    # 32.0 starts the outro (2s slots), so the climax->outro boundary moved off 34.0
    assert slot_length(by_start[32.0]) == pytest.approx(2.0)
    assert 34.0 not in by_start or slot_length(by_start[34.0]) >= 2.0 - 1e-9
    assert any("snapped to phrase" in note for note in plan.notes)


def test_drop_aligns_travel_climax():
    plan = plan_montage(
        make_long_reports(), make_arc_music(drops=[20.0]), style="travel", cut_lead=0.0
    )
    assert any("climax aligned to drop at 20.0s" in note for note in plan.notes)
    # a cut lands exactly on the drop, and the drop slot is a HOLD: 3 beats
    # (1.5s), longer than its neighbours — impact needs screen time
    drop_entries = [e for e in plan.entries if e.record_start == pytest.approx(20.0)]
    assert len(drop_entries) == 1
    assert slot_length(drop_entries[0]) == pytest.approx(1.5)
    # the build ends in the one-beat stutter burst that sharpens the drop
    before = [e for e in plan.entries if e.record_end == pytest.approx(20.0)]
    assert len(before) == 1
    assert slot_length(before[0]) == pytest.approx(0.5)
    assert slot_length(drop_entries[0]) > slot_length(before[0])


def test_drop_in_auto_forces_cut_and_takes_highest_highlight():
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=200.0,
        beats=[i * 0.3 for i in range(134)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],  # 2-beat cuts = 0.6s
        drops=[20.0],
    )
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=[
            Moment(0.0, 2.0, 0.9, highlight=0.1),
            Moment(10.0, 12.0, 0.8, highlight=0.2),
            Moment(30.0, 32.0, 0.4, highlight=0.95),  # the audible peak
        ],
    )
    plan = plan_montage([report], music, style="auto", allow_repeats=True, cut_lead=0.0)
    # 20.0 is not on the 0.6s grid; the drop forces a cut there
    drop_entries = [e for e in plan.entries if e.record_start == pytest.approx(20.0)]
    assert len(drop_entries) == 1
    # ...and the highest-highlight moment is reserved for that slot
    assert drop_entries[0].source_start == pytest.approx(30.0)
    assert any("drop" in note for note in plan.notes)


def test_highlight_preference_in_climax_phase():
    fillers = [Moment(i * 2.0, i * 2.0 + 2.0, 0.8) for i in range(12)]
    quiet_good = Moment(40.0, 42.0, 0.9, highlight=0.0)
    loud_cheer = Moment(44.0, 46.0, 0.5, highlight=0.9)
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=fillers + [quiet_good, loud_cheer],
    )
    plan = plan_montage(
        [report], make_arc_music(), style="travel", order=CHRONOLOGICAL, cut_lead=0.0
    )
    # the 12 fillers cover opening+build; at the first climax slot (16.0s)
    # the (highlight, score) sort puts the cheer ahead of the higher-scored moment
    climax_first = [e for e in plan.entries if e.record_start == pytest.approx(16.0)]
    assert len(climax_first) == 1
    assert climax_first[0].source_start == pytest.approx(44.0)


def test_motion_matching_breaks_near_ties():
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=1.5,
        tempo=120.0,
        beats=[i * 0.5 for i in range(4)],
        sections=[MusicSection(0.0, 1.5, 0.9, "high")],  # 0.5s slots -> 3 slots
    )
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=[
            Moment(0.0, 2.0, 0.9, exit_motion=(10.0, 0.0)),  # pans right on exit
            Moment(10.0, 12.0, 0.8, entry_motion=(-8.0, 0.0)),  # enters panning left
            Moment(20.0, 22.0, 0.7, entry_motion=(9.0, 1.0)),  # enters panning right
        ],
    )
    plan = plan_montage([report], music, order=CHRONOLOGICAL)
    sources = [e.source_start for e in plan.entries]
    # slot 2 skips the opposite-motion moment (10.0) for the matching one (20.0)
    assert sources == pytest.approx([0.0, 20.0, 10.0])


def test_style_plan_renders_timeline_unchanged():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel")
    timeline = montage_to_timeline(plan, fps=25.0)
    video = timeline.video_clips()
    assert len(video) == len(plan.entries)
    assert video[0].record_in == 0
    for prev, nxt in zip(video, video[1:]):
        assert nxt.record_in == prev.record_out
    assert video[-1].record_out == 1000  # 40s * 25fps
    audio = timeline.audio_clips()
    assert len(audio) == 1
    assert (audio[0].record_in, audio[0].record_out) == (0, 1000)


# --- rhythm (the anti-monotony canon) --------------------------------------------
#
# The field complaint: "es ist und bleibt eine Aneinanderreihung von kurzen,
# GLEICH LANGEN Clips". Within a phase, cut lengths must VARY deliberately —
# these tests pin the canon: establishing hold, build accelerando, drop hold
# with a stutter burst, phrase-anchored pattern texture, outro decelerando.


def test_phase_cut_lengths_deterministic_and_quantized():
    from monteur.montage import _phase_cut_lengths

    kwargs = dict(n_units=32, base=1, pattern=(1, 1.5, 2, 1), first_hold=3)
    a = _phase_cut_lengths(**kwargs)
    b = _phase_cut_lengths(**kwargs)
    assert a == b  # same inputs -> same lengths, no RNG anywhere
    assert all(isinstance(v, int) and v >= 1 for v in a)  # whole beats only
    # fractional multipliers quantize to whole units (1.5 x 1 -> 2)
    assert set(a[1:]) <= {1, 2}
    assert a[0] == 3  # the hold is the FIRST cut, exactly as asked


def test_phase_cut_lengths_hold_is_clamped_to_the_phase():
    from monteur.montage import _phase_cut_lengths

    assert _phase_cut_lengths(n_units=2, base=1, first_hold=5) == [2]
    assert _phase_cut_lengths(n_units=0, base=1, first_hold=5) == []


def test_phase_cut_lengths_accelerando_is_monotone():
    from monteur.montage import _phase_cut_lengths

    lengths = _phase_cut_lengths(n_units=24, base=2, ramp_from=4.0, ramp_to=1.0)
    assert lengths[0] == 4 and lengths[-1] == 1
    for a, b in zip(lengths, lengths[1:]):
        assert b <= a  # steps down, never back up
    # a stutter reserves trailing one-unit cuts ahead of the drop
    stuttered = _phase_cut_lengths(
        n_units=24, base=2, ramp_from=4.0, ramp_to=1.0, stutter=3
    )
    assert stuttered[-3:] == [1, 1, 1]
    for a, b in zip(stuttered, stuttered[1:]):
        assert b <= a


def test_phase_cut_lengths_decelerando_sums_exactly():
    from monteur.montage import _phase_cut_lengths

    # non-decreasing body; the final hold is the REMAINDER slot, so the
    # body must sum to exactly n_units - min(2 x base, n_units)
    lengths = _phase_cut_lengths(n_units=8, base=2, decel=True)
    for a, b in zip(lengths, lengths[1:]):
        assert b >= a
    assert sum(lengths) == 8 - 4  # leaves a 2x-base final hold
    assert max(lengths, default=0) <= 4
    # a phase too short for a body is one single (hold) slot
    assert _phase_cut_lengths(n_units=2, base=2, decel=True) == []


def test_phase_cut_lengths_phrase_boundary_reanchors_the_cycle():
    from monteur.montage import _phase_cut_lengths

    plain = _phase_cut_lengths(n_units=12, base=1, pattern=(1, 1, 2, 1))
    anchored = _phase_cut_lengths(
        n_units=12, base=1, pattern=(1, 1, 2, 1), phrase_units=(2,)
    )
    assert plain == [1, 1, 2, 1, 1, 1, 2, 1, 1, 1]
    # the phrase at unit 2 restarts the cycle there: the 2x accent shifts
    assert anchored == [1, 1, 1, 1, 2, 1, 1, 1, 2, 1]
    assert anchored != plain


def test_opening_and_drop_hold_caps():
    from monteur.montage import _drop_hold, _opening_hold

    assert _opening_hold(1, 24) == 2  # ~2x the base
    assert _opening_hold(2, 24) == 4
    assert _opening_hold(2, 4) == 2  # capped: never eats the whole phase
    assert _opening_hold(4, 4) == 4  # degrades to the plain base
    assert _drop_hold(1) == 3  # aim 3x ...
    assert _drop_hold(2) == 4  # ... clamped to 4 ...
    assert _drop_hold(6) == 6  # ... but never below the phase's own base


def test_rhythm_first_cut_is_longest_of_opening():
    # Canon 1: the viewer must arrive — the montage's first shot holds
    # noticeably longer than the opening's base and no opening cut tops it.
    for style, base in (("travel", 2.0), ("wedding", 2.0), ("trailer", 2.0)):
        plan = plan_montage(
            make_long_reports(), make_arc_music(), style=style, cut_lead=0.0
        )
        opening = [e for e in plan.entries if e.record_start < 8.0]
        first = slot_length(opening[0])
        assert first > base, style
        assert first == max(slot_length(e) for e in opening), style


def test_rhythm_varies_within_phases():
    # THE monotony regression test: within any phase with >= 4 cuts, the
    # cut lengths are NOT all equal — never again "GLEICH LANGE Clips".
    cases = (
        ("travel", None, [(8.0, 16.0), (16.0, 32.0)]),
        ("wedding", None, [(8.0, 16.0), (16.0, 32.0)]),
        ("music_video", None, [(0.0, 8.0), (8.0, 16.0), (16.0, 32.0)]),
        ("trailer", [20.0], [(8.0, 16.0), (20.0, 32.0)]),
    )
    for style, drops, windows in cases:
        plan = plan_montage(
            make_long_reports(), make_arc_music(drops=drops), style=style, cut_lead=0.0
        )
        for lo, hi in windows:
            lengths = [
                round(slot_length(e), 6)
                for e in plan.entries
                if lo - 1e-9 <= e.record_start < hi - 1e-9
            ]
            assert len(lengths) >= 4, (style, lo, hi)
            assert len(set(lengths)) > 1, (style, lo, hi, lengths)


def test_rhythm_build_accelerando_travel():
    # Canon 3: across the build the cut lengths step down from the
    # opening's base (4 beats) toward the climax's (1 beat) — trailer ramp.
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel", cut_lead=0.0)
    build = [slot_length(e) for e in plan.entries if 8.0 <= e.record_start < 16.0]
    assert build[0] == pytest.approx(2.0) and build[-1] == pytest.approx(0.5)
    for a, b in zip(build, build[1:]):
        assert b <= a + 1e-9  # monotone trend, never speeds back up


def test_rhythm_drop_hold_and_stutter():
    # Canon 4: the slot ON the drop is a HOLD (2-4 beats, longer than its
    # neighbours, never the shortest); a one-beat stutter burst directly
    # before the drop sharpens it.
    plan = plan_montage(
        make_long_reports(), make_arc_music(drops=[20.0]), style="trailer", cut_lead=0.0
    )
    drop = next(e for e in plan.entries if e.record_start == pytest.approx(20.0))
    hold = slot_length(drop)
    assert 1.0 - 1e-9 <= hold <= 2.0 + 1e-9  # 2..4 beats at 120 bpm
    before = [e for e in plan.entries if e.record_end <= 20.0 + 1e-9][-3:]
    assert all(slot_length(e) == pytest.approx(0.5) for e in before)  # stutter
    assert hold > slot_length(before[-1])
    after = next(e for e in plan.entries if e.record_start == pytest.approx(drop.record_end))
    assert hold > slot_length(after) - 1e-9
    assert hold > min(slot_length(e) for e in plan.entries)  # never the shortest
    assert any("drop hold" in n and "stutter" in n for n in plan.notes)


def test_rhythm_drop_hold_in_auto():
    # The auto style's drop-forced cut holds >= 2 beats too: grid cuts
    # inside the hold window are cleared, the forced cut itself stays.
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=200.0,
        beats=[i * 0.3 for i in range(134)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        drops=[20.0],
    )
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=[Moment(0.0, 2.0, 0.9), Moment(10.0, 12.0, 0.8), Moment(30.0, 32.0, 0.4)],
    )
    plan = plan_montage([report], music, style="auto", allow_repeats=True, cut_lead=0.0)
    drop = next(e for e in plan.entries if e.record_start == pytest.approx(20.0))
    assert slot_length(drop) >= 2 * 0.3 - 1e-9


def test_rhythm_outro_decelerando():
    # Canon 6: each outro cut at least as long as the last; the FINAL shot
    # is the longest (up to 2x the outro base) and the total is unchanged.
    for style in ("travel", "wedding", "music_video", "trailer"):
        plan = plan_montage(
            make_long_reports(), make_arc_music(), style=style, cut_lead=0.0
        )
        outro = [slot_length(e) for e in plan.entries if e.record_start >= 32.0]
        assert outro, style
        for a, b in zip(outro, outro[1:]):
            assert b >= a - 1e-9, style
        assert outro[-1] == max(outro), style
        assert plan.entries[-1].record_end == pytest.approx(40.0), style


def test_rhythm_cuts_still_land_on_the_beat_grid():
    # Every guarantee stays: cuts on musical positions, total unchanged.
    music = make_arc_music(drops=[20.0])
    on_grid = {round(b, 6) for b in music.beats} | {round(d, 6) for d in music.downbeats}
    for style in ("travel", "wedding", "music_video", "trailer"):
        plan = plan_montage(make_long_reports(), music, style=style, cut_lead=0.0)
        for e in plan.entries:
            assert round(e.record_start, 6) in on_grid | {0.0}, (style, e.record_start)
        assert plan.entries[-1].record_end == pytest.approx(40.0)


def test_rhythm_is_deterministic():
    # Same inputs -> bit-identical grids, for styles and auto alike.
    for style in ("auto", "travel", "trailer"):
        a = plan_montage(make_long_reports(), make_arc_music(drops=[20.0]), style=style)
        b = plan_montage(make_long_reports(), make_arc_music(drops=[20.0]), style=style)
        assert [entry_key(e) for e in a.entries] == [entry_key(e) for e in b.entries]
        assert a.notes == b.notes


def test_rhythm_plan_note_summarizes_the_canon():
    plan = plan_montage(
        make_long_reports(), make_arc_music(drops=[20.0]), style="travel", cut_lead=0.0
    )
    note = next(n for n in plan.notes if n.startswith("rhythm: "))
    assert "opening hold 8 beats" in note
    assert "build ramps 4->1 beats" in note
    assert "drop hold 3 beats" in note
    assert "outro decays" in note


# --- energy windowing (short cut from a long song) ------------------------------


def make_windowed_music(drops: list[float] | None = None) -> MusicAnalysis:
    """100s song, quiet intro (0-60s) and an energetic tail (60-100s).

    Beats every 0.5s, downbeats every 2s, phrases every 8s, drop at 64s.
    """
    return MusicAnalysis(
        path="/music/long.wav",
        duration=100.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(200)],
        sections=[
            MusicSection(0.0, 60.0, 0.2, "low"),
            MusicSection(60.0, 100.0, 0.9, "high"),
        ],
        downbeats=[i * 2.0 for i in range(50)],
        phrases=[i * 8.0 for i in range(13)],
        drops=drops if drops is not None else [64.0],
    )


def test_short_cut_uses_strongest_window_and_aligns_drop():
    music = make_windowed_music(drops=[64.0])
    plan = plan_montage(make_long_reports(), music, max_duration=20.0, style="travel")
    # the cut is built against the drop's neighbourhood, with a 15% lead-in
    assert plan.music_start == pytest.approx(64.0 - 0.15 * 20.0)  # 61.0
    assert any(
        "using the song's strongest 20s (from 1:01)" in n for n in plan.notes
    )
    # the drop now falls inside the window (at 3.0s), so the climax aligns
    assert any("climax aligned to drop at 3.0s" in n for n in plan.notes)
    # all cuts are placed within the montage window [0, 20]
    assert plan.entries[0].record_start == 0.0
    for e in plan.entries:
        assert 0.0 - 1e-9 <= e.record_start < e.record_end <= 20.0 + 1e-9
    assert plan.entries[-1].record_end == pytest.approx(20.0)


def test_short_cut_music_clip_starts_at_window_offset():
    music = make_windowed_music(drops=[64.0])
    plan = plan_montage(make_long_reports(), music, max_duration=20.0, style="travel")
    timeline = montage_to_timeline(plan, fps=25.0)
    music_clip = timeline.audio_clips()[0]
    # A1 spans [music_start, music_start + duration] in frames
    assert music_clip.source_in == 1525  # 61.0s * 25fps
    assert music_clip.source_out - music_clip.source_in == 500  # 20s * 25fps
    assert (music_clip.record_in, music_clip.record_out) == (0, 500)


def test_full_song_montage_music_start_zero():
    plan = plan_montage(make_reports(), make_music())  # no max_duration
    assert plan.music_start == 0.0
    assert not any("using the song's strongest" in n for n in plan.notes)
    timeline = montage_to_timeline(plan, fps=25.0)
    music_clip = timeline.audio_clips()[0]
    # full-song behaviour is unchanged: music plays from the top
    assert music_clip.source_in == 0
    assert (music_clip.source_in, music_clip.source_out) == (0, 300)


# --- musical ending -------------------------------------------------------------


def make_ending_music(**overrides) -> MusicAnalysis:
    """100s track: beats every 0.5s, downbeats every 2s, phrases every 8s."""
    kwargs = dict(
        path="/music/track.wav",
        duration=100.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(200)],
        sections=[MusicSection(0.0, 100.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(50)],
        phrases=[i * 8.0 for i in range(13)],
    )
    kwargs.update(overrides)
    return MusicAnalysis(**kwargs)


def test_end_on_phrase_snaps_to_nearest_phrase():
    music = make_ending_music(phrases=[0.0, 56.0, 72.0])
    plan = plan_montage(make_long_reports(), music, max_duration=60.0)
    assert plan.duration == pytest.approx(56.0)
    assert plan.entries[-1].record_end == pytest.approx(56.0)
    assert any("length snapped to phrase at 56.0s" in n for n in plan.notes)


def test_end_on_phrase_refuses_snap_beyond_tolerance():
    # nearest phrase is 84s = 40% longer than requested; downbeats/beats
    # already sit exactly on 60s, so the length stays untouched
    music = make_ending_music(phrases=[0.0, 84.0])
    plan = plan_montage(make_long_reports(), music, max_duration=60.0)
    assert plan.duration == pytest.approx(60.0)
    assert not any("length snapped" in n for n in plan.notes)


def test_end_on_phrase_prefers_shorter_on_tie():
    # phrases every 8s: 56s and 64s are equidistant from the 60s request
    plan = plan_montage(make_long_reports(), make_ending_music(), max_duration=60.0)
    assert plan.duration == pytest.approx(56.0)


def test_end_snap_falls_back_to_downbeats():
    music = make_ending_music(phrases=[])
    plan = plan_montage(make_long_reports(), music, max_duration=60.9, allow_repeats=True)
    assert plan.duration == pytest.approx(60.0)
    assert any("length snapped to downbeat at 60.0s" in n for n in plan.notes)


def test_end_on_phrase_disabled():
    music = make_ending_music(phrases=[0.0, 56.0])
    plan = plan_montage(make_long_reports(), music, max_duration=60.0, end_on_phrase=False)
    assert plan.duration == pytest.approx(60.0)
    assert not any("length snapped" in n for n in plan.notes)


def test_full_song_montage_length_not_snapped():
    music = make_ending_music(phrases=[0.0, 96.0])  # 96s is within 12% of 100s
    plan = plan_montage(
        make_long_reports(), music, allow_repeats=True
    )  # no max_duration: full song (40s of moments would otherwise cap it)
    assert plan.duration == pytest.approx(100.0)
    assert not any("length snapped" in n for n in plan.notes)


# --- fades and dissolves ----------------------------------------------------------


def test_fade_fields_for_arc_style():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel")
    assert plan.fade_in == pytest.approx(0.5)
    # last outro slot is 2.0s, capped at 2.0s
    assert plan.fade_out == pytest.approx(2.0)
    assert any("fades to black: 0.5s in, 2s out" in n for n in plan.notes)


def test_fade_fields_for_auto_style():
    plan = plan_montage(make_reports(), make_music())
    assert plan.fade_in == pytest.approx(0.5)
    assert plan.fade_out == pytest.approx(1.0)
    assert any("fades to black: 0.5s in, 1s out" in n for n in plan.notes)


def test_transitions_in_gentle_phases_only():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel")
    # phases: opening 0-8 (slow slots), build 8-16, climax 16-32, outro 32-40
    assert plan.entries[0].transition == 0.0  # first entry: its fade is fade_in
    for e in plan.entries[1:]:
        if e.record_start < 8.0 or e.record_start >= 32.0:  # opening / outro
            assert e.transition == pytest.approx(0.5)  # min(0.5, half the slot)
        else:  # build / climax cut hard
            assert e.transition == 0.0
    dissolves = sum(1 for e in plan.entries if e.transition > 0)
    assert dissolves == 5  # 3 opening (minus the first) + 3 outro
    assert any(f"{dissolves} dissolves in gentle phases" in n for n in plan.notes)


def test_auto_low_sections_get_transitions():
    plan = plan_montage(make_reports(), make_music())
    assert plan.entries[0].transition == 0.0
    low = [e for e in plan.entries[1:] if e.record_start < 4.0]
    assert low and all(e.transition == pytest.approx(0.5) for e in low)
    assert all(e.transition == 0.0 for e in plan.entries if e.record_start >= 4.0)


def test_timeline_carries_transitions_and_fades():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel")
    timeline = montage_to_timeline(plan, fps=25.0)
    video = timeline.video_clips()
    assert "transition" not in video[0].metadata  # first clip: no dissolve
    second = video[1]  # opening entry at 2.0s carries the dissolve into it
    assert second.metadata["transition"] == "dissolve"
    assert second.metadata["transition_frames"] == round(0.5 * 25.0)
    climax = [c for c in video if 16 * 25 <= c.record_in < 32 * 25]
    assert climax and all("transition" not in c.metadata for c in climax)
    assert timeline.metadata["fade_in_frames"] == 12  # 0.5s at 25fps
    assert timeline.metadata["fade_out_frames"] == 50  # 2.0s at 25fps
    music_clip = timeline.audio_clips()[0]
    assert "transition" not in music_clip.metadata


# --- timeline rendering (existing behavior) -------------------------------------


def test_montage_to_timeline_exact_frames_at_25fps():
    plan = plan_montage(make_reports(), make_music(), order=CHRONOLOGICAL, cut_lead=0.0)
    timeline = montage_to_timeline(plan, fps=25.0)

    video = timeline.video_clips()
    assert len(video) == len(plan.entries)
    assert all(c.track == "V1" for c in video)
    # back-to-back on the grid, starting at frame 0
    assert video[0].record_in == 0
    for prev, nxt in zip(video, video[1:]):
        assert nxt.record_in == prev.record_out
    assert video[-1].record_out == 300  # 12s * 25fps
    # exact frame math: first entry is slot 0-2s from a.mp4's moment at 1.0s
    assert video[0].record_out == 50
    assert video[0].source_in == 25
    assert video[0].source_out == 75
    assert video[0].source_name == "a"
    assert video[0].name == "a"
    # every video clip's source length matches its record length
    for c in video:
        assert c.source_out - c.source_in == c.record_out - c.record_in

    audio = timeline.audio_clips()
    assert len(audio) == 1
    music_clip = audio[0]
    assert music_clip.track == "A1"
    assert music_clip.source_name == "song"
    assert (music_clip.source_in, music_clip.source_out) == (0, 300)
    assert (music_clip.record_in, music_clip.record_out) == (0, 300)

    assert timeline.duration == 300
    assert timeline.name == "Monteur Montage"
    assert len(timeline.markers) == 1
    assert timeline.markers[0].frame == 0
    assert timeline.markers[0].name == "Cut to song"


# --- embedded start timecode / real source metadata ------------------------------


def test_entries_carry_media_start_and_clip_duration():
    reports = make_reports()
    reports[0].media_start = 6472.32  # 01:47:52:08 at 25 fps
    reports[1].media_start = 7800.0  # 02:10:00:00
    plan = plan_montage(reports, make_music(), order=CHRONOLOGICAL)
    assert plan.entries
    for entry in plan.entries:
        if entry.clip_path == "/footage/a.mp4":
            assert entry.media_start == pytest.approx(6472.32)
            assert entry.clip_duration == pytest.approx(30.0)
        else:
            assert entry.media_start == pytest.approx(7800.0)
            assert entry.clip_duration == pytest.approx(25.0)


def test_montage_to_timeline_publishes_media_metadata():
    reports = make_reports()
    reports[0].media_start = 6472.32
    reports[1].media_start = 7800.0
    plan = plan_montage(reports, make_music(), order=CHRONOLOGICAL)
    timeline = montage_to_timeline(plan, fps=25.0)

    by_path = {"/footage/a.mp4": (6472.32, 30.0), "/footage/b.mp4": (7800.0, 25.0)}
    for clip, entry in zip(timeline.video_clips(), plan.entries):
        start, duration = by_path[entry.clip_path]
        assert clip.metadata["media_start_seconds"] == pytest.approx(start)
        assert clip.metadata["media_duration_seconds"] == pytest.approx(duration)
        # source ranges stay FILE-RELATIVE; the offset is applied at write time
        assert clip.source_in == round(entry.source_start * 25)

    music_clip = timeline.audio_clips()[0]
    assert music_clip.metadata["media_duration_seconds"] == pytest.approx(
        plan.song_duration
    )
    assert "media_start_seconds" not in music_clip.metadata  # no TC concept here


# --- repetition guard -------------------------------------------------------------


def make_repeat_music(duration: float = 60.0, **overrides) -> MusicAnalysis:
    """A plain beat grid (0.5s) with no phrases/downbeats/sections."""
    kwargs = dict(
        path="/music/long.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
    )
    kwargs.update(overrides)
    return MusicAnalysis(**kwargs)


def ten_second_reports() -> list[ClipReport]:
    """Exactly 10s of unique, non-overlapping moment material."""
    return [
        ClipReport(
            path="/footage/a.mp4",
            duration=60.0,
            moments=[Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(5)],
        )
    ]


def test_repetition_guard_caps_length_with_note():
    # 10s of unique moments over a 60s song: capped at 10 x 1.5 = 15s.
    plan = plan_montage(ten_second_reports(), make_repeat_music())
    assert plan.duration <= 15.0 + 1e-6
    assert plan.duration == pytest.approx(15.0)
    assert plan.entries[-1].record_end == pytest.approx(plan.duration)
    note = next(n for n in plan.notes if "capped the cut" in n)
    assert "footage supports about 15s" in note
    assert "was 60s" in note
    assert "allow_repeats=True" in note and "--allow-repeats" in note


def test_repetition_guard_disabled_by_allow_repeats():
    plan = plan_montage(ten_second_reports(), make_repeat_music(), allow_repeats=True)
    assert plan.duration == pytest.approx(60.0)
    assert not any("capped the cut" in n for n in plan.notes)


def test_repetition_guard_never_raises_a_short_request():
    # 8s requested is already below the 15s cap: left untouched, no note.
    plan = plan_montage(ten_second_reports(), make_repeat_music(), max_duration=8.0)
    assert plan.duration == pytest.approx(8.0)
    assert not any("capped the cut" in n for n in plan.notes)


def test_repetition_guard_merges_overlapping_moments_per_clip():
    # Two overlapping moments (0-6 and 4-10) are 10s of unique material, not
    # 12s: the cap must use the merged span (15s, not 18s).
    reports = [
        ClipReport(
            path="/footage/a.mp4",
            duration=60.0,
            moments=[Moment(0.0, 6.0, 0.9), Moment(4.0, 10.0, 0.8)],
        )
    ]
    plan = plan_montage(reports, make_repeat_music())
    assert plan.duration == pytest.approx(15.0)


def test_repetition_guard_runs_before_strongest_window():
    # The capped (15s) length — not the requested full 60s — is what the
    # strongest-window logic places against the song's high-energy tail.
    music = make_repeat_music(
        sections=[
            MusicSection(0.0, 40.0, 0.2, "low"),
            MusicSection(40.0, 60.0, 0.9, "high"),
        ]
    )
    plan = plan_montage(ten_second_reports(), music)
    assert plan.duration == pytest.approx(15.0)
    assert plan.music_start == pytest.approx(40.0)  # 15s window inside "high"


# --- cut-ahead lead ----------------------------------------------------------------


def test_cut_lead_shifts_interior_cuts_earlier():
    base = plan_montage(make_reports(), make_music(), cut_lead=0.0)
    lead = plan_montage(make_reports(), make_music())  # default 0.04s
    assert len(base.entries) == len(lead.entries)
    # first cut stays at 0, final boundary stays at the montage length
    assert lead.entries[0].record_start == 0.0
    assert lead.entries[-1].record_end == pytest.approx(12.0)
    # every interior cut moved exactly 0.04s earlier (slots here are >= 0.5s)
    for b, l in zip(base.entries[1:], lead.entries[1:]):
        assert l.record_start == pytest.approx(b.record_start - 0.04)
    # no slot squeezed below the 0.25s floor
    assert all(slot_length(e) >= 0.25 - 1e-9 for e in lead.entries)


def test_cut_lead_zero_matches_default_grid_of_before():
    # cut_lead=0 reproduces the raw beat grid (with the rhythm canon: the
    # mid section opens on a 2s hold at 4.0 before its 1s base cuts).
    plan = plan_montage(make_reports(), make_music(), cut_lead=0.0)
    starts = [e.record_start for e in plan.entries]
    assert starts[:4] == pytest.approx([0.0, 2.0, 4.0, 6.0])
    assert plan.entries[-1].record_end == pytest.approx(12.0)


def test_cut_lead_clamps_to_preserve_order_and_min_slot():
    # 0.4s beat slots with an oversized 0.2s lead: cuts may not cross and no
    # slot may fall below 0.25s.
    music = MusicAnalysis(
        path="/music/fast.wav",
        duration=4.0,
        tempo=150.0,
        beats=[i * 0.4 for i in range(10)],
        sections=[MusicSection(0.0, 4.0, 0.9, "high")],
    )
    plan = plan_montage(make_reports(), music, cut_lead=0.2)
    assert plan.entries[0].record_start == 0.0
    assert plan.entries[-1].record_end == pytest.approx(4.0)
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
        assert nxt.record_start > prev.record_start
    assert all(slot_length(e) >= 0.25 - 1e-9 for e in plan.entries)


# --- cut pace -----------------------------------------------------------------


def test_pace_slows_the_auto_grid():
    # 120 bpm: pace 1s = every 2 beats in "high"; pace 4s = every 8 beats.
    snappy = plan_montage(make_reports(), make_music(), cut_lead=0.0, pace=1.0)
    calm = plan_montage(make_reports(), make_music(), cut_lead=0.0, pace=4.0)
    assert len(calm.entries) < len(snappy.entries)
    # the fastest (high-energy) section cuts at ~the requested pace: its
    # base cuts are 1s; the rhythm hold/breath never exceeds 2x the pace
    high = [slot_length(e) for e in snappy.entries if e.record_start >= 8.0]
    assert high and any(v == pytest.approx(1.0) for v in high)
    assert max(high) <= 2.0 + 1e-9
    assert any("cut pace ~1s" in n for n in snappy.notes)
    assert any("cut pace ~4s" in n for n in calm.notes)


def test_pace_scales_a_named_style_proportionally():
    default = plan_montage(
        make_reports(), make_music(), style="travel", cut_lead=0.0
    )
    calm = plan_montage(
        make_reports(), make_music(), style="travel", cut_lead=0.0, pace=2.0
    )
    assert len(calm.entries) < len(default.entries)


def test_pace_without_music_sets_the_interval():
    plan = plan_montage(
        make_reports(), None, max_duration=18.0, cut_lead=0.0, pace=3.0
    )
    # one flat interval: the pace itself (rounded to whole 0.75s pseudo-beats)
    assert all(slot_length(e) == pytest.approx(3.0) for e in plan.entries)
    assert len(plan.entries) == 6


def test_pace_rejects_nonpositive():
    with pytest.raises(ValueError, match="pace must be positive"):
        plan_montage(make_reports(), make_music(), pace=0.0)


# --- trailer smash-to-black & canvas ------------------------------------------


def test_trailer_smashes_to_black_at_act_changes():
    plan = plan_montage(
        make_reports(), make_music(), style="trailer", cut_lead=0.0,
        allow_repeats=True,
    )
    assert plan.dips, "trailer plans black title slots at act changes"
    assert any("smash-cuts to black" in n for n in plan.notes)
    ends = [e.record_end for e in plan.entries]
    starts = [e.record_start for e in plan.entries]
    for dip_start, dip_len in plan.dips:
        # an entry gives up its tail to the dip ...
        assert any(abs(end - dip_start) < 1e-6 for end in ends)
        # ... the next act starts right after the black
        assert any(abs(s - (dip_start + dip_len)) < 1e-6 for s in starts)
        # ... and nothing covers the black itself
        assert not any(
            e.record_start < dip_start + dip_len - 1e-6
            and e.record_end > dip_start + 1e-6
            for e in plan.entries
        )


def test_other_styles_do_not_dip():
    for style in ("auto", "travel", "wedding", "music_video"):
        plan = plan_montage(
            make_reports(), make_music(), style=style, allow_repeats=True
        )
        assert plan.dips == []


def test_dips_become_gaps_and_title_markers():
    plan = plan_montage(
        make_reports(), make_music(), style="trailer", cut_lead=0.0,
        allow_repeats=True,
    )
    timeline = montage_to_timeline(plan, fps=25.0)
    titles = [m for m in timeline.markers if m.name == "Title slot"]
    assert len(titles) == len(plan.dips)
    video = timeline.video_clips()
    gap_frames = {
        prev.record_out
        for prev, nxt in zip(video, video[1:])
        if nxt.record_in > prev.record_out
    }
    for marker in titles:
        assert marker.frame in gap_frames


def test_canvas_presets_set_timeline_size():
    plan = plan_montage(make_reports(), make_music(), allow_repeats=True)
    vertical = montage_to_timeline(plan, fps=25.0, canvas="vertical")
    assert (vertical.width, vertical.height) == (1080, 1920)
    cine = montage_to_timeline(plan, fps=25.0, canvas="cine")
    assert (cine.width, cine.height) == (1920, 804)
    vertical_4k = montage_to_timeline(plan, fps=25.0, canvas="vertical-uhd")
    assert (vertical_4k.width, vertical_4k.height) == (2160, 3840)
    cine_4k = montage_to_timeline(plan, fps=25.0, canvas="cine-uhd")
    assert (cine_4k.width, cine_4k.height) == (3840, 1608)
    default = montage_to_timeline(plan, fps=25.0)
    assert (default.width, default.height) == (1920, 1080)
    with pytest.raises(ValueError, match="valid canvases"):
        montage_to_timeline(plan, fps=25.0, canvas="imax")


def test_cine_canvas_appends_scaling_hint_once():
    plan = plan_montage(make_reports(), make_music(), allow_repeats=True)
    assert not any("Scale full frame with crop" in n for n in plan.notes)
    montage_to_timeline(plan, fps=25.0, canvas="cine")
    montage_to_timeline(plan, fps=25.0, canvas="cine-uhd")
    hints = [n for n in plan.notes if "Scale full frame with crop" in n]
    assert len(hints) == 1  # idempotent across rebuilds
    # non-cine canvases never add it
    plain = plan_montage(make_reports(), make_music(), allow_repeats=True)
    montage_to_timeline(plain, fps=25.0, canvas="uhd")
    assert not any("Scale full frame with crop" in n for n in plain.notes)


# --- transition modes ---------------------------------------------------------


def test_transitions_cuts_only():
    plan = plan_montage(
        make_reports(), make_music(), style="trailer", cut_lead=0.0,
        allow_repeats=True, transitions="cuts",
    )
    assert plan.dips == []
    assert all(e.transition == 0.0 for e in plan.entries)
    assert any("hard cuts only" in n for n in plan.notes)


def test_transitions_dissolve_every_cut():
    plan = plan_montage(
        make_reports(), make_music(), style="travel", cut_lead=0.0,
        allow_repeats=True, transitions="dissolves",
    )
    assert all(e.transition > 0 for e in plan.entries[1:])
    assert plan.entries[0].transition == 0.0  # the first entry fades in instead
    assert any("on every cut" in n for n in plan.notes)


def test_transitions_smash_on_any_style():
    plan = plan_montage(
        make_reports(), make_music(), style="travel", cut_lead=0.0,
        allow_repeats=True, transitions="smash",
    )
    assert plan.dips
    assert all(e.transition == 0.0 for e in plan.entries)


def test_transitions_smash_on_auto_uses_section_changes():
    plan = plan_montage(
        make_reports(), make_music(), cut_lead=0.0,
        allow_repeats=True, transitions="smash",
    )
    # make_music has section changes at 4s and 8s; both land on cuts
    dip_ends = sorted(start + length for start, length in plan.dips)
    assert dip_ends == pytest.approx([4.0, 8.0])


def test_transitions_rejects_unknown_mode():
    with pytest.raises(ValueError, match="valid modes"):
        plan_montage(make_reports(), make_music(), transitions="wipes")


# --- semantic casting (vision annotations) --------------------------------------


def sem_moment(start: float, end: float, score: float, **vision) -> Moment:
    """A Moment carrying vision annotations (label/tags/role/hero/group).

    Set via setattr so the helper works with and without the vision fields
    declared on the dataclass — montage reads them tolerantly either way.
    """
    m = Moment(start, end, score)
    for key, value in vision.items():
        setattr(m, key, value)
    return m


def entry_key(e) -> tuple:
    return (
        e.clip_path,
        e.source_start,
        e.source_end,
        e.record_start,
        e.record_end,
        e.score,
        e.transition,
        e.label,
    )


def test_role_bonus_flips_pick_in_matching_phase():
    # 20 equal-score moments; the third is a vision-tagged opener. Plain
    # order would spend it on the second opening slot's successor; the role
    # bonus pulls it one window position forward into the slot at 4.0s.
    moments = [Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(20)]
    moments[2] = sem_moment(8.0, 10.0, 0.8, role="opener")
    reports = [ClipReport(path="/footage/long.mp4", duration=120.0, moments=moments)]
    plan = plan_montage(reports, make_arc_music(), style="travel", cut_lead=0.0)
    by_start = {round(e.record_start, 6): e for e in plan.entries}
    # slot 0 keeps the pool leader: the opener sits TWO order steps behind,
    # and the mild bonus flips one step, never two
    assert by_start[0.0].source_start == pytest.approx(0.0)
    # at the 4.0s slot (opening phase) the opener is one step behind: it wins
    assert by_start[4.0].source_start == pytest.approx(8.0)
    assert any("semantic casting: 1 of 39 slots matched to roles" in n for n in plan.notes)


def test_first_and_last_slot_prefer_opener_and_closer_in_auto():
    # "auto" has no arc phases, but the montage's first/last slot still ask
    # for an opener/closer. make_music() yields 11 slots; with neutral
    # motion the fill would walk the pool in order — the roles flip it.
    moments = [Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(16)]
    moments[1] = sem_moment(4.0, 6.0, 0.8, role="opener")
    moments[11] = sem_moment(44.0, 46.0, 0.8, role="closer")
    reports = [ClipReport(path="/footage/long.mp4", duration=120.0, moments=moments)]
    plan = plan_montage(reports, make_music(), cut_lead=0.0)
    assert len(plan.entries) == 11
    assert plan.entries[0].source_start == pytest.approx(4.0)  # the opener opens
    assert plan.entries[-1].source_start == pytest.approx(44.0)  # the closer closes
    assert any("2 of 11 slots matched to roles" in n for n in plan.notes)


def test_hero_shot_wins_the_drop_slot():
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=200.0,
        beats=[i * 0.3 for i in range(134)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        drops=[20.0],
    )

    def moments(with_hero: bool):
        return [
            Moment(0.0, 2.0, 0.9, highlight=0.9),  # loudest: wins the drop today
            sem_moment(10.0, 12.0, 0.8, highlight=0.5, hero=0.9 if with_hero else 0.0),
            Moment(30.0, 32.0, 0.4, highlight=0.2),
        ]

    # baseline: without the hero annotation the highest highlight takes the drop
    report = ClipReport(path="/footage/a.mp4", duration=60.0, moments=moments(False))
    plan = plan_montage([report], music, style="auto", allow_repeats=True, cut_lead=0.0)
    drop_entry = next(e for e in plan.entries if e.record_start == pytest.approx(20.0))
    assert drop_entry.source_start == pytest.approx(0.0)
    # with hero=0.9 the hero shot outweighs the louder moment on the drop
    report = ClipReport(path="/footage/a.mp4", duration=60.0, moments=moments(True))
    plan = plan_montage([report], music, style="auto", allow_repeats=True, cut_lead=0.0)
    drop_entry = next(e for e in plan.entries if e.record_start == pytest.approx(20.0))
    assert drop_entry.source_start == pytest.approx(10.0)
    assert any("semantic casting: hero shot on the drop" in n for n in plan.notes)


def test_hero_bonus_beats_motion_continuity_in_climax():
    # Mirrors test_highlight_preference_in_climax_phase: at the first climax
    # slot the higher-scored moment leads the window, but the hero shot one
    # step behind takes it — the hero bonus outweighs order and motion.
    # (10 fillers cover the 3 opening + 7 build slots exactly.)
    fillers = [Moment(i * 2.0, i * 2.0 + 2.0, 0.8) for i in range(10)]
    quiet_good = Moment(40.0, 42.0, 0.9)
    hero_shot = sem_moment(44.0, 46.0, 0.5, hero=0.9)
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=fillers + [quiet_good, hero_shot],
    )
    plan = plan_montage(
        [report], make_arc_music(), style="travel", order=CHRONOLOGICAL, cut_lead=0.0
    )
    climax_first = [e for e in plan.entries if e.record_start == pytest.approx(16.0)]
    assert len(climax_first) == 1
    assert climax_first[0].source_start == pytest.approx(44.0)


def test_same_group_never_back_to_back_when_alternative_exists():
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=1.5,
        tempo=120.0,
        beats=[i * 0.5 for i in range(4)],
        sections=[MusicSection(0.0, 1.5, 0.9, "high")],  # 3 slots of 0.5s
    )
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=[
            sem_moment(0.0, 2.0, 0.9, group="lake"),
            sem_moment(10.0, 12.0, 0.8, group="lake"),  # same scene, next in order
            sem_moment(20.0, 22.0, 0.7, group="ridge"),
        ],
    )
    plan = plan_montage([report], music, order=CHRONOLOGICAL, cut_lead=0.0)
    # the ridge take jumps the queue so two lake takes don't sit together
    assert [e.source_start for e in plan.entries] == pytest.approx([0.0, 20.0, 10.0])
    assert any("semantic casting: 1 same-scene cut avoided" in n for n in plan.notes)


def test_same_group_kept_when_no_alternative():
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=1.5,
        tempo=120.0,
        beats=[i * 0.5 for i in range(4)],
        sections=[MusicSection(0.0, 1.5, 0.9, "high")],
    )
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=[
            sem_moment(0.0, 2.0, 0.9, group="lake"),
            sem_moment(10.0, 12.0, 0.8, group="lake"),
            sem_moment(20.0, 22.0, 0.7, group="lake"),
        ],
    )
    plan = plan_montage([report], music, order=CHRONOLOGICAL, cut_lead=0.0)
    # all takes share the scene: the penalty hits every candidate equally,
    # so the order is unchanged and nothing is claimed in the notes
    assert [e.source_start for e in plan.entries] == pytest.approx([0.0, 10.0, 20.0])
    assert not any("semantic casting" in n for n in plan.notes)


def test_entry_labels_flow_to_metadata_and_dip_markers():
    reports = make_reports()
    for r in reports:
        for i, m in enumerate(r.moments):
            setattr(m, "label", f"{r.path}:{i}")
    plan = plan_montage(
        reports, make_music(), style="trailer", cut_lead=0.0, allow_repeats=True
    )
    assert plan.dips
    assert all(e.label for e in plan.entries)
    # labels alone are not "semantic data used": the fill must not claim casting
    assert not any("semantic casting" in n for n in plan.notes)
    timeline = montage_to_timeline(plan, fps=25.0)
    for clip, entry in zip(timeline.video_clips(), plan.entries):
        assert clip.metadata["label"] == entry.label
    titles = [m for m in timeline.markers if m.name == "Title slot"]
    assert len(titles) == len(plan.dips)
    for marker, (dip_start, dip_len) in zip(titles, plan.dips):
        incoming = next(
            e for e in plan.entries
            if abs(e.record_start - (dip_start + dip_len)) < 1e-6
        )
        assert marker.note == f"{dip_len:g}s of black — next: {incoming.label}"


def test_unlabeled_dips_keep_generic_title_note():
    plan = plan_montage(
        make_reports(), make_music(), style="trailer", cut_lead=0.0,
        allow_repeats=True,
    )
    timeline = montage_to_timeline(plan, fps=25.0)
    titles = [m for m in timeline.markers if m.name == "Title slot"]
    assert titles
    assert all("drop a title here" in m.note for m in titles)
    assert all("label" not in c.metadata for c in timeline.video_clips())


def test_all_default_vision_fields_change_nothing():
    # The compatibility bar: a plan from plain moments and a plan from
    # moments carrying explicit all-default vision fields are identical.
    for order in (CHRONOLOGICAL, BEST_FIRST):
        plain = plan_montage(make_reports(), make_music(), order=order)
        annotated_reports = make_reports()
        for r in annotated_reports:
            for m in r.moments:
                for key, value in (
                    ("label", ""), ("tags", []), ("role", ""),
                    ("hero", 0.0), ("group", ""),
                ):
                    setattr(m, key, value)
        annotated = plan_montage(annotated_reports, make_music(), order=order)
        assert [entry_key(e) for e in plain.entries] == [
            entry_key(e) for e in annotated.entries
        ]
        assert plain.notes == annotated.notes
        assert not any("semantic casting" in n for n in plain.notes)


# --- energy-motion matching -----------------------------------------------------


def _loud_music(duration=1.0):
    """A single all-out section (energy 1.0): every slot is a peak slot."""
    return MusicAnalysis(
        path="/music/loud.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, 1.0, "high")],
    )


def test_energy_matching_moving_shot_wins_the_peak_slot():
    static = ClipReport(
        path="/f/static.mp4", duration=5.0, moments=[Moment(0.0, 0.5, 0.9)]
    )
    moving = ClipReport(
        path="/f/moving.mp4", duration=5.0,
        moments=[Moment(0.0, 0.5, 0.8, entry_motion=(10.0, 0.0), exit_motion=(10.0, 0.0))],
    )
    plan = plan_montage([static, moving], _loud_music(), max_duration=1.0, cut_lead=0.0)
    # pool order is (static, moving); the full-energy slot flips to the
    # moving shot one position later
    assert plan.entries[0].clip_path == "/f/moving.mp4"
    assert plan.entries[1].clip_path == "/f/static.mp4"


def test_energy_matching_neutral_when_all_static():
    a = ClipReport(path="/f/a.mp4", duration=5.0, moments=[Moment(0.0, 0.5, 0.9)])
    b = ClipReport(path="/f/b.mp4", duration=5.0, moments=[Moment(0.0, 0.5, 0.8)])
    plan = plan_montage([a, b], _loud_music(), max_duration=1.0, cut_lead=0.0)
    # no motion data anywhere: pool order is preserved exactly as before
    assert [e.clip_path for e in plan.entries] == ["/f/a.mp4", "/f/b.mp4"]


# --- sfx layer (film mode) -------------------------------------------------------


def test_sfx_default_off_and_plan_unchanged():
    # The compatibility bar: sfx=False (and the omitted default) plan
    # byte-identically to a plan built before the parameter existed.
    for order in (CHRONOLOGICAL, BEST_FIRST):
        without = plan_montage(make_reports(), make_music(), order=order)
        explicit = plan_montage(make_reports(), make_music(), order=order, sfx=False)
        assert without.sfx == [] == explicit.sfx
        assert [entry_key(e) for e in without.entries] == [
            entry_key(e) for e in explicit.entries
        ]
        assert without.notes == explicit.notes
        assert not any("sfx layer" in n for n in without.notes)


def test_sfx_travel_places_ambience_risers_impact_whooshes():
    # travel over the 40s arc track: phases 0-8 / 8-16 / 16-32 / 32-40.
    plan = plan_montage(
        make_long_reports(), make_arc_music(), style="travel", cut_lead=0.0, sfx=True
    )
    times = [c.time for c in plan.sfx]
    assert times == sorted(times)
    for c in plan.sfx:
        assert 0.0 <= c.time and c.time + c.duration <= 40.0 + 1e-6
    # opening ambience: at 0, exactly the opening phase long, honest fallback
    # query (no vision labels anywhere)
    ambience = [c for c in plan.sfx if c.kind == "ambience"]
    assert len(ambience) == 1
    assert ambience[0].time == 0.0
    assert ambience[0].duration == pytest.approx(8.0)
    assert ambience[0].query == "outdoor ambience"
    assert ambience[0].note == "opening"
    # risers END exactly on the act changes, min(2s, prior phase / 2) long
    risers = {c.time + c.duration: c for c in plan.sfx if c.kind == "riser"}
    assert set(risers) == {8.0, 16.0, 32.0}
    assert all(c.duration == pytest.approx(2.0) for c in risers.values())
    assert all(c.query == "riser build up" for c in risers.values())
    assert risers[16.0].note == "build -> climax"
    # impact ON the climax start
    impacts = [c for c in plan.sfx if c.kind == "impact"]
    assert len(impacts) == 1
    assert impacts[0].time == pytest.approx(16.0)
    assert impacts[0].query == "cinematic impact hit"
    # whooshes: centered on real cuts (fastest slots live in the climax),
    # 0.6s each, filling the density cap of ceil(40 / 5) = 8 cues
    whooshes = [c for c in plan.sfx if c.kind == "whoosh"]
    assert len(whooshes) == 3
    cut_times = {e.record_start for e in plan.entries}
    for c in whooshes:
        assert c.duration == pytest.approx(0.6)
        assert c.query == "whoosh transition fast"
        center = c.time + c.duration / 2.0
        assert any(abs(center - t) < 1e-6 for t in cut_times)
        assert 16.0 <= center <= 32.0  # the fastest (0.5s) slots are the climax
    assert len(plan.sfx) == 8
    assert any(
        "sfx layer: 8 cues planned "
        "(markers on the timeline; queries for your SFX library)" in n
        for n in plan.notes
    )


def test_sfx_trailer_with_drops_and_dips():
    # trailer + drop at 20s: the climax is pinned to the drop, the other
    # bounds snap to phrases -> phases 0-8 / 8-16 / 16-20 / 20-32 / 32-40,
    # and the trailer smashes to black where the outgoing slot allows it.
    plan = plan_montage(
        make_long_reports(), make_arc_music(drops=[20.0]), style="trailer",
        cut_lead=0.0, sfx=True,
    )
    assert plan.dips, "the trailer should still dip to black"
    times = [c.time for c in plan.sfx]
    assert times == sorted(times)
    for c in plan.sfx:
        assert 0.0 <= c.time and c.time + c.duration <= 40.0 + 1e-6
    assert len(plan.sfx) <= 8  # ceil(40 / 5)
    # ambience at 0 under the opening
    assert plan.sfx[0].kind == "ambience" and plan.sfx[0].time == 0.0
    # risers end exactly on the act changes (the split build gets none)
    riser_ends = {round(c.time + c.duration, 6) for c in plan.sfx if c.kind == "riser"}
    assert riser_ends == {8.0, 20.0, 32.0}
    # impact ON the climax start — which IS the drop here
    impacts = [c for c in plan.sfx if c.kind == "impact"]
    assert [c.time for c in impacts] == pytest.approx([20.0])
    assert impacts[0].note == "climax start"
    # one sub-drop per dip, sitting exactly on the black
    subs = sorted((c.time, c.duration) for c in plan.sfx if c.kind == "sub-drop")
    assert subs == [
        (pytest.approx(start), pytest.approx(length)) for start, length in sorted(plan.dips)
    ]
    assert all(c.query == "sub drop boom" for c in plan.sfx if c.kind == "sub-drop")
    assert all(c.note == "title slot" for c in plan.sfx if c.kind == "sub-drop")


def test_sfx_density_cap_drops_whooshes_then_risers():
    # travel over the 12s song: cap = ceil(12 / 5) = 3 cues. Whooshes never
    # make it in; of the three act-change risers only the one INTO the
    # climax survives; the backbone (ambience + impact) always stays.
    plan = plan_montage(
        make_reports(), make_music(), style="travel", cut_lead=0.0, sfx=True
    )
    assert len(plan.sfx) == 3
    kinds = [c.kind for c in plan.sfx]
    assert sorted(kinds) == ["ambience", "impact", "riser"]
    riser = next(c for c in plan.sfx if c.kind == "riser")
    assert riser.note == "build -> climax"
    assert any("sfx layer: 3 cues planned" in n for n in plan.notes)


def test_sfx_impact_on_auto_drop_cut():
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=200.0,
        beats=[i * 0.3 for i in range(134)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        drops=[20.0],
    )
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=[Moment(0.0, 2.0, 0.9), Moment(10.0, 12.0, 0.8), Moment(30.0, 32.0, 0.4)],
    )
    plan = plan_montage(
        [report], music, style="auto", allow_repeats=True, cut_lead=0.0, sfx=True
    )
    # "auto" has no arc: the drop-forced cut carries the impact instead
    impacts = [c for c in plan.sfx if c.kind == "impact"]
    assert [c.time for c in impacts] == pytest.approx([20.0])
    assert impacts[0].note == "cut on the drop"
    # ...and the arc-less ambience bed covers the first 4 seconds
    assert plan.sfx[0].kind == "ambience"
    assert plan.sfx[0].duration == pytest.approx(4.0)


def test_sfx_ambience_query_built_from_opening_labels():
    reports = make_long_reports()
    for m in reports[0].moments:
        m.label = "over the mountain pass"
    plan = plan_montage(
        reports, make_arc_music(), style="travel", cut_lead=0.0, sfx=True
    )
    ambience = next(c for c in plan.sfx if c.kind == "ambience")
    # stopwords drop out, the first two meaningful label words search well
    assert ambience.query == "mountain pass ambience"


def test_sfx_cues_become_green_markers():
    plan = plan_montage(
        make_long_reports(), make_arc_music(), style="travel", cut_lead=0.0, sfx=True
    )
    assert plan.sfx
    timeline = montage_to_timeline(plan, fps=25.0)
    sfx_markers = [m for m in timeline.markers if m.name.startswith("SFX: ")]
    assert len(sfx_markers) == len(plan.sfx)
    for marker, cue in zip(sfx_markers, plan.sfx):
        assert marker.frame == round(cue.time * 25.0)
        assert marker.name == f"SFX: {cue.kind}"
        assert marker.note == f"{cue.query} — {cue.note}"
        assert marker.color == "Green"


def test_sfx_works_without_music():
    # The film mode proper: no song at all, the SFX layer carries the cut.
    plan = plan_montage(
        make_long_reports(), None, max_duration=20.0, style="travel",
        cut_lead=0.0, sfx=True,
    )
    assert plan.sfx
    assert len(plan.sfx) <= 4  # ceil(20 / 5)
    assert plan.sfx[0].kind == "ambience" and plan.sfx[0].time == 0.0
    impacts = [c for c in plan.sfx if c.kind == "impact"]
    # pseudo-grid phases use the raw arc shares: climax starts at 50% of 20s
    assert [c.time for c in impacts] == pytest.approx([10.0])


# --- plan persistence (the revision loop's save format) ---------------------------


def make_full_plan():
    """A plan exercising every serialized field: dips, sfx, labels, fades."""
    reports = make_reports()
    for r in reports:
        r.media_start = 100.0
        for i, m in enumerate(r.moments):
            setattr(m, "label", f"shot {i}")
    return plan_montage(
        reports, make_music(), style="trailer", cut_lead=0.0,
        allow_repeats=True, sfx=True,
    )


def test_plan_round_trips_through_json():
    import json
    from dataclasses import asdict

    from monteur.montage import plan_from_dict, plan_to_dict

    plan = make_full_plan()
    assert plan.entries and plan.dips and plan.sfx and plan.notes
    data = plan_to_dict(plan)
    assert data["monteur_plan"] == 1
    # through real JSON, like the CLI writes and reads it
    restored = plan_from_dict(json.loads(json.dumps(data)))
    assert asdict(restored) == asdict(plan)
    # dips come back as tuples (JSON has no tuples), same values
    assert restored.dips == plan.dips
    assert all(isinstance(d, tuple) for d in restored.dips)


def test_plan_from_dict_requires_version_key():
    from monteur.montage import plan_from_dict, plan_to_dict

    data = plan_to_dict(make_full_plan())
    del data["monteur_plan"]
    with pytest.raises(ValueError, match="monteur_plan"):
        plan_from_dict(data)


def test_plan_from_dict_rejects_wrong_version():
    from monteur.montage import plan_from_dict, plan_to_dict

    data = plan_to_dict(make_full_plan())
    data["monteur_plan"] = 2
    with pytest.raises(ValueError, match="unsupported plan version 2"):
        plan_from_dict(data)


def test_plan_from_dict_rejects_malformed_entries():
    from monteur.montage import plan_from_dict, plan_to_dict

    data = plan_to_dict(make_full_plan())
    data["entries"][0]["no_such_field"] = 1.0
    with pytest.raises(ValueError, match="malformed plan JSON"):
        plan_from_dict(data)


# --- pin_entry (the revision pinning hook) ----------------------------------------


def test_pin_entry_splits_overlap_and_drops_covered_dip():
    from dataclasses import replace

    from monteur.montage import MontageEntry, MontagePlan, pin_entry

    def entry(rs, re_, src=0.0):
        return MontageEntry(
            clip_path="/f/a.mp4", source_start=src, source_end=src + (re_ - rs),
            record_start=rs, record_end=re_, score=0.5, transition=0.3,
        )

    plan = MontagePlan(music_path="", duration=6.0)
    plan.entries = [entry(0.0, 2.0, src=10.0), entry(2.0, 4.0, src=20.0), entry(4.0, 6.0, src=30.0)]
    plan.dips = [(2.8, 0.4)]
    pinned = MontageEntry(
        clip_path="/f/b.mp4", source_start=5.0, source_end=6.0,
        record_start=2.5, record_end=3.5, score=0.9,
    )
    pin_entry(plan, pinned)
    windows = [(e.record_start, e.record_end) for e in plan.entries]
    assert windows == [(0.0, 2.0), (2.0, 2.5), (2.5, 3.5), (3.5, 4.0), (4.0, 6.0)]
    # the pinned copy is verbatim (and a COPY, not the caller's object)
    inserted = plan.entries[2]
    assert inserted == replace(pinned) and inserted is not pinned
    # the split entry's source moved 1:1 with the record trim
    left, right = plan.entries[1], plan.entries[3]
    assert (left.source_start, left.source_end) == (20.0, 20.5)
    assert (right.source_start, right.source_end) == (21.5, 22.0)
    assert right.transition == 0.0  # the cut out of the pinned shot is hard
    # the dip under the pinned window is gone (the shot covers that time)
    assert plan.dips == []


# --- SfxCue.file: placed sound elements (monteur.elements) -------------------------


def test_sfx_file_serialization_round_trip():
    import json

    from monteur.montage import MontagePlan, SfxCue, plan_from_dict, plan_to_dict

    plan = MontagePlan(music_path="/m.wav", duration=10.0)
    plan.sfx = [
        SfxCue(1.0, 0.6, "whoosh", "whoosh transition fast", "fast cut"),
        SfxCue(5.0, 0.8, "impact", "hit", "on the drop", file="/sfx/hit.wav"),
    ]
    data = json.loads(json.dumps(plan_to_dict(plan)))
    # the plain cue serializes exactly as before the field existed
    assert "file" not in data["sfx"][0]
    assert data["sfx"][1]["file"] == "/sfx/hit.wav"
    restored = plan_from_dict(data)
    assert restored.sfx[0].file == ""
    assert restored.sfx[1].file == "/sfx/hit.wav"


def test_plans_without_files_serialize_byte_identically():
    """The compatibility bar: a plan whose cues carry no files writes the
    exact JSON it wrote before SfxCue.file existed (no new keys)."""
    import json

    from monteur.montage import plan_to_dict

    plan = make_full_plan()
    assert plan.sfx and all(not c.file for c in plan.sfx)
    data = plan_to_dict(plan)
    for cue in data["sfx"]:
        assert set(cue) == {"time", "duration", "kind", "query", "note"}
    # and old plans (written without the key) still load
    from monteur.montage import plan_from_dict

    assert plan_from_dict(json.loads(json.dumps(data))).sfx[0].file == ""


def test_filed_cues_become_real_clips_on_a2_in_music_mode():
    from monteur.model import AUDIO
    from monteur.montage import MontagePlan, MontageEntry, SfxCue

    plan = MontagePlan(music_path="/music/song.wav", duration=10.0)
    plan.entries = [
        MontageEntry(
            clip_path="/f/a.mp4", source_start=0.0, source_end=10.0,
            record_start=0.0, record_end=10.0, score=0.5,
        )
    ]
    plan.sfx = [
        SfxCue(2.0, 0.8, "impact", "hit", "on the drop", file="/sfx/hit.wav"),
        SfxCue(4.0, 0.6, "whoosh", "whoosh", "fast cut"),  # marker-only
    ]
    timeline = montage_to_timeline(plan, fps=25.0, audio="music")
    sfx_clips = [c for c in timeline.clips if c.track == "A2"]
    assert len(sfx_clips) == 1
    clip = sfx_clips[0]
    assert clip.kind == AUDIO
    assert clip.source_file == "/sfx/hit.wav"
    assert clip.name == "hit"
    # record at cue.time for cue.duration (the file path doesn't exist, so
    # the probe falls back to the cue's own duration)
    assert (clip.record_in, clip.record_out) == (50, 70)
    assert (clip.source_in, clip.source_out) == (0, 20)
    # both cues keep their Green markers (the intent is documented)
    sfx_markers = [m for m in timeline.markers if m.name.startswith("SFX: ")]
    assert len(sfx_markers) == 2
    # the music bed still sits alone on A1
    assert [c.track for c in timeline.clips if c.kind == AUDIO] == ["A1", "A2"]


def test_filed_cues_land_on_a3_in_mix_mode():
    from monteur.model import AUDIO
    from monteur.montage import MontagePlan, MontageEntry, SfxCue

    plan = MontagePlan(music_path="/music/song.wav", duration=10.0)
    plan.entries = [
        MontageEntry(
            clip_path="/f/a.mp4", source_start=0.0, source_end=10.0,
            record_start=0.0, record_end=10.0, score=0.5,
        )
    ]
    plan.sfx = [SfxCue(2.0, 0.8, "impact", "hit", "n", file="/sfx/hit.wav")]
    timeline = montage_to_timeline(plan, fps=25.0, audio="mix")
    audio_tracks = [c.track for c in timeline.clips if c.kind == AUDIO]
    # mix: camera sound on A2, song on A1 — the SFX element moves to A3
    assert sorted(audio_tracks) == ["A1", "A2", "A3"]
    sfx = next(c for c in timeline.clips if c.track == "A3")
    assert sfx.source_file == "/sfx/hit.wav"


def test_filed_cue_clamps_to_the_montage_end():
    from monteur.montage import MontagePlan, MontageEntry, SfxCue

    plan = MontagePlan(music_path="/music/song.wav", duration=10.0)
    plan.entries = [
        MontageEntry(
            clip_path="/f/a.mp4", source_start=0.0, source_end=10.0,
            record_start=0.0, record_end=10.0, score=0.5,
        )
    ]
    plan.sfx = [SfxCue(9.5, 3.0, "sub-drop", "boom", "n", file="/sfx/boom.wav")]
    timeline = montage_to_timeline(plan, fps=25.0)
    clip = next(c for c in timeline.clips if c.track == "A2")
    assert (clip.record_in, clip.record_out) == (238, 250)  # ends AT the cut


def test_filed_cue_uses_the_probed_file_duration(tmp_path, monkeypatch):
    from monteur import montage as montage_mod
    from monteur.montage import MontagePlan, MontageEntry, SfxCue

    plan = MontagePlan(music_path="/music/song.wav", duration=10.0)
    plan.entries = [
        MontageEntry(
            clip_path="/f/a.mp4", source_start=0.0, source_end=10.0,
            record_start=0.0, record_end=10.0, score=0.5,
        )
    ]
    # the cue claims 5s but the real file is only 1.2s long
    plan.sfx = [SfxCue(2.0, 5.0, "impact", "hit", "n", file="/sfx/hit.wav")]
    monkeypatch.setattr(
        montage_mod, "_probe_media_duration", lambda path: 1.2
    )
    timeline = montage_to_timeline(plan, fps=25.0)
    clip = next(c for c in timeline.clips if c.track == "A2")
    assert (clip.record_in, clip.record_out) == (50, 80)  # 1.2s, not 5s
    # the real duration feeds the writers, exactly like entries do
    assert clip.metadata["media_duration_seconds"] == 1.2
