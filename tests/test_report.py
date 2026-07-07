from __future__ import annotations

import statistics

import pytest

from fable.analysis import PacingStats, Section, Shot
from fable.report import render_report, save_report


def build_stats(name: str = "Rough Cut v3 <final?>") -> PacingStats:
    lengths = [
        0.8, 1.4, 2.1, 0.6, 3.3, 1.9, 2.7, 0.9, 1.2, 2.4,
        4.8, 5.6, 3.9, 6.2, 4.1, 7.5, 5.0, 4.4, 6.8, 5.3,
        12.0, 9.5, 16.4, 11.2, 31.7, 8.9, 14.1, 10.6, 18.3, 9.1,
        2.2, 1.6, 2.9, 1.1, 3.5, 2.0, 1.8, 2.6, 1.3, 2.3,
    ]
    shots: list[Shot] = []
    position = 0.0
    for i, length in enumerate(lengths, start=1):
        shots.append(Shot(name=f"Shot {i:03d}", start=position, length=length))
        position += length
    duration = position

    curve: list[tuple[float, float]] = []
    for i, shot in enumerate(shots):
        window = lengths[max(0, i - 2) : i + 3]
        curve.append((shot.start, statistics.fmean(window)))

    buckets = [
        ("0–1s", 0.0, 1.0),
        ("1–2s", 1.0, 2.0),
        ("2–4s", 2.0, 4.0),
        ("4–8s", 4.0, 8.0),
        ("8–15s", 8.0, 15.0),
        ("15–30s", 15.0, 30.0),
        ("30s+", 30.0, float("inf")),
    ]
    histogram = [
        (label, sum(1 for length in lengths if lo <= length < hi))
        for label, lo, hi in buckets
    ]

    fast_end = shots[10].start
    slow_end = shots[30].start
    sections = [
        Section(start=0.0, end=fast_end, avg_shot_length=statistics.fmean(lengths[:10]), label="fast"),
        Section(start=fast_end, end=slow_end, avg_shot_length=statistics.fmean(lengths[10:30]), label="slow"),
        Section(start=slow_end, end=duration, avg_shot_length=statistics.fmean(lengths[30:]), label="medium"),
    ]

    return PacingStats(
        timeline_name=name,
        fps=24.0,
        duration_seconds=duration,
        shot_count=len(shots),
        cut_count=len(shots) - 1,
        avg_shot_seconds=statistics.fmean(lengths),
        median_shot_seconds=statistics.median(lengths),
        min_shot_seconds=min(lengths),
        max_shot_seconds=max(lengths),
        std_shot_seconds=statistics.pstdev(lengths),
        shots=shots,
        pacing_curve=curve,
        histogram=histogram,
        longest_shots=sorted(shots, key=lambda s: s.length, reverse=True)[:5],
        sections=sections,
    )


def build_empty_stats() -> PacingStats:
    return PacingStats(
        timeline_name="Empty Timeline",
        fps=25.0,
        duration_seconds=0.0,
        shot_count=0,
        cut_count=0,
        avg_shot_seconds=0.0,
        median_shot_seconds=0.0,
        min_shot_seconds=0.0,
        max_shot_seconds=0.0,
        std_shot_seconds=0.0,
        shots=[],
        pacing_curve=[],
        histogram=[(label, 0) for label in ("0–1s", "1–2s", "2–4s", "4–8s", "8–15s", "15–30s", "30s+")],
        longest_shots=[],
        sections=[],
    )


@pytest.fixture
def stats() -> PacingStats:
    return build_stats()


def test_report_contains_escaped_timeline_name(stats: PacingStats) -> None:
    doc = render_report(stats)
    assert "Rough Cut v3 &lt;final?&gt;" in doc
    assert "Rough Cut v3 <final?>" not in doc


def test_report_has_one_svg_per_chart(stats: PacingStats) -> None:
    doc = render_report(stats)
    assert doc.count("<svg") == 3


def test_report_contains_stat_values(stats: PacingStats) -> None:
    doc = render_report(stats)
    assert ">40<" in doc
    assert ">39<" in doc
    assert f"{stats.avg_shot_seconds:.1f}s" in doc
    assert f"{stats.median_shot_seconds:.1f}s" in doc
    assert f"{stats.std_shot_seconds:.1f}s" in doc
    assert "31.7s" in doc
    assert "0.6s" in doc


def test_report_renders_all_sections(stats: PacingStats) -> None:
    doc = render_report(stats)
    assert "Fable pacing report" in doc
    assert "Pacing curve" in doc
    assert "Tempo sections" in doc
    assert "Shot-length histogram" in doc
    assert "Longest shots" in doc
    assert "Shot 025" in doc
    assert "30s+" in doc
    assert "24 fps" in doc


def test_empty_stats_render_without_exception() -> None:
    doc = render_report(build_empty_stats())
    assert "<!DOCTYPE html>" in doc
    assert "No shots to analyze" in doc
    assert "Empty Timeline" in doc


def test_compare_to_adds_overlay_and_delta(stats: PacingStats) -> None:
    other = build_stats(name="Rough Cut v4")
    other.avg_shot_seconds = 2.7
    other.shot_count = 52
    doc = render_report(stats, compare_to=other)
    assert "Rough Cut v4" in doc
    assert "&rarr; 2.7s" in doc
    assert "&rarr; 52" in doc
    assert doc.count("<polyline") == 2
    assert doc.count('class="key"') >= 2

    solo = render_report(stats)
    assert solo.count("<polyline") == 1
    assert "&rarr;" not in solo


def test_script_injection_in_timeline_name_is_escaped() -> None:
    evil = build_stats(name='<script>alert("pwned")</script>')
    doc = render_report(evil)
    assert "<script" not in doc
    assert "&lt;script&gt;" in doc


def test_save_report_writes_utf8(tmp_path, stats: PacingStats) -> None:
    target = tmp_path / "report.html"
    save_report(stats, target)
    text = target.read_text(encoding="utf-8")
    assert text == render_report(stats)
    assert "0–1s" in text
