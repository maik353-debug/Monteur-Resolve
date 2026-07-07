"""Tests for fable.papercut."""

from __future__ import annotations

import pytest

from fable.model import Transcript, TranscriptSegment
from fable.papercut import (
    Papercut,
    PapercutEntry,
    create_papercut,
    create_papercut_multi,
    merge_papercuts,
    papercut_to_timeline,
    parse_papercut,
)


def _transcript() -> Transcript:
    return Transcript(
        segments=[
            TranscriptSegment(0, 1.0, 4.2, "text of the segment", speaker="ANNA"),
            TranscriptSegment(1, 4.5, 9.1, "more text"),
            TranscriptSegment(2, 12.345, 15.678, "third take", speaker="BOB"),
        ],
        source_name="interview_A.mov",
    )


def test_create_papercut_format():
    text = create_papercut(_transcript(), fps=25.0, title="Rough Cut")
    lines = text.splitlines()
    assert lines[0] == "# Papercut: Rough Cut"
    assert "source: interview_A.mov" in lines
    assert "fps: 25" in lines
    assert "- [ ] [00:00:01.000 --> 00:00:04.200] ANNA: text of the segment" in lines
    assert "- [ ] [00:00:04.500 --> 00:00:09.100] more text" in lines
    assert "- [ ] [00:00:12.345 --> 00:00:15.678] BOB: third take" in lines


def test_roundtrip_preserves_times_texts_speakers():
    transcript = _transcript()
    pc = parse_papercut(create_papercut(transcript, fps=25.0, title="Rough Cut"))
    assert pc.title == "Rough Cut"
    assert pc.fps == 25.0
    assert pc.default_source == "interview_A.mov"
    assert len(pc.entries) == len(transcript.segments)
    for entry, seg in zip(pc.entries, transcript.segments):
        assert entry.start == pytest.approx(seg.start, abs=0.0005)
        assert entry.end == pytest.approx(seg.end, abs=0.0005)
        assert entry.text == seg.text
        assert entry.speaker == seg.speaker
        assert entry.source_name == "interview_A.mov"
        assert entry.selected is False


def test_selection_and_reordering_exact_frames():
    text = """# Papercut: Reordered

source: interview_A.mov
fps: 25

- [x] [00:00:10.000 --> 00:00:12.000] BOB: second in source, first in cut
- [ ] [00:00:01.000 --> 00:00:02.000] skipped take
- [X] [00:00:04.000 --> 00:00:06.000] ANNA: first in source, second in cut
"""
    timeline = papercut_to_timeline(parse_papercut(text))
    video = timeline.video_clips()
    audio = timeline.audio_clips()
    assert len(video) == 2
    assert len(audio) == 2

    first, second = video
    assert (first.source_in, first.source_out) == (250, 300)
    assert (first.record_in, first.record_out) == (0, 50)
    assert second.source_in == 100
    assert second.source_out == 150
    assert (second.record_in, second.record_out) == (50, 100)

    assert "skipped take" not in {c.name for c in timeline.clips}
    for v, a in zip(video, audio):
        assert a.track == "A1"
        assert a.kind == "audio"
        assert v.track == "V1"
        assert v.kind == "video"
        assert (a.source_in, a.source_out) == (v.source_in, v.source_out)
        assert (a.record_in, a.record_out) == (v.record_in, v.record_out)
        assert a.source_name == v.source_name == "interview_A.mov"


def test_unchecked_entries_excluded():
    text = create_papercut(_transcript(), fps=25.0)
    timeline = papercut_to_timeline(parse_papercut(text))
    assert timeline.clips == []


def test_handles_clamped_at_zero():
    pc = Papercut(
        title="",
        fps=25.0,
        entries=[
            PapercutEntry("a.mov", start=0.2, end=2.0, text="early take", selected=True)
        ],
        default_source="a.mov",
    )
    timeline = papercut_to_timeline(pc, handles=1.0)
    clip = timeline.video_clips()[0]
    assert clip.source_in == 0
    assert clip.source_out == 75
    assert (clip.record_in, clip.record_out) == (0, 75)


def test_multi_source_sections_resolve_source_name():
    a = _transcript()
    b = Transcript(
        segments=[TranscriptSegment(0, 2.0, 5.0, "b-cam line", speaker="EVA")],
        source_name="interview_B.mov",
    )
    text = create_papercut_multi([a, b], fps=25.0, title="Both")
    assert "## source: interview_B.mov" in text

    pc = parse_papercut(text)
    assert pc.default_source == "interview_A.mov"
    assert [e.source_name for e in pc.entries] == [
        "interview_A.mov",
        "interview_A.mov",
        "interview_A.mov",
        "interview_B.mov",
    ]
    assert pc.entries[-1].speaker == "EVA"

    for entry in pc.entries:
        entry.selected = True
    timeline = papercut_to_timeline(pc)
    assert timeline.video_clips()[-1].source_name == "interview_B.mov"
    assert timeline.video_clips()[0].source_name == "interview_A.mov"


def test_merge_papercuts_preserves_selection_and_sections():
    a = create_papercut(_transcript(), fps=25.0, title="A")
    b = create_papercut(
        Transcript(
            segments=[TranscriptSegment(0, 2.0, 5.0, "b-cam line")],
            source_name="interview_B.mov",
        ),
        fps=25.0,
        title="B",
    )
    a = a.replace("- [ ] [00:00:04.500", "- [x] [00:00:04.500")
    merged = merge_papercuts([a, b], title="Merged")
    assert "## source: interview_B.mov" in merged
    pc = parse_papercut(merged)
    assert pc.title == "Merged"
    assert [e.selected for e in pc.entries] == [False, True, False, False]
    assert pc.entries[-1].source_name == "interview_B.mov"


def test_malformed_timestamp_raises_with_line_number():
    text = """# Papercut: Bad

source: x.mov
fps: 25

- [ ] [00:00:01.000 --> 00:00:02.000] fine
- [x] [00:00:0Z.000 --> 00:00:04.000] broken
"""
    with pytest.raises(ValueError, match="line 7"):
        parse_papercut(text)


def test_entry_without_timestamp_raises_with_line_number():
    with pytest.raises(ValueError, match="line 1"):
        parse_papercut("- [x] no timestamp here at all\n")


def test_missing_fps_warns_and_defaults():
    text = "source: a.mov\n\n- [x] [00:00:01.000 --> 00:00:02.000] take\n"
    with pytest.warns(UserWarning, match="fps"):
        pc = parse_papercut(text)
    assert pc.fps == 25.0
    assert pc.entries[0].source_name == "a.mov"


def test_parser_tolerates_noise_and_case():
    text = """# Papercut: Noisy

fps: 24
some prose the editor typed for themselves
- not a checklist entry, ignored

- [X] [00:00:01.500 --> 00:00:03.250] KIM O'HARA: keeper
"""
    pc = parse_papercut(text)
    assert pc.fps == 24.0
    assert pc.default_source == ""
    assert len(pc.entries) == 1
    entry = pc.entries[0]
    assert entry.selected is True
    assert entry.speaker == "KIM O'HARA"
    assert entry.text == "keeper"
    assert entry.start == pytest.approx(1.5)
    assert entry.end == pytest.approx(3.25)


def test_timeline_name_from_title_or_arg():
    pc = Papercut(title="My Cut", fps=25.0, entries=[], default_source="")
    assert papercut_to_timeline(pc).name == "My Cut"
    assert papercut_to_timeline(pc, name="Override").name == "Override"
