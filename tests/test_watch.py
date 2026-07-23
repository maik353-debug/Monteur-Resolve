"""Tests for watch mode (monteur.watch) — sift is always injected."""

from __future__ import annotations

from dataclasses import dataclass, field

from monteur import watch


@dataclass
class _Seg:
    label: str


@dataclass
class _Report:
    path: str
    usable_ratio: float = 0.8
    segments: list = field(default_factory=list)


def _media(folder, *names):
    for n in names:
        (folder / n).write_bytes(b"not really a video")


def _fake_sift(ratio=0.8, labels=("USABLE",)):
    def sift(path):
        return _Report(path=path, usable_ratio=ratio, segments=[_Seg(l) for l in labels])
    return sift


def test_pending_lists_untriaged_media(tmp_path):
    _media(tmp_path, "a.mp4", "b.mov", "notes.txt")
    state = watch.load_state(tmp_path)
    names = sorted(p.name for p in watch.pending(tmp_path, state))
    assert names == ["a.mp4", "b.mov"]  # only media, and nothing seen yet


def test_run_pass_triages_and_marks_seen(tmp_path):
    _media(tmp_path, "a.mp4", "b.mp4")
    result = watch.run_pass(tmp_path, sift=_fake_sift(0.72, ("USABLE", "DARK")))
    assert sorted(e.name for e in result["entries"]) == ["a.mp4", "b.mp4"]
    # a second pass has nothing new to do
    assert watch.run_pass(tmp_path, sift=_fake_sift())["entries"] == []


def test_only_new_files_are_triaged_on_the_next_pass(tmp_path):
    _media(tmp_path, "a.mp4")
    watch.run_pass(tmp_path, sift=_fake_sift())
    _media(tmp_path, "b.mp4")  # a clip arrives later
    result = watch.run_pass(tmp_path, sift=_fake_sift())
    assert [e.name for e in result["entries"]] == ["b.mp4"]


def test_report_accumulates_entries(tmp_path):
    _media(tmp_path, "a.mp4")
    watch.run_pass(tmp_path, sift=_fake_sift(0.9, ("USABLE",)), stamp="Mon 09:00")
    _media(tmp_path, "b.mp4")
    watch.run_pass(tmp_path, sift=_fake_sift(0.3, ("USABLE", "SHAKY", "BLURRY")), stamp="Mon 09:05")
    text = watch.report_path(tmp_path).read_text(encoding="utf-8")
    assert "# Monteur watch report" in text
    assert "a.mp4 — 90% usable" in text
    assert "b.mp4 — 30% usable · blurry, shaky" in text
    assert "## Mon 09:00" in text and "## Mon 09:05" in text


def test_entry_line_formats_flags():
    e = watch.WatchEntry("clip.mp4", 0.5, ["DARK"])
    assert e.line() == "- clip.mp4 — 50% usable · dark"
    assert watch.WatchEntry("ok.mp4", 1.0, []).line() == "- ok.mp4 — 100% usable"


def test_a_clip_that_fails_to_sift_is_retried_next_pass(tmp_path):
    _media(tmp_path, "bad.mp4")

    def boom(path):
        raise RuntimeError("ffmpeg blew up")

    assert watch.run_pass(tmp_path, sift=boom)["entries"] == []
    # it stayed pending, so a later (working) pass still picks it up
    assert [e.name for e in watch.run_pass(tmp_path, sift=_fake_sift())["entries"]] == ["bad.mp4"]


def test_re_exported_clip_is_triaged_again(tmp_path):
    import os
    import time

    _media(tmp_path, "a.mp4")
    watch.run_pass(tmp_path, sift=_fake_sift())
    # bump mtime -> a different key -> triaged again
    future = time.time() + 10
    os.utime(tmp_path / "a.mp4", (future, future))
    assert [e.name for e in watch.run_pass(tmp_path, sift=_fake_sift())["entries"]] == ["a.mp4"]


def test_watch_once_runs_a_single_pass(tmp_path):
    _media(tmp_path, "a.mp4")
    logs = []
    watch.watch(tmp_path, once=True, sift=_fake_sift(), log=logs.append)
    assert any("triaged a.mp4" in m for m in logs)
