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
    plan = plan_montage(make_reports(), make_music(), cut_lead=0.0)
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
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel", cut_lead=0.0)
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
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel", cut_lead=0.0)
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
    plan = plan_montage(
        make_long_reports(), make_arc_music(drops=[20.0]), style="travel", cut_lead=0.0
    )
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
    # phases: opening 0-8 (2s slots), build 8-16, climax 16-32, outro 32-40 (2s)
    assert plan.entries[0].transition == 0.0  # first entry: its fade is fade_in
    for e in plan.entries[1:]:
        if e.record_start < 8.0 or e.record_start >= 32.0:  # opening / outro
            assert e.transition == pytest.approx(0.5)  # min(0.5, half of 2.0s)
        else:  # build / climax cut hard
            assert e.transition == 0.0
    dissolves = sum(1 for e in plan.entries if e.transition > 0)
    assert dissolves == 7  # 3 opening (minus the first) + 4 outro
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
    # cut_lead=0 reproduces the exact historical grid (2s/1s/0.5s slots).
    plan = plan_montage(make_reports(), make_music(), cut_lead=0.0)
    starts = [e.record_start for e in plan.entries]
    assert starts[:4] == pytest.approx([0.0, 2.0, 4.0, 5.0])
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
    # the fastest (high-energy) section cuts at ~the requested pace
    high = [e for e in snappy.entries if e.record_start >= 8.0]
    assert high and all(slot_length(e) == pytest.approx(1.0) for e in high)
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
