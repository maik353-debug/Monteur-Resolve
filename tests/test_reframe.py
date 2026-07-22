"""Tests for monteur.reframe — the Auto-Reframe 9:16 crop-offset math, its
fallback byte-parity, the in-memory-only entry field, and a real render
proving an off-centre subject survives the vertical crop.

The pure-math tests need neither ffmpeg nor footage; the render test builds
one tiny synthetic clip (coloured thirds) on the fly and is skipped when
ffmpeg / numpy are unavailable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from monteur import reframe
from monteur.montage import MontageEntry, MontagePlan, plan_to_dict

try:
    import imageio_ffmpeg  # noqa: F401

    from monteur.media import find_ffmpeg

    HAVE_FFMPEG = True
except Exception:  # noqa: BLE001
    HAVE_FFMPEG = False

try:
    import numpy  # noqa: F401

    HAVE_NUMPY = True
except Exception:  # noqa: BLE001
    HAVE_NUMPY = False

needs_render = pytest.mark.skipif(
    not (HAVE_FFMPEG and HAVE_NUMPY), reason="ffmpeg + numpy needed"
)

# A 16:9 source cropped to 9:16 vertical — the canonical reframe case.
SRC = (640, 360)
VERT = (1080, 1920)


# ------------------------------------------------------------ cover geometry


def test_cover_scale_vertical_fills_height_and_overflows_width():
    sw, sh = reframe.cover_scale(*SRC, *VERT)
    # Height fills exactly, width overflows (the cropped dimension).
    assert round(sh) == VERT[1]
    assert sw > VERT[0]


def test_cover_scale_same_aspect_has_no_slack():
    sw, sh = reframe.cover_scale(640, 360, 1280, 720)
    assert (round(sw), round(sh)) == (1280, 720)


# ------------------------------------------------------------ crop offset math


def test_none_focus_is_the_centre_offset():
    assert reframe.crop_offset(*SRC, *VERT, None) == reframe.center_offset(*SRC, *VERT)


def test_centred_focus_is_the_centre_offset():
    assert reframe.crop_offset(*SRC, *VERT, (0.5, 0.5)) == reframe.center_offset(
        *SRC, *VERT
    )


def test_focus_left_shifts_the_crop_left_of_centre():
    cx, _ = reframe.center_offset(*SRC, *VERT)
    x, _ = reframe.crop_offset(*SRC, *VERT, (0.2, 0.5))
    assert x < cx  # a left subject pulls the window left


def test_focus_right_shifts_the_crop_right_of_centre():
    cx, _ = reframe.center_offset(*SRC, *VERT)
    x, _ = reframe.crop_offset(*SRC, *VERT, (0.8, 0.5))
    assert x > cx


def test_extreme_focus_is_clamped_inside_the_source():
    sw, sh = reframe.cover_scale(*SRC, *VERT)
    # Hard left / hard right: never negative, never past the slack edge.
    xl, yl = reframe.crop_offset(*SRC, *VERT, (0.0, 0.5))
    xr, yr = reframe.crop_offset(*SRC, *VERT, (1.0, 0.5))
    assert xl == 0.0
    assert xr == pytest.approx(sw - VERT[0])
    # The fitted (height) dimension has no slack — always 0, never clamped off.
    assert yl == 0.0 and yr == 0.0


def test_cine_crop_shifts_the_vertical_axis():
    # 16:9 -> 2.39:1 (cine) crops the HEIGHT; focus y drives the offset.
    cine = (1920, 804)
    cy = reframe.center_offset(640, 360, *cine)[1]
    y_high = reframe.crop_offset(640, 360, *cine, (0.5, 0.1))[1]
    y_low = reframe.crop_offset(640, 360, *cine, (0.5, 0.9))[1]
    assert y_high < cy < y_low


def test_same_aspect_is_a_noop_even_with_a_focus():
    # 16:9 on a 16:9 canvas: no slack, so any focus lands on the centre (0,0).
    off = reframe.crop_offset(640, 360, 1280, 720, (0.1, 0.9))
    assert off == (0.0, 0.0)
    assert reframe.is_centered(640, 360, 1280, 720, (0.1, 0.9))


# ------------------------------------------------------------ is_centered gate


def test_is_centered_true_for_fallbacks_false_for_a_real_shift():
    assert reframe.is_centered(*SRC, *VERT, None)  # no signal
    assert reframe.is_centered(*SRC, *VERT, (0.5, 0.5))  # centred focus
    assert not reframe.is_centered(*SRC, *VERT, (0.2, 0.5))  # off-centre subject


def test_is_centered_true_when_only_the_fitted_axis_is_off_centre():
    # Vertical crop ignores focus y (height fits) — a y-only shift is still center.
    assert reframe.is_centered(*SRC, *VERT, (0.5, 0.1))


# ------------------------------------------------------------ average_focus


def test_average_focus_blends_both_points():
    assert reframe.average_focus((0.2, 0.4), (0.4, 0.8)) == pytest.approx((0.3, 0.6))


def test_average_focus_falls_back_to_the_one_present():
    assert reframe.average_focus((0.2, 0.4), None) == (0.2, 0.4)
    assert reframe.average_focus(None, (0.7, 0.1)) == (0.7, 0.1)
    assert reframe.average_focus(None, None) is None


# ------------------------------------------------------------ in-memory only


def _entry(**kw):
    base = dict(
        clip_path="a.mp4", source_start=0.0, source_end=2.0,
        record_start=0.0, record_end=2.0, score=1.0,
    )
    base.update(kw)
    return MontageEntry(**base)


def test_reframe_focus_is_excluded_from_plan_to_dict():
    plan = MontagePlan(music_path="", duration=2.0, entries=[_entry(reframe_focus=(0.2, 0.7))])
    d = plan_to_dict(plan)
    assert "reframe_focus" not in d["entries"][0]


def test_reframe_focus_does_not_change_the_serialized_bytes():
    plain = MontagePlan(music_path="", duration=2.0, entries=[_entry()])
    with_focus = MontagePlan(
        music_path="", duration=2.0, entries=[_entry(reframe_focus=(0.2, 0.7))]
    )
    assert json.dumps(plan_to_dict(plain), sort_keys=True) == json.dumps(
        plan_to_dict(with_focus), sort_keys=True
    )


# ------------------------------------------------------------ render fallback


@needs_render
def test_render_cover_string_is_byte_identical_without_a_focus(tmp_path):
    # No reframe_focus -> the export keeps the exact centre-crop filter string.
    from monteur import preview

    base = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=25"
    entry = _entry(reframe_focus=None)
    out = preview._reframe_cover(entry, base, 1080, 1920, 25.0, {})
    assert out == base


@needs_render
def test_render_cover_string_unchanged_for_a_centred_focus(tmp_path, monkeypatch):
    from monteur import preview

    class _Info:
        width, height = 640, 360

    monkeypatch.setattr(preview, "probe", lambda p: _Info())
    base = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=25"
    entry = _entry(reframe_focus=(0.5, 0.5))
    out = preview._reframe_cover(entry, base, 1080, 1920, 25.0, {})
    assert out == base  # centred focus -> byte-identical fallback


# ------------------------------------------------------------ real render


def _make_clip(path: Path, seconds: float = 1.0) -> str:
    """A 640x360 (16:9) clip: left third RED, middle GREEN, right third BLUE,
    with a faint tone so every export audio mode has a real stream to work
    with (pure silence makes loudnorm emit NaN)."""
    ff = find_ffmpeg()
    subprocess.run(
        [
            ff, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=green:s=640x360:d={seconds}:r=10",
            "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100",
            "-vf",
            "drawbox=x=0:y=0:w=213:h=360:color=red@1:t=fill,"
            "drawbox=x=427:y=0:w=213:h=360:color=blue@1:t=fill",
            "-t", f"{seconds}", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return str(path)


def _center_color(path: str, w: int, h: int):
    """The RGB of the rendered frame's centre pixel (native w x h decode)."""
    from monteur.media import extract_rgb_frame

    frame = extract_rgb_frame(path, 0.3, size=(w, h))
    return tuple(int(c) for c in frame[h // 2, w // 2])


def _dominant(rgb):
    r, g, b = rgb
    return "red" if r > g and r > b else "blue" if b > r and b > g else "green"


@needs_render
def test_center_crop_shows_the_middle_but_reframe_keeps_the_left_subject(tmp_path):
    """The whole point: a subject in the LEFT third is center-cropped away on a
    vertical canvas, but a left focus keeps it dead-centre in the 9:16 frame."""
    from monteur import preview

    clip = _make_clip(tmp_path / "thirds.mp4")
    w, h = 180, 320  # a tiny 9:16 canvas (fast)

    def one(focus):
        plan = MontagePlan(
            music_path="",
            duration=1.0,
            entries=[
                MontageEntry(
                    clip_path=clip, source_start=0.0, source_end=1.0,
                    record_start=0.0, record_end=1.0, score=1.0,
                    reframe_focus=focus,
                )
            ],
        )
        out = tmp_path / f"out_{focus}.mp4"
        preview.render_export(plan, str(out), size=(w, h), audio="original")
        return _dominant(_center_color(str(out), w, h))

    # Center-crop (no focus) lands on the middle GREEN band...
    assert one(None) == "green"
    # ...a LEFT focus pulls the RED subject into the centre of the vertical frame.
    assert one((0.12, 0.5)) == "red"
    # ...and a RIGHT focus brings the BLUE subject in.
    assert one((0.9, 0.5)) == "blue"
