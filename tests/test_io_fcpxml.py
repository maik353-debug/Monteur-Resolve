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
    # The asset-clip start (3600s) is expressed in the asset's timescale,
    # which begins at the asset's start (3600s = the file's embedded start
    # timecode): source ranges come back FILE-RELATIVE, with the embedded TC
    # preserved in metadata.
    anna = next(c for c in sample.clips if c.name == "Interview Anna" and c.kind == VIDEO)
    assert anna.track == "V1"
    assert (anna.record_in, anna.record_out) == (0, 100)
    assert (anna.source_in, anna.source_out) == (0, 100)
    assert anna.metadata["media_start_seconds"] == 3600.0
    assert anna.source_name == "Interview Anna"
    assert anna.metadata["src"] == "file:///media/interview_anna.mov"

    broll = next(c for c in sample.clips if c.name == "B-Roll Street")
    assert (broll.record_in, broll.record_out) == (100, 175)
    assert (broll.source_in, broll.source_out) == (0, 75)
    assert broll.metadata["media_start_seconds"] == 7200.0


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
    # asset start 60s subtracted: file-relative source range, TC in metadata
    assert (drone.source_in, drone.source_out) == (0, 25)
    assert drone.metadata["media_start_seconds"] == 60.0


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


def test_write_transition_element_present_and_reread() -> None:
    timeline = Timeline(name="Dissolve", fps=25.0)
    timeline.clips = [
        Clip("Alpha", "V1", VIDEO, 0, 100, 0, 100, source_name="AlphaSrc"),
        Clip(
            "Beta",
            "V1",
            VIDEO,
            0,
            100,
            100,
            200,
            source_name="BetaSrc",
            metadata={"transition": "dissolve", "transition_frames": 12},
        ),
    ]
    timeline.metadata["fade_out_frames"] = 50  # noted only; no audio fade params
    text = write_fcpxml(timeline)
    assert "<transition" in text
    assert 'name="Cross Dissolve"' in text
    assert 'offset="4s"' in text  # dissolve starts at the cut (frame 100 @ 25fps)
    assert 'duration="12/25s"' in text
    # the transition sits in the spine before the incoming asset-clip
    assert text.index("<transition") < text.index('name="Beta"')

    # our reader ignores the transition element; clips come back intact
    back = read_fcpxml(text)
    assert len(back.video_clips()) == 2
    ranges = sorted((c.record_in, c.record_out) for c in back.video_clips())
    assert ranges == [(0, 100), (100, 200)]


def test_write_first_clip_transition_metadata_ignored() -> None:
    # a dissolve INTO the very first clip has nothing to dissolve from
    timeline = Timeline(name="First", fps=25.0)
    timeline.clips = [
        Clip(
            "Only",
            "V1",
            VIDEO,
            0,
            50,
            0,
            50,
            source_name="S",
            metadata={"transition": "dissolve", "transition_frames": 10},
        ),
    ]
    text = write_fcpxml(timeline)
    assert "<transition" not in text
    back = read_fcpxml(text)
    assert len(back.video_clips()) == 1


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


def test_write_fcpxml_emits_media_paths_and_music():
    """Regression: montage FCPXML must reference real media (else Resolve
    imports an EMPTY timeline) and must carry the music bed."""
    from monteur.model import Clip, Timeline
    from monteur.io.fcpxml import write_fcpxml, read_fcpxml

    tl = Timeline(name="Montage", fps=25)
    tl.clips.append(Clip(
        name="A", track="V1", kind="video",
        source_in=50, source_out=125, record_in=0, record_out=75,
        source_name="A", source_file=r"C:\footage\A.MP4",
    ))
    tl.clips.append(Clip(
        name="B", track="V1", kind="video",
        source_in=25, source_out=100, record_in=75, record_out=150,
        source_name="B", source_file=r"C:\footage\B.MP4",
    ))
    tl.clips.append(Clip(
        name="song", track="A1", kind="audio",
        source_in=1500, source_out=1650, record_in=0, record_out=150,
        source_name="song", source_file=r"C:\music\song.mp3",
    ))
    xml = write_fcpxml(tl)
    # Every source file is referenced via a linkable file:// media-rep.
    assert xml.count("<media-rep") == 3
    assert "file:///C:/footage/A.MP4" in xml
    assert "file:///C:/music/song.mp3" in xml
    assert 'audioRole="music"' in xml
    # And it all reads back at the right positions.
    back = read_fcpxml(xml)
    assert len(back.video_clips()) == 2
    assert len(back.audio_clips()) == 1
    music = back.audio_clips()[0]
    assert (music.record_in, music.record_out, music.source_in) == (0, 150, 1500)


def test_write_fcpxml_without_paths_still_valid():
    """A hand-built timeline with no source_file still exports (no media-rep,
    but valid) — keeps older callers and tests working."""
    from monteur.model import Clip, Timeline
    from monteur.io.fcpxml import write_fcpxml, read_fcpxml

    tl = Timeline(name="T", fps=25)
    tl.clips.append(Clip(name="A", kind="video", source_in=0, source_out=50,
                         record_in=0, record_out=50, source_name="A"))
    xml = write_fcpxml(tl)
    assert "<media-rep" not in xml
    assert len(read_fcpxml(xml).video_clips()) == 1


# --- real source ranges (embedded start timecode) --------------------------------


def _tc_timeline() -> Timeline:
    """Two clips from one TC-stamped source + a music bed + one plain source."""
    meta = {"media_start_seconds": 6472.32, "media_duration_seconds": 14.0}
    tl = Timeline(name="TC", fps=25.0)
    tl.clips = [
        Clip("A", "V1", VIDEO, 50, 100, 0, 50, source_name="A",
             source_file="/footage/a.mp4", metadata=dict(meta)),
        Clip("A", "V1", VIDEO, 150, 200, 50, 100, source_name="A",
             source_file="/footage/a.mp4", metadata=dict(meta)),
        Clip("B", "V1", VIDEO, 25, 75, 100, 150, source_name="B",
             source_file="/footage/b.mp4"),  # no media metadata
        Clip("song", "A1", AUDIO, 0, 150, 0, 150, source_name="song",
             source_file="/music/song.wav"),
    ]
    return tl


def test_write_asset_claims_real_source_range():
    """Regression for the Resolve import bug: each asset must claim the FILE's
    timecode range (start = embedded TC, duration = file length), never the
    timeline's duration — Resolve verifies these against the media and drops
    clips on mismatch ("No overlap")."""
    import xml.etree.ElementTree as ET

    xml = write_fcpxml(_tc_timeline())
    root = ET.fromstring(xml)

    asset_a = next(a for a in root.iter("asset") if a.get("name") == "A")
    assert asset_a.get("start") == "161808/25s"  # 6472.32s = 01:47:52:08 @ 25fps
    assert asset_a.get("duration") == "14s"  # the FILE's length...
    assert asset_a.get("duration") != root.find(".//sequence").get("duration")

    # an asset WITHOUT the metadata: start 0s, duration = furthest source frame
    asset_b = next(a for a in root.iter("asset") if a.get("name") == "B")
    assert asset_b.get("start") == "0s"
    assert asset_b.get("duration") == "3s"  # max(source_out) = 75 frames

    # every asset-clip's source position = file TC + source_in
    starts = [c.get("start") for c in root.findall(".//spine/asset-clip")]
    assert starts == [
        "161858/25s",  # 6472.32 + 50/25
        "161958/25s",  # 6472.32 + 150/25
        "1s",  # B: no TC, source_in 25 frames
    ]

    # the music bed's asset: no TC concept, duration = its own source extent
    asset_song = next(a for a in root.iter("asset") if a.get("name") == "song")
    assert asset_song.get("start") == "0s"
    assert asset_song.get("duration") == "6s"  # source_out 150 frames


def test_write_asset_music_duration_metadata_wins():
    import xml.etree.ElementTree as ET

    tl = _tc_timeline()
    tl.clips[-1].metadata["media_duration_seconds"] = 187.4  # the real song length
    root = ET.fromstring(write_fcpxml(tl))
    asset_song = next(a for a in root.iter("asset") if a.get("name") == "song")
    assert asset_song.get("duration") == "937/5s"  # 187.4s (4685 frames @ 25fps)


def test_roundtrip_preserves_file_relative_source_ranges():
    """write -> read must keep source_in/source_out file-relative (0-based):
    the writer shifts by the embedded TC, the reader subtracts it back."""
    tl = _tc_timeline()
    back = read_fcpxml(write_fcpxml(tl))
    original = {
        (c.name, c.kind, c.source_in, c.source_out, c.record_in, c.record_out)
        for c in tl.clips
    }
    reread = {
        (c.name, c.kind, c.source_in, c.source_out, c.record_in, c.record_out)
        for c in back.clips
    }
    assert reread == original
    a = next(c for c in back.clips if c.name == "A")
    assert a.metadata["media_start_seconds"] == pytest.approx(6472.32)
    b = next(c for c in back.clips if c.name == "B")
    assert "media_start_seconds" not in b.metadata


def test_write_canvas_size_and_roundtrip():
    tl = Timeline(name="Vertical", fps=25.0, width=1080, height=1920)
    tl.clips.append(
        Clip(name="A", track="V1", kind=VIDEO, source_in=0, source_out=50,
             record_in=0, record_out=50, source_name="A", source_file="/m/a.mp4")
    )
    text = write_fcpxml(tl)
    assert 'width="1080"' in text and 'height="1920"' in text
    back = read_fcpxml(text)
    assert (back.width, back.height) == (1080, 1920)


def test_write_markers_land_on_clip_and_gap():
    import xml.etree.ElementTree as ET

    from monteur.model import Marker

    tl = Timeline(name="Marked", fps=25.0)
    tl.clips.append(
        Clip(name="A", track="V1", kind=VIDEO, source_in=0, source_out=50,
             record_in=0, record_out=50, source_name="A", source_file="/m/a.mp4")
    )
    tl.clips.append(
        Clip(name="B", track="V1", kind=VIDEO, source_in=0, source_out=50,
             record_in=60, record_out=110, source_name="B", source_file="/m/b.mp4")
    )
    tl.markers.append(Marker(frame=10, name="In clip"))
    tl.markers.append(Marker(frame=55, name="Title slot", note="0.4s of black"))
    root = ET.fromstring(write_fcpxml(tl))

    # The in-clip marker sits on clip A, in the clip's local source timebase.
    clip_markers = root.findall(".//asset-clip/marker")
    assert [m.get("value") for m in clip_markers] == ["In clip"]
    assert clip_markers[0].get("start") == "2/5s"  # frame 10 at 25 fps

    # The title-slot marker sits on the black gap, local to the gap's start,
    # with the note folded into the value.
    gap_markers = root.findall(".//gap/marker")
    assert [m.get("value") for m in gap_markers] == ["Title slot — 0.4s of black"]
    assert gap_markers[0].get("start") == "1/5s"  # frame 55 - gap start 50


def test_write_fade_metadata_becomes_head_and_tail_transitions():
    import xml.etree.ElementTree as ET

    tl = Timeline(name="Faded", fps=25.0)
    tl.clips.append(
        Clip(name="A", track="V1", kind=VIDEO, source_in=0, source_out=100,
             record_in=0, record_out=100, source_name="A", source_file="/m/a.mp4")
    )
    tl.metadata["fade_in_frames"] = 12
    tl.metadata["fade_out_frames"] = 25
    root = ET.fromstring(write_fcpxml(tl))
    spine = root.find(".//spine")

    # Resolve only imports transitions sitting BETWEEN two spine items, so
    # the fades dissolve from/to black gaps: leading gap, head transition,
    # the clip (shifted right by the fade-in), tail transition, trailing gap.
    tags = [el.tag for el in spine]
    assert tags == ["gap", "transition", "asset-clip", "transition", "gap"]

    lead_gap, head, clip_el, tail, trail_gap = list(spine)
    assert lead_gap.get("offset") == "0s"
    assert lead_gap.get("duration") == "12/25s"
    # head fade starts AT the shifted cut (start-aligned: no handles needed)
    assert head.get("offset") == "12/25s"
    assert head.get("duration") == "12/25s"
    # all content shifted right by the fade-in
    assert clip_el.get("offset") == "12/25s"
    # tail fade ends AT the cut into the trailing gap (end-aligned)
    assert tail.get("offset") == "87/25s"  # (100 + 12 - 25) frames
    assert tail.get("duration") == "1s"
    assert trail_gap.get("offset") == "112/25s"
    assert trail_gap.get("duration") == "1s"

    # the sequence covers the shift and the trailing gap
    assert root.find(".//sequence").get("duration") == "137/25s"


def test_write_no_fade_transitions_without_metadata():
    import xml.etree.ElementTree as ET

    tl = Timeline(name="Plain", fps=25.0)
    tl.clips.append(
        Clip(name="A", track="V1", kind=VIDEO, source_in=0, source_out=50,
             record_in=0, record_out=50, source_name="A", source_file="/m/a.mp4")
    )
    root = ET.fromstring(write_fcpxml(tl))
    assert root.find(".//spine").findall("transition") == []


# --- placed SFX elements: multi-track connected audio ------------------------------


def _sfx_timeline() -> Timeline:
    """Video on V1, music bed on A1, one placed SFX element on A2."""
    tl = Timeline(name="SFX", fps=25.0)
    tl.clips = [
        Clip("A", "V1", VIDEO, 0, 250, 0, 250, source_name="A",
             source_file="/footage/a.mp4"),
        Clip("song", "A1", AUDIO, 0, 250, 0, 250, source_name="song",
             source_file="/music/song.mp3",
             metadata={"media_duration_seconds": 30.0}),
        Clip("hit", "A2", AUDIO, 0, 20, 50, 70, source_name="hit",
             source_file="/sfx/hit.wav",
             metadata={"media_duration_seconds": 1.2}),
    ]
    return tl


def test_write_sfx_element_as_connected_effects_clip():
    from monteur.io.fcpxml import write_fcpxml

    xml = write_fcpxml(_sfx_timeline())
    # the music bed keeps its lane and role untouched...
    assert 'lane="-1"' in xml
    assert 'audioRole="music"' in xml
    # ...and the SFX element gets its own lane with the effects role
    assert 'lane="-2"' in xml
    assert 'audioRole="effects"' in xml
    # the element is linkable real media with its honest duration
    assert "file:///sfx/hit.wav" in xml
    import re as _re

    sfx_asset = _re.search(
        r'<asset[^>]*name="hit"[^>]*duration="([^"]+)"', xml
    )
    assert sfx_asset is not None
    assert sfx_asset.group(1) == "6/5s"  # 1.2s as rational seconds


def test_sfx_element_round_trips_on_its_own_track():
    from monteur.io.fcpxml import read_fcpxml, write_fcpxml

    back = read_fcpxml(write_fcpxml(_sfx_timeline()))
    audio = {c.track: c for c in back.audio_clips()}
    assert set(audio) == {"A1", "A2"}
    hit = audio["A2"]
    assert (hit.record_in, hit.record_out) == (50, 70)
    assert (hit.source_in, hit.source_out) == (0, 20)
    music = audio["A1"]
    assert (music.record_in, music.record_out) == (0, 250)


def test_no_music_montage_carries_clip_sound_and_sfx():
    """A no-music plan's timeline (audio="original") writes valid FCPXML:
    the clips' own sound folds into the asset-clips (hasAudio), the placed
    SFX element rides as a connected effects clip — no song asset at all."""
    from monteur.io.fcpxml import read_fcpxml, write_fcpxml
    from monteur.montage import MontageEntry, MontagePlan, SfxCue, montage_to_timeline

    plan = MontagePlan(music_path="", duration=10.0)
    plan.entries = [
        MontageEntry(
            clip_path="/footage/a.mp4", source_start=0.0, source_end=6.0,
            record_start=0.0, record_end=6.0, score=0.9,
        ),
        MontageEntry(
            clip_path="/footage/b.mp4", source_start=1.0, source_end=5.0,
            record_start=6.0, record_end=10.0, score=0.8,
        ),
    ]
    plan.sfx = [SfxCue(2.0, 0.8, "impact", "hit", "n", file="/sfx/hit.wav")]
    timeline = montage_to_timeline(plan, fps=25.0, audio="original")
    xml = write_fcpxml(timeline)
    assert 'hasAudio="1"' in xml  # the clips' own sound rides along
    assert 'audioRole="effects"' in xml and 'lane="-2"' in xml
    assert 'audioRole="music"' not in xml  # there IS no song
    back = read_fcpxml(xml)
    a1 = [c for c in back.audio_clips() if c.track == "A1"]
    assert len(a1) == 2  # camera sound, one per entry
    hit = next(c for c in back.audio_clips() if c.track == "A2")
    assert (hit.record_in, hit.record_out) == (50, 70)


def test_write_three_audio_tracks_mix_mode_layout():
    """mix mode: camera sound pairs into the asset-clips, song on A1 and the
    SFX element on A3 both come back on their own tracks."""
    from monteur.io.fcpxml import read_fcpxml, write_fcpxml

    tl = Timeline(name="Mix", fps=25.0)
    tl.clips = [
        Clip("A", "V1", VIDEO, 0, 250, 0, 250, source_name="A",
             source_file="/footage/a.mp4"),
        # the entry's own sound: same source + record range -> folded
        Clip("A", "A2", AUDIO, 0, 250, 0, 250, source_name="A",
             source_file="/footage/a.mp4"),
        Clip("song", "A1", AUDIO, 0, 250, 0, 250, source_name="song",
             source_file="/music/song.mp3"),
        Clip("hit", "A3", AUDIO, 0, 20, 50, 70, source_name="hit",
             source_file="/sfx/hit.wav"),
    ]
    xml = write_fcpxml(tl)
    assert 'lane="-3"' in xml
    back = read_fcpxml(xml)
    tracks = sorted(c.track for c in back.audio_clips())
    # the folded camera audio reads back on A1 alongside the music bed;
    # the SFX element keeps its own A3
    assert tracks == ["A1", "A1", "A3"]
    hit = next(c for c in back.audio_clips() if c.track == "A3")
    assert (hit.record_in, hit.record_out) == (50, 70)
