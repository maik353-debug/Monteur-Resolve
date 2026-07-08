"""Tests for the SFX film-mode CLI surface (``monteur create --sfx``).

Only argument parsing is exercised here — the cue planning itself is
tested with monteur.montage, the markers with montage_to_timeline.
"""

from __future__ import annotations

from monteur.cli import build_parser, cmd_create


def test_create_parses_sfx_flag():
    args = build_parser().parse_args(
        ["create", "clips", "song.mp3", "-o", "out.fcpxml", "--sfx"]
    )
    assert args.sfx is True
    assert args.func is cmd_create


def test_create_sfx_defaults_off():
    args = build_parser().parse_args(["create", "clips", "song.mp3", "-o", "out.fcpxml"])
    assert args.sfx is False


def test_create_sfx_combines_with_original_audio():
    # The film mode proper: the clips' own sound plus the planned SFX layer.
    args = build_parser().parse_args(
        [
            "create", "clips", "--audio", "original", "--max-duration", "60",
            "-o", "out.fcpxml", "--sfx",
        ]
    )
    assert args.sfx is True
    assert args.audio == "original"
    assert args.music is None
