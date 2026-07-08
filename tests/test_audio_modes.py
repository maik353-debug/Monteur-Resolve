"""Tests for montage audio modes and no-music plans (monteur.montage).

Covers montage_to_timeline(audio=...) — "music" / "mix" / "original" — and
plan_montage(music=None, ...), the ride-POV mode where the clips' own sound
(engine audio recorded straight into the clips) is the soundtrack.
"""

from __future__ import annotations

import pytest

from monteur.model import AUDIO
from monteur.montage import montage_to_timeline, plan_montage
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


def make_pov_reports() -> list[ClipReport]:
    """Plenty of material so the repetition guard never caps these plans."""
    return [
        ClipReport(
            path="/footage/ride.mp4",
            duration=120.0,
            moments=[Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(20)],
        )
    ]


def slot_length(entry) -> float:
    return entry.record_end - entry.record_start


# --- audio="mix" ----------------------------------------------------------------


def test_mix_adds_one_a2_clip_per_video_entry():
    plan = plan_montage(make_reports(), make_music())
    timeline = montage_to_timeline(plan, fps=25.0, audio="mix")

    video = timeline.video_clips()
    own = timeline.track_clips("A2")
    assert len(own) == len(video) == len(plan.entries)
    for v, a in zip(video, own):
        assert a.kind == AUDIO
        assert a.track == "A2"
        # identical source range, record range and identity as the video entry
        assert (a.source_in, a.source_out) == (v.source_in, v.source_out)
        assert (a.record_in, a.record_out) == (v.record_in, v.record_out)
        assert a.source_name == v.source_name
        assert a.source_file == v.source_file

    # the song still plays on A1
    song = timeline.track_clips("A1")
    assert len(song) == 1
    assert song[0].source_file == plan.music_path
    assert song[0].kind == AUDIO


# --- audio="original" -------------------------------------------------------------


def test_original_drops_song_and_puts_own_audio_on_a1():
    plan = plan_montage(make_reports(), make_music())
    timeline = montage_to_timeline(plan, fps=25.0, audio="original")

    # no song clip anywhere
    assert all(c.source_file != plan.music_path for c in timeline.clips)
    assert timeline.track_clips("A2") == []

    video = timeline.video_clips()
    own = timeline.track_clips("A1")
    assert len(own) == len(video) == len(plan.entries)
    for v, a in zip(video, own):
        assert a.kind == AUDIO
        assert (a.source_in, a.source_out) == (v.source_in, v.source_out)
        assert (a.record_in, a.record_out) == (v.record_in, v.record_out)
        assert a.source_name == v.source_name
        assert a.source_file == v.source_file


def test_default_audio_mode_is_music_only():
    plan = plan_montage(make_reports(), make_music())
    timeline = montage_to_timeline(plan, fps=25.0)
    audio = timeline.audio_clips()
    assert len(audio) == 1  # the song, nothing else
    assert audio[0].track == "A1"
    assert audio[0].source_file == plan.music_path


def test_invalid_audio_mode_raises_listing_the_three():
    plan = plan_montage(make_reports(), make_music())
    with pytest.raises(ValueError) as excinfo:
        montage_to_timeline(plan, fps=25.0, audio="karaoke")
    message = str(excinfo.value)
    for mode in ("music", "mix", "original"):
        assert mode in message


# --- no-music plans ----------------------------------------------------------------


def test_no_music_plan_travel_uses_pseudo_beat_grid():
    plan = plan_montage(
        make_pov_reports(), music=None, max_duration=30.0, style="travel", cut_lead=0.0
    )
    assert plan.music_path == ""
    assert plan.music_start == 0.0
    assert plan.song_duration == 0.0
    assert plan.entries
    assert any("no music" in n for n in plan.notes)

    # travel arc over 30s: opening 0-4.5 (3s cuts), build 4.5-15 (1.5s),
    # climax 15-25.5 (0.75s), outro 25.5-30 (3s) — beats_per_cut x 0.75s.
    starts = {round(e.record_start, 6) for e in plan.entries}
    assert {0.0, 3.0, 4.5, 15.0, 25.5} <= starts
    build = [e for e in plan.entries if 4.5 <= e.record_start < 15.0 - 1e-9]
    assert build and all(slot_length(e) == pytest.approx(1.5) for e in build)
    climax = [e for e in plan.entries if 15.0 <= e.record_start < 25.5 - 1e-9]
    assert climax and all(slot_length(e) == pytest.approx(0.75) for e in climax)
    # grid is contiguous and closes exactly on the requested duration
    assert plan.entries[0].record_start == 0.0
    for prev, nxt in zip(plan.entries, plan.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert plan.entries[-1].record_end == pytest.approx(30.0)


def test_no_music_plan_auto_style_does_not_crash():
    plan = plan_montage(make_pov_reports(), music=None, max_duration=12.0, cut_lead=0.0)
    assert plan.entries
    assert plan.music_path == ""
    # flat "mid" interval: 2 beats x 0.75s pseudo-beat = 1.5s slots
    assert all(slot_length(e) == pytest.approx(1.5) for e in plan.entries)


def test_no_music_without_max_duration_raises():
    with pytest.raises(ValueError, match="without music, pass max_duration"):
        plan_montage(make_pov_reports(), music=None)


def test_no_music_plan_rejects_music_and_mix_rendering():
    plan = plan_montage(make_pov_reports(), music=None, max_duration=30.0)
    with pytest.raises(ValueError):
        montage_to_timeline(plan, fps=25.0, audio="music")
    with pytest.raises(ValueError):
        montage_to_timeline(plan, fps=25.0, audio="mix")
    timeline = montage_to_timeline(plan, fps=25.0, audio="original")
    assert len(timeline.track_clips("A1")) == len(plan.entries)
    assert len(timeline.video_clips()) == len(plan.entries)


# --- CLI validation ----------------------------------------------------------------


def test_cli_create_without_music_requires_audio_original(capsys):
    from monteur.cli import build_parser

    args = build_parser().parse_args(["create", "clips", "-o", "out.fcpxml"])
    assert args.music is None
    with pytest.raises(SystemExit):
        args.func(args)  # fails validation before touching the disk
    err = capsys.readouterr().err
    assert "--audio original" in err


def test_cli_create_without_music_requires_max_duration(capsys):
    from monteur.cli import build_parser

    args = build_parser().parse_args(
        ["create", "clips", "-o", "out.fcpxml", "--audio", "original"]
    )
    with pytest.raises(SystemExit):
        args.func(args)
    err = capsys.readouterr().err
    assert "--max-duration" in err
