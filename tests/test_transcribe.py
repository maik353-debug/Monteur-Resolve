"""Tests for fable.transcribe — no real whisper install required."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from fable import transcribe
from fable.transcribe import (
    FableTranscribeError,
    find_backend,
    scene_take_from_name,
    transcribe_directory,
    transcribe_file,
)

CANNED_WHISPER_JSON = json.dumps(
    {
        "language": "en",
        "segments": [
            {"id": 0, "start": 0.0, "end": 2.5, "text": " Hello there."},
            {"id": 1, "start": 2.5, "end": 4.25, "text": " General Kenobi."},
        ],
    }
)

CANNED_CPP_JSON = json.dumps(
    {
        "result": {"language": "de"},
        "transcription": [
            {"offsets": {"from": 0, "to": 1500}, "text": " Guten Tag."},
            {"offsets": {"from": 1500, "to": 3000}, "text": " Und Schnitt."},
        ],
    }
)


# --- helpers ------------------------------------------------------------------


def patch_whisper_only(monkeypatch):
    """Pretend only the openai-whisper CLI is installed."""
    monkeypatch.setattr(
        transcribe.shutil,
        "which",
        lambda name: "/usr/bin/whisper" if name == "whisper" else None,
    )
    monkeypatch.delenv("WHISPER_CPP", raising=False)
    monkeypatch.delenv("WHISPER_CPP_MODEL", raising=False)


def make_whisper_runner(canned=CANNED_WHISPER_JSON, returncode=0, stderr=""):
    """Fake subprocess.run for the openai-whisper CLI: writes canned JSON to
    the --output_dir the module chose, named after the input file's stem."""
    calls = []

    def run(cmd, capture_output=True, text=True, **kw):
        calls.append(list(cmd))
        if returncode == 0:
            outdir = Path(cmd[cmd.index("--output_dir") + 1])
            media = Path(cmd[1])
            (outdir / (media.stem + ".json")).write_text(canned, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    run.calls = calls
    return run


# --- transcribe_file: openai-whisper backend -----------------------------------


def test_transcribe_file_parses_whisper_json(tmp_path, monkeypatch):
    patch_whisper_only(monkeypatch)
    media = tmp_path / "S01_T01.mov"
    media.write_bytes(b"\x00")
    runner = make_whisper_runner()

    t = transcribe_file(media, model="small", language="en", runner=runner)

    assert [s.text for s in t.segments] == ["Hello there.", "General Kenobi."]
    assert t.segments[0].start == 0.0 and t.segments[1].end == 4.25
    assert t.source_name == "S01_T01.mov"
    assert t.language == "en"
    # the whisper CLI was invoked with the flags we promise
    (cmd,) = runner.calls
    assert cmd[0] == "/usr/bin/whisper" and cmd[1] == str(media)
    assert cmd[cmd.index("--model") + 1] == "small"
    assert cmd[cmd.index("--language") + 1] == "en"
    assert cmd[cmd.index("--output_format") + 1] == "json"


def test_transcribe_file_nonzero_exit_carries_stderr(tmp_path, monkeypatch):
    patch_whisper_only(monkeypatch)
    media = tmp_path / "clip.mov"
    media.write_bytes(b"\x00")
    runner = make_whisper_runner(returncode=1, stderr="RuntimeError: model 'tiny' not found")

    with pytest.raises(FableTranscribeError) as exc:
        transcribe_file(media, runner=runner)
    msg = str(exc.value)
    assert "model 'tiny' not found" in msg
    assert "whisper" in msg
    assert "exit 1" in msg


def test_transcribe_file_missing_media(monkeypatch, tmp_path):
    patch_whisper_only(monkeypatch)
    with pytest.raises(FableTranscribeError, match="not found"):
        transcribe_file(tmp_path / "nope.mov", runner=make_whisper_runner())


# --- backend discovery ----------------------------------------------------------


def test_no_backend_raises_helpful_error(monkeypatch):
    monkeypatch.setattr(transcribe.shutil, "which", lambda name: None)
    monkeypatch.delenv("WHISPER_CPP", raising=False)
    monkeypatch.delenv("WHISPER_CPP_MODEL", raising=False)
    with pytest.raises(FableTranscribeError) as exc:
        find_backend()
    msg = str(exc.value)
    assert "pip install openai-whisper" in msg
    assert "WHISPER_CPP" in msg and "WHISPER_CPP_MODEL" in msg
    assert "16 kHz" in msg  # whisper.cpp WAV requirement is explained


def test_find_backend_prefers_openai_whisper(monkeypatch):
    patch_whisper_only(monkeypatch)
    backend = find_backend()
    assert backend.name == "whisper"
    assert backend.executable == "/usr/bin/whisper"


def test_cpp_backend_requires_model_env(monkeypatch):
    monkeypatch.setattr(
        transcribe.shutil,
        "which",
        lambda name: "/opt/wcpp/whisper-cli" if name == "/opt/wcpp/whisper-cli" else None,
    )
    monkeypatch.setenv("WHISPER_CPP", "/opt/wcpp/whisper-cli")
    monkeypatch.delenv("WHISPER_CPP_MODEL", raising=False)
    with pytest.raises(FableTranscribeError, match="WHISPER_CPP_MODEL"):
        find_backend()


# --- whisper.cpp backend ----------------------------------------------------------


def patch_cpp_only(monkeypatch):
    monkeypatch.setattr(
        transcribe.shutil,
        "which",
        lambda name: "/opt/wcpp/whisper-cli" if name == "/opt/wcpp/whisper-cli" else None,
    )
    monkeypatch.setenv("WHISPER_CPP", "/opt/wcpp/whisper-cli")
    monkeypatch.setenv("WHISPER_CPP_MODEL", "/models/ggml-small.bin")


def test_transcribe_file_whisper_cpp(tmp_path, monkeypatch):
    patch_cpp_only(monkeypatch)  # note: no ffmpeg on the fake PATH
    media = tmp_path / "take.wav"  # WAV passes through without ffmpeg
    media.write_bytes(b"\x00")

    def run(cmd, capture_output=True, text=True, **kw):
        assert cmd[0] == "/opt/wcpp/whisper-cli"
        assert cmd[cmd.index("-m") + 1] == "/models/ggml-small.bin"
        assert cmd[cmd.index("-f") + 1] == str(media)
        assert "-oj" in cmd
        out_base = Path(cmd[cmd.index("-of") + 1])
        out_base.with_suffix(".json").write_text(CANNED_CPP_JSON, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    t = transcribe_file(media, runner=run)
    assert [s.text for s in t.segments] == ["Guten Tag.", "Und Schnitt."]
    assert (t.segments[0].start, t.segments[0].end) == (0.0, 1.5)
    assert t.language == "de"
    assert t.source_name == "take.wav"


def test_whisper_cpp_non_wav_without_ffmpeg_errors(tmp_path, monkeypatch):
    patch_cpp_only(monkeypatch)
    media = tmp_path / "take.mov"
    media.write_bytes(b"\x00")
    with pytest.raises(FableTranscribeError) as exc:
        transcribe_file(media, runner=lambda *a, **k: pytest.fail("must not run"))
    msg = str(exc.value)
    assert "16 kHz" in msg and "ffmpeg" in msg


def test_whisper_cpp_converts_via_ffmpeg(tmp_path, monkeypatch):
    def which(name):
        return {
            "/opt/wcpp/whisper-cli": "/opt/wcpp/whisper-cli",
            "ffmpeg": "/usr/bin/ffmpeg",
        }.get(name)

    monkeypatch.setattr(transcribe.shutil, "which", which)
    monkeypatch.setenv("WHISPER_CPP", "/opt/wcpp/whisper-cli")
    monkeypatch.setenv("WHISPER_CPP_MODEL", "/models/ggml-small.bin")
    media = tmp_path / "take.mov"
    media.write_bytes(b"\x00")
    seen = []

    def run(cmd, capture_output=True, text=True, **kw):
        seen.append(list(cmd))
        if cmd[0] == "/usr/bin/ffmpeg":
            assert cmd[cmd.index("-ar") + 1] == "16000"
            assert cmd[cmd.index("-ac") + 1] == "1"
            Path(cmd[-1]).write_bytes(b"RIFF")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        wav = Path(cmd[cmd.index("-f") + 1])
        assert wav.suffix == ".wav" and wav.exists()
        # whisper.cpp appends ".json" to the -of base verbatim
        out_base = cmd[cmd.index("-of") + 1]
        Path(out_base + ".json").write_text(CANNED_CPP_JSON, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    t = transcribe_file(media, runner=run)
    assert len(seen) == 2  # ffmpeg conversion, then whisper.cpp
    assert t.source_name == "take.mov"
    assert len(t.segments) == 2


# --- transcribe_directory ----------------------------------------------------------


def test_transcribe_directory_skips_sibling_srt(tmp_path, monkeypatch, capsys):
    patch_whisper_only(monkeypatch)
    done = tmp_path / "a_done.mov"
    done.write_bytes(b"\x00")
    (tmp_path / "a_done.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nAlready transcribed.\n", encoding="utf-8"
    )
    fresh = tmp_path / "b_fresh.mov"
    fresh.write_bytes(b"\x00")
    (tmp_path / "notes.txt").write_text("not media", encoding="utf-8")

    runner = make_whisper_runner()
    results = transcribe_directory(tmp_path, runner=runner)

    assert sorted(results) == [str(done), str(fresh)]
    # sibling .srt was loaded, not re-transcribed
    assert results[str(done)].segments[0].text == "Already transcribed."
    assert results[str(done)].source_name == "a_done.mov"
    assert "skipping a_done.mov" in capsys.readouterr().out
    # only the fresh file hit the backend
    assert [Path(c[1]).name for c in runner.calls] == ["b_fresh.mov"]
    assert results[str(fresh)].source_name == "b_fresh.mov"
    assert len(results[str(fresh)].segments) == 2


def test_transcribe_directory_skips_sibling_json(tmp_path, monkeypatch, capsys):
    patch_whisper_only(monkeypatch)
    media = tmp_path / "clip.MP4"  # extension matching is case-insensitive
    media.write_bytes(b"\x00")
    (tmp_path / "clip.json").write_text(CANNED_WHISPER_JSON, encoding="utf-8")

    results = transcribe_directory(
        tmp_path, runner=lambda *a, **k: pytest.fail("must not transcribe")
    )
    assert list(results) == [str(media)]
    assert results[str(media)].language == "en"
    assert "skipping clip.MP4" in capsys.readouterr().out


def test_transcribe_directory_not_a_directory(tmp_path):
    with pytest.raises(FableTranscribeError, match="not a directory"):
        transcribe_directory(tmp_path / "missing")


# --- scene_take_from_name ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # marker patterns
        ("S12_T03.mov", ("12", "3")),
        ("SC12_TK3.mov", ("12", "3")),
        ("Scene12_Take3.mov", ("12", "3")),
        ("scene 12 take 3.wav", ("12", "3")),
        ("s12-t3.mp4", ("12", "3")),
        ("S12aT2.mov", ("12A", "2")),
        ("sc012a_tk07.braw", ("12A", "7")),
        ("A001_SC4_TK2.mxf", ("4", "2")),
        # bare stem pattern
        ("12-3.mov", ("12", "3")),
        ("12_3.wav", ("12", "3")),
        ("12A-3.mov", ("12A", "3")),
        # non-matches
        ("interview.mov", ("", "")),
        ("IMG_1234.MOV", ("", "")),
        ("Setup1_Part2.mov", ("", "")),
        ("s01e02.mkv", ("", "")),
    ],
)
def test_scene_take_from_name(name, expected):
    assert scene_take_from_name(name) == expected
