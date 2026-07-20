"""Tests for Director's Notes (monteur.director) and its CLI surface.

MontagePlan / ClipReport objects are constructed directly, as in
tests/test_revise.py. The AI seam is monkeypatched at monteur.ai.complete
(director calls it through the module attribute), so no backend is needed;
apply_review is pure and tested against the exact contract in its
docstring: record grid bit-identical, only source-describing fields swap.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

import monteur.ai
from monteur.ai import MonteurAIError
from monteur.director import (
    BENCH_LIMIT,
    REVIEW_SCHEMA,
    apply_review,
    direct_cut,
    review_context,
)
from monteur.montage import MontageEntry, MontagePlan, plan_to_dict
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import ClipReport, Moment


def make_music(drops=(6.0,)) -> MusicAnalysis:
    return MusicAnalysis(
        path="/music/song.wav",
        duration=30.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(60)],
        sections=[
            MusicSection(0.0, 4.0, 0.2, "low"),
            MusicSection(4.0, 20.0, 0.6, "mid"),
            MusicSection(20.0, 30.0, 0.9, "high"),
        ],
        drops=list(drops),
    )


def make_reports() -> list[ClipReport]:
    a = ClipReport(
        path="/footage/a.mp4",
        duration=30.0,
        moments=[
            Moment(1.0, 6.0, 0.9, label="mountain road", role="opener",
                   hero=0.7, group="road"),
            Moment(10.0, 12.0, 0.5),
            Moment(20.0, 23.0, 0.7, label="sunset over the pass", role="closer"),
        ],
    )
    b = ClipReport(
        path="/footage/b.mp4",
        duration=25.0,
        moments=[
            Moment(2.0, 5.0, 0.95, label="overtake", role="climax",
                   hero=0.9, group="race"),
            Moment(8.0, 10.0, 0.6),
            Moment(15.0, 19.0, 0.8),
        ],
    )
    return [a, b]


def make_plan() -> MontagePlan:
    return MontagePlan(
        music_path="/music/song.wav",
        duration=8.0,
        song_duration=30.0,
        entries=[
            MontageEntry("/footage/a.mp4", 1.0, 3.0, 0.0, 2.0, 0.9,
                         clip_duration=30.0),
            MontageEntry("/footage/b.mp4", 2.0, 4.0, 2.0, 4.0, 0.95,
                         transition=0.5, clip_duration=25.0),
            MontageEntry("/footage/a.mp4", 10.0, 12.0, 4.0, 6.0, 0.5,
                         clip_duration=30.0),
            MontageEntry("/footage/b.mp4", 15.0, 17.0, 6.0, 8.0, 0.8,
                         clip_duration=25.0),
        ],
        notes=['style "travel": Travel film'],
    )


# --- review_context: the dossier -------------------------------------------------


class TestReviewContext:
    def test_overview_reads_style_duration_music(self):
        context = review_context(make_plan(), make_reports(), make_music())
        overview = context["overview"]
        assert overview["style"] == "travel"  # from the plan's own notes
        assert overview["duration"] == 8.0
        assert overview["entries"] == 4
        assert overview["tempo"] == 120.0
        # sections are clipped to the montage window [0, 8]
        assert overview["sections"] == [
            {"label": "low", "start": 0.0, "end": 4.0},
            {"label": "mid", "start": 4.0, "end": 8.0},
        ]
        assert overview["drops"] == [6.0]

    def test_overview_without_music_and_style_note(self):
        plan = make_plan()
        plan.notes = []
        context = review_context(plan, make_reports())
        assert context["overview"]["style"] == "auto"
        assert "tempo" not in context["overview"]
        assert "sections" not in context["overview"]

    def test_slot_enriched_from_overlapping_moment(self):
        context = review_context(make_plan(), make_reports(), make_music())
        slot = context["slots"][0]
        assert slot["slot"] == 0
        assert slot["clip"] == "a.mp4"  # basename only
        assert slot["record"] == [0.0, 2.0]
        assert slot["source"] == [1.0, 3.0]
        # entry carries no label -> enriched from the overlapping moment
        assert slot["label"] == "mountain road"
        assert slot["role"] == "opener"
        assert slot["hero"] == 0.7
        assert slot["group"] == "road"
        assert slot["music"] == "low"

    def test_slot_prefers_the_entrys_own_label(self):
        plan = make_plan()
        plan.entries[0].label = "what the planner chose"
        context = review_context(plan, make_reports(), make_music())
        assert context["slots"][0]["label"] == "what the planner chose"

    def test_unannotated_slot_omits_empty_fields(self):
        context = review_context(make_plan(), make_reports(), make_music())
        slot = context["slots"][2]  # a.mp4 10-12: moment with no annotations
        assert "label" not in slot and "role" not in slot
        assert "hero" not in slot and "group" not in slot

    def test_dissolve_and_section_under_later_slot(self):
        context = review_context(make_plan(), make_reports(), make_music())
        assert context["slots"][1]["dissolve"] == 0.5
        assert context["slots"][3]["music"] == "mid"

    def test_music_start_shifts_sections_and_drops(self):
        plan = make_plan()
        plan.music_start = 14.0  # cut from the song's stronger passage
        context = review_context(plan, make_reports(), make_music(drops=(20.0,)))
        assert context["overview"]["drops"] == [6.0]  # 20.0 in song time
        assert context["slots"][3]["music"] == "high"  # 14 + 6 = 20 -> high

    def test_bench_excludes_used_moments(self):
        context = review_context(make_plan(), make_reports(), make_music())
        bench = context["bench"]
        # used: a 1-6 (slot 0), a 10-12 (slot 2), b 2-5 (slot 1), b 15-19
        # (slot 3) -> only a 20-23 and b 8-10 remain, strongest first
        assert [(item["clip"], item["start"], item["end"]) for item in bench] == [
            ("a.mp4", 20.0, 23.0),
            ("b.mp4", 8.0, 10.0),
        ]
        assert bench[0]["label"] == "sunset over the pass"
        assert bench[0]["role"] == "closer"
        assert "label" not in bench[1]

    def test_bench_caps_at_twenty(self):
        reports = make_reports()
        reports.append(
            ClipReport(
                path="/footage/c.mp4",
                duration=300.0,
                moments=[
                    Moment(i * 10.0, i * 10.0 + 2.0, 0.99 - i * 0.001)
                    for i in range(25)
                ],
            )
        )
        context = review_context(make_plan(), reports, make_music())
        assert len(context["bench"]) == BENCH_LIMIT == 20
        # strongest-first: all cap slots go to the 0.99-ish c.mp4 moments
        assert all(item["clip"] == "c.mp4" for item in context["bench"])

    def test_dossier_is_compact(self):
        plan = make_plan()
        plan.entries[0].source_start = 1.23456789
        dumped = json.dumps(review_context(plan, make_reports(), make_music()))
        assert "/footage" not in dumped  # basenames only
        assert "1.23456789" not in dumped and "1.23" in dumped  # rounded


# --- direct_cut: the AI call ------------------------------------------------------


def _canned_review() -> dict:
    return {
        "verdict": "solid bones, weak drop",
        "score": 250,  # out of range on purpose: must clamp to 100
        "praise": ["the opening establishes the road"],
        "issues": [
            {
                "slots": [1],
                "kind": "same_scene",
                "problem": "slots 2 and 3 sit in the same scene",
                "suggestion": "swap slot 2 for the sunset",
                "replacement": {"clip": "a.mp4", "start": 20.0, "end": 23.0},
            },
            {  # out-of-range slot: the whole issue must be dropped
                "slots": [99],
                "kind": "bogus",
                "problem": "x",
                "suggestion": "y",
                "replacement": None,
            },
            {  # malformed replacement: issue survives, swap degrades to None
                "slots": [0],
                "kind": "weak_opening",
                "problem": "p",
                "suggestion": "s",
                "replacement": {"clip": "a.mp4", "start": 20.0},
            },
        ],
        "summary": "one swap away from shipping",
    }


class TestDirectCut:
    def test_happy_path_prompt_schema_and_validation(self, monkeypatch):
        captured: dict = {}

        def fake_complete(prompt, *, system="", json_schema=None, **kwargs):
            captured.update(prompt=prompt, system=system, json_schema=json_schema)
            return json.dumps(_canned_review())

        monkeypatch.setattr(monteur.ai, "complete", fake_complete)
        review = direct_cut(
            make_plan(), make_reports(), make_music(), notes="Instagram teaser"
        )

        # the structured-output contract went along
        assert captured["json_schema"] is REVIEW_SCHEMA
        # the system prompt IS the craft contract
        for phrase in ("establish", "drop", "same scene", "outro", "energy"):
            assert phrase in captured["system"], phrase
        # the dossier and the editor's context are in the prompt
        assert "bench" in captured["prompt"]
        assert "mountain road" in captured["prompt"]
        assert "Instagram teaser" in captured["prompt"]

        # defensive validation: clamp + drop + degrade
        assert review["score"] == 100
        assert review["verdict"] == "solid bones, weak drop"
        assert len(review["issues"]) == 2  # slot-99 issue dropped
        assert review["issues"][0]["replacement"] == {
            "clip": "a.mp4", "start": 20.0, "end": 23.0,
        }
        assert review["issues"][1]["replacement"] is None
        assert review["summary"] == "one swap away from shipping"

    def test_negative_and_missing_scores(self, monkeypatch):
        for raw, expected in (({"score": -5}, 0), ({}, 50), ({"score": "??"}, 50)):
            monkeypatch.setattr(
                monteur.ai, "complete",
                lambda *a, _raw=raw, **k: json.dumps(_raw),
            )
            review = direct_cut(make_plan(), make_reports())
            assert review["score"] == expected, raw
            assert review["issues"] == [] and review["praise"] == []

    def test_ai_error_passes_through_unchanged(self, monkeypatch):
        boom = MonteurAIError("no way to reach Claude")

        def fake_complete(*args, **kwargs):
            raise boom

        monkeypatch.setattr(monteur.ai, "complete", fake_complete)
        with pytest.raises(MonteurAIError) as exc_info:
            direct_cut(make_plan(), make_reports())
        assert exc_info.value is boom

    def test_unparseable_reply_is_ai_error(self, monkeypatch):
        monkeypatch.setattr(monteur.ai, "complete", lambda *a, **k: "not json")
        with pytest.raises(MonteurAIError) as exc_info:
            direct_cut(make_plan(), make_reports())
        assert "unparseable" in str(exc_info.value)


# --- apply_review: pure plan surgery ---------------------------------------------


def _review_with(replacement, slots=(1,), kind="same_scene") -> dict:
    return {
        "verdict": "", "score": 70, "praise": [], "summary": "",
        "issues": [
            {
                "slots": list(slots),
                "kind": kind,
                "problem": "p",
                "suggestion": "s",
                "replacement": replacement,
            }
        ],
    }


class TestApplyReview:
    def test_swap_happy_path(self):
        plan = make_plan()
        before = [asdict(e) for e in plan.entries]
        review = _review_with({"clip": "a.mp4", "start": 20.0, "end": 23.0})
        improved, notes = apply_review(plan, review, make_reports())

        # the original plan is untouched
        assert [asdict(e) for e in plan.entries] == before

        swapped = improved.entries[1]
        # source side swapped ...
        assert swapped.clip_path == "/footage/a.mp4"
        assert swapped.source_start == 20.0
        assert swapped.source_end == 22.0  # trimmed to the 2s record length
        assert swapped.label == "sunset over the pass"
        assert swapped.score == 0.7
        assert swapped.clip_duration == 30.0
        # ... record grid and transition bit-identical
        assert swapped.record_start == before[1]["record_start"]
        assert swapped.record_end == before[1]["record_end"]
        assert swapped.transition == before[1]["transition"]
        # every other entry is bit-identical
        for i in (0, 2, 3):
            assert asdict(improved.entries[i]) == before[i], i
        assert notes and notes[0].startswith("slot 2: b.mp4 2.00-4.00s -> a.mp4")
        assert any(n.startswith("director:") for n in improved.notes)
        # dips/sfx/scalars survive the copy
        assert improved.duration == plan.duration
        assert improved.music_path == plan.music_path

    def test_window_snapped_into_the_moment_range(self):
        plan = make_plan()
        # request hangs off the end of the 20-23 moment: snap back inside
        review = _review_with({"clip": "a.mp4", "start": 22.5, "end": 24.5})
        improved, _notes = apply_review(plan, review, make_reports())
        assert improved.entries[1].source_start == 21.0
        assert improved.entries[1].source_end == 23.0

    def test_short_moment_pads_toward_clip_end_with_note(self):
        plan = make_plan()
        reports = make_reports() + [
            ClipReport(
                path="/footage/c.mp4",
                duration=10.0,
                moments=[Moment(9.0, 9.8, 0.9)],
            )
        ]
        review = _review_with({"clip": "c.mp4", "start": 9.0, "end": 9.8})
        improved, notes = apply_review(plan, review, reports)
        entry = improved.entries[1]
        assert entry.source_start == 9.0
        assert entry.source_end == 10.0  # padded to the clip's real end
        assert any("only 1.00s of source for a 2.00s slot" in n for n in notes)

    def test_pinned_slot_is_skipped(self):
        plan = make_plan()
        before = [asdict(e) for e in plan.entries]
        review = _review_with({"clip": "a.mp4", "start": 20.0, "end": 23.0})
        improved, notes = apply_review(
            plan, review, make_reports(), pinned=[3.0]  # inside slot 1 (2-4s)
        )
        assert [asdict(e) for e in improved.entries] == before
        assert notes == ["slot 2: pinned — left untouched"]

    def test_unknown_basename_is_skipped_with_note(self):
        plan = make_plan()
        before = [asdict(e) for e in plan.entries]
        review = _review_with({"clip": "zzz.mp4", "start": 1.0, "end": 3.0})
        improved, notes = apply_review(plan, review, make_reports())
        assert [asdict(e) for e in improved.entries] == before
        assert notes == ["slot 2: no clip named 'zzz.mp4' in the footage — skipped"]

    def test_issues_without_replacement_do_nothing(self):
        plan = make_plan()
        before = [asdict(e) for e in plan.entries]
        review = _review_with(None)
        improved, notes = apply_review(plan, review, make_reports())
        assert [asdict(e) for e in improved.entries] == before
        assert notes == ["no replacement suggestions to apply"]


# --- CLI: monteur direct ----------------------------------------------------------


class TestDirectCli:
    def _plan_file(self, tmp_path, plan=None) -> str:
        plan = plan or make_plan()
        plan.music_path = ""  # keep the CLI run offline (no analyze_music)
        path = tmp_path / "plan.json"
        path.write_text(
            json.dumps(plan_to_dict(plan), ensure_ascii=False), encoding="utf-8"
        )
        return str(path)

    def _patch_pipeline(self, monkeypatch, review):
        monkeypatch.setattr(
            "monteur.sift.list_media", lambda folder: ["a.mp4", "b.mp4"]
        )
        monkeypatch.setattr(
            "monteur.sift.sift_directory",
            lambda folder, progress=None, cancel=None: make_reports(),
        )
        calls: dict = {}

        def fake_direct_cut(plan, reports, music=None, notes=""):
            calls.update(entries=len(plan.entries), reports=len(reports),
                         music=music, notes=notes)
            return review

        monkeypatch.setattr("monteur.director.direct_cut", fake_direct_cut)
        return calls

    def test_parses_with_defaults(self):
        from monteur.cli import build_parser, cmd_direct

        args = build_parser().parse_args(["direct", "plan.json", "clips"])
        assert args.plan == "plan.json" and args.folder == "clips"
        assert args.apply is False and args.notes == "" and args.music == ""
        assert args.output is None and args.save_plan == ""
        assert args.func is cmd_direct

    def test_apply_without_output_fails(self, tmp_path, capsys):
        from monteur.cli import build_parser

        args = build_parser().parse_args(
            ["direct", self._plan_file(tmp_path), "clips", "--apply"]
        )
        with pytest.raises(SystemExit):
            args.func(args)
        assert "-o/--output" in capsys.readouterr().err

    def test_review_printout(self, tmp_path, capsys, monkeypatch):
        from monteur.cli import build_parser

        review = {
            "verdict": "solid",
            "score": 82,
            "praise": ["good opening"],
            "issues": [
                {
                    "slots": [1, 2], "kind": "same_scene",
                    "problem": "back to back", "suggestion": "split them",
                    "replacement": {"clip": "a.mp4", "start": 20.0, "end": 23.0},
                }
            ],
            "summary": "one swap away",
        }
        calls = self._patch_pipeline(monkeypatch, review)
        args = build_parser().parse_args(
            ["direct", self._plan_file(tmp_path), "clips", "--notes", "teaser"]
        )
        args.func(args)
        out = capsys.readouterr().out
        assert "Verdict: solid" in out
        assert "82/100" in out
        assert "+ good opening" in out
        assert "1. slot 2+3 — same scene: back to back" in out
        assert "-> split them" in out
        assert "swap in a.mp4 20.0-23.0s" in out
        assert "one swap away" in out
        assert calls["notes"] == "teaser" and calls["reports"] == 2

    def test_apply_writes_timeline_and_plan(self, tmp_path, capsys, monkeypatch):
        from monteur.cli import build_parser

        review = _review_with({"clip": "a.mp4", "start": 20.0, "end": 23.0})
        self._patch_pipeline(monkeypatch, review)
        out_file = tmp_path / "improved.fcpxml"
        plan_out = tmp_path / "improved.json"
        args = build_parser().parse_args(
            [
                "direct", self._plan_file(tmp_path), "clips",
                "--apply", "-o", str(out_file), "--save-plan", str(plan_out),
            ]
        )
        args.func(args)
        out = capsys.readouterr().out
        assert out_file.exists() and "<fcpxml" in out_file.read_text()
        assert "4 cuts ->" in out
        assert "slot 2: b.mp4 2.00-4.00s -> a.mp4 20.00-22.00s" in out
        saved = json.loads(plan_out.read_text(encoding="utf-8"))
        assert saved["entries"][1]["clip_path"] == "/footage/a.mp4"
        assert saved["entries"][1]["source_start"] == 20.0
        assert any(n.startswith("director:") for n in saved["notes"])

    def test_ai_error_fails_cleanly(self, tmp_path, capsys, monkeypatch):
        from monteur.cli import build_parser

        monkeypatch.setattr(
            "monteur.sift.list_media", lambda folder: ["a.mp4"]
        )
        monkeypatch.setattr(
            "monteur.sift.sift_directory",
            lambda folder, progress=None, cancel=None: make_reports(),
        )

        def boom(*args, **kwargs):
            raise MonteurAIError("no way to reach Claude")

        monkeypatch.setattr("monteur.director.direct_cut", boom)
        args = build_parser().parse_args(
            ["direct", self._plan_file(tmp_path), "clips"]
        )
        with pytest.raises(SystemExit):
            args.func(args)
        assert "no way to reach Claude" in capsys.readouterr().err
