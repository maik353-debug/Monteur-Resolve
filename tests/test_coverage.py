"""Tests for the pre-cut coverage check (monteur.coverage) and its CLI.

ClipReport/Moment objects are hand-built like in tests/test_director.py.
The deterministic layer (coverage_basics) is tested as pure math; the AI
layer (missing_shots) monkeypatches the one seam monteur.ai.complete —
coverage calls it through the module attribute, so no backend runs. The
CLI surface monkeypatches the sift and missing_shots themselves.
"""

from __future__ import annotations

import json

import pytest

import monteur.ai
from monteur.ai import MonteurAIError
from monteur.compose import CRAFT_BRIEFS
from monteur.coverage import (
    MISSING_LIMIT,
    MISSING_SCHEMA,
    coverage_basics,
    missing_shots,
)
from monteur.sift import ClipReport, Moment


def make_labeled_reports() -> list[ClipReport]:
    """Two clips, vision-annotated: roles, heroes and two scene groups."""
    a = ClipReport(
        path="/footage/a.mp4",
        duration=30.0,
        usable_ratio=0.8,
        moments=[
            Moment(1.0, 6.0, 0.9, label="mountain road", role="opener",
                   hero=0.7, group="road"),
            Moment(10.0, 12.0, 0.5, label="riders resting", role="build",
                   group="rest", tags=["break", "friends"]),
        ],
    )
    b = ClipReport(
        path="/footage/b.mp4",
        duration=20.0,
        usable_ratio=0.5,
        moments=[
            Moment(2.0, 5.0, 0.95, label="overtake in the curve",
                   role="climax", hero=0.9, group="road"),
        ],
    )
    return [a, b]


def make_unlabeled_reports() -> list[ClipReport]:
    """The same shape without any vision annotations."""
    return [
        ClipReport(
            path="/footage/a.mp4", duration=30.0, usable_ratio=0.8,
            moments=[Moment(1.0, 6.0, 0.9), Moment(10.0, 12.0, 0.5)],
        ),
        ClipReport(
            path="/footage/b.mp4", duration=20.0, usable_ratio=0.5,
            moments=[Moment(2.0, 5.0, 0.95)],
        ),
    ]


# ------------------------------------------------------------ coverage_basics


class TestCoverageBasics:
    def test_counts_roles_heroes_groups_and_duration(self):
        facts = coverage_basics(make_labeled_reports(), "trailer")
        assert facts["style"] == "trailer"
        assert facts["clips"] == 2
        assert facts["moments"] == 3
        # unique material: (6-1) + (12-10) + (5-2) = 10 s
        assert facts["usable_seconds"] == pytest.approx(10.0)
        # the no-repeat maximum IS the usable material — no 1.5x anymore
        assert facts["max_comfortable_seconds"] == pytest.approx(10.0)
        assert facts["roles"] == {"opener": 1, "build": 1, "climax": 1, "closer": 0}
        assert facts["heroes"] == 2  # hero >= 0.5
        assert facts["groups"] == 2  # "road" + "rest"
        assert facts["vision"] is True
        # no target given -> the target keys stay out of the facts
        assert "target_seconds" not in facts
        assert "repetition_risk" not in facts

    def test_unusable_share_is_duration_weighted(self):
        facts = coverage_basics(make_labeled_reports())
        # usable time = 0.8*30 + 0.5*20 = 34 of 50 -> 32% unusable
        assert facts["unusable_share"] == pytest.approx(0.32)

    def test_repetition_risk_uses_the_planner_no_repeat_rule(self):
        risky = coverage_basics(make_labeled_reports(), "auto", target_seconds=30.0)
        assert risky["target_seconds"] == pytest.approx(30.0)
        assert risky["repetition_risk"] is True  # 30 > 10s of unique material
        assert any("shortens to the material" in f for f in risky["findings"])

        fine = coverage_basics(make_labeled_reports(), "auto", target_seconds=10.0)
        assert fine["repetition_risk"] is False  # 10 <= 10
        assert not any("shortens to the material" in f for f in fine["findings"])

        over = coverage_basics(make_labeled_reports(), "auto", target_seconds=12.0)
        assert over["repetition_risk"] is True  # 12 > 10: the old 1.5x grace is gone

    def test_zero_closers_is_a_finding_when_vision_ran(self):
        facts = coverage_basics(make_labeled_reports())
        assert any(f.startswith("no closer") for f in facts["findings"])
        # the covered roles do NOT fire
        assert not any(f.startswith("no opener") for f in facts["findings"])
        assert not any(f.startswith("no hero") for f in facts["findings"])

    def test_zero_openers_is_a_finding(self):
        reports = make_labeled_reports()
        for report in reports:
            for m in report.moments:
                if m.role == "opener":
                    m.role = "build"
        facts = coverage_basics(reports)
        assert any(f.startswith("no opener") for f in facts["findings"])

    def test_one_scene_group_is_a_finding(self):
        reports = make_labeled_reports()
        for report in reports:
            for m in report.moments:
                m.group = "road"
        facts = coverage_basics(reports)
        assert facts["groups"] == 1
        assert any("one scene group" in f for f in facts["findings"])

    def test_unlabeled_material_flags_nothing_it_cannot_know(self):
        facts = coverage_basics(make_unlabeled_reports())
        assert facts["vision"] is False
        assert facts["roles"] == {"opener": 0, "build": 0, "climax": 0, "closer": 0}
        assert facts["heroes"] == 0
        assert facts["groups"] == 0
        # unknown is not missing: no role/group findings without labels
        assert facts["findings"] == []

    def test_mostly_unusable_footage_is_a_finding(self):
        reports = [
            ClipReport(path="/f/x.mp4", duration=10.0, usable_ratio=0.2,
                       moments=[Moment(0.0, 2.0, 0.5)]),
        ]
        facts = coverage_basics(reports)
        assert facts["unusable_share"] == pytest.approx(0.8)
        assert any("unusable" in f for f in facts["findings"])

    def test_unknown_style_falls_back_to_auto(self):
        facts = coverage_basics(make_labeled_reports(), "nope")
        assert facts["style"] == "auto"

    def test_empty_reports_stay_calm(self):
        facts = coverage_basics([], "travel", target_seconds=60.0)
        assert facts["clips"] == 0
        assert facts["usable_seconds"] == 0.0
        assert facts["unusable_share"] == 0.0
        assert facts["repetition_risk"] is True  # 60s from nothing repeats


# ------------------------------------------------------------ missing_shots


GOOD_REPLY = {
    "verdict": "solid road coverage, thin on people",
    "coverage_score": 62,
    "have": ["strong road action", "a real hero moment"],
    "missing": [
        {"shot": "calm wide establishing the valley", "why": "the opener",
         "priority": "must", "tip": "tripod, 10s hold, morning light"},
        {"shot": "faces reacting at the summit", "why": "the closer",
         "priority": "nice", "tip": "step closer, let it run long"},
    ],
    "summary": "Film the opener and a closer, then cut.",
}


class TestMissingShots:
    def test_happy_path_prompt_schema_and_result(self, monkeypatch):
        calls: dict = {}

        def fake_complete(prompt, *, system="", json_schema=None, **kwargs):
            calls.update(prompt=prompt, system=system, schema=json_schema)
            return json.dumps(GOOD_REPLY)

        monkeypatch.setattr(monteur.ai, "complete", fake_complete)
        result = missing_shots(
            make_labeled_reports(), style="trailer",
            brief="epic alps trailer, end on the summit", target_seconds=45.0,
        )

        # the one AI seam got the schema and the coverage stance
        assert calls["schema"] is MISSING_SCHEMA
        assert "coverage check BEFORE the cut" in calls["system"]
        # the prompt carries the craft brief, the editor's brief, the facts
        # and the inventory
        assert CRAFT_BRIEFS["trailer"] in calls["prompt"]
        assert "epic alps trailer, end on the summit" in calls["prompt"]
        assert '"usable_seconds": 10.0' in calls["prompt"]
        assert '"target_seconds": 45.0' in calls["prompt"]
        assert "a.mp4" in calls["prompt"]
        assert "overtake in the curve" in calls["prompt"]
        # labeled material announces itself, not the unlabeled disclaimer
        assert "vision labels — judge coverage by" in calls["prompt"]
        assert "No vision labels" not in calls["prompt"]

        assert result["verdict"] == GOOD_REPLY["verdict"]
        assert result["coverage_score"] == 62
        assert result["have"] == GOOD_REPLY["have"]
        assert result["missing"] == GOOD_REPLY["missing"]
        assert result["summary"] == GOOD_REPLY["summary"]
        assert result["basics"]["vision"] is True
        assert result["notes"] == []  # labeled: no vision recommendation

    def test_unlabeled_material_is_said_and_noted(self, monkeypatch):
        calls: dict = {}

        def fake_complete(prompt, *, system="", json_schema=None, **kwargs):
            calls["prompt"] = prompt
            return json.dumps(GOOD_REPLY)

        monkeypatch.setattr(monteur.ai, "complete", fake_complete)
        result = missing_shots(make_unlabeled_reports())
        assert "No vision labels are available" in calls["prompt"]
        assert "Let Claude watch your clips" in calls["prompt"]
        assert any("Let Claude watch your clips" in n for n in result["notes"])

    def test_validation_clamps_drops_and_caps(self, monkeypatch):
        sloppy = {
            "verdict": None,
            "coverage_score": 250,
            "have": ["  keep me  ", "", 7],
            "missing": (
                [
                    "not-a-dict",
                    {"why": "no shot key"},
                    {"shot": "  a real one ", "priority": "MUST"},
                    {"shot": "odd priority", "priority": "urgent",
                     "why": 3, "tip": None},
                ]
                + [{"shot": f"filler {i}", "priority": "nice", "why": "",
                    "tip": ""} for i in range(20)]
            ),
        }
        monkeypatch.setattr(
            monteur.ai, "complete", lambda *a, **k: json.dumps(sloppy)
        )
        result = missing_shots(make_labeled_reports())
        assert result["coverage_score"] == 100  # clamped
        assert result["verdict"] == ""
        assert result["summary"] == ""
        assert result["have"] == ["keep me", "7"]
        assert len(result["missing"]) == MISSING_LIMIT  # capped at 10
        first, second = result["missing"][0], result["missing"][1]
        assert first == {"shot": "a real one", "why": "", "priority": "must",
                         "tip": ""}
        # an unknown priority degrades to "nice", never invents urgency
        assert second["priority"] == "nice"

    def test_non_numeric_score_reads_neutral(self, monkeypatch):
        monkeypatch.setattr(
            monteur.ai, "complete",
            lambda *a, **k: json.dumps({"coverage_score": "high"}),
        )
        result = missing_shots(make_labeled_reports())
        assert result["coverage_score"] == 50
        assert result["missing"] == []

    def test_ai_error_passes_through(self, monkeypatch):
        def boom(*args, **kwargs):
            raise MonteurAIError("no way to reach Claude found")

        monkeypatch.setattr(monteur.ai, "complete", boom)
        with pytest.raises(MonteurAIError, match="no way to reach Claude"):
            missing_shots(make_labeled_reports())

    def test_unparseable_reply_raises_ai_error(self, monkeypatch):
        monkeypatch.setattr(monteur.ai, "complete", lambda *a, **k: "not json")
        with pytest.raises(MonteurAIError, match="unparseable JSON"):
            missing_shots(make_labeled_reports())


# ------------------------------------------------------------ the CLI


class TestMissingCli:
    def _fake_sift(self, monkeypatch, reports):
        monkeypatch.setattr(
            "monteur.sift.list_media", lambda folder: [r.path for r in reports]
        )
        monkeypatch.setattr(
            "monteur.sift.sift_directory",
            lambda folder, progress=None, cancel=None: reports,
        )

    def test_missing_parses_with_defaults(self):
        from monteur.cli import build_parser

        args = build_parser().parse_args(["missing", "clips"])
        assert args.folder == "clips"
        assert args.style == "auto"
        assert args.brief == ""
        assert args.target is None

    def test_missing_prints_score_have_and_priorities(self, monkeypatch, capsys):
        from monteur.cli import main

        reports = make_labeled_reports()
        self._fake_sift(monkeypatch, reports)
        calls: dict = {}

        def fake_missing_shots(got_reports, style="auto", brief="",
                               target_seconds=None):
            calls.update(reports=got_reports, style=style, brief=brief,
                         target=target_seconds)
            result = dict(GOOD_REPLY)
            result["basics"] = coverage_basics(got_reports, style, target_seconds)
            result["notes"] = ["a calm note"]
            return result

        monkeypatch.setattr("monteur.coverage.missing_shots", fake_missing_shots)
        main([
            "missing", "clips", "--style", "trailer",
            "--brief", "epic trailer", "--target", "45",
        ])

        assert calls["reports"] is reports
        assert calls["style"] == "trailer"
        assert calls["brief"] == "epic trailer"
        assert calls["target"] == 45.0

        out = capsys.readouterr().out
        assert "Coverage: 62/100" in out
        assert "solid road coverage" in out
        assert "10s usable for a 45s target" in out
        assert "+ strong road action" in out
        assert "Still missing (1 must, 1 nice):" in out
        # musts print before nices, each with its why and tip
        assert out.index("MUST") < out.index("NICE")
        assert "calm wide establishing the valley" in out
        assert "tip: tripod, 10s hold, morning light" in out
        assert "Film the opener and a closer, then cut." in out
        assert "a calm note" in out

    def test_missing_ai_error_exits_cleanly(self, monkeypatch, capsys):
        from monteur.cli import main

        self._fake_sift(monkeypatch, make_labeled_reports())

        def boom(*args, **kwargs):
            raise MonteurAIError("no way to reach Claude found")

        monkeypatch.setattr("monteur.coverage.missing_shots", boom)
        with pytest.raises(SystemExit):
            main(["missing", "clips"])
        assert "no way to reach Claude" in capsys.readouterr().err

    def test_missing_empty_folder_fails(self, monkeypatch):
        from monteur.cli import main

        monkeypatch.setattr("monteur.sift.list_media", lambda folder: [])
        with pytest.raises(SystemExit):
            main(["missing", "clips"])
