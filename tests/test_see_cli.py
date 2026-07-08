"""Tests for the vision CLI surface (``monteur see`` and ``create --see``).

Only argument parsing and the pre-vision validation are exercised here —
the vision pass itself needs the anthropic package and an API key, and is
tested with monteur.vision.
"""

from __future__ import annotations

import pytest

from monteur.cli import build_parser, cmd_create, cmd_see


def test_see_parses_with_defaults():
    args = build_parser().parse_args(["see", "clips"])
    assert args.folder == "clips"
    assert args.model is None
    assert args.max_moments == 48
    assert args.func is cmd_see


def test_see_accepts_model_and_max_moments():
    args = build_parser().parse_args(
        ["see", "clips", "--model", "claude-test", "--max-moments", "12"]
    )
    assert args.model == "claude-test"
    assert args.max_moments == 12


def test_see_fails_cleanly_on_empty_folder(tmp_path, capsys):
    args = build_parser().parse_args(["see", str(tmp_path)])
    with pytest.raises(SystemExit):
        args.func(args)  # no clips: fails before any vision work
    err = capsys.readouterr().err
    assert "no video files" in err


def test_create_parses_see_flag():
    args = build_parser().parse_args(
        [
            "create", "clips", "song.mp3", "-o", "out.fcpxml",
            "--see", "--max-moments", "24",
        ]
    )
    assert args.see is True
    assert args.max_moments == 24
    assert args.func is cmd_create


def test_create_see_defaults_off():
    args = build_parser().parse_args(["create", "clips", "song.mp3", "-o", "out.fcpxml"])
    assert args.see is False
    assert args.max_moments == 48
