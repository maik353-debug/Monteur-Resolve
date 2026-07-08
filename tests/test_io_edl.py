"""Tests for monteur.io.edl (CMX3600 EDL read/write)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monteur.io import load_timeline, read_edl, write_edl
from monteur.model import AUDIO, VIDEO, Clip, Timeline

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def sample() -> Timeline:
    text = (FIXTURES / "sample.edl").read_text(encoding="utf-8")
    return read_edl(text, fps=25.0)


def test_read_sample_counts(sample: Timeline) -> None:
    assert len(sample.clips) == 10
    assert len(sample.video_clips()) == 5
    assert len(sample.audio_clips()) == 5
    assert sample.name == "Monteur Sample Cut"
    assert sample.metadata["fcm"] == "NON-DROP FRAME"
    assert sample.fps == 25.0


def test_read_sample_video_order_and_names(sample: Timeline) -> None:
    names = [c.name for c in sample.video_clips()]
    assert names == [
        "Interview Anna Wide",
        "B-Roll Street",
        "Sunset Drone",
        "Vox Pop",
        "Interview Anna CU",
    ]


def test_read_sample_exact_frames(sample: Timeline) -> None:
    first = sample.video_clips()[0]
    assert first.source_in == 90250
    assert first.source_out == 90375
    assert first.record_in == 0
    assert first.record_out == 125
    assert first.source_name == "TAPE001"

    broll = sample.video_clips()[1]
    assert broll.source_in == 180000
    assert broll.source_out == 180075
    assert broll.record_in == 125
    assert broll.record_out == 200


def test_read_sample_dissolve_imported_as_cut(sample: Timeline) -> None:
    drone = next(c for c in sample.clips if c.name == "Sunset Drone")
    assert drone.kind == VIDEO
    assert drone.record_in == 200
    assert drone.record_out == 325
    assert drone.source_in == 360000
    assert drone.source_out == 360125
    assert drone.metadata["transition"] == "D"
    assert drone.metadata["transition_duration"] == 25
    zero_length = [c for c in sample.clips if c.record_in == c.record_out]
    assert zero_length == []


def test_read_sample_channel_mapping(sample: Timeline) -> None:
    music = [c for c in sample.clips if c.name == "Music Bed"]
    assert sorted(c.track for c in music) == ["A1", "A2"]
    assert all(c.kind == AUDIO for c in music)
    assert all(c.record_in == 125 and c.record_out == 225 for c in music)

    vox = [c for c in sample.clips if c.name == "Vox Pop"]
    assert sorted((c.kind, c.track) for c in vox) == [(AUDIO, "A1"), (VIDEO, "V1")]

    ambience = next(c for c in sample.clips if c.name == "Ambience")
    assert (ambience.kind, ambience.track) == (AUDIO, "A2")
    assert (ambience.record_in, ambience.record_out) == (325, 475)


def test_write_read_roundtrip() -> None:
    timeline = Timeline(name="Roundtrip", fps=24.0)
    timeline.clips = [
        Clip("Scene 1", "V1", VIDEO, 100, 200, 0, 100, source_name="A001C003"),
        Clip("Scene 1", "A1", AUDIO, 100, 200, 0, 100, source_name="A001C003"),
        Clip("Scene 2", "V1", VIDEO, 48, 96, 100, 148, source_name="A002C001"),
        Clip("Room Tone", "A2", AUDIO, 0, 148, 0, 148, source_name="AMBIENT ROOM"),
    ]
    text = write_edl(timeline, title="Roundtrip")
    assert text.startswith("TITLE: Roundtrip")
    assert "FCM: NON-DROP FRAME" in text
    assert "A001C003" in text
    assert "* FROM CLIP NAME: Scene 1" in text

    back = read_edl(text, fps=24.0)
    original = {
        (c.name, c.kind, c.track, c.source_in, c.source_out, c.record_in, c.record_out)
        for c in timeline.clips
    }
    reread = {
        (c.name, c.kind, c.track, c.source_in, c.source_out, c.record_in, c.record_out)
        for c in back.clips
    }
    assert reread == original


def test_write_dissolve_pair_roundtrips() -> None:
    timeline = Timeline(name="Dissolve", fps=25.0)
    timeline.clips = [
        Clip("Opening Wide", "V1", VIDEO, 0, 100, 0, 100, source_name="TAPE1"),
        Clip(
            "Sunset",
            "V1",
            VIDEO,
            250,
            350,
            100,
            200,
            source_name="TAPE2",
            metadata={"transition": "dissolve", "transition_frames": 12},
        ),
    ]
    text = write_edl(timeline, title="Dissolve")
    # CMX dissolve pair: zero-duration outgoing tail, then D with frames
    assert "D    012" in text
    assert "* FROM CLIP NAME: Opening Wide" in text
    assert "* TO CLIP NAME: Sunset" in text
    pair = [l for l in text.splitlines() if l.startswith("002")]
    assert len(pair) == 2  # both source lines share the event number
    assert " C " in f" {pair[0]} " and "00:00:04:00 00:00:04:00" in pair[0]

    back = read_edl(text, fps=25.0)
    assert len(back.clips) == 2  # the zero-duration tail yields no clip
    sunset = next(c for c in back.clips if c.name == "Sunset")
    assert sunset.metadata["transition"] == "D"
    assert sunset.metadata["transition_duration"] == 12
    assert (sunset.record_in, sunset.record_out) == (100, 200)
    assert (sunset.source_in, sunset.source_out) == (250, 350)
    wide = next(c for c in back.clips if c.name == "Opening Wide")
    assert (wide.record_in, wide.record_out) == (0, 100)
    assert "transition" not in wide.metadata


def test_write_dissolve_without_predecessor_falls_back_to_cut() -> None:
    timeline = Timeline(name="Solo", fps=25.0)
    timeline.clips = [
        Clip(
            "Only",
            "V1",
            VIDEO,
            0,
            50,
            100,
            150,
            metadata={"transition": "dissolve", "transition_frames": 10},
        ),
    ]
    text = write_edl(timeline)
    events = [l for l in text.splitlines() if l.startswith("001")]
    assert len(events) == 1
    assert " C " in f" {events[0]} "
    assert "TO CLIP NAME" not in text


def test_write_reel_sanitized_and_fallback() -> None:
    timeline = Timeline(name="Reels", fps=25.0)
    timeline.clips = [
        Clip("long", "V1", VIDEO, 0, 25, 0, 25, source_name="my source #42 (v2)"),
        Clip("", "V1", VIDEO, 0, 25, 25, 50, source_name="???"),
    ]
    text = write_edl(timeline)
    assert "MY_SOURC" in text
    assert " AX " in text


def test_drop_frame_fcm_written() -> None:
    timeline = Timeline(name="DF", fps=29.97)
    timeline.clips = [Clip("a", "V1", VIDEO, 0, 30, 0, 30)]
    assert "FCM: DROP FRAME" in write_edl(timeline)


def test_malformed_event_line_raises() -> None:
    bad = "001  TAPE1  V  C  01:00:00:00 01:00:01:00 00:00:00:00"
    with pytest.raises(ValueError, match="malformed EDL event"):
        read_edl(bad, fps=25.0)


def test_bad_timecode_raises_with_line_number() -> None:
    bad = "001  TAPE1  V  C  01:00:99:00 01:00:01:00 00:00:00:00 00:00:01:00"
    with pytest.raises(ValueError, match="line 1"):
        read_edl(bad, fps=25.0)


def test_unknown_channel_raises() -> None:
    bad = "001  TAPE1  Q  C  01:00:00:00 01:00:01:00 00:00:00:00 00:00:01:00"
    with pytest.raises(ValueError, match="channel"):
        read_edl(bad, fps=25.0)


def test_load_timeline_requires_fps_for_edl() -> None:
    with pytest.raises(ValueError, match="fps"):
        load_timeline(FIXTURES / "sample.edl")


def test_load_timeline_edl_dispatch() -> None:
    timeline = load_timeline(FIXTURES / "sample.edl", fps=25.0)
    assert len(timeline.clips) == 10
    assert timeline.name == "sample"


def test_save_timeline_edl(tmp_path: Path) -> None:
    from monteur.io import save_timeline

    timeline = Timeline(name="Saved", fps=25.0)
    timeline.clips = [Clip("x", "V1", VIDEO, 0, 50, 0, 50, source_name="TAPE1")]
    out = tmp_path / "out.edl"
    save_timeline(timeline, out)
    back = load_timeline(out, fps=25.0)
    assert len(back.clips) == 1
    assert back.clips[0].record_out == 50


# --- embedded start timecode (source TC offset) -----------------------------------


def test_write_edl_shifts_source_tc_by_media_start() -> None:
    """EDL conform in Resolve matches by ABSOLUTE source timecode, so a clip
    carrying media_start_seconds (the file's embedded start TC) must have its
    source in/out shifted by that offset; record TCs stay untouched."""
    timeline = Timeline(name="TC", fps=25.0)
    timeline.clips = [
        Clip(
            "A",
            "V1",
            VIDEO,
            50,
            100,
            0,
            50,
            source_name="TAPE1",
            metadata={"media_start_seconds": 6472.32},  # 01:47:52:08 @ 25fps
        ),
    ]
    text = write_edl(timeline)
    # source 50..100 frames + 6472.32s = 01:47:54:08..01:47:56:08; record unchanged
    assert "01:47:54:08 01:47:56:08 00:00:00:00 00:00:02:00" in text


def test_write_edl_without_media_start_unchanged() -> None:
    timeline = Timeline(name="Plain", fps=25.0)
    timeline.clips = [Clip("A", "V1", VIDEO, 50, 100, 0, 50, source_name="TAPE1")]
    text = write_edl(timeline)
    assert "00:00:02:00 00:00:04:00 00:00:00:00 00:00:02:00" in text


def test_write_edl_dissolve_pair_shifts_both_sides() -> None:
    timeline = Timeline(name="Dissolve", fps=25.0)
    timeline.clips = [
        Clip(
            "Out",
            "V1",
            VIDEO,
            0,
            100,
            0,
            100,
            source_name="TAPE1",
            metadata={"media_start_seconds": 3600.0},  # 01:00:00:00
        ),
        Clip(
            "In",
            "V1",
            VIDEO,
            250,
            350,
            100,
            200,
            source_name="TAPE2",
            metadata={
                "transition": "dissolve",
                "transition_frames": 12,
                "media_start_seconds": 7200.0,  # 02:00:00:00
            },
        ),
    ]
    text = write_edl(timeline)
    # outgoing zero-duration tail: TAPE1's source_out (100f) + 01:00:00:00
    assert "01:00:04:00 01:00:04:00 00:00:04:00 00:00:04:00" in text
    # incoming clip: TAPE2's source 250..350 frames + 02:00:00:00
    assert "02:00:10:00 02:00:14:00 00:00:04:00 00:00:08:00" in text
