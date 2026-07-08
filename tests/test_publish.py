"""Tests for the publish kit (monteur.publish)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monteur.montage import MontageEntry, MontagePlan
from monteur.publish import (
    Chapter,
    _collect_tags,
    extract_thumbnails,
    plan_chapters,
    publish_kit,
)
from monteur.sift import ClipReport, Moment

DEMO = Path(
    "/tmp/claude-0/-home-user-Fable-tool/"
    "90401078-872b-52b4-9d55-214193ea4ea5/scratchpad/demo-footage"
)


def entry(clip, s, e, rs, re_, score=0.5, label=""):
    return MontageEntry(
        clip_path=clip, source_start=s, source_end=e,
        record_start=rs, record_end=re_, score=score, label=label,
    )


def sem_moment(start, end, score, **fields):
    m = Moment(start, end, score)
    for key, value in fields.items():
        setattr(m, key, value)
    return m


def make_plan_and_reports():
    """3 scenes over 36s: pass (2 takes), tunnel, valley — labeled."""
    a = ClipReport(
        path="/f/a.mp4", duration=60.0,
        moments=[
            sem_moment(0.0, 10.0, 0.9, label="climbing the alpine pass",
                       tags=["pass", "curve"], hero=0.9, group="pass"),
            sem_moment(20.0, 30.0, 0.7, label="tunnel run",
                       tags=["tunnel"], hero=0.2, group="tunnel"),
        ],
    )
    b = ClipReport(
        path="/f/b.mp4", duration=40.0,
        moments=[
            sem_moment(5.0, 15.0, 0.8, label="valley sweep",
                       tags=["valley", "curve"], hero=0.5, group="valley"),
        ],
    )
    plan = MontagePlan(music_path="/m/song.wav", duration=36.0)
    plan.entries = [
        entry("/f/a.mp4", 0.0, 6.0, 0.0, 6.0, 0.9, label="climbing the alpine pass"),
        entry("/f/a.mp4", 6.0, 10.0, 6.0, 10.0, 0.9, label="climbing the alpine pass"),
        entry("/f/a.mp4", 20.0, 28.0, 10.0, 20.0, 0.7, label="tunnel run"),
        entry("/f/b.mp4", 5.0, 13.0, 20.0, 30.0, 0.8, label="valley sweep"),
        entry("/f/a.mp4", 28.0, 30.0, 30.0, 36.0, 0.7, label="tunnel run"),
    ]
    return plan, [a, b]


# --- chapters -------------------------------------------------------------------


def test_chapters_follow_scene_groups_with_min_spacing():
    plan, reports = make_plan_and_reports()
    chapters = plan_chapters(plan, reports)
    assert chapters[0].start == 0.0
    assert [c.title for c in chapters] == [
        "climbing the alpine pass", "tunnel run", "valley sweep", "tunnel run",
    ]
    # the second pass take (same group) did NOT open a chapter
    assert [c.start for c in chapters] == [0.0, 10.0, 20.0, 30.0]
    for prev, nxt in zip(chapters, chapters[1:]):
        assert nxt.start - prev.start >= 10.0


def test_chapters_too_close_are_merged():
    plan, reports = make_plan_and_reports()
    # squeeze the whole cut: scene changes now come faster than 10s
    for e in plan.entries:
        e.record_start /= 4.0
        e.record_end /= 4.0
    chapters = plan_chapters(plan, reports)
    assert len(chapters) == 1  # only 00:00 survives the spacing rule


def test_chapters_without_vision_fall_back_to_clip_names():
    plan, _ = make_plan_and_reports()
    for e in plan.entries:
        e.label = ""
    chapters = plan_chapters(plan, None)
    assert chapters[0].title == "a"  # clip stem
    assert any(c.title == "b" for c in chapters)


# --- tags -----------------------------------------------------------------------


def test_tags_ranked_by_use():
    plan, reports = make_plan_and_reports()
    tags = _collect_tags(plan, reports)
    assert tags[0] == "curve"  # used by pass takes AND the valley entry
    assert set(tags) == {"curve", "pass", "tunnel", "valley"}


# --- thumbnails -----------------------------------------------------------------


@pytest.mark.skipif(not DEMO.is_dir(), reason="demo footage not generated")
def test_thumbnails_extracted_hero_first(tmp_path):
    clip = str(DEMO / "clip_A.mp4")
    report = ClipReport(
        path=clip, duration=6.0,
        moments=[
            sem_moment(0.0, 2.0, 0.5, label="steady ride", hero=0.1, group="x"),
            sem_moment(3.0, 5.0, 0.6, label="the money shot", hero=0.9, group="y"),
        ],
    )
    plan = MontagePlan(music_path="", duration=4.0)
    plan.entries = [
        entry(clip, 0.0, 2.0, 0.0, 2.0, 0.5, label="steady ride"),
        entry(clip, 3.0, 5.0, 2.0, 4.0, 0.6, label="the money shot"),
    ]
    written = extract_thumbnails(plan, [report], tmp_path, max_thumbs=2)
    assert len(written) == 2
    # hero shot ranks first despite being the later entry
    assert written[0].startswith("thumbs/thumb_01_the-money-shot")
    for item in written:
        path = tmp_path / item.split("|")[0]
        assert path.stat().st_size > 0
        assert path.read_bytes()[:2] == b"\xff\xd8"  # JPEG SOI


def test_thumbnails_missing_media_lose_thumbs_not_kit(tmp_path):
    plan, reports = make_plan_and_reports()  # /f/*.mp4 do not exist
    assert extract_thumbnails(plan, reports, tmp_path) == []


# --- the kit --------------------------------------------------------------------


def test_publish_kit_offline_template(tmp_path, monkeypatch):
    import monteur.publish as publish

    def no_ai(*args, **kwargs):
        raise RuntimeError("no ANTHROPIC_API_KEY")

    monkeypatch.setattr(publish, "_ai_copy", no_ai)
    plan, reports = make_plan_and_reports()
    notes = publish_kit(plan, reports, tmp_path, brief="ride pov, kein gelaber")
    doc = (tmp_path / "publish.md").read_text(encoding="utf-8")
    assert "## Title ideas" in doc and "## Tags" in doc
    assert "00:00 climbing the alpine pass" in doc
    assert "00:20 valley sweep" in doc
    assert "curve" in doc
    assert "ride pov, kein gelaber" in doc
    assert any("offline template" in n for n in notes)


def test_publish_kit_uses_claude_copy_when_available(tmp_path, monkeypatch):
    import monteur.publish as publish

    def fake_ai(chapters, tags, brief, duration):
        assert any(c.title == "tunnel run" for c in chapters)
        assert "curve" in tags
        return "## Title ideas\n- Alpenpass POV\n## Description\nHin und weg.\n## Tags\nalps"

    monkeypatch.setattr(publish, "_ai_copy", fake_ai)
    plan, reports = make_plan_and_reports()
    notes = publish_kit(plan, reports, tmp_path)
    doc = (tmp_path / "publish.md").read_text(encoding="utf-8")
    assert "Alpenpass POV" in doc
    assert "Hin und weg." in doc
    assert "## Chapters (paste below your description)" in doc
    assert any("copy by Claude" in n for n in notes)


def test_publish_kit_without_vision_suggests_see(tmp_path, monkeypatch):
    import monteur.publish as publish

    monkeypatch.setattr(publish, "_ai_copy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    plan = MontagePlan(music_path="", duration=10.0)
    plan.entries = [entry("/f/plain.mp4", 0.0, 5.0, 0.0, 5.0)]
    notes = publish_kit(plan, [ClipReport(path="/f/plain.mp4", duration=10.0)], tmp_path)
    assert any("--see" in n for n in notes)
