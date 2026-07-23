"""Tests for monteur/vision.py — Claude vision annotation of sifted moments.

The Claude client is always faked (there is no ANTHROPIC_API_KEY in CI):
tests monkeypatch ``monteur.vision._client``, the single seam through which
the module reaches the API. Frame extraction is mocked for the logic tests
and exercised for real (ffmpeg on a generated clip) in the integration
tests at the bottom.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import monteur.vision as vision
from monteur.sift import ClipReport, Moment
from monteur.vision import DEFAULT_VISION_MODEL, MonteurVisionError, analyze_reports

try:
    import imageio_ffmpeg

    HAVE_FFMPEG = True
except ImportError:
    HAVE_FFMPEG = False

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="imageio_ffmpeg not installed")

FAKE_JPEG = b"\xff\xd8fake-jpeg-bytes"


# --------------------------------------------------------------- test helpers


def make_report(tmp_path, name, moments):
    """A ClipReport over a real (dummy) file so mtime-based cache keys work."""
    clip = tmp_path / name
    if not clip.exists():
        clip.write_bytes(b"\x00" * 64)
    return ClipReport(path=str(clip), duration=60.0, moments=moments)


def make_moment(start, score):
    return Moment(start=start, end=start + 1.0, score=score)


def default_entry(n, text):
    """Annotation echoing the moment header, so tests can verify the mapping."""
    return {
        "index": n,
        "label": f"seen {text}",
        "tags": ["road", "curve"],
        "role": "build",
        "hero": 0.5,
        "group": "ride",
    }


class FakeClient:
    """Answers vision batches from the 'Moment N: ...' text blocks it is shown.

    Records every request in ``calls``; ``entry_factory(n, text)`` produces
    the annotation for moment N (return None to drop a moment from the
    reply). ``fail_after`` raises on every call past that count, simulating
    a mid-run API failure.
    """

    def __init__(self, entry_factory=default_entry, fail_after=None):
        self.calls = []
        self.entry_factory = entry_factory
        self.fail_after = fail_after
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("simulated API failure")
        entries = []
        for block in kwargs["messages"][0]["content"]:
            if block.get("type") == "text" and block["text"].startswith("Moment "):
                n = int(block["text"].split(":", 1)[0].split()[1])
                entry = self.entry_factory(n, block["text"])
                if entry is not None:
                    entries.append(entry)
        text_block = SimpleNamespace(type="text", text=json.dumps({"moments": entries}))
        return SimpleNamespace(content=[text_block], stop_reason="end_turn")


@pytest.fixture
def fake_frames(monkeypatch):
    """Replace ffmpeg keyframe extraction with canned JPEG bytes."""
    monkeypatch.setattr(vision, "_extract_frame", lambda path, t, height: FAKE_JPEG)


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    monkeypatch.delenv("MONTEUR_VISION_MODEL", raising=False)


def use_client(monkeypatch, client):
    monkeypatch.setattr(vision, "_client", lambda: client)
    return client


# ----------------------------------------------------------------- annotation


def test_annotation_lands_on_the_right_moments(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4", [make_moment(10.0, 0.9), make_moment(30.0, 0.5)])
    b = make_report(tmp_path, "b.mp4", [make_moment(5.0, 0.8)])
    client = use_client(monkeypatch, FakeClient())

    notes = analyze_reports([a, b])

    # Each moment got the annotation minted for ITS header line.
    assert a.moments[0].label.startswith("seen Moment 1: a.mp4 at 00:10")  # mid 10.5s
    assert a.moments[1].label.startswith("seen Moment 2: a.mp4 at 00:30")
    assert b.moments[0].label.startswith("seen Moment 3: b.mp4 at 00:05")
    for moment in (*a.moments, *b.moments):
        assert moment.tags == ["road", "curve"]
        assert moment.role == "build"
        assert moment.hero == 0.5
        assert moment.group == "ride"
    assert notes[0] == "3 moments analyzed, 0 from cache"

    # Request shape follows the module contract (brief.py conventions).
    kwargs = client.calls[0]
    assert kwargs["model"] == DEFAULT_VISION_MODEL
    assert kwargs["max_tokens"] == 2000
    assert "editor" in kwargs["system"]
    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    items = fmt["schema"]["properties"]["moments"]["items"]
    assert set(items["required"]) == {"index", "label", "tags", "role", "hero", "group"}
    # Content: per moment a text header followed by its consecutive frames
    # (start/middle/end), so the model reads motion, not one still.
    content = kwargs["messages"][0]["content"]
    headers = [i for i, blk in enumerate(content)
               if blk["type"] == "text" and blk["text"].startswith("Moment ")]
    assert len(headers) == 3
    for i in headers:
        # the three blocks after each header are its frames
        for k in range(1, vision._FRAMES_PER_MOMENT + 1):
            image = content[i + k]
            assert image["type"] == "image"
            assert image["source"]["media_type"] == "image/jpeg"
            assert base64.standard_b64decode(image["source"]["data"]) == FAKE_JPEG
    total_images = sum(1 for blk in content if blk["type"] == "image")
    assert total_images == 3 * vision._FRAMES_PER_MOMENT


def test_model_env_override_and_explicit_arg(tmp_path, monkeypatch, fake_frames):
    report = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9)])
    client = use_client(monkeypatch, FakeClient())
    monkeypatch.setenv("MONTEUR_VISION_MODEL", "claude-env-model")
    analyze_reports([report])
    assert client.calls[0]["model"] == "claude-env-model"

    # An explicit argument beats the environment.
    report2 = make_report(tmp_path, "a.mp4", [make_moment(2.0, 0.9)])
    analyze_reports([report2], model="claude-explicit")
    assert client.calls[-1]["model"] == "claude-explicit"


def test_no_moments_returns_calm_note(tmp_path, monkeypatch):
    report = make_report(tmp_path, "a.mp4", [])
    monkeypatch.setattr(
        vision, "_client", lambda: pytest.fail("no API call expected")
    )
    assert analyze_reports([report]) == ["no moments to analyze"]
    assert analyze_reports([]) == ["no moments to analyze"]


# ---------------------------------------------------------------------- cache


def test_cache_written_then_second_run_is_all_cache(tmp_path, monkeypatch, fake_frames):
    moments = [make_moment(10.0, 0.9), make_moment(5.0, 0.8)]
    a = make_report(tmp_path, "a.mp4", moments[:1])
    b = make_report(tmp_path, "b.mp4", moments[1:])
    client = use_client(monkeypatch, FakeClient())
    analyze_reports([a, b])
    assert len(client.calls) == 1

    # Cache landed next to the footage (folder of the first report's path).
    cache_file = tmp_path / ".monteur-vision.json"
    assert cache_file.exists()
    cache = json.loads(cache_file.read_text())
    assert len(cache) == 2
    for key in cache:
        assert DEFAULT_VISION_MODEL in key
        assert str(tmp_path) in key

    # Second run over fresh Moment objects: everything from cache, ZERO API
    # calls — the client factory must not even be invoked.
    a2 = make_report(tmp_path, "a.mp4", [make_moment(10.0, 0.9)])
    b2 = make_report(tmp_path, "b.mp4", [make_moment(5.0, 0.8)])
    monkeypatch.setattr(
        vision, "_client", lambda: pytest.fail("all-cache run must not build a client")
    )
    stages = []
    notes = analyze_reports(
        [a2, b2], progress=lambda i, t, name, stage: stages.append(stage)
    )
    assert notes[0] == "2 moments analyzed, 2 from cache"
    assert stages == ["cache", "cache"]
    assert a2.moments[0].label.startswith("seen Moment")
    assert b2.moments[0].tags == ["road", "curve"]


def test_mtime_change_invalidates_cache(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4", [make_moment(10.0, 0.9)])
    use_client(monkeypatch, FakeClient())
    analyze_reports([a])

    # Touch the file: same path, same window — but the footage "changed".
    mtime = os.path.getmtime(a.path)
    os.utime(a.path, (mtime + 10, mtime + 10))

    a2 = make_report(tmp_path, "a.mp4", [make_moment(10.0, 0.9)])
    client2 = use_client(monkeypatch, FakeClient())
    notes = analyze_reports([a2])
    assert len(client2.calls) == 1  # cache miss -> real request
    assert notes[0] == "1 moments analyzed, 0 from cache"


def test_interrupted_run_keeps_batch_progress(tmp_path, monkeypatch, fake_frames):
    # 10 moments = 2 batches; the API dies on batch 2. Batch 1 must be cached.
    first = vision._BATCH_SIZE
    moments = [make_moment(float(i * 2), 1.0 - i * 0.05) for i in range(10)]
    a = make_report(tmp_path, "a.mp4", moments)
    use_client(monkeypatch, FakeClient(fail_after=1))
    with pytest.raises(MonteurVisionError, match="simulated API failure"):
        analyze_reports([a])
    cache = json.loads((tmp_path / ".monteur-vision.json").read_text())
    assert len(cache) == first  # the successful first batch survived

    # Resuming only pays for the missing moments.
    a2 = make_report(tmp_path, "a.mp4", [make_moment(float(i * 2), 1.0 - i * 0.05) for i in range(10)])
    client2 = use_client(monkeypatch, FakeClient())
    notes = analyze_reports([a2])
    assert notes[0] == f"10 moments analyzed, {first} from cache"
    assert len(client2.calls) == 1


def test_near_duplicate_moments_are_deduped_from_selection():
    # a clip with three moments, two of them near-identical (same phash) plus
    # one distinct — the selection keeps the clip's best + the DISTINCT one,
    # never the near-duplicate, so the budget is not spent twice on one shot
    dup_a = Moment(start=0.0, end=1.0, score=0.9, phash=0b1010101010101010)
    dup_b = Moment(start=5.0, end=6.0, score=0.85, phash=0b1010101010101011)  # 1 bit off
    distinct = Moment(start=10.0, end=11.0, score=0.8, phash=0b0101010101010101)
    report = ClipReport(path="/x/a.mp4", duration=60.0,
                        moments=[dup_a, dup_b, distinct])
    picked = vision._select_moments([report], max_moments=3)
    starts = sorted(m.start for _r, m in picked)
    assert 0.0 in starts        # the clip's best (reserved)
    assert 10.0 in starts       # the distinct moment
    assert 5.0 not in starts    # the near-duplicate of the best is dropped


def test_moment_frame_times_sample_across_the_window():
    # a normal window yields _FRAMES_PER_MOMENT distinct, ordered times inside it
    m = Moment(start=10.0, end=11.0, score=0.9)
    times = vision._moment_frame_times(m, vision._FRAMES_PER_MOMENT)
    assert len(times) == vision._FRAMES_PER_MOMENT
    assert times == sorted(times)
    assert all(10.0 <= t <= 11.0 for t in times)
    # a sub-second window collapses to a single midpoint (no duplicate frames)
    tiny = Moment(start=5.0, end=5.02, score=0.5)
    assert vision._moment_frame_times(tiny, 3) == [pytest.approx(5.01)]


def test_extract_moment_frames_survives_partial_failure(tmp_path, monkeypatch):
    # if some frames fail but at least one lands, the moment is still described
    calls = {"n": 0}

    def flaky(path, t, height):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("one bad seek")
        return FAKE_JPEG

    monkeypatch.setattr(vision, "_extract_frame", flaky)
    m = Moment(start=0.0, end=1.0, score=0.9)
    frames = vision._extract_moment_frames("x.mp4", m, 360)
    assert frames == [FAKE_JPEG, FAKE_JPEG]  # 3 attempted, 1 failed, 2 kept

    # all frames failing raises (the caller then skips the clip)
    monkeypatch.setattr(vision, "_extract_frame",
                        lambda p, t, h: (_ for _ in ()).throw(RuntimeError("dead")))
    with pytest.raises(MonteurVisionError):
        vision._extract_moment_frames("x.mp4", m, 360)


def test_explicit_cache_path_is_used(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9)])
    use_client(monkeypatch, FakeClient())
    custom = tmp_path / "elsewhere" / "vision.json"
    analyze_reports([a], cache_path=custom)
    assert custom.exists()
    assert not (tmp_path / ".monteur-vision.json").exists()


# ------------------------------------------------------------------ selection


def test_max_moments_caps_but_keeps_one_per_clip(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4",
                    [make_moment(0.0, 0.9), make_moment(10.0, 0.8), make_moment(20.0, 0.7)])
    b = make_report(tmp_path, "b.mp4", [make_moment(0.0, 0.6), make_moment(10.0, 0.5)])
    c = make_report(tmp_path, "c.mp4", [make_moment(0.0, 0.4), make_moment(10.0, 0.3)])
    client = use_client(monkeypatch, FakeClient())

    notes = analyze_reports([a, b, c], max_moments=4)

    annotated = [m for r in (a, b, c) for m in r.moments if m.label]
    assert len(annotated) == 4
    # Every clip's best moment made the cut, even lowly c (score 0.4)...
    assert a.moments[0].label and b.moments[0].label and c.moments[0].label
    # ...and the one spare slot went to the best remaining moment (a @ 0.8).
    assert a.moments[1].label
    assert not a.moments[2].label and not b.moments[1].label and not c.moments[1].label
    assert "selected the best 4 of 7 moments (cost cap)" in notes
    # Cost control on the wire too: exactly one batch of 4 moments, each
    # carrying its consecutive frames.
    assert len(client.calls) == 1
    images = [blk for blk in client.calls[0]["messages"][0]["content"]
              if blk["type"] == "image"]
    assert len(images) == 4 * vision._FRAMES_PER_MOMENT


def test_batches_by_batch_size(tmp_path, monkeypatch, fake_frames):
    moments = [make_moment(float(i * 2), 1.0 - i * 0.05) for i in range(10)]
    a = make_report(tmp_path, "a.mp4", moments)
    client = use_client(monkeypatch, FakeClient())
    analyze_reports([a])
    # 10 moments, _BATCH_SIZE per request -> two batches
    import math
    assert len(client.calls) == math.ceil(10 / vision._BATCH_SIZE)
    per_call_moments = [
        sum(1 for blk in call["messages"][0]["content"]
            if blk["type"] == "text" and blk["text"].startswith("Moment "))
        for call in client.calls
    ]
    assert per_call_moments == [vision._BATCH_SIZE, 10 - vision._BATCH_SIZE]
    # each moment carries its frames
    per_call_images = [
        sum(1 for blk in call["messages"][0]["content"] if blk["type"] == "image")
        for call in client.calls
    ]
    assert per_call_images == [
        vision._BATCH_SIZE * vision._FRAMES_PER_MOMENT,
        (10 - vision._BATCH_SIZE) * vision._FRAMES_PER_MOMENT,
    ]


# ----------------------------------------------------------------- validation


def test_malformed_model_output_is_clamped(tmp_path, monkeypatch, fake_frames):
    def messy_entry(n, text):
        if n == 1:
            return {
                "index": n,
                "label": "  epic\n mountain   pass ",
                "tags": ["Sky", "MOUNTAINS", "sky", "a", "b", "c", "d"],
                "role": "epic",   # not a known role
                "hero": 3.5,       # above range
                "group": "ALPINE Pass",
            }
        return {"index": n, "label": 42, "tags": "curve", "role": "closer",
                "hero": "very", "group": ""}

    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9), make_moment(5.0, 0.8)])
    use_client(monkeypatch, FakeClient(entry_factory=messy_entry))
    analyze_reports([a])

    first, second = a.moments
    assert first.label == "epic mountain pass"          # one line, squeezed
    assert first.tags == ["sky", "mountains", "a", "b", "c"]  # lowercase, deduped, max 5
    assert first.role == ""                             # unknown role -> ""
    assert first.hero == 1.0                            # clamped into 0..1
    assert first.group == "alpine pass"
    assert second.label == "42"                         # stringified defensively
    assert second.tags == []                            # non-list tags dropped
    assert second.role == "closer"
    assert second.hero == 0.0                           # non-numeric -> 0


def test_negative_hero_clamps_to_zero(tmp_path, monkeypatch, fake_frames):
    def entry(n, text):
        return {"index": n, "label": "x", "tags": [], "role": "", "hero": -0.4,
                "group": "g"}

    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9)])
    use_client(monkeypatch, FakeClient(entry_factory=entry))
    analyze_reports([a])
    assert a.moments[0].hero == 0.0


def test_dropped_index_leaves_moment_unannotated(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9), make_moment(5.0, 0.8)])
    use_client(
        monkeypatch,
        FakeClient(entry_factory=lambda n, text: default_entry(n, text) if n == 1 else None),
    )
    notes = analyze_reports([a])
    assert a.moments[0].label
    assert a.moments[1].label == ""  # the model forgot it; no crash, no cache
    assert notes[0] == "1 moments analyzed, 0 from cache"


# --------------------------------------------------------------- failure paths


def test_missing_credentials_raise_vision_error(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9)])

    def no_client():
        raise MonteurVisionError(
            "could not create the Claude client — set ANTHROPIC_API_KEY"
        )

    monkeypatch.setattr(vision, "_client", no_client)
    with pytest.raises(MonteurVisionError, match="ANTHROPIC_API_KEY"):
        analyze_reports([a])


def test_client_without_anthropic_package_raises():
    with mock.patch.dict(sys.modules, {"anthropic": None}):
        with pytest.raises(MonteurVisionError, match=r"monteur\[ai\]"):
            vision._client()


def test_frame_extraction_failure_is_note_not_crash(tmp_path, monkeypatch):
    def flaky_extract(path, t, height):
        if "b.mp4" in path:
            raise RuntimeError("broken stream")
        return FAKE_JPEG

    monkeypatch.setattr(vision, "_extract_frame", flaky_extract)
    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9)])
    b = make_report(tmp_path, "b.mp4", [make_moment(1.0, 0.8), make_moment(5.0, 0.7)])
    use_client(monkeypatch, FakeClient())

    notes = analyze_reports([a, b])

    assert a.moments[0].label            # the healthy clip still got annotated
    assert not b.moments[0].label and not b.moments[1].label
    assert notes[0] == "1 moments analyzed, 0 from cache"
    failure_notes = [n for n in notes if "b.mp4" in n]
    assert len(failure_notes) == 1       # the clip failed ONCE, not per moment
    assert "skipped" in failure_notes[0]


def test_broken_progress_callback_is_swallowed(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9)])
    use_client(monkeypatch, FakeClient())

    def broken_progress(index, total, name, stage):
        raise RuntimeError("UI went away")

    notes = analyze_reports([a], progress=broken_progress)
    assert notes[0] == "1 moments analyzed, 0 from cache"


def test_progress_stages_frames_then_vision(tmp_path, monkeypatch, fake_frames):
    a = make_report(tmp_path, "a.mp4", [make_moment(1.0, 0.9)])
    b = make_report(tmp_path, "b.mp4", [make_moment(2.0, 0.8)])
    use_client(monkeypatch, FakeClient())
    events = []
    analyze_reports([a, b], progress=lambda *args: events.append(args))
    assert events == [
        (1, 2, "a.mp4", "frames"),
        (2, 2, "b.mp4", "frames"),
        (1, 2, "a.mp4", "vision"),
        (2, 2, "b.mp4", "vision"),
    ]


# ------------------------------------------------------ real ffmpeg integration


def make_test_clip(tmp_path, seconds=2, name="clip.mp4"):
    """Encode a small testsrc2 clip (same pattern as tests/test_media_motion)."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = tmp_path / name
    subprocess.run(
        [exe, "-y", "-f", "lavfi",
         "-i", f"testsrc2=duration={seconds}:size=320x180:rate=30",
         "-pix_fmt", "yuv420p", str(out)],
        check=True,
        capture_output=True,
    )
    return out


@needs_ffmpeg
def test_extract_frame_real_ffmpeg(tmp_path):
    clip = make_test_clip(tmp_path)
    jpeg = vision._extract_frame(str(clip), 1.0, 120)
    assert jpeg[:2] == b"\xff\xd8"  # JPEG start-of-image marker
    assert len(jpeg) > 500          # a real image, not an empty pipe


@needs_ffmpeg
def test_extract_frame_real_ffmpeg_bad_file_raises(tmp_path):
    bad = tmp_path / "not-a-video.mp4"
    bad.write_bytes(b"garbage")
    with pytest.raises(MonteurVisionError, match="could not extract a frame"):
        vision._extract_frame(str(bad), 0.5, 120)


@needs_ffmpeg
def test_end_to_end_real_frames_fake_client(tmp_path, monkeypatch):
    clip = make_test_clip(tmp_path)
    report = ClipReport(path=str(clip), duration=2.0, moments=[make_moment(0.5, 0.9)])
    client = use_client(monkeypatch, FakeClient())

    notes = analyze_reports([report], frame_height=120)

    assert notes[0] == "1 moments analyzed, 0 from cache"
    assert report.moments[0].label.startswith("seen Moment 1: clip.mp4 at 00:01")
    image = next(blk for blk in client.calls[0]["messages"][0]["content"]
                 if blk["type"] == "image")
    jpeg = base64.standard_b64decode(image["source"]["data"])
    assert jpeg[:2] == b"\xff\xd8"  # a REAL extracted frame went over the wire
