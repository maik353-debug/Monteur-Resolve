"""Tests for trailer distillation (monteur.distill).

Timelines are built in code (see test_model.py / test_io_edl.py for the
Clip construction patterns); the probe path additionally runs against the
real demo footage when it is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from monteur.distill import distill, timeline_to_reports
from monteur.io import load_timeline, read_edl, write_edl
from monteur.media import MonteurMediaError, probe, start_timecode_seconds
from monteur.model import AUDIO, VIDEO, Clip, Timeline
from monteur.montage import montage_to_timeline
from monteur.music import MusicAnalysis, MusicSection

FIXTURES = Path(__file__).parent / "fixtures"
DEMO = Path(
    "/tmp/claude-0/-home-user-Fable-tool/90401078-872b-52b4-9d55-214193ea4ea5"
    "/scratchpad/demo-footage"
)


def vclip(
    source_file: str,
    source_in: int,
    source_out: int,
    record_in: int,
    metadata: dict | None = None,
    source_name: str = "",
) -> Clip:
    """A video clip whose record range mirrors its source length."""
    return Clip(
        name=Path(source_file or source_name).stem,
        kind=VIDEO,
        source_in=source_in,
        source_out=source_out,
        record_in=record_in,
        record_out=record_in + (source_out - source_in),
        source_name=source_name,
        source_file=source_file,
        metadata=metadata or {},
    )


def make_film() -> Timeline:
    """A finished cut: 8 shots of 4s each from 8 distinct files, at 25 fps."""
    clips = [
        vclip(f"/footage/shot_{i}.mp4", 100, 200, i * 100) for i in range(8)
    ]
    return Timeline("My Travel Film", 25.0, clips=clips)


def make_trailer_music(drops: list[float] | None = None) -> MusicAnalysis:
    """40s track: beats every 0.5s, downbeats every 2s, phrases every 4s."""
    return MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 4.0 for i in range(10)],
        drops=drops or [],
    )


# --- timeline_to_reports --------------------------------------------------------


def test_no_video_clips_raises():
    audio_only = Timeline(
        "voiceover",
        25.0,
        clips=[
            Clip(
                name="vo",
                track="A1",
                kind=AUDIO,
                source_in=0,
                source_out=100,
                record_in=0,
                record_out=100,
            )
        ],
    )
    with pytest.raises(ValueError, match="no video clips"):
        timeline_to_reports(audio_only)
    with pytest.raises(ValueError, match="no video clips"):
        distill(audio_only, None, target=10.0)


def test_grouping_merges_overlapping_uses_of_the_same_file():
    # /footage/x.mp4 used 3 times; the first two source ranges overlap and
    # must merge into one moment — a shot used twice is one moment.
    clips = [
        vclip("/footage/x.mp4", 100, 200, 0),  # 4.0-8.0s
        vclip("/footage/x.mp4", 150, 250, 100),  # 6.0-10.0s (overlaps)
        vclip("/footage/x.mp4", 500, 550, 200),  # 20.0-22.0s (separate)
        vclip("/footage/y.mp4", 0, 100, 250),
    ]
    reports = timeline_to_reports(Timeline("cut", 25.0, clips=clips))
    assert [r.path for r in reports] == ["/footage/x.mp4", "/footage/y.mp4"]
    x = reports[0]
    assert len(x.moments) == 2
    spans = sorted((m.start, m.end) for m in x.moments)
    assert spans == [
        (pytest.approx(4.0), pytest.approx(10.0)),
        (pytest.approx(20.0), pytest.approx(22.0)),
    ]
    assert all(r.usable_ratio == 1.0 for r in reports)


def test_screen_time_scoring_order():
    # Longest shot in the cut: 8s -> score 0.95; a 2s shot -> 0.75 + 0.2/4.
    clips = [
        vclip("/footage/long.mp4", 0, 200, 0),  # 8s
        vclip("/footage/short.mp4", 0, 50, 200),  # 2s
        vclip("/footage/mid.mp4", 0, 100, 250),  # 4s
    ]
    reports = timeline_to_reports(Timeline("cut", 25.0, clips=clips))
    by_path = {r.path: r.moments[0].score for r in reports}
    assert by_path["/footage/long.mp4"] == pytest.approx(0.95)
    assert by_path["/footage/short.mp4"] == pytest.approx(0.80)
    assert by_path["/footage/mid.mp4"] == pytest.approx(0.85)
    # moments within a report are ordered best first
    two = timeline_to_reports(
        Timeline(
            "cut",
            25.0,
            clips=[
                vclip("/footage/x.mp4", 0, 50, 0),  # 2s, weaker
                vclip("/footage/x.mp4", 200, 400, 50),  # 8s, stronger
            ],
        )
    )[0]
    assert two.moments[0].start == pytest.approx(8.0)  # the long shot leads
    assert two.moments[0].score > two.moments[1].score


def test_frames_to_seconds_at_25_fps():
    clips = [vclip("/footage/x.mp4", 37, 138, 0)]
    report = timeline_to_reports(Timeline("cut", 25.0, clips=clips))[0]
    moment = report.moments[0]
    assert moment.start == pytest.approx(37 / 25)
    assert moment.end == pytest.approx(138 / 25)
    # honest lower bound: duration = max source end seen
    assert report.duration == pytest.approx(138 / 25)


def test_media_metadata_passthrough():
    clips = [
        vclip(
            "/nonexistent/cam.mp4",
            0,
            100,
            0,
            metadata={"media_start_seconds": 3600.0, "media_duration_seconds": 12.0},
        )
    ]
    report = timeline_to_reports(Timeline("cut", 25.0, clips=clips))[0]
    assert report.media_start == pytest.approx(3600.0)
    # metadata's media duration beats the 4s max-source-end lower bound
    assert report.duration == pytest.approx(12.0)
    assert report.usable_ratio == 1.0
    assert any("not found on disk" in note for note in report.notes)


def test_monteur_labels_survive_the_merge():
    clips = [
        vclip("/footage/x.mp4", 100, 200, 0, metadata={"label": "sunset ridge"}),
        vclip("/footage/x.mp4", 150, 250, 100),  # merges into the labeled shot
        vclip("/footage/x.mp4", 500, 550, 200),  # unlabeled, separate
    ]
    report = timeline_to_reports(Timeline("cut", 25.0, clips=clips))[0]
    by_start = {round(m.start, 6): m for m in report.moments}
    merged = by_start[4.0]
    assert merged.end == pytest.approx(10.0)
    assert merged.label == "sunset ridge"
    assert by_start[20.0].label == ""


def test_bare_edl_reels_fall_back_to_source_name():
    clips = [
        vclip("", 0, 100, 0, source_name="TAPE001"),
        vclip("", 200, 300, 100, source_name="TAPE001"),
        vclip("", 0, 50, 200, source_name="TAPE002"),
    ]
    reports = timeline_to_reports(Timeline("cut", 25.0, clips=clips))
    assert [r.path for r in reports] == ["TAPE001", "TAPE002"]
    assert len(reports[0].moments) == 2
    assert any("relink" in note for note in reports[0].notes)


@pytest.mark.skipif(
    not (DEMO / "clip_A.mp4").exists(), reason="demo footage not present"
)
def test_probe_path_with_real_demo_clip():
    path = str(DEMO / "clip_A.mp4")
    try:
        info = probe(path)
    except MonteurMediaError:
        pytest.skip("ffmpeg unavailable")
    clips = [vclip(path, 0, 50, 0)]  # only 2s of an 8s file used
    report = timeline_to_reports(Timeline("cut", 25.0, clips=clips))[0]
    assert report.duration == pytest.approx(info.duration, abs=0.05)
    assert report.duration > 2.0  # the REAL duration, not the cut's lower bound
    assert report.media_start == pytest.approx(start_timecode_seconds(info))
    assert not report.notes  # the file is on disk and probed cleanly
    # probe_media=False stays off disk: honest lower bound from the cut
    offline = timeline_to_reports(
        Timeline("cut", 25.0, clips=clips), probe_media=False
    )[0]
    assert offline.duration == pytest.approx(2.0)


# --- distill --------------------------------------------------------------------


def test_distill_trailer_with_music():
    plan = distill(make_film(), make_trailer_music(drops=[16.0]), target=24.0)
    assert plan.duration <= 24.0 + 1e-6
    assert plan.entries
    assert plan.entries[-1].record_end == pytest.approx(plan.duration)
    assert plan.dips, "the trailer style smashes to black at act changes"
    assert plan.notes[0] == (
        "distilled from 'My Travel Film': 8 shots, 8 unique sources"
    )
    # every entry slices real cut material (moments are 4.0-8.0s per file)
    for entry in plan.entries:
        assert entry.source_start >= 4.0 - 1e-6
        assert entry.source_end <= 8.0 + 1e-6
    # the fake footage paths are not on disk -> relink reminder
    assert any("relink" in note for note in plan.notes)


def test_distill_no_music_path():
    plan = distill(make_film(), None, target=12.0)
    assert plan.music_path == ""
    assert plan.duration <= 12.0 + 1e-6
    assert plan.entries
    assert plan.notes[0].startswith("distilled from 'My Travel Film'")


def test_distill_repetition_guard_stays_active():
    # One 4s shot cannot honestly fill a 60s trailer: the guard caps the cut.
    short = Timeline("tiny", 25.0, clips=[vclip("/footage/only.mp4", 0, 100, 0)])
    plan = distill(short, None, target=60.0)
    assert plan.duration <= 4.0 * 1.5 + 1e-6
    assert any("capped the cut" in note for note in plan.notes)


def test_distill_forwards_plan_kwargs():
    plan = distill(
        make_film(),
        make_trailer_music(),
        target=24.0,
        transitions="cuts",
        order="best_first",
    )
    assert plan.dips == []  # "cuts" suppresses the trailer's smash-to-black
    assert any("hard cuts only" in note for note in plan.notes)
    with pytest.raises(ValueError, match="unknown order"):
        distill(make_film(), make_trailer_music(), target=24.0, order="sideways")


def test_end_to_end_sample_edl_to_trailer_edl():
    timeline = load_timeline(FIXTURES / "sample.edl", fps=25)
    plan = distill(timeline, None, target=8.0)
    assert plan.duration <= 8.0 + 1e-6
    assert plan.entries
    assert plan.notes[0].startswith("distilled from 'sample'")
    assert any("relink" in note for note in plan.notes)  # EDL reels, no files
    trailer = montage_to_timeline(plan, fps=25, audio="original")
    text = write_edl(trailer)
    assert text.startswith("TITLE:")
    round_trip = read_edl(text, 25)
    assert round_trip.video_clips()
    assert round_trip.duration <= 8 * 25 + 1  # frames
    # the reel names carried through so Resolve can (manually) relink
    reels = {c.source_name or c.name for c in round_trip.video_clips()}
    assert reels & {"TAPE001", "TAPE002", "TAPE004", "TAPE005"}
