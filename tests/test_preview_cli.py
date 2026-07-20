"""Tests for the ``monteur preview`` and ``monteur export`` CLIs — the
thin render_preview / render_export wrappers.

Argument parsing, the plan/audio validation and the exact argument
passthrough are exercised with a monkeypatched engine; one real tiny
render each runs against the demo footage (skipped when it is missing)
so the wrappers are proven end-to-end at least once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _demo import DEMO
from monteur.cli import build_parser, cmd_export, cmd_preview, main
from monteur.montage import MontageEntry, MontagePlan, plan_to_dict

needs_demo = pytest.mark.skipif(
    not (DEMO / "clip_A.mp4").exists(), reason="demo footage not present"
)


def _plan(music: bool = True, clips: Path | None = None) -> MontagePlan:
    base = clips or Path("/clips")
    return MontagePlan(
        music_path=str(base / "song.wav") if music else "",
        duration=2.0,
        entries=[
            MontageEntry(
                clip_path=str(base / "clip_A.mp4"),
                source_start=0.5, source_end=1.5,
                record_start=0.0, record_end=1.0, score=1.0,
            ),
            MontageEntry(
                clip_path=str(base / "clip_C.mp4"),
                source_start=1.0, source_end=2.0,
                record_start=1.0, record_end=2.0, score=0.9,
            ),
        ],
    )


def _write_plan(tmp_path: Path, plan: MontagePlan) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan_to_dict(plan)), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------ parsing


def test_preview_parses_with_defaults():
    args = build_parser().parse_args(["preview", "plan.json", "-o", "out.mp4"])
    assert args.plan == "plan.json"
    assert args.output == "out.mp4"
    assert args.width == 640
    assert args.audio is None  # auto: music when the plan has a song
    assert args.fps == 25.0
    assert args.func is cmd_preview


def test_preview_parses_all_options():
    args = build_parser().parse_args(
        ["preview", "p.json", "-o", "p.mp4",
         "--width", "320", "--audio", "mix", "--fps", "30"]
    )
    assert args.width == 320
    assert args.audio == "mix"
    assert args.fps == 30.0


def test_preview_rejects_unknown_audio_mode(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["preview", "p.json", "-o", "p.mp4", "--audio", "karaoke"]
        )


# ------------------------------------------------------------ passthrough


def test_preview_forwards_arguments_to_the_engine(tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_render(plan, out_path, *, width, fps, audio, progress):
        calls.update(
            plan=plan, out_path=out_path, width=width, fps=fps,
            audio=audio, progress=progress,
        )
        progress(1, 2, "clip_A.mp4")
        progress(2, 2, "mux")
        return {"path": out_path, "duration": 2.0, "width": width, "segments": 2}

    monkeypatch.setattr("monteur.preview.render_preview", fake_render)
    plan_path = _write_plan(tmp_path, _plan())
    out = str(tmp_path / "preview.mp4")
    main(["preview", plan_path, "-o", out, "--width", "320", "--fps", "30"])

    assert calls["out_path"] == out
    assert calls["width"] == 320
    assert calls["fps"] == 30.0
    assert calls["audio"] == "music"  # default: the plan has a song
    assert calls["plan"].duration == 2.0
    assert len(calls["plan"].entries) == 2

    output = capsys.readouterr().out
    assert "[1/2] clip_A.mp4" in output  # engine progress printed live
    assert "[2/2] mux" in output
    assert f"Preview -> {out} (2.0s, 320px wide, 2 segments)" in output
    assert "Rough pixels, real cut" in output


def test_preview_audio_defaults_to_original_without_music(
    tmp_path, monkeypatch
):
    seen = {}

    def fake_render(plan, out_path, *, width, fps, audio, progress):
        seen["audio"] = audio
        return {"path": out_path, "duration": 2.0, "width": width, "segments": 2}

    monkeypatch.setattr("monteur.preview.render_preview", fake_render)
    plan_path = _write_plan(tmp_path, _plan(music=False))
    main(["preview", plan_path, "-o", str(tmp_path / "p.mp4")])
    assert seen["audio"] == "original"


def test_preview_explicit_audio_wins(tmp_path, monkeypatch):
    seen = {}

    def fake_render(plan, out_path, *, width, fps, audio, progress):
        seen["audio"] = audio
        return {"path": out_path, "duration": 2.0, "width": width, "segments": 2}

    monkeypatch.setattr("monteur.preview.render_preview", fake_render)
    plan_path = _write_plan(tmp_path, _plan())
    main(["preview", plan_path, "-o", str(tmp_path / "p.mp4"), "--audio", "mix"])
    assert seen["audio"] == "mix"


# ------------------------------------------------------------ validation


def test_preview_music_mode_without_music_fails_cleanly(tmp_path, capsys):
    plan_path = _write_plan(tmp_path, _plan(music=False))
    with pytest.raises(SystemExit):
        main(["preview", plan_path, "-o", str(tmp_path / "p.mp4"),
              "--audio", "music"])
    assert "no music" in capsys.readouterr().err


def test_preview_empty_plan_fails_cleanly(tmp_path, capsys):
    plan = MontagePlan(music_path="", duration=0.0)
    plan_path = _write_plan(tmp_path, plan)
    with pytest.raises(SystemExit):
        main(["preview", plan_path, "-o", str(tmp_path / "p.mp4")])
    assert "no entries" in capsys.readouterr().err


def test_preview_missing_plan_file_fails_cleanly(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["preview", str(tmp_path / "nope.json"),
              "-o", str(tmp_path / "p.mp4")])
    assert "plan file not found" in capsys.readouterr().err


# ------------------------------------------------------------ end to end


@needs_demo
def test_preview_renders_for_real(tmp_path, capsys):
    from monteur.media import probe

    plan_path = _write_plan(tmp_path, _plan(clips=DEMO, music=False))
    out = tmp_path / "real.mp4"
    main(["preview", plan_path, "-o", str(out), "--width", "320",
          "--audio", "original"])
    assert out.is_file() and out.stat().st_size > 0
    info = probe(out)
    assert info.duration == pytest.approx(2.0, abs=0.3)
    assert info.width == 320
    output = capsys.readouterr().out
    assert "Preview ->" in output


# ============================================================ monteur export
#
# The Direct Export command — same wrapper shape as `monteur preview`.


# ------------------------------------------------------------ parsing


def test_export_parses_with_defaults():
    args = build_parser().parse_args(["export", "plan.json", "-o", "out.mp4"])
    assert args.plan == "plan.json"
    assert args.output == "out.mp4"
    assert args.canvas == "uhd"
    assert args.audio is None  # auto: music when the plan has a song
    assert args.quality == "high"
    assert args.fps == 25.0
    assert args.size == ""  # advanced/testing only
    assert args.func is cmd_export


def test_export_parses_all_options():
    args = build_parser().parse_args(
        ["export", "p.json", "-o", "p.mp4", "--canvas", "cine",
         "--audio", "mix", "--quality", "medium", "--fps", "30",
         "--size", "480x270"]
    )
    assert args.canvas == "cine"
    assert args.audio == "mix"
    assert args.quality == "medium"
    assert args.fps == 30.0
    assert args.size == "480x270"


def test_export_rejects_unknown_quality():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["export", "p.json", "-o", "p.mp4", "--quality", "ultra"]
        )


def test_export_rejects_unknown_canvas():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["export", "p.json", "-o", "p.mp4", "--canvas", "imax"]
        )


# ------------------------------------------------------------ passthrough


def test_export_forwards_arguments_to_the_engine(tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_export(plan, out_path, *, canvas, fps, audio, quality,
                    progress, size):
        calls.update(
            plan=plan, out_path=out_path, canvas=canvas, fps=fps,
            audio=audio, quality=quality, size=size,
        )
        progress(1, 3, "clip_A.mp4")
        progress(3, 3, "mux")
        return {
            "path": out_path, "duration": 2.0, "width": 480, "height": 270,
            "seconds": 1.5, "notes": ["one honest note"],
        }

    monkeypatch.setattr("monteur.preview.render_export", fake_export)
    plan_path = _write_plan(tmp_path, _plan())
    out = str(tmp_path / "video.mp4")
    main(["export", plan_path, "-o", out, "--canvas", "hd",
          "--quality", "medium", "--fps", "30", "--size", "480x270"])

    assert calls["out_path"] == out
    assert calls["canvas"] == "hd"
    assert calls["fps"] == 30.0
    assert calls["audio"] == "music"  # default: the plan has a song
    assert calls["quality"] == "medium"
    assert calls["size"] == (480, 270)
    assert len(calls["plan"].entries) == 2

    output = capsys.readouterr().out
    assert "[1/3] clip_A.mp4" in output  # engine progress printed live
    assert "[3/3] mux" in output
    assert f"Export -> {out} (2.0s, 480x270, rendered in 1.5s)" in output
    assert "one honest note" in output  # degradation notes surface verbatim
    assert "Resolve stays the place for grading and fine-tuning" in output


def test_export_audio_defaults_to_original_without_music(tmp_path, monkeypatch):
    seen = {}

    def fake_export(plan, out_path, **kwargs):
        seen["audio"] = kwargs["audio"]
        return {"path": out_path, "duration": 2.0, "width": 480,
                "height": 270, "seconds": 1.0, "notes": []}

    monkeypatch.setattr("monteur.preview.render_export", fake_export)
    plan_path = _write_plan(tmp_path, _plan(music=False))
    main(["export", plan_path, "-o", str(tmp_path / "v.mp4")])
    assert seen["audio"] == "original"


# ------------------------------------------------------------ validation


def test_export_music_mode_without_music_fails_cleanly(tmp_path, capsys):
    plan_path = _write_plan(tmp_path, _plan(music=False))
    with pytest.raises(SystemExit):
        main(["export", plan_path, "-o", str(tmp_path / "v.mp4"),
              "--audio", "music"])
    assert "no music" in capsys.readouterr().err


def test_export_bad_size_fails_cleanly(tmp_path, capsys):
    plan_path = _write_plan(tmp_path, _plan())
    with pytest.raises(SystemExit):
        main(["export", plan_path, "-o", str(tmp_path / "v.mp4"),
              "--size", "wide"])
    assert "--size" in capsys.readouterr().err


def test_export_empty_plan_fails_cleanly(tmp_path, capsys):
    plan = MontagePlan(music_path="", duration=0.0)
    plan_path = _write_plan(tmp_path, plan)
    with pytest.raises(SystemExit):
        main(["export", plan_path, "-o", str(tmp_path / "v.mp4")])
    assert "no entries" in capsys.readouterr().err


# ------------------------------------------------------------ end to end


@needs_demo
def test_export_renders_for_real(tmp_path, capsys):
    from monteur.media import probe

    plan_path = _write_plan(tmp_path, _plan(clips=DEMO, music=False))
    out = tmp_path / "real_export.mp4"
    main(["export", plan_path, "-o", str(out), "--size", "320x180",
          "--quality", "medium", "--audio", "original"])
    assert out.is_file() and out.stat().st_size > 0
    info = probe(out)
    assert info.duration == pytest.approx(2.0, abs=0.3)
    assert info.width == 320 and info.height == 180
    assert info.has_audio
    output = capsys.readouterr().out
    assert "Export ->" in output
