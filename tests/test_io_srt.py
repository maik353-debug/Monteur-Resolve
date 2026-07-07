"""Tests for fable.io.srt and fable.io.whisperjson."""

from __future__ import annotations

from pathlib import Path

import pytest

from fable.io import load_transcript, read_srt, read_whisper_json, write_srt
from fable.model import Transcript, TranscriptSegment

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def sample() -> Transcript:
    text = (FIXTURES / "sample.srt").read_text(encoding="utf-8")
    return read_srt(text, source_name="sample")


def test_read_sample_counts(sample: Transcript) -> None:
    assert len(sample.segments) == 10
    assert sample.source_name == "sample"
    assert [s.index for s in sample.segments] == list(range(1, 11))


def test_read_sample_times_and_text(sample: Transcript) -> None:
    first = sample.segments[0]
    assert first.start == pytest.approx(1.0)
    assert first.end == pytest.approx(3.5)
    assert first.text == "Welcome back to the cutting room."
    assert first.speaker == ""

    dotted = sample.segments[6]
    assert dotted.start == pytest.approx(21.1)
    assert dotted.end == pytest.approx(23.9)
    assert dotted.text == "Unless the scene is cut to playback."


def test_read_sample_multiline_and_speaker(sample: Transcript) -> None:
    anna = sample.segments[2]
    assert anna.speaker == "ANNA"
    assert anna.text == (
        "I always start with the interview and build the scene around it."
    )
    assert not any(s.speaker for s in sample.segments if s is not anna)


def test_read_tolerates_bom_crlf_and_missing_index() -> None:
    text = "﻿00:00:01,000 --> 00:00:02,000\r\nHello there.\r\n\r\n"
    transcript = read_srt(text)
    assert len(transcript.segments) == 1
    assert transcript.segments[0].text == "Hello there."
    assert transcript.segments[0].index == 1


def test_lowercase_prefix_is_not_a_speaker() -> None:
    text = "1\n00:00:01,000 --> 00:00:02,000\nNote: check the mix.\n"
    transcript = read_srt(text)
    assert transcript.segments[0].speaker == ""
    assert transcript.segments[0].text == "Note: check the mix."


def test_write_read_roundtrip(sample: Transcript) -> None:
    text = write_srt(sample)
    assert "00:00:01,000 --> 00:00:03,500" in text
    assert "ANNA: I always start" in text
    back = read_srt(text, source_name="sample")
    assert [
        (s.index, s.start, s.end, s.text, s.speaker) for s in back.segments
    ] == [(s.index, s.start, s.end, s.text, s.speaker) for s in sample.segments]


def test_write_empty_transcript() -> None:
    assert write_srt(Transcript()) == ""


def test_write_millisecond_rounding() -> None:
    t = Transcript(segments=[TranscriptSegment(1, 0.0015, 1.9996, "x")])
    text = write_srt(t)
    assert "00:00:00,002 --> 00:00:02,000" in text


def test_read_malformed_time_line_raises() -> None:
    with pytest.raises(ValueError, match="time line"):
        read_srt("1\nnot a time line\nHello.\n")


def test_read_end_before_start_raises() -> None:
    with pytest.raises(ValueError, match="precedes"):
        read_srt("1\n00:00:05,000 --> 00:00:01,000\nBackwards.\n")


def test_load_transcript_srt_sets_source_name() -> None:
    transcript = load_transcript(FIXTURES / "sample.srt")
    assert transcript.source_name == "sample"
    assert len(transcript.segments) == 10


def test_read_whisper_json_fixture() -> None:
    text = (FIXTURES / "sample_whisper.json").read_text(encoding="utf-8")
    transcript = read_whisper_json(text, source_name="interview")
    assert transcript.language == "en"
    assert transcript.source_name == "interview"
    assert len(transcript.segments) == 4
    seg = transcript.segments[2]
    assert seg.index == 2
    assert seg.start == pytest.approx(7.0)
    assert seg.end == pytest.approx(11.8)
    assert seg.text == "I always start with the interview."
    assert seg.speaker == "ANNA"
    assert transcript.segments[0].speaker == ""


def test_read_whisper_bare_list() -> None:
    transcript = read_whisper_json(
        '[{"start": 0, "end": 1.5, "text": " hi"}]'
    )
    assert len(transcript.segments) == 1
    assert transcript.segments[0].text == "hi"
    assert transcript.segments[0].end == pytest.approx(1.5)


def test_read_whisper_invalid_json_raises() -> None:
    with pytest.raises(ValueError, match="JSON"):
        read_whisper_json("{not json")


def test_read_whisper_missing_segments_raises() -> None:
    with pytest.raises(ValueError, match="segments"):
        read_whisper_json('{"text": "no segments here"}')


def test_read_whisper_missing_field_raises() -> None:
    with pytest.raises(ValueError, match="start"):
        read_whisper_json('{"segments": [{"end": 1.0, "text": "x"}]}')


def test_load_transcript_whisper_json() -> None:
    transcript = load_transcript(FIXTURES / "sample_whisper.json")
    assert transcript.source_name == "sample_whisper"
    assert transcript.language == "en"
    assert len(transcript.segments) == 4


def test_load_transcript_unsupported_extension(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with pytest.raises(ValueError, match="unsupported"):
        load_transcript(p)
