"""Embedded start-timecode handling, end to end.

Real camera files (DJI action cams, most pro cameras) carry an embedded
time-of-day start timecode. DaVinci Resolve links imported media by checking
each asset's claimed [start, start + duration] source range against the real
file; a mismatch logs "Mismatch between specified target timecodes ... and
located file timecodes ..." -> "No overlap" and the clips are dropped.

These tests cover the whole chain: probe() reading the TC from ffmpeg's
stderr, start_timecode_seconds(), and the export invariant Resolve checks on
a full sift -> music -> montage -> FCPXML pipeline run.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from monteur.media import _TIMECODE_RE, MediaInfo, probe, start_timecode_seconds

try:
    import imageio_ffmpeg

    HAVE_FFMPEG = True
except ImportError:
    HAVE_FFMPEG = False

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="imageio_ffmpeg not installed")

TC = "01:47:52:08"
TC_SECONDS = 1 * 3600 + 47 * 60 + 52 + 8 / 25  # 6472.32 at 25 fps


def make_tc_clip(tmp_path, name="tc.mp4", timecode=TC, seconds=8, rate=25):
    """Encode a testsrc2 clip with an embedded start timecode."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = tmp_path / name
    cmd = [
        exe, "-y", "-f", "lavfi",
        "-i", f"testsrc2=size=320x180:rate={rate}:duration={seconds}",
    ]
    if timecode:
        cmd += ["-timecode", timecode]
    cmd += ["-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


# ------------------------------------------------------------- probe parsing


@needs_ffmpeg
def test_probe_reads_embedded_start_timecode(tmp_path):
    clip = make_tc_clip(tmp_path)
    info = probe(clip)
    assert info.start_timecode == TC
    assert start_timecode_seconds(info) == pytest.approx(TC_SECONDS)


@needs_ffmpeg
def test_probe_without_timecode_returns_empty(tmp_path):
    clip = make_tc_clip(tmp_path, name="plain.mp4", timecode="")
    info = probe(clip)
    assert info.start_timecode == ""
    assert start_timecode_seconds(info) == 0.0


def test_timecode_regex_matches_ffmpeg_metadata_lines():
    m = _TIMECODE_RE.search("      timecode        : 01:47:52:08\r\n")
    assert m is not None and m.group(1) == "01:47:52:08"
    # drop-frame separator before the frame field
    m = _TIMECODE_RE.search("timecode: 00:59:59;28")
    assert m is not None and m.group(1) == "00:59:59;28"
    # a Duration line must not be mistaken for a timecode
    assert _TIMECODE_RE.search("  Duration: 00:00:08.00, start: 0.000000") is None


def _info(tc: str, fps: float = 25.0) -> MediaInfo:
    return MediaInfo(
        path="x.mp4", duration=8.0, fps=fps, width=320, height=180,
        has_audio=False, start_timecode=tc,
    )


def test_start_timecode_seconds_guards():
    assert start_timecode_seconds(_info("")) == 0.0
    assert start_timecode_seconds(_info(TC, fps=0.0)) == 0.0
    assert start_timecode_seconds(_info(TC, fps=-1.0)) == 0.0
    assert start_timecode_seconds(_info("99:99:99:99")) == 0.0  # unparseable
    assert start_timecode_seconds(_info(TC)) == pytest.approx(TC_SECONDS)


# ---------------------------------------------------- end-to-end (Resolve's
# import invariant: every asset's claimed source range matches its real file)


@needs_ffmpeg
def test_end_to_end_fcpxml_assets_match_real_file_timecodes(tmp_path):
    from monteur.io.fcpxml import _parse_rational, write_fcpxml
    from monteur.montage import montage_to_timeline, plan_montage
    from monteur.music import analyze_music
    from monteur.sift import sift_directory

    # cam_a is short (2s) so the montage must reach into cam_b as well.
    make_tc_clip(tmp_path, "cam_a.mp4", "01:47:52:08", seconds=2)
    make_tc_clip(tmp_path, "cam_b.mp4", "02:10:00:00", seconds=8)
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    song = tmp_path / "song.wav"
    subprocess.run(
        [exe, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=10", str(song)],
        check=True, capture_output=True,
    )

    reports = sift_directory(str(tmp_path))
    assert [Path(r.path).name for r in reports] == ["cam_a.mp4", "cam_b.mp4"]
    assert reports[0].media_start == pytest.approx(TC_SECONDS)
    assert reports[1].media_start == pytest.approx(2 * 3600 + 10 * 60)

    plan = plan_montage(reports, analyze_music(str(song)))
    timeline = montage_to_timeline(plan, fps=25.0)
    xml = write_fcpxml(timeline)
    root = ET.fromstring(xml)

    # Every asset's claimed [start, start + duration] must correspond to its
    # file's REAL timecode range — exactly what Resolve verifies on import.
    assets: dict[str, tuple[float, float]] = {}
    for asset in root.iter("asset"):
        src = asset.get("src", "")
        assert src.startswith("file://")
        info = probe(Path(unquote(urlparse(src).path)))
        start = float(_parse_rational(asset.get("start", "0s")))
        duration = float(_parse_rational(asset.get("duration", "0s")))
        assert start == pytest.approx(start_timecode_seconds(info), abs=0.05)
        assert duration == pytest.approx(info.duration, abs=0.25)
        assets[asset.get("id")] = (start, duration)
    assert sorted(a.get("name") for a in root.iter("asset")) == [
        "cam_a", "cam_b", "song",
    ]

    # ...and every asset-clip's source position lies WITHIN its asset's range.
    clip_count = 0
    for el in root.iter("asset-clip"):
        a_start, a_dur = assets[el.get("ref")]
        el_start = float(_parse_rational(el.get("start", "0s")))
        assert a_start - 1e-6 <= el_start <= a_start + a_dur + 1e-6
        clip_count += 1
    assert clip_count >= 3  # both cameras plus the music bed
