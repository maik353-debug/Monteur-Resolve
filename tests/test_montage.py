"""Tests for the montage builder (monteur.montage).

MusicAnalysis / ClipReport objects are constructed directly — the analysis
modules are implemented separately and are not exercised here.
"""

from __future__ import annotations

import pytest

from monteur.montage import (
    BEST_FIRST,
    CHRONOLOGICAL,
    MontageEntry,
    MontagePlan,
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


def make_varied_reports(n: int = 20) -> list[ClipReport]:
    """One 2s moment per DISTINCT clip: no same-clip adjacency anywhere.

    Grid/rhythm/transition tests use this so they observe the raw cut
    grid — the continuity merge and the jump-cut guard (same-clip craft)
    have nothing to act on. Chronological pool order = clip-name order.
    """
    return [
        ClipReport(
            path=f"/footage/v{i:02d}.mp4",
            duration=30.0,
            moments=[Moment(4.0, 6.0, 0.8)],
        )
        for i in range(n)
    ]


def slot_length(entry) -> float:
    return entry.record_end - entry.record_start


# --- grid ---------------------------------------------------------------------


def test_grid_density_follows_section_energy():
    # Faster where loud: each section's cuts average at least twice the next
    # louder section's, no cut is ever faster than the section's base step,
    # and (the rhythm canon) a section opens on a hold with a breath later —
    # not one metronomic interval. Varied clips: every slot is a distinct
    # clip, so the continuity merge leaves the raw grid observable.
    plan = plan_montage(make_varied_reports(), make_music(), cut_lead=0.0)
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
    plan = plan_montage(make_varied_reports(), make_music(), order=CHRONOLOGICAL)
    starts = [e.record_start for e in plan.entries]
    assert starts == sorted(starts)
    # first pass follows (clip path, moment start) order — with one clip
    # per moment that IS the clip-name order, undisturbed by the guard
    clips = [e.clip_path for e in plan.entries]
    assert clips == sorted(clips)
    assert clips[0] == "/footage/v00.mp4"


def test_jump_cut_guard_prefers_a_different_clip():
    # a.mp4's two moments sit ~6s apart in source — cut back to back that
    # is a visible jump inside one scene, too far for a continuity join.
    # Pool order would cast a, a, b; the guard diverts to a, b, a.
    a = ClipReport(
        path="/footage/a.mp4",
        duration=20.0,
        moments=[Moment(0.0, 2.0, 0.9), Moment(8.0, 10.0, 0.8)],
    )
    b = ClipReport(
        path="/footage/b.mp4", duration=20.0, moments=[Moment(0.0, 2.0, 0.7)]
    )
    plan = plan_montage([a, b], None, max_duration=4.5, cut_lead=0.0)
    clips = [e.clip_path.rsplit("/", 1)[-1] for e in plan.entries[:3]]
    assert clips == ["a.mp4", "b.mp4", "a.mp4"]
    assert not any(n.startswith("footage variety is low") for n in plan.notes)


def test_jump_cut_unavoidable_is_noted_once():
    # One clip, moments ~5s apart: the guard has no other clip to divert
    # to, so same-scene jumps survive — one honest note, not a failure.
    report = ClipReport(
        path="/footage/only.mp4",
        duration=30.0,
        moments=[Moment(i * 7.0, i * 7.0 + 2.0, 0.8) for i in range(3)],
    )
    plan = plan_montage([report], None, max_duration=6.0, cut_lead=0.0)
    jumps = 0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        if prev.clip_path == nxt.clip_path:
            gap = nxt.source_start - prev.source_end
            if gap <= 8.0 and (gap >= 0.25 or gap < -1e-6):
                jumps += 1
    notes = [n for n in plan.notes if n.startswith("footage variety is low")]
    if jumps:
        assert len(notes) == 1
        assert "jump cuts were unavoidable" in notes[0]
    else:
        assert notes == []


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
    # the continuity merge re-joins adjacent slices of the one long take
    # into continuous shots (nothing repeats, nothing jumps)
    assert len(plan.entries) < 6
    assert any(n.startswith("continuity:") for n in plan.notes)
    # all pieces from the long moment are non-overlapping slices
    long_pieces = [
        (e.source_start, e.source_end) for e in plan.entries if e.source_start < 4.0
    ]
    long_pieces.sort()
    for (s1, e1), (s2, e2) in zip(long_pieces, long_pieces[1:]):
        assert s2 >= e1 - 1e-9
    # the record grid still tiles the montage exactly
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(4.0)


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
    # varied clips: one clip per slot, so no continuity joins hide the grid
    plan = plan_montage(make_varied_reports(), make_arc_music(), style="travel", cut_lead=0.0)
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
    # Blueprint 1.6 (breath in the canon): the 32-beat climax alternates
    # hot 8-beat phrase groups (the travel pattern) with cool ones (its
    # multipliers doubled) instead of looping one flat 4-cycle — 19 raw
    # climax cuts instead of the old uniform 26; the continuity merge then
    # joins 3 same-clip reuse cuts back into continuous shots.
    assert len(plan.entries) == 3 + 7 + 19 + 3 - 3
    # grid stays contiguous and closes on the montage length
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(40.0)
    assert any("travel" in note for note in plan.notes)


def test_phase_boundaries_snap_to_phrase_starts():
    plan = plan_montage(make_varied_reports(), make_arc_music(), style="travel", cut_lead=0.0)
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
            make_varied_reports(), make_arc_music(), style=style, cut_lead=0.0
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
    beds = [c for c in timeline.audio_clips() if c.track == "A1"]
    # the drop at record 3.0s earns the deliberate pre-drop silence, so the
    # bed splits — but the record<->song mapping holds on every piece:
    # source_in == (music_start + record_in) in frames
    for bed in beds:
        assert bed.source_in == 1525 + bed.record_in  # 61.0s * 25fps offset
        assert bed.source_out - bed.source_in == bed.record_out - bed.record_in
    assert beds[0].source_in == 1525
    assert beds[0].record_in == 0
    assert beds[-1].record_out == 500  # the bed still ends at 20s
    # with the song forced continuous the old single full-length clip returns
    continuous = plan_montage(
        make_long_reports(), music, max_duration=20.0, style="travel",
        music_flow="continuous",
    )
    timeline = montage_to_timeline(continuous, fps=25.0)
    music_clip = [c for c in timeline.audio_clips() if c.track == "A1"][0]
    assert music_clip.source_in == 1525
    assert music_clip.source_out - music_clip.source_in == 500
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
    plan = plan_montage(make_long_reports(), music, max_duration=60.0, allow_repeats=True)
    assert plan.duration == pytest.approx(56.0)
    assert plan.entries[-1].record_end == pytest.approx(56.0)
    assert any("length snapped to phrase at 56.0s" in n for n in plan.notes)


def test_end_on_phrase_refuses_snap_beyond_tolerance():
    # nearest phrase is 84s = 40% longer than requested; downbeats/beats
    # already sit exactly on 60s, so the length stays untouched
    music = make_ending_music(phrases=[0.0, 84.0])
    plan = plan_montage(make_long_reports(), music, max_duration=60.0, allow_repeats=True)
    assert plan.duration == pytest.approx(60.0)
    assert not any("length snapped" in n for n in plan.notes)


def test_end_on_phrase_prefers_shorter_on_tie():
    # phrases every 8s: 56s and 64s are equidistant from the 60s request
    plan = plan_montage(
        make_long_reports(), make_ending_music(), max_duration=60.0, allow_repeats=True
    )
    assert plan.duration == pytest.approx(56.0)


def test_end_snap_falls_back_to_downbeats():
    music = make_ending_music(phrases=[])
    plan = plan_montage(make_long_reports(), music, max_duration=60.9, allow_repeats=True)
    assert plan.duration == pytest.approx(60.0)
    assert any("length snapped to downbeat at 60.0s" in n for n in plan.notes)


def test_end_on_phrase_disabled():
    music = make_ending_music(phrases=[0.0, 56.0])
    plan = plan_montage(
        make_long_reports(), music, max_duration=60.0, end_on_phrase=False,
        allow_repeats=True,
    )
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
    # varied clips: no same-clip continuations, so the classic gentle-phase
    # dissolves stay observable
    plan = plan_montage(make_varied_reports(), make_arc_music(), style="travel")
    # phases: opening 0-8 (slow slots), build 8-16, climax 16-32, outro 32-40
    assert plan.entries[0].transition == 0.0  # first entry: its fade is fade_in
    # Blueprint 1.7 (dissolve lead 0): dissolving boundaries return to
    # their UNSHIFTED grid positions — the same five gentle boundaries as
    # always (2 opening interiors, the opening->build handover, 2 outro
    # interiors), now sitting exactly ON the grid while the hard cuts
    # keep the 0.04s cut-ahead lead.
    dissolve_starts = {
        round(e.record_start, 3) for e in plan.entries if e.transition > 0
    }
    assert dissolve_starts == {4.0, 6.0, 8.0, 34.0, 36.0}
    for e in plan.entries[1:]:
        if round(e.record_start, 3) in dissolve_starts:
            assert e.transition == pytest.approx(0.5)  # min(0.5, half the slot)
        else:  # build / climax (and the lead-shifted hard cuts)
            assert e.transition == 0.0
    dissolves = sum(1 for e in plan.entries if e.transition > 0)
    assert dissolves == 5
    assert any(f"{dissolves} dissolves in gentle phases" in n for n in plan.notes)


def test_auto_low_sections_get_transitions():
    plan = plan_montage(make_varied_reports(), make_music())
    assert plan.entries[0].transition == 0.0
    # Blueprint 1.7 (dissolve lead 0): the dissolving boundaries — the
    # "low"-section interiors plus the low->mid handover — sit exactly ON
    # the grid (2.0, 4.0), no longer 0.04s early; everything from the mid
    # section on cuts hard.
    low = [e for e in plan.entries[1:] if e.record_start <= 4.0]
    assert low and all(e.transition == pytest.approx(0.5) for e in low)
    assert {round(e.record_start, 3) for e in low} == {2.0, 4.0}
    assert all(e.transition == 0.0 for e in plan.entries if e.record_start > 4.0)


def test_timeline_carries_transitions_and_fades():
    plan = plan_montage(make_varied_reports(), make_arc_music(), style="travel")
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


def duplicate_source_pairs(plan) -> list[tuple[str, float]]:
    """(clip_path, source_start) pairs appearing more than once in the plan."""
    seen: dict[tuple[str, float], int] = {}
    for e in plan.entries:
        key = (e.clip_path, round(e.source_start, 3))
        seen[key] = seen.get(key, 0) + 1
    return [k for k, n in seen.items() if n > 1]


def test_repetition_guard_caps_length_at_unique_material_with_note():
    # 10s of unique moments over a 60s song: zero repeats means the cut
    # shrinks to the material itself — 10s, not 1.5x of it.
    plan = plan_montage(ten_second_reports(), make_repeat_music())
    assert plan.duration == pytest.approx(10.0)
    assert plan.entries[-1].record_end == pytest.approx(plan.duration)
    note = next(n for n in plan.notes if "length reduced" in n)
    assert "10s" in note and "was 60s" in note
    assert "allow_repeats=True" in note and "--allow-repeats" in note
    assert duplicate_source_pairs(plan) == []


def test_no_repeats_is_zero_duplicates_even_on_long_requests():
    # The user's field bug: checkbox off + a request far beyond the
    # material must yield a SHORTER plan, never a recycled one.
    plan = plan_montage(ten_second_reports(), make_repeat_music(), max_duration=60.0)
    assert plan.duration == pytest.approx(10.0)
    assert duplicate_source_pairs(plan) == []
    assert not any("footage repeats" in n for n in plan.notes)
    assert any("length reduced" in n for n in plan.notes)


def test_repetition_guard_disabled_by_allow_repeats():
    plan = plan_montage(ten_second_reports(), make_repeat_music(), allow_repeats=True)
    assert plan.duration == pytest.approx(60.0)
    assert not any("length reduced" in n for n in plan.notes)
    # the full length genuinely repeats footage — knowingly and noted
    assert duplicate_source_pairs(plan)
    assert any("moments reused" in n for n in plan.notes)


def test_repetition_guard_never_raises_a_short_request():
    # 8s requested is already below the 10s material: left untouched, no note.
    plan = plan_montage(ten_second_reports(), make_repeat_music(), max_duration=8.0)
    assert plan.duration == pytest.approx(8.0)
    assert not any("length reduced" in n for n in plan.notes)


def test_repetition_guard_merges_overlapping_moments_per_clip():
    # Two overlapping moments (0-6 and 4-10) are 10s of unique material, not
    # 12s: the cap must use the merged span (10s, not 12s) — and the shared
    # 4-6s span must not enter the cut twice.
    reports = [
        ClipReport(
            path="/footage/a.mp4",
            duration=60.0,
            moments=[Moment(0.0, 6.0, 0.9), Moment(4.0, 10.0, 0.8)],
        )
    ]
    plan = plan_montage(reports, make_repeat_music())
    assert plan.duration == pytest.approx(10.0)
    assert duplicate_source_pairs(plan) == []
    # overlap-trimmed sources: no two entries share any source span
    windows = sorted((e.source_start, e.source_end) for e in plan.entries)
    for (a_lo, a_hi), (b_lo, b_hi) in zip(windows, windows[1:]):
        assert b_lo >= a_hi - 1e-6


def test_repetition_guard_runs_before_strongest_window():
    # The capped (10s) length — not the requested full 60s — is what the
    # strongest-window logic places against the song's high-energy tail.
    music = make_repeat_music(
        sections=[
            MusicSection(0.0, 40.0, 0.2, "low"),
            MusicSection(40.0, 60.0, 0.9, "high"),
        ]
    )
    plan = plan_montage(ten_second_reports(), music)
    assert plan.duration == pytest.approx(10.0)
    assert plan.music_start >= 40.0 - 1e-6  # 10s window inside "high"


def test_fill_truncates_instead_of_rewinding_when_repeats_off():
    # Force the fill dry directly: three 2s slots, one 2s moment, no
    # padding room. With repeats off the grid is CUT at the last fillable
    # slot; with repeats on the old rewind fills all three.
    from monteur.montage import _PoolItem, _fill

    def slots_and_pool():
        slots = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
        pool = [_PoolItem("/footage/a.mp4", 2.0, Moment(0.0, 2.0, 0.9))]
        return slots, pool

    slots, pool = slots_and_pool()
    entries, notes, short_at = _fill(
        slots, [0, 1, 2], pool, allow_repeats=False
    )
    assert short_at == pytest.approx(2.0)
    assert len(entries) == 1
    assert entries[0].record_end == pytest.approx(2.0)

    slots, pool = slots_and_pool()
    entries, notes, short_at = _fill(slots, [0, 1, 2], pool, allow_repeats=True)
    assert short_at is None
    assert len(entries) == 3  # the old rewind behavior, unchanged
    assert any("footage repeats" in n for n in notes)


def test_variety_note_when_one_clip_dominates():
    # 4 moments in a.mp4, 1 in b.mp4 -> most slots come from a.mp4.
    reports = [
        ClipReport(
            path="/footage/a.mp4",
            duration=60.0,
            moments=[Moment(i * 6.0, i * 6.0 + 2.0, 0.8) for i in range(4)],
        ),
        ClipReport(
            path="/footage/b.mp4", duration=10.0, moments=[Moment(0.0, 2.0, 0.7)]
        ),
    ]
    plan = plan_montage(reports, make_repeat_music(duration=10.0))
    counts: dict[str, int] = {}
    for e in plan.entries:
        counts[e.clip_path] = counts.get(e.clip_path, 0) + 1
    top = max(counts.values())
    assert top > 0.6 * len(plan.entries)  # the fixture really is lopsided
    notes = [n for n in plan.notes if n.startswith("variety:")]
    assert notes == [
        f"variety: {top} of {len(plan.entries)} shots come from one clip "
        "— more footage would help"
    ]


def test_no_variety_note_for_balanced_footage():
    reports = [
        ClipReport(
            path=f"/footage/{name}.mp4",
            duration=30.0,
            moments=[Moment(i * 8.0, i * 8.0 + 2.0, 0.8) for i in range(3)],
        )
        for name in ("a", "b", "c")
    ]
    plan = plan_montage(reports, make_repeat_music(duration=18.0))
    assert not any(n.startswith("variety:") for n in plan.notes)


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
        make_varied_reports(), None, max_duration=18.0, cut_lead=0.0, pace=3.0
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
    # one clip per moment (clip-name order == moment order): the jump-cut
    # guard and continuity merge stay out of this semantic-casting test
    reports = [
        ClipReport(path=f"/footage/v{i:02d}.mp4", duration=120.0, moments=[m])
        for i, m in enumerate(moments)
    ]
    plan = plan_montage(reports, make_arc_music(), style="travel", cut_lead=0.0)
    by_start = {round(e.record_start, 6): e for e in plan.entries}
    # slot 0 keeps the pool leader: the opener sits TWO order steps behind,
    # and the mild bonus flips one step, never two
    assert by_start[0.0].source_start == pytest.approx(0.0)
    # at the 4.0s slot (opening phase) the opener is one step behind: it wins
    assert by_start[4.0].source_start == pytest.approx(8.0)
    # 32 slots, not 39: blueprint 1.6's hot/cool climax phrase groups cut
    # the long climax more sparsely than the old flat 4-cycle did.
    assert any("semantic casting: 1 of 32 slots matched to roles" in n for n in plan.notes)


def test_first_and_last_slot_prefer_opener_and_closer_in_auto():
    # "auto" has no arc phases, but the montage's first/last slot still ask
    # for an opener/closer. make_music() yields 11 slots; with neutral
    # motion the fill would walk the pool in order — the roles flip it.
    moments = [Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(16)]
    moments[1] = sem_moment(4.0, 6.0, 0.8, role="opener")
    moments[11] = sem_moment(44.0, 46.0, 0.8, role="closer")
    # one clip per moment: the continuity merge/jump-cut guard stay out
    reports = [
        ClipReport(path=f"/footage/v{i:02d}.mp4", duration=120.0, moments=[m])
        for i, m in enumerate(moments)
    ]
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
    # (10 fillers cover the 3 opening + 7 build slots exactly; the 36s
    # target replicates what the old 1.5x tolerance derived here.)
    fillers = [Moment(i * 2.0, i * 2.0 + 2.0, 0.8) for i in range(10)]
    quiet_good = Moment(40.0, 42.0, 0.9)
    hero_shot = sem_moment(44.0, 46.0, 0.5, hero=0.9)
    report = ClipReport(
        path="/footage/a.mp4",
        duration=60.0,
        moments=fillers + [quiet_good, hero_shot],
    )
    plan = plan_montage(
        [report], make_arc_music(), style="travel", order=CHRONOLOGICAL,
        cut_lead=0.0, max_duration=36.0, allow_repeats=True,
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


def _sectionless(music):
    """The same song with its section data removed."""
    from dataclasses import replace

    return replace(music, sections=[])


def test_arc_energy_blend_only_applies_with_real_sections(monkeypatch):
    # An arc style's _PHASE_ENERGY is the FALLBACK for a song with no section
    # data. With sections present the slot energy blends toward what the music
    # actually does — but a section-less song must plan exactly as before, at
    # any blend weight (neutral degradation).
    import monteur.montage as mm
    from monteur.montage import plan_to_dict

    reports = make_long_reports()
    bare = _sectionless(make_arc_music())
    plans = []
    for weight in (0.0, 0.35, 1.0):
        monkeypatch.setattr(mm, "_ARC_ENERGY_SONG_WEIGHT", weight)
        plans.append(
            plan_to_dict(plan_montage(reports, bare, style="trailer", cut_lead=0.0))
        )
    assert plans[0] == plans[1] == plans[2], "no sections -> the blend must be inert"


def test_arc_energy_blend_is_arc_dominant():
    # The blend's contract: a real lull inside a "climax" phase pulls that
    # slot's demand for motion DOWN, but never past the arc's own intent —
    # the style keeps its promise, the song only modulates within it.
    from monteur.montage import _ARC_ENERGY_SONG_WEIGHT, _blend_arc_energy

    assert _ARC_ENERGY_SONG_WEIGHT < 0.5, "the arc must stay dominant"
    # agreement is a no-op
    for value in (0.0, 0.3, 0.65, 1.0):
        assert _blend_arc_energy(value, value) == pytest.approx(value)
    # a lull under a climax pulls down, a surge under an outro pulls up
    assert 0.5 < _blend_arc_energy(1.0, 0.05) < 1.0
    assert 0.3 < _blend_arc_energy(0.3, 0.9) < 0.6
    # ...and the result always lands nearer the ARC than the song
    for nominal, song in ((1.0, 0.05), (0.3, 0.9), (0.65, 0.1), (0.35, 1.0)):
        blended = _blend_arc_energy(nominal, song)
        assert abs(blended - nominal) < abs(blended - song)
        assert min(nominal, song) <= blended <= max(nominal, song)


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
    # whooshes: centered on real cuts, 0.6s each, filling the looser density cap
    # of ceil(40 / 3.5) = 12 cues (they spread across the fastest cuts, no longer
    # confined to the climax now that the whoosh budget is higher)
    whooshes = [c for c in plan.sfx if c.kind == "whoosh"]
    assert len(whooshes) == 6
    cut_times = {e.record_start for e in plan.entries}
    for c in whooshes:
        assert c.duration == pytest.approx(0.6)
        assert c.query == "whoosh transition fast"
        center = c.time + c.duration / 2.0
        assert any(abs(center - t) < 1e-6 for t in cut_times)
    assert len(plan.sfx) == 11
    assert any(
        "sfx layer: 11 cues planned "
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
    assert len(plan.sfx) <= 12  # ceil(40 / 3.5)
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
    # travel over the 12s song: cap = ceil(12 / 3.5) = 4 cues. Whooshes never
    # make it in (dropped first); the act-change risers fill the room left after
    # the backbone (ambience + impact), which always stays.
    plan = plan_montage(
        make_reports(), make_music(), style="travel", cut_lead=0.0, sfx=True
    )
    assert len(plan.sfx) == 4
    kinds = [c.kind for c in plan.sfx]
    assert "whoosh" not in kinds  # whooshes are dropped first under the cap
    assert kinds.count("ambience") == 1 and kinds.count("impact") == 1
    risers = [c for c in plan.sfx if c.kind == "riser"]
    assert risers and any(c.note == "build -> climax" for c in risers)
    assert any("sfx layer: 4 cues planned" in n for n in plan.notes)


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
    assert len(plan.sfx) <= 6  # ceil(20 / 3.5)
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


def test_no_music_plan_carries_sound_on_every_track():
    # Field bug: "built without music — the sound track is missing entirely
    # (no clip sound, no SFX track)". A no-music plan must yield the clips'
    # own audio on A1 PLUS placed SFX on A2 in the rendered timeline.
    from monteur.model import AUDIO
    from monteur.montage import plan_montage

    plan = plan_montage(
        make_long_reports(), None, max_duration=20.0, style="travel",
        cut_lead=0.0, sfx=True,
    )
    assert plan.music_path == "" and plan.sfx
    # file one cue like monteur.elements would
    filed = plan.sfx[-1]
    filed.file = "/sfx/hit.wav"
    filed.duration = 0.8
    timeline = montage_to_timeline(plan, fps=25.0, audio="original")
    a1 = [c for c in timeline.clips if c.kind == AUDIO and c.track == "A1"]
    assert len(a1) == len(plan.entries)  # the clips' own sound, per entry
    for audio_clip, entry in zip(a1, plan.entries):
        assert audio_clip.source_file == entry.clip_path
        assert audio_clip.record_in == round(entry.record_start * 25.0)
    a2 = [c for c in timeline.clips if c.track == "A2"]
    assert [c.source_file for c in a2] == ["/sfx/hit.wav"]
    # every cue keeps its Green marker, filed or not
    sfx_markers = [m for m in timeline.markers if m.name.startswith("SFX: ")]
    assert len(sfx_markers) == len(plan.sfx)
    # and there is NO phantom song clip anywhere
    assert all(c.track != "A3" for c in timeline.clips)
    assert not any(m.name.startswith("Cut to") for m in timeline.markers)


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


def test_rhythm_holds_capped_in_absolute_seconds():
    # Field bug: trailer at pace 4s / 122 BPM produced a 32-beat (~16s)
    # establishing hold and slots longer than the source clips. Holds cap
    # at ~6s and no generated cut may exceed ~8s — unless the phase's own
    # base is already slower (a deliberately extreme pace still wins).
    from monteur.montage import (
        _MAX_CUT_SECONDS,
        _MAX_HOLD_SECONDS,
        MIN_CUT_INTERVAL,
        _apply_pace,
        _build_style_grid,
        STYLES,
    )

    tempo = 122.0
    beat = 60.0 / tempo
    length = 36.0
    beats = [i * beat for i in range(int(length / beat) + 2)]
    music = MusicAnalysis(
        path="/m/song.wav", duration=120.0, tempo=tempo, beats=beats,
        sections=[MusicSection(start=0.0, end=length, energy=0.8, label="high")],
        downbeats=beats[::4], phrases=beats[::16], drops=[24.0],
    )
    style, _steps, _note = _apply_pace(STYLES["trailer"], 4.0, beat)
    cuts, phases, notes = _build_style_grid(music, length, style)
    gaps = [b - a for a, b in zip(cuts, cuts[1:])]
    # The opening BASE at pace 4s is ~16 beats (7.9s) — a deliberately
    # slow pace wins, so the ceiling clamps the DOUBLING, not the base:
    # the old bug held 32 beats (~16s), now the opener stays at its base.
    opening_base_s = 16 * beat
    assert gaps[0] <= opening_base_s + 4 * beat + 1e-6
    assert gaps[0] < 10.0  # nothing balloons toward the old 16s hold
    assert max(gaps) <= opening_base_s + 4 * beat + 1e-6  # downbeat slack
    assert len(cuts) >= 7  # a 36s trailer is not 5 shots
    # ...and none of them strobes: this fixture used to emit a cut at 23.607
    # right before the drop-pinned climax boundary at 24.0 — a 0.39 s flash on
    # the most important moment in the cut. Structural cuts absorb such slivers.
    assert min(gaps) >= MIN_CUT_INTERVAL - 1e-6


def test_structural_cuts_never_strobe_at_any_tempo():
    # The in-phase rhythm always honoured MIN_CUT_INTERVAL, but the STRUCTURAL
    # cuts (a phase boundary, the montage end) were appended unchecked — so a
    # boundary could land a sliver after the last rhythmic cut. At 400 BPM that
    # was a 0.10 s flash. Sweep the realistic tempo range and assert the floor
    # holds everywhere, while the grid still spans 0..length and every phase
    # boundary is still a cut.
    from monteur.montage import MIN_CUT_INTERVAL, STYLES, _build_style_grid

    for style_name in ("trailer", "travel", "wedding", "music_video"):
        for tempo in (90.0, 120.0, 180.0, 240.0, 400.0):
            for length in (12.0, 40.0):
                beat = 60.0 / tempo
                beats = [i * beat for i in range(int(length / beat) + 2)]
                music = MusicAnalysis(
                    path="/m/song.wav", duration=length, tempo=tempo, beats=beats,
                    sections=[MusicSection(0.0, length, 0.9, "high")],
                )
                cuts, phases, _notes = _build_style_grid(
                    music, length, STYLES[style_name]
                )
                where = f"{style_name} @{tempo:g}bpm / {length:g}s"
                gaps = [b - a for a, b in zip(cuts, cuts[1:])]
                assert min(gaps) >= MIN_CUT_INTERVAL - 1e-6, f"strobe in {where}"
                # the grid still spans the whole montage, in order...
                assert cuts[0] == 0.0 and cuts[-1] == pytest.approx(length), where
                assert cuts == sorted(cuts), where
                # ...and absorption never ate a phase boundary
                placed = {round(c, 6) for c in cuts}
                for _start, end, _label in phases:
                    if 1e-9 < end < length - 1e-9:
                        assert round(end, 6) in placed, f"boundary lost in {where}"


# --- timeline-strip metadata (phases / music_energy / beat_marks / drop_marks) --


def make_marked_music() -> MusicAnalysis:
    """make_music() plus downbeats, phrases and one drop — strip material."""
    music = make_music()
    music.downbeats = [i * 2.0 for i in range(6)]
    music.phrases = [0.0, 8.0]
    music.drops = [8.0]
    return music


class TestStripMetadata:
    def test_arc_style_records_phases_and_music_marks(self):
        from monteur.montage import MUSIC_ENERGY_RATE

        plan = plan_montage(
            make_reports(), make_marked_music(), style="travel", cut_lead=0.0
        )
        assert plan.phases, "an arc style must record its phase spans"
        labels = {label for _, _, label in plan.phases}
        assert labels <= {"opening", "build", "climax", "outro"}
        # phases tile the montage in record time
        assert plan.phases[0][0] == pytest.approx(0.0)
        assert plan.phases[-1][1] == pytest.approx(plan.duration)
        # energy: one sample per 1/MUSIC_ENERGY_RATE seconds, all in 0..1
        expected = int(plan.duration * MUSIC_ENERGY_RATE) + 1
        assert len(plan.music_energy) == expected
        assert all(0.0 <= v <= 1.0 for v in plan.music_energy)
        # compact marks: downbeats only (not the 24 beats), plus the drop
        assert plan.beat_marks == [i * 2.0 for i in range(6)]
        assert plan.drop_marks == [8.0]

    def test_auto_style_has_energy_but_no_phases(self):
        plan = plan_montage(make_reports(), make_marked_music(), cut_lead=0.0)
        assert plan.phases == []  # "auto" cuts on sections, not an arc
        assert plan.music_energy  # the lane still renders
        assert plan.beat_marks

    def test_no_music_plan_carries_phases_only(self):
        plan = plan_montage(
            make_reports(), None, max_duration=10.0, style="travel"
        )
        assert plan.phases  # the pseudo grid still walks the arc
        assert plan.music_energy == []
        assert plan.beat_marks == []
        assert plan.drop_marks == []

    def test_round_trip(self):
        from monteur.montage import plan_from_dict, plan_to_dict

        plan = plan_montage(make_reports(), make_marked_music(), style="travel")
        loaded = plan_from_dict(plan_to_dict(plan))
        assert loaded.phases == plan.phases
        assert loaded.music_energy == plan.music_energy
        assert loaded.beat_marks == plan.beat_marks
        assert loaded.drop_marks == plan.drop_marks

    def test_old_plan_dict_without_strip_keys_loads(self):
        from monteur.montage import plan_from_dict, plan_to_dict

        data = plan_to_dict(plan_montage(make_reports(), make_marked_music()))
        for key in ("phases", "music_energy", "beat_marks", "drop_marks"):
            data.pop(key, None)
        loaded = plan_from_dict(data)
        assert loaded.phases == []
        assert loaded.music_energy == []
        assert loaded.beat_marks == []
        assert loaded.drop_marks == []

    def test_unset_fields_serialize_byte_identical(self):
        import json

        from monteur.montage import MontagePlan, plan_to_dict

        plan = MontagePlan(music_path="", duration=4.0)
        data = plan_to_dict(plan)
        for key in ("phases", "music_energy", "beat_marks", "drop_marks"):
            assert key not in data
        # a plan without strip metadata writes the exact pre-strip payload
        stripped = MontagePlan(music_path="", duration=4.0)
        assert json.dumps(plan_to_dict(stripped), sort_keys=True) == json.dumps(
            data, sort_keys=True
        )


# --- adjust_entry_boundary (the inspector's boundary control) -------------------


def make_boundary_plan():
    """Three contiguous 2s slots from one long clip; slot 1 dissolves in."""
    from monteur.montage import MontageEntry, MontagePlan

    entries = [
        MontageEntry("/footage/a.mp4", 10.0, 12.0, 0.0, 2.0, 0.9,
                     clip_duration=30.0),
        MontageEntry("/footage/b.mp4", 5.0, 7.0, 2.0, 4.0, 0.8,
                     transition=0.5, clip_duration=25.0),
        MontageEntry("/footage/a.mp4", 20.0, 22.0, 4.0, 6.0, 0.7,
                     clip_duration=30.0),
    ]
    return MontagePlan(
        music_path="/music/song.wav", duration=6.0, entries=entries,
        notes=["style \"travel\": Travel film"],
    )


class TestAdjustEntryBoundary:
    def test_set_dissolve_uses_the_half_slot_rule(self):
        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        adjusted = adjust_entry_boundary(plan, 2, "dissolve")
        assert adjusted.entries[2].transition == pytest.approx(0.5)
        assert plan.entries[2].transition == 0.0  # the original is untouched
        assert any("dissolve into slot 3" in n for n in adjusted.notes)

    def test_short_slot_dissolve_is_half_the_slot(self):
        from dataclasses import replace

        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        plan.entries[2] = replace(
            plan.entries[2], record_start=5.4, record_end=6.0, source_end=20.6
        )
        plan.entries[1] = replace(plan.entries[1], record_end=5.4, source_end=8.4)
        adjusted = adjust_entry_boundary(plan, 2, "dissolve")
        assert adjusted.entries[2].transition == pytest.approx(0.3)

    def test_clear_to_cut(self):
        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        adjusted = adjust_entry_boundary(plan, 1, "cut")
        assert adjusted.entries[1].transition == 0.0
        assert plan.entries[1].transition == pytest.approx(0.5)
        assert any("hard cut into slot 2" in n for n in adjusted.notes)

    def test_smash_carves_the_planners_dip(self):
        from monteur.montage import _DIP_SECONDS, adjust_entry_boundary

        plan = make_boundary_plan()
        adjusted = adjust_entry_boundary(plan, 2, "smash")
        prev = adjusted.entries[1]
        assert prev.record_end == pytest.approx(4.0 - _DIP_SECONDS)
        assert prev.source_end == pytest.approx(7.0 - _DIP_SECONDS)
        assert adjusted.dips == [(pytest.approx(4.0 - _DIP_SECONDS), _DIP_SECONDS)]
        assert adjusted.entries[2].transition == 0.0
        assert plan.dips == []  # the original is untouched

    def test_smash_then_cut_restores_the_grid(self):
        from dataclasses import asdict

        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        smashed = adjust_entry_boundary(plan, 2, "smash")
        restored = adjust_entry_boundary(smashed, 2, "cut")
        assert restored.dips == []
        assert [asdict(e) for e in restored.entries] == [
            asdict(e) for e in plan.entries
        ]

    def test_smash_on_a_smashed_boundary_is_a_note_not_a_change(self):
        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        smashed = adjust_entry_boundary(plan, 2, "smash")
        again = adjust_entry_boundary(smashed, 2, "smash")
        assert again.dips == smashed.dips
        assert again.entries[1].record_end == smashed.entries[1].record_end
        assert any("already smashes" in n for n in again.notes)

    def test_smash_too_short_outgoing_slot_raises(self):
        from dataclasses import replace

        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        plan.entries[1] = replace(
            plan.entries[1], record_start=3.5, source_start=6.5
        )
        with pytest.raises(ValueError, match="too short"):
            adjust_entry_boundary(plan, 2, "smash")

    def test_dip_removal_without_source_room_raises(self):
        from dataclasses import replace

        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        smashed = adjust_entry_boundary(plan, 2, "smash")
        # the outgoing clip ends exactly at its cut: no material to grow back
        smashed.entries[1] = replace(smashed.entries[1], clip_duration=6.6)
        with pytest.raises(ValueError, match="source left"):
            adjust_entry_boundary(smashed, 2, "cut")

    def test_title_texts_stay_aligned_with_dips(self):
        from monteur.montage import _DIP_SECONDS, adjust_entry_boundary

        plan = make_boundary_plan()
        plan.dips = [(1.6, 0.4)]
        plan.title_texts = ["ACT ONE"]
        plan.entries[0].record_end = 1.6
        plan.entries[0].source_end = 11.6
        smashed = adjust_entry_boundary(plan, 2, "smash")
        assert smashed.dips == [(1.6, 0.4), (pytest.approx(4.0 - _DIP_SECONDS), _DIP_SECONDS)]
        assert smashed.title_texts == ["ACT ONE", ""]
        # removing the FIRST dip drops its title, the later one keeps its slot
        opened = adjust_entry_boundary(smashed, 1, "cut")
        assert opened.dips == smashed.dips[1:]
        assert opened.title_texts == [""]

    def test_everything_else_stays_bit_identical(self):
        from dataclasses import asdict

        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        plan.sfx = []
        adjusted = adjust_entry_boundary(plan, 1, "cut")
        assert asdict(adjusted.entries[0]) == asdict(plan.entries[0])
        assert asdict(adjusted.entries[2]) == asdict(plan.entries[2])
        assert adjusted.duration == plan.duration
        assert adjusted.music_start == plan.music_start
        assert adjusted.notes[: len(plan.notes)] == plan.notes

    def test_validation_errors(self):
        from monteur.montage import adjust_entry_boundary

        plan = make_boundary_plan()
        with pytest.raises(ValueError, match="valid transitions"):
            adjust_entry_boundary(plan, 1, "wipe")
        with pytest.raises(ValueError, match="not in this plan"):
            adjust_entry_boundary(plan, 3, "cut")
        with pytest.raises(ValueError, match="not in this plan"):
            adjust_entry_boundary(plan, -1, "cut")
        with pytest.raises(ValueError, match="entry index"):
            adjust_entry_boundary(plan, "x", "cut")
        with pytest.raises(ValueError, match="fade-in"):
            adjust_entry_boundary(plan, 0, "dissolve")


def _record_grid_is_contiguous(entries, dips):
    """The re-flow invariant: first shot at 0, every shot butts the
    previous shot's out (plus any black dip that sits between them)."""
    from monteur.montage import _BOUNDARY_EPS, _EPS

    assert entries[0].record_start == pytest.approx(0.0)
    starts = sorted(ds for ds, _ in dips)
    lengths = {round(ds, 6): dl for ds, dl in dips}
    for prev, cur in zip(entries, entries[1:]):
        gap = 0.0
        # a dip sitting exactly on the previous shot's out extends the seam
        for ds in starts:
            if abs(ds - prev.record_end) <= _BOUNDARY_EPS + _EPS:
                gap = lengths[round(ds, 6)]
        assert cur.record_start == pytest.approx(prev.record_end + gap)
        assert cur.record_end >= cur.record_start


class TestDeleteEntry:
    def test_removes_the_slot_and_reflows(self):
        from monteur.montage import delete_entry

        plan = make_boundary_plan()
        adjusted = delete_entry(plan, 1)
        assert len(adjusted.entries) == 2
        assert len(plan.entries) == 3  # the original is untouched
        # the two survivors are shots a and c, in order
        assert [e.clip_path for e in adjusted.entries] == [
            "/footage/a.mp4",
            "/footage/a.mp4",
        ]
        _record_grid_is_contiguous(adjusted.entries, adjusted.dips)
        assert any(n.startswith("delete: removed slot 2") for n in adjusted.notes)

    def test_source_windows_are_preserved(self):
        from monteur.montage import delete_entry

        plan = make_boundary_plan()
        adjusted = delete_entry(plan, 0)
        # shot c kept its exact source window, only its record slot moved
        kept = adjusted.entries[-1]
        assert (kept.source_start, kept.source_end) == (20.0, 22.0)
        assert kept.record_start == pytest.approx(2.0)
        assert kept.record_end == pytest.approx(4.0)

    def test_duration_shrinks_by_the_removed_span(self):
        from monteur.montage import delete_entry

        plan = make_boundary_plan()
        adjusted = delete_entry(plan, 2)  # drops the 2s tail shot
        assert adjusted.duration == pytest.approx(4.0)

    def test_orphaned_dip_and_title_are_dropped(self):
        from monteur.montage import delete_entry

        plan = make_boundary_plan()
        # shot a smashes to black into shot b; act title on that dip
        plan.dips = [(1.6, 0.4)]
        plan.title_texts = ["ACT ONE"]
        plan.entries[0].record_end = 1.6
        plan.entries[0].source_end = 11.6
        plan.entries[1].record_start = 2.0  # the dip fills 1.6 -> 2.0
        adjusted = delete_entry(plan, 0)  # remove the shot the dip rode on
        assert adjusted.dips == []
        assert adjusted.title_texts == []
        _record_grid_is_contiguous(adjusted.entries, adjusted.dips)

    def test_out_of_range_raises(self):
        from monteur.montage import delete_entry

        plan = make_boundary_plan()
        with pytest.raises(ValueError, match="not in this plan"):
            delete_entry(plan, 3)
        with pytest.raises(ValueError, match="not in this plan"):
            delete_entry(plan, -1)
        with pytest.raises(ValueError, match="entry index"):
            delete_entry(plan, "x")

    def test_deleting_the_last_entry_raises(self):
        from dataclasses import replace

        from monteur.montage import delete_entry

        plan = make_boundary_plan()
        plan = replace(plan, entries=plan.entries[:1])
        with pytest.raises(ValueError, match="last entry"):
            delete_entry(plan, 0)


class TestMoveEntry:
    def test_reorders_and_reflows(self):
        from monteur.montage import move_entry

        plan = make_boundary_plan()
        adjusted = move_entry(plan, 0, 2)  # first shot to the end
        assert [e.clip_path for e in adjusted.entries] == [
            "/footage/b.mp4",
            "/footage/a.mp4",
            "/footage/a.mp4",
        ]
        # the moved shot kept its source window
        assert (adjusted.entries[-1].source_start, adjusted.entries[-1].source_end) == (
            10.0,
            12.0,
        )
        assert len(plan.entries) == 3  # the original is untouched
        _record_grid_is_contiguous(adjusted.entries, adjusted.dips)
        assert adjusted.duration == pytest.approx(plan.duration)
        assert any(n.startswith("move: slot 1 -> position 3") for n in adjusted.notes)

    def test_move_earlier_preserves_every_source_window(self):
        from monteur.montage import move_entry

        plan = make_boundary_plan()
        before = [(e.source_start, e.source_end) for e in plan.entries]
        adjusted = move_entry(plan, 2, 0)  # last shot to the front
        after = [(e.source_start, e.source_end) for e in adjusted.entries]
        assert after == [before[2], before[0], before[1]]
        _record_grid_is_contiguous(adjusted.entries, adjusted.dips)

    def test_noop_move_is_an_equivalent_plan(self):
        from monteur.montage import move_entry

        plan = make_boundary_plan()
        adjusted = move_entry(plan, 1, 1)
        assert [e.clip_path for e in adjusted.entries] == [
            e.clip_path for e in plan.entries
        ]
        assert [
            (e.record_start, e.record_end, e.source_start, e.source_end)
            for e in adjusted.entries
        ] == [
            (e.record_start, e.record_end, e.source_start, e.source_end)
            for e in plan.entries
        ]
        _record_grid_is_contiguous(adjusted.entries, adjusted.dips)

    def test_out_of_range_raises(self):
        from monteur.montage import move_entry

        plan = make_boundary_plan()
        with pytest.raises(ValueError, match="not in this plan"):
            move_entry(plan, 3, 0)
        with pytest.raises(ValueError, match="not in this plan"):
            move_entry(plan, 0, 3)
        with pytest.raises(ValueError, match="not in this plan"):
            move_entry(plan, -1, 0)
        with pytest.raises(ValueError, match="entry indices"):
            move_entry(plan, "x", 0)


class TestFreeEdits:
    """The NLE edits: nothing on the timeline moves except what you grabbed.

    The generated plan is contiguous; these deliberately break that, because a
    hole is an editorial choice (a short black pause), not damage to repair.
    """

    def test_lift_leaves_the_hole_and_keeps_the_duration(self):
        from monteur.montage import lift_entry

        plan = make_boundary_plan()  # 3 x 2s slots, 6s total
        adjusted = lift_entry(plan, 1)
        assert len(adjusted.entries) == 2
        assert len(plan.entries) == 3  # the original is untouched
        # the survivors did NOT slide: the middle 2s is now an open hole
        assert [(e.record_start, e.record_end) for e in adjusted.entries] == [
            (0.0, 2.0), (4.0, 6.0),
        ]
        assert adjusted.duration == pytest.approx(6.0)  # the music is untouched
        assert any(n.startswith("lift: removed slot 2") for n in adjusted.notes)

    def test_ripple_delete_still_closes_the_gap(self):
        # the OTHER delete is unchanged — both behaviours stay available
        from monteur.montage import delete_entry, lift_entry

        plan = make_boundary_plan()
        assert delete_entry(plan, 1).duration == pytest.approx(4.0)
        assert lift_entry(plan, 1).duration == pytest.approx(6.0)

    def test_lift_may_empty_the_picture_track(self):
        # an empty timeline is a legal starting point, not an error
        from monteur.montage import lift_entry

        plan = make_boundary_plan()
        for _ in range(3):
            plan = lift_entry(plan, 0)
        assert plan.entries == []
        assert plan.duration == pytest.approx(6.0)

    def test_move_to_a_free_position_moves_nothing_else(self):
        from monteur.montage import lift_entry, move_entry_to

        plan = lift_entry(make_boundary_plan(), 1)  # hole at 2..4
        adjusted = move_entry_to(plan, 1, 2.5)      # slide shot c into it
        assert [(e.record_start, e.record_end) for e in adjusted.entries] == [
            (0.0, 2.0), (2.5, 4.5),
        ]
        # its source window rode along verbatim — a move is not a trim
        moved = adjusted.entries[1]
        assert (moved.source_start, moved.source_end) == (20.0, 22.0)
        assert adjusted.duration == pytest.approx(6.0)

    def test_move_past_a_neighbour_reorders_the_entry_list(self):
        # dragging a shot beyond its neighbour genuinely reorders the cut —
        # the entries come back in record order, whatever the old index was
        from monteur.montage import MontageEntry, MontagePlan, move_entry_to

        plan = MontagePlan(
            music_path="/music/song.wav", duration=10.0,
            entries=[
                MontageEntry("/f/a.mp4", 0.0, 1.0, 0.0, 1.0, 0.9, clip_duration=30.0),
                MontageEntry("/f/b.mp4", 0.0, 1.0, 2.0, 3.0, 0.8, clip_duration=30.0),
            ],
        )
        adjusted = move_entry_to(plan, 0, 5.0)  # drag a past b
        assert [e.clip_path for e in adjusted.entries] == ["/f/b.mp4", "/f/a.mp4"]
        assert [(e.record_start, e.record_end) for e in adjusted.entries] == [
            (2.0, 3.0), (5.0, 6.0),
        ]

    def test_trim_moves_the_source_window_with_the_edge(self):
        from monteur.montage import trim_entry

        plan = make_boundary_plan()
        # drag slot 0's tail 0.5s earlier
        adjusted = trim_entry(plan, 0, record_end=1.5)
        e = adjusted.entries[0]
        assert (e.record_start, e.record_end) == (0.0, 1.5)
        assert (e.source_start, e.source_end) == (10.0, 11.5)  # follows 1:1
        # drag its head 0.5s later: the picture under the head does not shift
        adjusted = trim_entry(plan, 0, record_start=0.5)
        e = adjusted.entries[0]
        assert (e.record_start, e.record_end) == (0.5, 2.0)
        assert (e.source_start, e.source_end) == (10.5, 12.0)

    def test_trim_refuses_a_sliver(self):
        from monteur.montage import _MIN_SLOT_SECONDS, trim_entry

        plan = make_boundary_plan()
        with pytest.raises(ValueError, match="sliver"):
            trim_entry(plan, 0, record_end=0.1)
        # exactly at the floor is fine
        ok = trim_entry(plan, 0, record_end=_MIN_SLOT_SECONDS)
        assert ok.entries[0].record_end == pytest.approx(_MIN_SLOT_SECONDS)

    def test_trim_refuses_to_reach_past_the_media(self):
        # room in the MONTAGE, but no footage left in the clip
        from monteur.montage import MontageEntry, MontagePlan, trim_entry

        plan = MontagePlan(
            music_path="/music/song.wav", duration=10.0,
            entries=[
                # the last half-second of a 5s clip
                MontageEntry("/f/a.mp4", 4.0, 4.5, 0.0, 0.5, 0.9, clip_duration=5.0),
            ],
        )
        with pytest.raises(ValueError, match="past the end of the clip"):
            trim_entry(plan, 0, record_end=2.0)  # would need 6s of a 5s clip
        # ...and a head trim never reaches before the clip's own start
        plan_head = MontagePlan(
            music_path="/music/song.wav", duration=10.0,
            entries=[
                MontageEntry("/f/a.mp4", 0.2, 2.0, 1.0, 2.8, 0.9, clip_duration=5.0),
            ],
        )
        with pytest.raises(ValueError, match="before the start of the clip"):
            trim_entry(plan_head, 0, record_start=0.0)  # needs source -0.2

    def test_free_edits_refuse_to_overlap(self):
        from monteur.montage import move_entry_to, trim_entry

        plan = make_boundary_plan()
        with pytest.raises(ValueError, match="overlap"):
            move_entry_to(plan, 0, 1.0)   # onto slot 1
        with pytest.raises(ValueError, match="overlap"):
            trim_entry(plan, 0, record_end=3.0)  # into slot 1

    def test_free_edits_stay_inside_the_montage(self):
        from monteur.montage import move_entry_to

        plan = make_boundary_plan()
        with pytest.raises(ValueError, match="past the end"):
            move_entry_to(plan, 0, 5.0)  # a 2s shot cannot start at 5s of 6s

    def test_a_hole_exports_as_black(self):
        # the payoff: montage_to_timeline already places clips at absolute
        # record time, so a lifted span is simply ABSENT from V1 — black.
        from monteur.montage import lift_entry, montage_to_timeline

        plan = lift_entry(make_boundary_plan(), 1)
        timeline = montage_to_timeline(plan, fps=25.0)
        v1 = sorted(
            (c for c in timeline.clips if c.track == "V1"),
            key=lambda c: c.record_in,
        )
        assert [(c.record_in, c.record_out) for c in v1] == [(0, 50), (100, 150)]
        # the hole is real: frames 50..100 carry no picture at all
        assert not any(
            c.record_in < 100 and 50 < c.record_out for c in v1[1:]
        )


def _sfx_plan():
    """A boundary plan carrying a full music-bed strip + SFX layer."""
    from monteur.montage import SfxCue

    plan = make_boundary_plan()  # 3x 2s = 6s
    plan.phases = [(0.0, 2.0, "opening"), (2.0, 4.0, "build"), (4.0, 6.0, "climax")]
    plan.music_energy = [round(0.1 * i, 2) for i in range(13)]  # 6s * 2/s + 1
    plan.beat_marks = [1.0, 2.0, 3.0, 4.0, 5.0]
    plan.drop_marks = [2.0, 4.0]
    plan.sfx = [
        SfxCue(0.0, 2.0, "ambience", "amb", "opening"),
        SfxCue(4.0, 0.5, "impact", "hit", "on the drop"),
    ]
    return plan


class TestResequenceClipsTheMusicBed:
    def test_delete_truncates_marks_energy_and_phases(self):
        from monteur.montage import MUSIC_ENERGY_RATE, delete_entry

        plan = _sfx_plan()
        adjusted = delete_entry(plan, 1)  # 6s -> 4s
        assert adjusted.duration == pytest.approx(4.0)
        # drops/beats/energy/phases beyond the new end are clipped, not shifted
        assert adjusted.drop_marks == [2.0, 4.0]  # both still within 4s
        assert adjusted.beat_marks == [1.0, 2.0, 3.0, 4.0]  # 5.0 dropped
        assert len(adjusted.music_energy) == int(4.0 * MUSIC_ENERGY_RATE) + 1
        assert all(s <= 4.0 + 1e-6 for s, _e, _l in adjusted.phases)
        assert adjusted.phases[-1][1] == pytest.approx(4.0)  # climax clipped
        # a cue past the new end is dropped; the original plan is untouched
        assert all(c.time <= 4.0 + 1e-6 for c in adjusted.sfx)
        assert plan.duration == pytest.approx(6.0)


class TestResyncAudio:
    def test_relays_sfx_onto_the_current_cut(self):
        from monteur.montage import delete_entry, resync_audio

        plan = _sfx_plan()
        edited = delete_entry(plan, 1)  # the sfx layer is now stale
        before = list(edited.sfx)
        resynced = resync_audio(edited)
        # a fresh SFX layer was planned (ambience under the opening is always
        # present) and every cue lands within the edited duration
        assert resynced.sfx  # non-empty
        assert all(c.time <= resynced.duration + 1e-6 for c in resynced.sfx)
        assert any(c.kind == "ambience" for c in resynced.sfx)
        assert any(n.startswith("resync:") for n in resynced.notes)
        # the input plan is never modified in place
        assert edited.sfx == before

    def test_no_entries_is_a_safe_noop(self):
        from dataclasses import replace

        from monteur.montage import resync_audio

        plan = replace(_sfx_plan(), entries=[], duration=0.0)
        resynced = resync_audio(plan)
        assert resynced.sfx == []  # nothing to re-lay
        assert any(n.startswith("resync:") for n in resynced.notes)


class TestSfxCueSurgery:
    def test_add_appends_and_keeps_sorted(self):
        from monteur.montage import add_sfx_cue

        plan = _sfx_plan()  # ambience@0, impact@4
        adjusted = add_sfx_cue(plan, 2.0, 0.4, "whoosh", "swoosh", "fast cut")
        times = [c.time for c in adjusted.sfx]
        assert times == sorted(times)
        assert any(c.kind == "whoosh" and c.time == 2.0 for c in adjusted.sfx)
        assert len(plan.sfx) == 2  # original untouched
        assert any(n.startswith("sfx: added") for n in adjusted.notes)

    def test_add_clamps_time_into_the_cut(self):
        from monteur.montage import add_sfx_cue

        plan = _sfx_plan()  # 6s
        adjusted = add_sfx_cue(plan, 99.0, kind="impact")
        added = [c for c in adjusted.sfx if c.note == "added by hand"][0]
        assert added.time == pytest.approx(6.0)  # clamped to duration

    def test_add_rejects_unknown_kind(self):
        from monteur.montage import add_sfx_cue

        with pytest.raises(ValueError, match="kind must be one of"):
            add_sfx_cue(_sfx_plan(), 1.0, kind="boom")

    def test_update_edits_the_indexed_cue(self):
        from monteur.montage import update_sfx_cue

        plan = _sfx_plan()  # index 0 = ambience@0
        adjusted = update_sfx_cue(plan, 0, query="new ambience", duration=3.0)
        assert adjusted.sfx[0].query == "new ambience"
        assert adjusted.sfx[0].duration == pytest.approx(3.0)
        assert plan.sfx[0].query == "amb"  # original untouched

    def test_update_time_change_re_sorts(self):
        from monteur.montage import update_sfx_cue

        plan = _sfx_plan()  # ambience@0, impact@4
        adjusted = update_sfx_cue(plan, 0, time=5.0)  # push ambience past impact
        assert [c.time for c in adjusted.sfx] == [4.0, 5.0]
        assert adjusted.sfx[1].kind == "ambience"

    def test_update_out_of_range_raises(self):
        from monteur.montage import update_sfx_cue

        with pytest.raises(ValueError, match="not in this plan"):
            update_sfx_cue(_sfx_plan(), 9, query="x")

    def test_update_sets_a_hand_picked_file(self):
        from monteur.montage import update_sfx_cue

        plan = _sfx_plan()  # index 0 = ambience@0, no file
        adjusted = update_sfx_cue(plan, 0, file="/sfx/my_boom.wav")
        assert adjusted.sfx[0].file == "/sfx/my_boom.wav"
        assert adjusted.sfx[0].source_offset == 0.0  # a picked file plays from 0
        assert plan.sfx[0].file == ""  # original untouched

    def test_update_clears_the_file_back_to_auto(self):
        from dataclasses import replace

        from monteur.montage import update_sfx_cue

        plan = _sfx_plan()
        plan.sfx[0] = replace(plan.sfx[0], file="/sfx/x.wav", source_offset=1.2)
        adjusted = update_sfx_cue(plan, 0, file="")
        assert adjusted.sfx[0].file == ""

    def test_update_time_preserves_an_existing_file(self):
        # editing time WITHOUT touching file keeps the file and its offset
        from dataclasses import replace

        from monteur.montage import update_sfx_cue

        plan = _sfx_plan()
        plan.sfx[0] = replace(plan.sfx[0], file="/sfx/x.wav", source_offset=1.2)
        adjusted = update_sfx_cue(plan, 0, duration=2.0)
        assert adjusted.sfx[0].file == "/sfx/x.wav"
        assert adjusted.sfx[0].source_offset == pytest.approx(1.2)

    def test_delete_removes_the_indexed_cue(self):
        from monteur.montage import delete_sfx_cue

        plan = _sfx_plan()
        adjusted = delete_sfx_cue(plan, 0)  # drop the ambience
        assert [c.kind for c in adjusted.sfx] == ["impact"]
        assert len(plan.sfx) == 2  # original untouched
        assert any(n.startswith("sfx: removed") for n in adjusted.notes)

    def test_delete_out_of_range_raises(self):
        from monteur.montage import delete_sfx_cue

        with pytest.raises(ValueError, match="not in this plan"):
            delete_sfx_cue(_sfx_plan(), -1)


# --- time-of-day coherence (Moment.daylight) -----------------------------------


def _flat_music(duration: float = 12.0, energy: float = 0.5, drops=None) -> MusicAnalysis:
    """One flat section so the grid stays simple and slot energy constant."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, energy, "mid")],
        drops=list(drops or []),
    )


def _alternating_daylight_report(with_daylight: bool = True) -> ClipReport:
    """Six 2s moments whose daylight alternates day/night in file order.

    Moments sit 12s apart — far enough that neither the jump-cut guard
    (< 8s gaps) nor the continuity merge (< 3s gaps) interferes, so these
    tests observe the daylight terms in isolation."""
    moments = []
    for i in range(6):
        m = Moment(i * 12.0, i * 12.0 + 2.0, 0.8)
        if with_daylight:
            m.daylight = "day" if i % 2 == 0 else "night"
        moments.append(m)
    return ClipReport(path="/footage/mixed.mp4", duration=80.0, moments=moments)


def _entry_daylight(entry, reports) -> str:
    """The daylight class of the moment an entry was cast from."""
    best, best_ov = "", 0.0
    for report in reports:
        if report.path != entry.clip_path:
            continue
        for m in report.moments:
            ov = min(m.end, entry.source_end) - max(m.start, entry.source_start)
            if ov > best_ov:
                best, best_ov = m.daylight, ov
    return best


def _daylight_switches(plan, reports) -> int:
    classes = [
        _entry_daylight(e, reports)
        for e in sorted(plan.entries, key=lambda e: e.record_start)
    ]
    classes = [c for c in classes if c]
    return sum(1 for a, b in zip(classes, classes[1:]) if a != b)


def test_daylight_coherence_reduces_switches_on_shuffled_material():
    shuffled = [_alternating_daylight_report(with_daylight=True)]
    blank = [_alternating_daylight_report(with_daylight=False)]
    plan_coherent = plan_montage(shuffled, _flat_music(), cut_lead=0.0)
    plan_blank = plan_montage(blank, _flat_music(), cut_lead=0.0)
    # The blank plan fills in pool order (alternating day/night); the
    # coherence penalty + block bonus regroup the material into blocks.
    before = _daylight_switches(plan_blank, shuffled)  # same windows, read classes
    after = _daylight_switches(plan_coherent, shuffled)
    assert before >= 3  # the shuffled baseline really does thrash
    assert after < before
    assert any("story: daylight arc day -> night (soft)" in n for n in plan_coherent.notes)


def test_daylight_blank_is_neutral_and_unnoted():
    plan = plan_montage(
        [_alternating_daylight_report(with_daylight=False)], _flat_music(), cut_lead=0.0
    )
    assert not any("daylight" in n for n in plan.notes)
    assert not any(n.startswith("story:") for n in plan.notes)
    # Pool order preserved exactly (first pass walks the moments in order).
    starts = [e.source_start for e in plan.entries[:6]]
    assert starts == sorted(starts)


def test_daylight_single_class_has_no_arc():
    report = _alternating_daylight_report(with_daylight=False)
    for m in report.moments:
        m.daylight = "day"  # one class only: nothing to order
    plan = plan_montage([report], _flat_music(), cut_lead=0.0)
    assert not any(n.startswith("story: daylight") for n in plan.notes)
    starts = [e.source_start for e in plan.entries[:6]]
    assert starts == sorted(starts)  # switch penalty never fires: no reorder


def test_daylight_arc_follows_available_classes():
    report = _alternating_daylight_report(with_daylight=False)
    for i, m in enumerate(report.moments):
        m.daylight = "golden" if i % 2 == 0 else "night"  # no day at all
    plan = plan_montage([report], _flat_music(), cut_lead=0.0)
    assert any(
        "story: daylight arc golden -> night (soft)" in n for n in plan.notes
    )


def test_daylight_against_flow_slot_is_flagged():
    # Plenty of day material plus ONE night moment carrying the audio
    # highlight: the drop reservation puts it early, inside the day block.
    day = ClipReport(
        path="/footage/day.mp4",
        duration=40.0,
        moments=[Moment(i * 5.0, i * 5.0 + 3.0, 0.8) for i in range(6)],
    )
    for m in day.moments:
        m.daylight = "day"
    night = ClipReport(
        path="/footage/night.mp4",
        duration=10.0,
        moments=[Moment(0.0, 2.0, 0.5, highlight=0.95)],
    )
    night.moments[0].daylight = "night"
    plan = plan_montage([day, night], _flat_music(drops=[2.0]), cut_lead=0.0)
    drop_entry = next(e for e in plan.entries if abs(e.record_start - 2.0) < 1e-6)
    assert drop_entry.clip_path == "/footage/night.mp4"
    assert any("night shot inside the day block" in n for n in plan.notes)


def test_daylight_arranged_slots_win_and_are_not_flagged():
    report = _alternating_daylight_report(with_daylight=True)
    # The editor demands night first — the arrangement wins outright.
    arrangement = [
        {"clip": "mixed.mp4", "start": 12.0},  # a night moment
        {"clip": "mixed.mp4", "start": 0.0},   # then a day moment
    ]
    plan = plan_montage(
        [report], _flat_music(), cut_lead=0.0, arrangement=arrangement
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    assert entries[0].source_start == pytest.approx(12.0, abs=0.3)
    assert entries[1].source_start == pytest.approx(0.0, abs=0.3)
    # Arranged slots are never flagged against the arc.
    assert not any(
        n.startswith(("slot 1:", "slot 2:")) and "block" in n for n in plan.notes
    )


# --- content-adaptive pacing (the slot-merge pass) ------------------------------


def _pacing_music() -> MusicAnalysis:
    """12s: a calm low half and a loud high half, beats every 0.5s."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=12.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(24)],
        sections=[
            MusicSection(0.0, 6.0, 0.2, "low"),
            MusicSection(6.0, 12.0, 0.9, "high"),
        ],
    )


def _pacing_reports(calm_motion=(0.2, 0.0)) -> list[ClipReport]:
    """Two calm clips (one long moment each) + a fast clip (the motion peak).

    The calm moments live in DIFFERENT clips: same-clip adjacency would be
    joined by the CONTINUITY merge first — these tests exercise the
    calm-on-calm PACING merge across clips."""
    calm = ClipReport(
        path="/f/calm_a.mp4",
        duration=20.0,
        moments=[
            Moment(0.0, 8.0, 0.9, entry_motion=calm_motion, exit_motion=calm_motion),
        ],
    )
    calm_b = ClipReport(
        path="/f/calm_b.mp4",
        duration=20.0,
        moments=[
            Moment(0.0, 8.0, 0.8, entry_motion=calm_motion, exit_motion=calm_motion),
        ],
    )
    # Plenty of fast moments so the fill never re-slices the calm clip's
    # tail into a later slot (which would rightly block a merge — the
    # extension would repeat material another slot plays).
    fast = ClipReport(
        path="/f/fast.mp4",
        duration=40.0,
        moments=[
            Moment(i * 3.0, i * 3.0 + 2.0, 0.7,
                   entry_motion=(10.0, 0.0), exit_motion=(10.0, 0.0))
            for i in range(12)
        ],
    )
    return [calm, calm_b, fast]


def test_calm_adjacent_slots_merge_in_the_quiet_section():
    merged = plan_montage(_pacing_reports(), _pacing_music(), cut_lead=0.0)
    allfast = plan_montage(
        _pacing_reports(calm_motion=(10.0, 0.0)), _pacing_music(), cut_lead=0.0
    )
    # Fewer cuts exactly where the content is slow.
    assert len(merged.entries) < len(allfast.entries)
    assert any(n.startswith("pacing:") and "merged" in n for n in merged.notes)
    assert not any(n.startswith("pacing:") for n in allfast.notes)
    # The merged shot: longer record, source extended 1:1, capped at 8s.
    longest = max(merged.entries, key=lambda e: e.record_end - e.record_start)
    length = longest.record_end - longest.record_start
    assert length > 2.0 + 1e-6
    assert length <= 8.0 + 1e-6
    assert longest.source_end - longest.source_start == pytest.approx(length)
    # Cuts stay on the grid: every merged boundary is one of the unmerged
    # plan's boundaries (a merge only REMOVES cuts, never moves one).
    unmerged_bounds = {round(e.record_start, 4) for e in allfast.entries} | {
        round(e.record_end, 4) for e in allfast.entries
    }
    for entry in merged.entries:
        assert round(entry.record_start, 4) in unmerged_bounds
        assert round(entry.record_end, 4) in unmerged_bounds
    # The loud half keeps its fast cuts (energy gate).
    for entry in merged.entries:
        if entry.record_start >= 6.0 - 1e-6:
            assert entry.record_end - entry.record_start <= 2.0 + 1e-6
    # The montage still tiles contiguously to the full length.
    ordered = sorted(merged.entries, key=lambda e: e.record_start)
    assert ordered[0].record_start == pytest.approx(0.0)
    for a, b in zip(ordered, ordered[1:]):
        assert a.record_end == pytest.approx(b.record_start)
    assert ordered[-1].record_end == pytest.approx(merged.duration)


def test_merge_keeps_split_when_the_moment_lacks_material():
    # Calm 2s moments: extending past moment.end is impossible, the split
    # is kept and no pacing note appears.
    calm = ClipReport(
        path="/f/calm.mp4",
        duration=30.0,
        moments=[
            Moment(i * 2.0, i * 2.0 + 2.0, 0.9,
                   entry_motion=(0.2, 0.0), exit_motion=(0.2, 0.0))
            for i in range(6)
        ],
    )
    fast = ClipReport(
        path="/f/fast.mp4",
        duration=30.0,
        moments=[
            Moment(i * 3.0, i * 3.0 + 2.0, 0.7,
                   entry_motion=(10.0, 0.0), exit_motion=(10.0, 0.0))
            for i in range(4)
        ],
    )
    plan = plan_montage([calm, fast], _pacing_music(), cut_lead=0.0)
    assert not any(n.startswith("pacing:") for n in plan.notes)


def test_merge_parity_when_all_moments_are_high_motion():
    reports = _pacing_reports(calm_motion=(10.0, 0.0))
    plan_a = plan_montage(reports, _pacing_music(), cut_lead=0.0)
    plan_b = plan_montage(
        _pacing_reports(calm_motion=(10.0, 0.0)), _pacing_music(), cut_lead=0.0
    )
    from monteur.montage import plan_to_dict as _ptd

    assert _ptd(plan_a) == _ptd(plan_b)  # deterministic, byte-identical
    assert not any(n.startswith("pacing:") for n in plan_a.notes)


def test_merge_never_absorbs_the_drop_slot():
    music = _pacing_music()
    music.drops = [2.0]
    plan = plan_montage(_pacing_reports(), music, cut_lead=0.0)
    # The drop-forced cut at 2.0 survives every merge.
    assert any(abs(e.record_start - 2.0) < 1e-6 for e in plan.entries)


def test_merge_spares_arranged_slots():
    plain = plan_montage(_pacing_reports(), _pacing_music(), cut_lead=0.0)
    assert any(n.startswith("pacing:") for n in plain.notes)
    first_len_plain = min(
        plain.entries, key=lambda e: e.record_start
    )
    arrangement = [
        {"clip": "calm_a.mp4", "start": 0.0},
        {"clip": "calm_b.mp4", "start": 0.0},
    ]
    arranged = plan_montage(
        _pacing_reports(), _pacing_music(), cut_lead=0.0, arrangement=arrangement
    )
    entries = sorted(arranged.entries, key=lambda e: e.record_start)
    # The two arranged slots keep their own cut between them (no merge),
    # so the first entry is strictly shorter than the plain plan's merged
    # opening shot.
    assert entries[0].record_end == pytest.approx(entries[1].record_start)
    assert (
        entries[0].record_end - entries[0].record_start
        < first_len_plain.record_end - first_len_plain.record_start - 1e-6
    )


def test_merge_stays_out_of_the_climax_and_inside_phases():
    """A styled cut merges only in gentle phases and never across bounds."""
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
    )
    calm = ClipReport(
        path="/f/calm.mp4",
        duration=90.0,
        moments=[
            Moment(i * 12.0, i * 12.0 + 10.0, 0.9,
                   entry_motion=(0.2, 0.0), exit_motion=(0.2, 0.0))
            for i in range(7)
        ],
    )
    # A surplus of fast moments: with more moments than slots no calm
    # tail is ever re-sliced, so the merge pass has room to act.
    fast = ClipReport(
        path="/f/fast.mp4",
        duration=90.0,
        moments=[
            Moment(i * 2.0, i * 2.0 + 1.5, 0.7,
                   entry_motion=(10.0, 0.0), exit_motion=(10.0, 0.0))
            for i in range(40)
        ],
    )
    plan = plan_montage([calm, fast], music, style="travel", cut_lead=0.0)
    assert any(n.startswith("pacing:") for n in plan.notes)  # merges DID happen
    assert plan.phases
    climax = next((s, e) for s, e, lab in plan.phases if lab == "climax")
    for entry in plan.entries:
        # No entry ever crosses a phase boundary (merges stay inside acts).
        spans = [
            (s, e) for s, e, _lab in plan.phases
            if entry.record_start >= s - 1e-6 and entry.record_start < e - 1e-6
        ]
        assert spans and entry.record_end <= spans[0][1] + 1e-6
        # Climax slots never CALM-merge: calm material there stays at/below
        # the longest single climax cut — blueprint 1.6's cool phrase groups
        # slam up to 4 beats (2s at 120 bpm). (Longer climax entries exist,
        # but only as same-clip CONTINUITY joins of the fast material, which
        # the climax deliberately allows — the ride is held, not re-cut.)
        if climax[0] - 1e-6 <= entry.record_start < climax[1] - 1e-6:
            if entry.clip_path == "/f/calm.mp4":
                assert entry.record_end - entry.record_start <= 2.0 + 1e-6
            else:
                # a continuity join plays continuous source 1:1 — never a skip
                assert (entry.source_end - entry.source_start) == pytest.approx(
                    entry.record_end - entry.record_start
                )


def test_merge_never_swallows_a_smash_dip():
    calm = ClipReport(
        path="/f/calm.mp4",
        duration=90.0,
        moments=[
            Moment(i * 12.0, i * 12.0 + 10.0, 0.9,
                   entry_motion=(0.2, 0.0), exit_motion=(0.2, 0.0))
            for i in range(7)
        ],
    )
    fast = ClipReport(
        path="/f/fast.mp4",
        duration=90.0,
        moments=[
            Moment(i * 2.0, i * 2.0 + 1.5, 0.7,
                   entry_motion=(10.0, 0.0), exit_motion=(10.0, 0.0))
            for i in range(40)
        ],
    )
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
    )
    plan = plan_montage([calm, fast], music, style="trailer", cut_lead=0.0)
    assert any(n.startswith("pacing:") for n in plan.notes)  # merges DID happen
    assert plan.dips  # the trailer still smashes to black
    for dip_start, _length in plan.dips:
        assert any(
            abs(e.record_end - dip_start) < 1e-3 for e in plan.entries
        )  # every dip still sits on a real entry boundary


# --- same-clip continuity (the continuity merge + the jump-cut guard) -----------


def _cont_music(duration: float = 12.0) -> MusicAnalysis:
    """Flat mid-energy track: simple grid, no drops, no phases."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, 0.5, "mid")],
    )


def test_continuity_merge_bridges_a_small_source_gap():
    # Two moments of ONE clip 1s apart in source: cut on adjacent slots
    # that is a jump cut — the merge plays straight through the bridge.
    report = ClipReport(
        path="/f/ride.mp4",
        duration=30.0,
        moments=[Moment(0.0, 1.5, 0.9), Moment(2.5, 4.5, 0.8)],
    )
    other = ClipReport(
        path="/f/z_other.mp4", duration=30.0, moments=[Moment(0.0, 2.0, 0.7)]
    )
    plan = plan_montage([report, other], None, max_duration=4.5, cut_lead=0.0)
    note = [n for n in plan.notes if n.startswith("continuity:")]
    assert note and "joined" in note[0]
    merged = plan.entries[0]
    assert merged.clip_path == "/f/ride.mp4"
    # continuous playback: source runs 1:1 with the record, INCLUDING the
    # bridge frames between the two sifted windows
    assert merged.record_end - merged.record_start == pytest.approx(3.0)
    assert merged.source_start == pytest.approx(0.0)
    assert merged.source_end == pytest.approx(3.0)
    # the record grid still tiles
    ordered = sorted(plan.entries, key=lambda e: e.record_start)
    for a, b in zip(ordered, ordered[1:]):
        assert a.record_end == pytest.approx(b.record_start)


def test_continuity_merge_respects_the_cut_ceiling():
    # One continuous 20s take sliced onto 1.5s slots: joins are capped at
    # the absolute cut ceiling (8s) — never one giant shot.
    report = ClipReport(
        path="/f/ride.mp4", duration=25.0, moments=[Moment(0.0, 20.0, 0.9)]
    )
    plan = plan_montage([report], None, max_duration=18.0, cut_lead=0.0)
    assert any(n.startswith("continuity:") for n in plan.notes)
    assert len(plan.entries) >= 3
    for e in plan.entries:
        assert e.record_end - e.record_start <= 8.0 + 1e-6
    # sources stay non-overlapping (the zero-repeat promise survives)
    windows = sorted((e.source_start, e.source_end) for e in plan.entries)
    for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
        assert s2 >= e1 - 1e-9


def test_continuity_merge_zero_repeat_promise_survives():
    # Merges + reuse slicing on one clip: no frame is ever on screen twice.
    report = ClipReport(
        path="/f/ride.mp4",
        duration=40.0,
        moments=[Moment(0.0, 8.0, 0.9), Moment(9.0, 17.0, 0.8)],
    )
    plan = plan_montage([report], _cont_music(), cut_lead=0.0)
    windows = sorted((e.source_start, e.source_end) for e in plan.entries)
    for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
        assert s2 >= e1 - 1e-9


def test_continuity_merge_never_crosses_an_act_change():
    # A continuous take through a travel arc: joins stay inside phases, so
    # every act still opens on its own cut.
    report = ClipReport(
        path="/f/ride.mp4", duration=60.0, moments=[Moment(0.0, 44.0, 0.9)]
    )
    plan = plan_montage([report], make_arc_music(), style="travel", cut_lead=0.0)
    assert any(n.startswith("continuity:") for n in plan.notes)
    starts = {round(e.record_start, 6) for e in plan.entries}
    # snapped phase boundaries (8 / 16 / 32) all keep their cut
    assert {8.0, 16.0, 32.0} <= starts


def test_continuity_merge_may_hold_the_climax_ride():
    # UNLIKE the calm merge, the continuity merge works inside the climax:
    # the same ride continuing over the peak is held, not re-cut.
    report = ClipReport(
        path="/f/ride.mp4", duration=60.0, moments=[Moment(0.0, 44.0, 0.9)]
    )
    plan = plan_montage([report], make_arc_music(), style="travel", cut_lead=0.0)
    climax = [
        e for e in plan.entries if 16.0 - 1e-6 <= e.record_start < 32.0 - 1e-6
    ]
    # the climax base is 1 beat (0.5s); a joined shot is strictly longer
    assert any(e.record_end - e.record_start > 0.5 + 1e-6 for e in climax)


def test_continuity_merge_never_absorbs_a_drop_slot():
    music = _cont_music()
    music.drops = [6.0]
    report = ClipReport(
        path="/f/ride.mp4", duration=40.0, moments=[Moment(0.0, 30.0, 0.9)]
    )
    plan = plan_montage([report], music, cut_lead=0.0)
    # the drop-forced cut survives every join
    assert any(abs(e.record_start - 6.0) < 1e-6 for e in plan.entries)


def test_continuity_merge_never_absorbs_the_final_entry():
    report = ClipReport(
        path="/f/ride.mp4", duration=25.0, moments=[Moment(0.0, 20.0, 0.9)]
    )
    plan = plan_montage([report], None, max_duration=18.0, cut_lead=0.0)
    # the closing shot keeps its own cut: the last entry starts where the
    # second-to-last ends, and is not part of one 8s-capped mega-join that
    # would swallow the cast closer
    assert len(plan.entries) >= 2
    last = plan.entries[-1]
    assert last.record_end == pytest.approx(plan.duration)
    assert plan.entries[-2].record_end == pytest.approx(last.record_start)


# --- auto pace (pace=None derives the per-phase base) ---------------------------


def _paced_reports(calm: bool) -> list[ClipReport]:
    """20 one-moment clips; calm=True makes 18 of 20 calm (share 0.9)."""
    motion_calm = (0.2, 0.0)
    motion_fast = (10.0, 0.0)
    reports = []
    for i in range(20):
        motion = motion_fast if (not calm or i >= 18) else motion_calm
        reports.append(
            ClipReport(
                path=f"/f/v{i:02d}.mp4",
                duration=30.0,
                moments=[Moment(4.0, 6.0, 0.8, entry_motion=motion,
                                exit_motion=motion)],
            )
        )
    return reports


def _quiet_arc_music() -> MusicAnalysis:
    m = make_arc_music()
    m.sections = [MusicSection(0.0, 40.0, 0.2, "low")]
    return m


def test_auto_pace_calm_content_slows_the_cut():
    calm = plan_montage(_paced_reports(calm=True), make_arc_music(),
                        style="travel", cut_lead=0.0)
    fast = plan_montage(_paced_reports(calm=False), make_arc_music(),
                        style="travel", cut_lead=0.0)
    assert any(n.startswith("auto pace: calm footage dominates") for n in calm.notes)
    assert not any(n.startswith("auto pace:") for n in fast.notes)
    assert len(calm.entries) < len(fast.entries)


def test_auto_pace_quiet_song_slows_the_cut():
    # No motion data anywhere: the content signal is unknowable; the quiet
    # song alone slows one notch.
    plan = plan_montage(make_varied_reports(), _quiet_arc_music(),
                        style="travel", cut_lead=0.0)
    assert any(n.startswith("auto pace: a quiet song") for n in plan.notes)
    loud = plan_montage(make_varied_reports(), make_arc_music(),
                        style="travel", cut_lead=0.0)
    assert len(plan.entries) < len(loud.entries)


def test_auto_pace_two_signals_slow_two_notches():
    plan = plan_montage(_paced_reports(calm=True), _quiet_arc_music(),
                        style="travel", cut_lead=0.0)
    note = [n for n in plan.notes if n.startswith("auto pace:")]
    assert note and "two notches" in note[0]
    assert "calm footage" in note[0] and "quiet song" in note[0]


def test_auto_pace_explicit_pace_overrides_the_bias():
    plan = plan_montage(_paced_reports(calm=True), _quiet_arc_music(),
                        style="travel", cut_lead=0.0, pace=1.0)
    assert not any(n.startswith("auto pace:") for n in plan.notes)
    assert any(n.startswith("cut pace ~1s") for n in plan.notes)


def test_auto_pace_skips_auto_and_short_styles():
    # "auto" reads the song's density directly; "short" never slows down.
    a = plan_montage(_paced_reports(calm=True), make_music(), cut_lead=0.0)
    assert not any(n.startswith("auto pace:") for n in a.notes)
    s = plan_montage(_paced_reports(calm=True), _quiet_arc_music(),
                     style="short", cut_lead=0.0)
    assert not any(n.startswith("auto pace:") for n in s.notes)


# --- per-cut transitions (the "auto" decision matrix) ---------------------------


def _finishing_case(clips, semantics, phase_label="opening"):
    """Run _plan_finishing over synthetic 2s entries in one travel phase."""
    from monteur.montage import STYLES, _plan_finishing

    entries = []
    for i, clip in enumerate(clips):
        entries.append(
            MontageEntry(
                clip_path=clip,
                source_start=i * 10.0, source_end=i * 10.0 + 2.0,
                record_start=i * 2.0, record_end=i * 2.0 + 2.0,
                score=0.8, clip_duration=60.0,
            )
        )
    length = 2.0 * len(clips)
    phases = [(0.0, length, phase_label)]
    plan = MontagePlan(music_path="/music/song.wav", duration=length)
    music = MusicAnalysis(path="/music/song.wav", duration=length, tempo=120.0)
    _plan_finishing(
        plan, entries, music, STYLES["travel"], phases, "auto",
        entry_semantics=semantics,
    )
    return plan, entries


def test_transition_matrix_same_clip_always_cuts_hard():
    # Same clip continuing in a GENTLE phase: hard cut, never a dissolve.
    plan, entries = _finishing_case(
        ["/f/a.mp4", "/f/a.mp4", "/f/a.mp4"], None, phase_label="opening"
    )
    assert all(e.transition == 0.0 for e in entries)
    assert any(
        n.startswith("transitions:") and "scene continues" in n for n in plan.notes
    )


def test_transition_matrix_scene_change_dissolves_in_calm_music():
    plan, entries = _finishing_case(
        ["/f/a.mp4", "/f/b.mp4", "/f/c.mp4"],
        [("g1", ""), ("g2", ""), ("g2", "")],
        phase_label="opening",
    )
    assert entries[1].transition == pytest.approx(0.5)  # g1 -> g2: dissolve
    assert entries[2].transition == 0.0  # same group: two takes, hard cut
    note = next(n for n in plan.notes if n.startswith("transitions:"))
    assert "1 dissolve at scene changes" in note


def test_transition_matrix_daylight_change_dissolves():
    # A daylight-block handover dissolves even mid-arc (build phase).
    plan, entries = _finishing_case(
        ["/f/a.mp4", "/f/b.mp4"],
        [("", "day"), ("", "golden")],
        phase_label="build",
    )
    assert entries[1].transition == pytest.approx(0.5)
    note = next(n for n in plan.notes if n.startswith("transitions:"))
    assert "1 at daylight changes" in note


def test_transition_matrix_climax_cuts_hard_whatever_the_content():
    plan, entries = _finishing_case(
        ["/f/a.mp4", "/f/b.mp4"],
        [("g1", "day"), ("g2", "night")],
        phase_label="climax",
    )
    assert all(e.transition == 0.0 for e in entries)


def test_transition_matrix_unknown_groups_keep_the_gentle_dissolve():
    plan, entries = _finishing_case(
        ["/f/a.mp4", "/f/b.mp4"], None, phase_label="opening"
    )
    assert entries[1].transition == pytest.approx(0.5)
    # classic behavior, classic note
    assert any("dissolves in gentle phases" in n for n in plan.notes)


def test_transition_overrides_ignore_the_content():
    # "dissolves" dissolves everything (even same-clip continuations);
    # "cuts" cuts everything (even scene changes in calm passages).
    from monteur.montage import STYLES, _plan_finishing

    for mode, expect in (("dissolves", 0.5), ("cuts", 0.0)):
        entries = [
            MontageEntry(
                clip_path="/f/a.mp4",
                source_start=i * 10.0, source_end=i * 10.0 + 2.0,
                record_start=i * 2.0, record_end=i * 2.0 + 2.0,
                score=0.8, clip_duration=60.0,
            )
            for i in range(3)
        ]
        plan = MontagePlan(music_path="/music/song.wav", duration=6.0)
        music = MusicAnalysis(path="/music/song.wav", duration=6.0, tempo=120.0)
        _plan_finishing(
            plan, entries, music, STYLES["travel"], [(0.0, 6.0, "opening")],
            mode, entry_semantics=[("g1", "day"), ("g1", "day"), ("g1", "day")],
        )
        assert entries[1].transition == pytest.approx(expect)
        assert entries[2].transition == pytest.approx(expect)
