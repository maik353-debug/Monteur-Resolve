"""Tests for monteur.io.fcpxml (FCPXML 1.x read/write)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monteur.io import load_timeline, read_fcpxml, write_fcpxml
from monteur.model import AUDIO, VIDEO, Clip, Timeline

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def sample() -> Timeline:
    text = (FIXTURES / "sample.fcpxml").read_text(encoding="utf-8")
    return read_fcpxml(text)


def test_read_sample_counts(sample: Timeline) -> None:
    assert sample.fps == 25.0
    assert sample.name == "Monteur Sample Project"
    assert len(sample.clips) == 5
    assert len(sample.video_clips()) == 4
    assert len(sample.audio_clips()) == 1


def test_read_sample_exact_frames(sample: Timeline) -> None:
    anna = next(c for c in sample.clips if c.name == "Interview Anna" and c.kind == VIDEO)
    assert anna.track == "V1"
    assert (anna.record_in, anna.record_out) == (0, 100)
    assert (anna.source_in, anna.source_out) == (90000, 90100)
    assert anna.source_name == "Interview Anna"
    assert anna.metadata["src"] == "file:///media/interview_anna.mov"

    broll = next(c for c in sample.clips if c.name == "B-Roll Street")
    assert (broll.record_in, broll.record_out) == (100, 175)
    assert (broll.source_in, broll.source_out) == (180000, 180075)


def test_read_sample_audio_from_asset(sample: Timeline) -> None:
    anna_audio = next(
        c for c in sample.clips if c.name == "Interview Anna" and c.kind == AUDIO
    )
    assert anna_audio.track == "A1"
    assert (anna_audio.record_in, anna_audio.record_out) == (0, 100)


def test_read_sample_connected_clip_in_gap(sample: Timeline) -> None:
    drone = next(c for c in sample.clips if c.name == "Sunset Drone")
    assert drone.kind == VIDEO
    assert drone.track == "V2"
    assert (drone.record_in, drone.record_out) == (200, 225)
    assert (drone.source_in, drone.source_out) == (1500, 1525)


def test_read_sample_clip_element(sample: Timeline) -> None:
    vox = next(c for c in sample.clips if c.name == "Vox Pop")
    assert vox.kind == VIDEO
    assert vox.track == "V1"
    assert (vox.record_in, vox.record_out) == (225, 300)
    assert (vox.source_in, vox.source_out) == (0, 75)


def test_write_read_roundtrip() -> None:
    timeline = Timeline(name="Round Trip", fps=25.0)
    timeline.clips = [
        Clip("Alpha", "V1", VIDEO, 250, 350, 0, 100, source_name="AlphaSrc"),
        Clip("Alpha", "A1", AUDIO, 250, 350, 0, 100, source_name="AlphaSrc"),
        Clip("Beta", "V1", VIDEO, 0, 100, 150, 250, source_name="BetaSrc"),
    ]
    text = write_fcpxml(timeline)
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<!DOCTYPE fcpxml>" in text
    assert 'version="1.9"' in text

    back = read_fcpxml(text)
    assert back.fps == 25.0
    assert back.name == "Round Trip"
    original = {
        (c.name, c.kind, c.track, c.source_in, c.source_out, c.record_in, c.record_out)
        for c in timeline.clips
    }
    reread = {
        (c.name, c.kind, c.track, c.source_in, c.source_out, c.record_in, c.record_out)
        for c in back.clips
    }
    assert reread == original


def test_write_inserts_gap_for_hole() -> None:
    timeline = Timeline(name="Gappy", fps=25.0)
    timeline.clips = [Clip("Late", "V1", VIDEO, 0, 50, 100, 150, source_name="S")]
    text = write_fcpxml(timeline)
    assert "<gap" in text
    back = read_fcpxml(text)
    clip = back.clips[0]
    assert (clip.record_in, clip.record_out) == (100, 150)


def test_write_ntsc_frame_duration() -> None:
    timeline = Timeline(name="NTSC", fps=23.976)
    timeline.clips = [Clip("a", "V1", VIDEO, 0, 24, 0, 24, source_name="S")]
    text = write_fcpxml(timeline)
    assert 'frameDuration="1001/24000s"' in text
    back = read_fcpxml(text)
    assert abs(back.fps - 23.976) < 0.001
    assert back.clips[0].record_out == 24


def test_write_overlapping_video_raises() -> None:
    timeline = Timeline(name="Bad", fps=25.0)
    timeline.clips = [
        Clip("a", "V1", VIDEO, 0, 50, 0, 50),
        Clip("b", "V1", VIDEO, 0, 50, 25, 75),
    ]
    with pytest.raises(ValueError, match="overlap"):
        write_fcpxml(timeline)


def test_read_not_xml_raises() -> None:
    with pytest.raises(ValueError, match="not well-formed"):
        read_fcpxml("this is not xml at all")


def test_read_wrong_root_raises() -> None:
    with pytest.raises(ValueError, match="fcpxml"):
        read_fcpxml("<xmeml version='4'/>")


def test_read_missing_sequence_raises() -> None:
    with pytest.raises(ValueError, match="sequence"):
        read_fcpxml('<fcpxml version="1.9"><resources/></fcpxml>')


def test_read_bad_rational_time_raises() -> None:
    doc = (
        '<fcpxml version="1.9"><resources>'
        '<format id="r1" frameDuration="1/25s"/></resources>'
        '<library><event><project name="p"><sequence format="r1"><spine>'
        '<asset-clip name="c" offset="banana" duration="1s"/>'
        "</spine></sequence></project></event></library></fcpxml>"
    )
    with pytest.raises(ValueError, match="offset"):
        read_fcpxml(doc)


def test_load_timeline_fcpxml_dispatch() -> None:
    timeline = load_timeline(FIXTURES / "sample.fcpxml")
    assert len(timeline.clips) == 5
    assert timeline.fps == 25.0


def test_save_timeline_fcpxml(tmp_path: Path) -> None:
    from monteur.io import save_timeline

    timeline = Timeline(name="Saved", fps=25.0)
    timeline.clips = [Clip("x", "V1", VIDEO, 0, 50, 0, 50, source_name="Src")]
    out = tmp_path / "out.fcpxml"
    save_timeline(timeline, out)
    back = load_timeline(out)
    assert len(back.clips) == 1
    assert back.clips[0].record_out == 50


def test_load_timeline_unsupported_extension(tmp_path: Path) -> None:
    p = tmp_path / "cut.avb"
    p.write_text("binary-ish")
    with pytest.raises(ValueError, match="unsupported"):
        load_timeline(p)
