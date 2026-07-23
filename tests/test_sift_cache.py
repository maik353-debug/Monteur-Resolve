"""Persistence of sift reports (never re-crunch a re-opened project).

Reports are written to a `.monteur-sift.json` sidecar next to the footage,
keyed by clip path + mtime — so a fresh process (a re-opened project) reuses
the sift instead of running it again.
"""

from __future__ import annotations

import os
import time

import pytest

from monteur import sift
from monteur.sift import ClipReport, ClipSegment, Moment


@pytest.fixture(autouse=True)
def _sift_cache_on(monkeypatch):
    # this file tests persistence itself — re-enable it over conftest's default
    monkeypatch.setenv("MONTEUR_SIFT_CACHE", "1")


def _report(path, ratio=0.8):
    return ClipReport(
        path=str(path), duration=10.0,
        segments=[ClipSegment(0.0, 2.0, "usable", 0.9)],
        moments=[Moment(start=0.0, end=2.0, score=0.9, label="curve", tags=["kurve"], hero=0.7)],
        usable_ratio=ratio, notes=["ok"], media_start=0.0,
    )


def _clip(folder, name):
    p = folder / name
    p.write_bytes(b"not really a video")
    return p


def test_remember_then_recall_across_a_fresh_read(tmp_path):
    clip = _clip(tmp_path, "a.mp4")
    sift.remember_reports([_report(clip, 0.72)])
    # a NEW read (simulating a re-opened project — no in-memory state)
    recalled = sift.recall_report(str(clip))
    assert recalled is not None
    assert recalled.usable_ratio == 0.72
    assert recalled.moments[0].tags == ["kurve"]
    assert (tmp_path / sift.SIFT_CACHE_FILENAME).is_file()


def test_recall_is_stale_after_the_clip_changes(tmp_path):
    clip = _clip(tmp_path, "a.mp4")
    sift.remember_reports([_report(clip)])
    future = time.time() + 10
    os.utime(clip, (future, future))  # a re-export -> mtime changed
    assert sift.recall_report(str(clip)) is None


def test_recall_none_when_clip_gone(tmp_path):
    clip = _clip(tmp_path, "a.mp4")
    sift.remember_reports([_report(clip)])
    clip.unlink()
    assert sift.recall_report(str(clip)) is None


def test_remember_merges_a_later_subset(tmp_path):
    a, b = _clip(tmp_path, "a.mp4"), _clip(tmp_path, "b.mp4")
    sift.remember_reports([_report(a)])       # analyse a
    sift.remember_reports([_report(b)])       # later, analyse b — must not drop a
    assert sift.recall_report(str(a)) is not None
    assert sift.recall_report(str(b)) is not None
    assert len(sift.load_reports(tmp_path)) == 2


def test_load_reports_skips_a_corrupt_sidecar(tmp_path):
    (tmp_path / sift.SIFT_CACHE_FILENAME).write_text("{not json", encoding="utf-8")
    assert sift.load_reports(tmp_path) == {}


def test_remember_survives_a_readonly_folder(tmp_path, monkeypatch):
    # a footage folder we cannot write to must degrade, never raise
    clip = _clip(tmp_path, "a.mp4")

    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith(sift.SIFT_CACHE_FILENAME):
            raise OSError("read-only")
        return real_replace(src, dst)

    monkeypatch.setattr("monteur.sift.os.replace", boom)
    sift.remember_reports([_report(clip)])  # must not raise
    assert sift.recall_report(str(clip)) is None  # nothing persisted, but no crash
