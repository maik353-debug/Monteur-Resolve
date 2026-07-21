"""Tests for the offline time-of-day classification (monteur.daylight).

classify_frame is pure math over synthetic RGB arrays; annotate_reports is
exercised through a monkeypatched classify_moment (no ffmpeg needed); one
end-to-end test renders tiny lavfi clips through the real pipeline.
"""

from __future__ import annotations

import json
import subprocess

import pytest

np = pytest.importorskip("numpy")

import monteur.daylight as daylight
from monteur.daylight import (
    CACHE_FILENAME,
    DAYLIGHT_CLASSES,
    annotate_reports,
    classify_frame,
    classify_moment,
)
from monteur.sift import ClipReport, Moment


def flat(r: int, g: int, b: int, shape=(36, 64)) -> "np.ndarray":
    frame = np.zeros((*shape, 3), dtype=np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = r, g, b
    return frame


# --- classify_frame on a synthetic accuracy set ---------------------------------


def _night_with_lamp() -> "np.ndarray":
    """A dark street with a warm lamp: bright local spot, low mean luma."""
    frame = flat(8, 8, 12)
    frame[:8, :8] = (220, 170, 90)  # the lamp: ~2.8% of the pixels
    return frame


def _textured_day() -> "np.ndarray":
    """A bright neutral frame with real texture (not a flat card)."""
    ramp = np.linspace(120, 220, 64, dtype=np.uint8)
    frame = np.zeros((36, 64, 3), dtype=np.uint8)
    frame[..., 0] = ramp
    frame[..., 1] = ramp
    frame[..., 2] = np.clip(ramp.astype(int) + 10, 0, 255).astype(np.uint8)
    return frame


SYNTHETIC_SET = [
    # (frame, expected label)
    (flat(180, 180, 185), "day"),        # bright neutral midday
    (flat(140, 145, 150), "day"),        # overcast, slightly cool
    (flat(120, 160, 220), "day"),        # blue-sky bright, clearly cool
    (flat(220, 200, 160), "day"),        # warm but VERY bright = noon sun, not golden
    (_textured_day(), "day"),
    (flat(200, 140, 60), "golden"),      # warm mid-bright low sun
    (flat(150, 100, 50), "golden"),      # dim warm golden hour
    (flat(10, 10, 20), "night"),         # deep night
    (flat(40, 35, 30), "night"),         # dark dusk remnant
    (_night_with_lamp(), "night"),       # artificial light does not flip night
]


def test_synthetic_set_classifies_correctly():
    for frame, expected in SYNTHETIC_SET:
        result = classify_frame(frame)
        assert result["label"] == expected, (
            f"expected {expected}, got {result!r}"
        )
        assert result["label"] in DAYLIGHT_CLASSES


def test_confidence_is_bounded_and_monotone():
    for frame, _expected in SYNTHETIC_SET:
        conf = classify_frame(frame)["confidence"]
        assert 0.5 <= conf <= 1.0
    # Deeper night is more confidently night than a borderline dark frame.
    deep = classify_frame(flat(5, 5, 8))
    border = classify_frame(flat(52, 50, 48))
    assert deep["label"] == border["label"] == "night"
    assert deep["confidence"] > border["confidence"]
    # A strongly warm mid-bright frame beats a barely-warm one.
    strong = classify_frame(flat(190, 130, 55))
    weak = classify_frame(flat(140, 125, 118))
    assert strong["label"] == "golden"
    assert strong["confidence"] > 0.5


def test_classify_frame_reports_measurements():
    result = classify_frame(flat(200, 140, 60))
    assert result["warmth"] == pytest.approx(140.0)
    assert result["saturation"] == pytest.approx(140.0)
    assert 100 < result["brightness"] < 170


def test_gray_warm_cast_is_not_golden():
    # A mild warm cast without saturation is white-balance drift, not
    # golden hour (the saturation gate).
    result = classify_frame(flat(120, 112, 100))
    assert result["label"] == "day"


# --- classify_moment end to end (real ffmpeg, tiny lavfi clips) -----------------


def _make_clip(path, color: str) -> bool:
    from monteur.media import find_ffmpeg

    cmd = [
        find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s=64x36:d=1:r=10",
        "-pix_fmt", "yuv420p", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and path.is_file()


def test_classify_moment_on_rendered_clips(tmp_path):
    pytest.importorskip("imageio_ffmpeg")
    cases = [
        ("day.mp4", "0xB4B4B8", "day"),
        ("golden.mp4", "0xC88C3C", "golden"),
        ("night.mp4", "0x0A0A14", "night"),
    ]
    for name, color, expected in cases:
        clip = tmp_path / name
        if not _make_clip(clip, color):
            pytest.skip("ffmpeg cannot render lavfi test clips here")
        result = classify_moment(str(clip), 0.5)
        assert result["label"] == expected
        assert 0.5 <= result["confidence"] <= 1.0


# --- annotate_reports ------------------------------------------------------------


def make_reports() -> list[ClipReport]:
    a = ClipReport(
        path="/footage/a.mp4", duration=20.0,
        moments=[Moment(0.0, 2.0, 0.9), Moment(4.0, 6.0, 0.8)],
    )
    b = ClipReport(
        path="/footage/b.mp4", duration=20.0,
        moments=[Moment(1.0, 3.0, 0.7)],
    )
    return [a, b]


def _fake_classify(label_by_path: dict, calls: list | None = None):
    def fake(path, t):
        if calls is not None:
            calls.append((path, t))
        label = label_by_path[path]
        if isinstance(label, Exception):
            raise label
        return {
            "label": label, "confidence": 0.9,
            "brightness": 100.0, "warmth": 0.0, "saturation": 10.0,
        }

    return fake


def test_annotate_fills_daylight_at_the_midpoint(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(
        daylight, "classify_moment",
        _fake_classify({"/footage/a.mp4": "day", "/footage/b.mp4": "night"}, calls),
    )
    reports = make_reports()
    notes = annotate_reports(reports, cache_path=tmp_path / CACHE_FILENAME)
    assert [m.daylight for m in reports[0].moments] == ["day", "day"]
    assert [m.daylight for m in reports[1].moments] == ["night"]
    # Sampled at each moment's midpoint.
    assert [t for _p, t in calls] == [1.0, 5.0, 2.0]
    assert any("3 of 3 moments classified" in n for n in notes)
    assert any("2 day" in n and "1 night" in n for n in notes)


def test_annotate_cache_round_trip(monkeypatch, tmp_path):
    cache_path = tmp_path / CACHE_FILENAME
    monkeypatch.setattr(
        daylight, "classify_moment",
        _fake_classify({"/footage/a.mp4": "golden", "/footage/b.mp4": "night"}),
    )
    annotate_reports(make_reports(), cache_path=cache_path)
    assert cache_path.is_file()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert all(entry["label"] in DAYLIGHT_CLASSES for entry in data.values())

    # A second run must be served entirely from the cache: classification
    # itself now raises, yet every field still fills.
    monkeypatch.setattr(
        daylight, "classify_moment",
        _fake_classify({
            "/footage/a.mp4": RuntimeError("no ffmpeg"),
            "/footage/b.mp4": RuntimeError("no ffmpeg"),
        }),
    )
    fresh = make_reports()
    notes = annotate_reports(fresh, cache_path=cache_path)
    assert [m.daylight for m in fresh[0].moments] == ["golden", "golden"]
    assert fresh[1].moments[0].daylight == "night"
    assert any("3 from cache" in n for n in notes)


def test_annotate_survives_a_failing_clip(monkeypatch, tmp_path):
    monkeypatch.setattr(
        daylight, "classify_moment",
        _fake_classify({
            "/footage/a.mp4": RuntimeError("boom"),
            "/footage/b.mp4": "day",
        }),
    )
    reports = make_reports()
    notes = annotate_reports(reports, cache_path=tmp_path / CACHE_FILENAME)
    # The broken clip's moments stay empty; the healthy clip still fills.
    assert [m.daylight for m in reports[0].moments] == ["", ""]
    assert reports[1].moments[0].daylight == "day"
    assert any("a.mp4" in n and "skipped" in n for n in notes)


def test_annotate_tolerates_a_corrupt_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / CACHE_FILENAME
    cache_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        daylight, "classify_moment",
        _fake_classify({"/footage/a.mp4": "day", "/footage/b.mp4": "day"}),
    )
    reports = make_reports()
    annotate_reports(reports, cache_path=cache_path)
    assert all(m.daylight == "day" for r in reports for m in r.moments)


def test_annotate_ignores_invalid_cache_entries(monkeypatch, tmp_path):
    cache_path = tmp_path / CACHE_FILENAME
    monkeypatch.setattr(
        daylight, "classify_moment",
        _fake_classify({"/footage/a.mp4": "night", "/footage/b.mp4": "night"}),
    )
    reports = make_reports()
    key = daylight._moment_key("/footage/a.mp4", 0.0, 2.0)
    cache_path.write_text(
        json.dumps({key: {"label": "noon"}}), encoding="utf-8"
    )  # unknown label: must be re-classified, not applied
    annotate_reports(reports, cache_path=cache_path)
    assert reports[0].moments[0].daylight == "night"


def test_annotate_with_no_moments():
    report = ClipReport(path="/footage/empty.mp4", duration=3.0)
    assert annotate_reports([report]) == ["no moments to classify"]


def test_progress_callback_and_broken_callback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        daylight, "classify_moment",
        _fake_classify({"/footage/a.mp4": "day", "/footage/b.mp4": "day"}),
    )
    events: list = []

    def progress(index, total, name, stage):
        events.append((index, total, name, stage))

    annotate_reports(
        make_reports(), progress=progress, cache_path=tmp_path / CACHE_FILENAME
    )
    assert len(events) == 3
    assert all(stage == "frame" for *_rest, stage in events)
    assert events[0][:2] == (1, 3)

    def boom(index, total, name, stage):
        raise RuntimeError("broken UI")

    reports = make_reports()
    annotate_reports(reports, progress=boom, cache_path=tmp_path / "other.json")
    assert all(m.daylight == "day" for r in reports for m in r.moments)


# --- the scan wiring (sift_directory calls the daylight pass) --------------------


def test_sift_directory_runs_daylight_pass(monkeypatch):
    import monteur.sift as sift_module

    monkeypatch.setattr(
        sift_module, "list_media",
        lambda directory: ["footage/x.mp4", "footage/y.mp4"],
    )
    monkeypatch.setattr(
        sift_module, "analyze_clip",
        lambda path: ClipReport(
            path=str(path), duration=6.0, moments=[Moment(0.0, 1.0, 0.5)]
        ),
    )
    seen: dict = {}

    def fake_annotate(reports, **kwargs):
        seen["reports"] = reports
        for report in reports:
            for m in report.moments:
                m.daylight = "day"
        return ["daylight: stub"]

    monkeypatch.setattr(daylight, "annotate_reports", fake_annotate)
    reports = sift_module.sift_directory("footage")
    assert seen["reports"] is reports or [r.path for r in seen["reports"]] == [
        r.path for r in reports
    ]
    assert all(m.daylight == "day" for r in reports for m in r.moments)


def test_sift_directory_survives_daylight_failure(monkeypatch):
    import monteur.sift as sift_module

    monkeypatch.setattr(
        sift_module, "list_media", lambda directory: ["footage/x.mp4"]
    )
    monkeypatch.setattr(
        sift_module, "analyze_clip",
        lambda path: ClipReport(
            path=str(path), duration=6.0, moments=[Moment(0.0, 1.0, 0.5)]
        ),
    )

    def broken(reports, **kwargs):
        raise RuntimeError("daylight exploded")

    monkeypatch.setattr(daylight, "annotate_reports", broken)
    reports = sift_module.sift_directory("footage")
    assert len(reports) == 1  # the scan itself must never fail
