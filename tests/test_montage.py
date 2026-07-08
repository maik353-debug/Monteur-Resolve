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
    plan = plan_montage(make_reports(), make_music())
    assert plan.duration == 12.0
    for e in plan.entries:
        if e.record_start < 4.0:  # low: every 4 beats
            assert slot_length(e) == pytest.approx(2.0)
        elif e.record_start < 8.0:  # mid: every 2 beats
            assert slot_length(e) == pytest.approx(1.0)
        else:  # high: every beat
            assert slot_length(e) == pytest.approx(0.5)
    # 2 low + 4 mid + 8 high slots
    assert len(plan.entries) == 14


def test_grid_is_contiguous_and_ends_on_duration():
    plan = plan_montage(make_reports(), make_music())
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(12.0)


def test_no_beats_falls_back_to_fixed_grid():
    music = MusicAnalysis(path="/music/song.wav", duration=12.0, tempo=0.0, beats=[])
    plan = plan_montage(make_reports(), music)
    assert all(slot_length(e) == pytest.approx(2.0) for e in plan.entries)
    assert len(plan.entries) == 6
    assert any("no beats" in note for note in plan.notes)


def test_max_duration_truncates_cleanly():
    plan = plan_montage(make_reports(), make_music(), max_duration=5.0)
    assert plan.duration == 5.0
    assert plan.entries[-1].record_end == pytest.approx(5.0)
    # low slots 0-2, 2-4, then the mid remainder 4-5
    assert [e.record_start for e in plan.entries] == pytest.approx([0.0, 2.0, 4.0])


def test_anti_strobe_doubles_dense_grid():
    music = MusicAnalysis(
        path="/music/fast.wav",
        duration=12.0,
        tempo=300.0,
        beats=[i * 0.2 for i in range(60)],
        sections=[MusicSection(0.0, 12.0, 0.9, "high")],
    )
    plan = plan_montage(make_reports(), music)
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
    plan = plan_montage([report], music, order=CHRONOLOGICAL)
    assert len(plan.entries) == 24  # 0.5s slots over 12s
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
    assert len(plan.entries) == 8
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
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel")
    assert plan.duration == 40.0
    # phases after phrase snapping: opening 0-8, build 8-16, climax 16-32, outro 32-40
    for e in plan.entries:
        if e.record_start < 8.0:  # opening: downbeats, every 4 beats = 2s
            assert slot_length(e) == pytest.approx(2.0)
        elif e.record_start < 16.0:  # build: every 2 beats = 1s
            assert slot_length(e) == pytest.approx(1.0)
        elif e.record_start < 32.0:  # climax: every beat = 0.5s
            assert slot_length(e) == pytest.approx(0.5)
        else:  # outro: every 4 beats = 2s
            assert slot_length(e) == pytest.approx(2.0)
    assert len(plan.entries) == 4 + 8 + 32 + 4
    # grid stays contiguous and closes on the montage length
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(40.0)
    assert any("travel" in note for note in plan.notes)


def test_phase_boundaries_snap_to_phrase_starts():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel")
    by_start = {round(e.record_start, 6): e for e in plan.entries}
    # raw boundaries 6.0 / 20.0 / 34.0 snap to the phrase grid (multiples of 8)
    assert {8.0, 16.0, 32.0} <= set(by_start)
    # 6.0 is still inside the opening (2s slots), so the build really moved to 8.0
    assert slot_length(by_start[6.0]) == pytest.approx(2.0)
    assert slot_length(by_start[8.0]) == pytest.approx(1.0)
    # 32.0 starts the outro (2s slots), so the climax->outro boundary moved off 34.0
    assert slot_length(by_start[32.0]) == pytest.approx(2.0)
    assert 34.0 not in by_start or slot_length(by_start[34.0]) == pytest.approx(2.0)
    assert any("snapped to phrase" in note for note in plan.notes)


def test_drop_aligns_travel_climax():
    plan = plan_montage(make_long_reports(), make_arc_music(drops=[20.0]), style="travel")
    assert any("climax aligned to drop at 20.0s" in note for note in plan.notes)
    # a cut lands exactly on the drop and the climax density starts there
    drop_entries = [e for e in plan.entries if e.record_start == pytest.approx(20.0)]
    assert len(drop_entries) == 1
    assert slot_length(drop_entries[0]) == pytest.approx(0.5)
    # the slot before the drop still belongs to the (slower) build
    before = [e for e in plan.entries if e.record_end == pytest.approx(20.0)]
    assert len(before) == 1
    assert slot_length(before[0]) == pytest.approx(1.0)


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
    plan = plan_montage([report], music, style="auto")
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
    plan = plan_montage([report], make_arc_music(), style="travel", order=CHRONOLOGICAL)
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


# --- timeline rendering (existing behavior) -------------------------------------


def test_montage_to_timeline_exact_frames_at_25fps():
    plan = plan_montage(make_reports(), make_music(), order=CHRONOLOGICAL)
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
