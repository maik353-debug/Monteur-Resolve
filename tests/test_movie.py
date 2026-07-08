"""Tests for the movie creator, stage 1 (monteur.movie)."""

from __future__ import annotations

import json

import pytest

import monteur.movie as movie
from monteur.ai import MonteurAIError
from monteur.movie import (
    DialogueLine,
    MovieProject,
    MovieScene,
    generate_movie,
    load_project,
    project_from_dict,
    project_to_dict,
    render_fountain,
    save_project,
    shotlist_markdown,
)


def _blueprint_json(n_scenes=3) -> dict:
    return {
        "title": "Nachtfahrt",
        "logline": "Ein Fahrer, ein Wald, ein Geheimnis.",
        "scenes": [
            {
                "heading": f"EXT. WALDWEG - NIGHT" if i % 2 else "INT. AUTO - NIGHT",
                "summary": f"Szene {i + 1} treibt die Geschichte voran.",
                "action": "Scheinwerfer schneiden durch den Nebel.",
                "dialogue": (
                    [{"character": "lena", "line": "Halt an.", "parenthetical": "leise"}]
                    if i == 1
                    else []
                ),
                "shooting_tips": [
                    "Kamera auf Kinnhöhe im Fußraum, 2 Takes",
                    "Gegenlicht durch die Heckscheibe",
                ],
                "sound_notes": "Motor im Leerlauf separat aufnehmen.",
                "cut_intent": "ruhig halten, harter Schnitt in die nächste Szene",
            }
            for i in range(n_scenes)
        ],
    }


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    stop_reason = "end_turn"

    def __init__(self, payload):
        self.content = [_FakeBlock(json.dumps(payload))]


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _FakeResponse(outer._payload)

        self.messages = _Messages()


def test_generate_movie_builds_validated_project(monkeypatch):
    fake = _FakeClient(_blueprint_json())
    monkeypatch.setattr(movie, "_client", lambda: fake)
    project = generate_movie("5 Minuten, Wald und Auto", genre="thriller")
    assert project.title == "Nachtfahrt"
    assert project.genre == "thriller"
    assert [s.number for s in project.scenes] == [1, 2, 3]
    assert project.scenes[1].dialogue[0].character == "LENA"  # upper-cased
    assert all(s.status == "planned" and s.folder == "" for s in project.scenes)
    # the request used structured output and the movie system prompt
    kwargs = fake.calls[0]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert "writer-director" in kwargs["system"]


def test_generate_movie_too_few_scenes_is_actionable(monkeypatch):
    payload = _blueprint_json(1)
    monkeypatch.setattr(movie, "_client", lambda: _FakeClient(payload))
    with pytest.raises(MonteurAIError, match="at least 3"):
        generate_movie("zu dünn")


def test_generate_movie_clamps_scene_count(monkeypatch):
    payload = _blueprint_json(30)
    monkeypatch.setattr(movie, "_client", lambda: _FakeClient(payload))
    project = generate_movie("episch")
    assert len(project.scenes) == 24
    assert any("clamped" in n for n in project.notes)


def test_render_fountain_is_assembly_compatible(monkeypatch):
    monkeypatch.setattr(movie, "_client", lambda: _FakeClient(_blueprint_json()))
    project = generate_movie("test")
    text = render_fountain(project)
    assert text.startswith("Title: Nachtfahrt")
    assert "INT. AUTO - NIGHT" in text and "EXT. WALDWEG - NIGHT" in text
    assert "LENA" in text and "(leise)" in text and "Halt an." in text

    # the screenplay module (assembly's parser) must see the same scenes
    from monteur.screenplay import parse_fountain

    screenplay = parse_fountain(text)
    assert len(screenplay.scenes) == 3
    assert screenplay.scenes[0].heading.startswith(("INT.", "EXT."))


def test_shotlist_markdown_contents():
    project = MovieProject(
        title="T", genre="doku", brief="b", logline="l",
        scenes=[
            MovieScene(
                number=3, heading="EXT. PASS - DAY", summary="s", action="a",
                shooting_tips=["tief halten"], sound_notes="Wind!",
                cut_intent="schnell",
            )
        ],
    )
    md = shotlist_markdown(project)
    assert "## Scene 3 — EXT. PASS - DAY" in md
    assert "- [ ] tief halten" in md
    assert "Sound: Wind!" in md
    assert "Edit intent: schnell" in md
    assert "`S03_T01`" in md


def test_save_and_load_roundtrip(tmp_path):
    project = MovieProject(
        title="T", genre="g", brief="b", logline="l",
        scenes=[
            MovieScene(
                number=1, heading="INT. X - DAY", summary="s", action="a",
                dialogue=[DialogueLine(character="A", line="hi")],
                shooting_tips=["t"], sound_notes="n", cut_intent="c",
            )
        ],
    )
    paths = save_project(project, tmp_path / "proj")
    assert [p.name for p in paths] == ["movie.json", "script.fountain", "shotlist.md"]
    assert all(p.is_file() for p in paths)
    loaded = load_project(tmp_path / "proj")
    assert project_to_dict(loaded) == project_to_dict(project)


def test_project_from_dict_version_guard():
    with pytest.raises(ValueError, match="monteur_movie"):
        project_from_dict({"title": "x"})
    with pytest.raises(ValueError, match="monteur_movie"):
        project_from_dict({"monteur_movie": 99, "title": "x"})


def test_load_project_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="movie new"):
        load_project(tmp_path)


def test_cli_movie_new(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(movie, "_client", lambda: _FakeClient(_blueprint_json()))
    from monteur.cli import build_parser

    args = build_parser().parse_args(
        ["movie", "new", str(tmp_path / "proj"), "--brief", "Waldthriller", "--genre", "thriller"]
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "'Nachtfahrt' — 3 scenes" in out
    assert (tmp_path / "proj" / "script.fountain").is_file()
    assert (tmp_path / "proj" / "shotlist.md").is_file()


# --- stage 2: production slots & footage checks (pure helpers) ----------------------


from monteur.movie import assign_scene, check_scene_footage, project_progress
from monteur.sift import ClipReport, Moment


def _project(n_scenes=3):
    return MovieProject(
        title="Nachtfahrt", genre="thriller", brief="b", logline="l",
        scenes=[
            MovieScene(
                number=i + 1,
                heading="EXT. WALDWEG - NIGHT" if i % 2 else "INT. AUTO - NIGHT",
                summary=f"Szene {i + 1}",
                action="a",
            )
            for i in range(n_scenes)
        ],
    )


class TestAssignScene:
    def test_assign_sets_folder_and_status(self):
        project = _project()
        scene = assign_scene(project, 2, "/footage/wald")
        assert scene is project.scenes[1]
        assert scene.folder == "/footage/wald"
        assert scene.status == "assigned"
        # the other scenes are untouched
        assert project.scenes[0].status == "planned"
        assert project.scenes[2].folder == ""

    def test_unknown_scene_is_value_error(self):
        with pytest.raises(ValueError, match="no scene 9"):
            assign_scene(_project(), 9, "/footage")

    def test_empty_folder_unassigns(self):
        project = _project()
        assign_scene(project, 1, "/footage")
        scene = assign_scene(project, 1, "")
        assert scene.folder == ""
        assert scene.status == "planned"

    def test_whitespace_folder_counts_as_empty(self):
        scene = assign_scene(_project(), 1, "   ")
        assert scene.folder == ""
        assert scene.status == "planned"

    def test_assignment_survives_save_load(self, tmp_path):
        project = _project()
        assign_scene(project, 3, "/footage/auto")
        save_project(project, tmp_path)
        loaded = load_project(tmp_path)
        assert loaded.scenes[2].folder == "/footage/auto"
        assert loaded.scenes[2].status == "assigned"


class TestProjectProgress:
    def test_counts_assigned_scenes(self):
        project = _project(4)
        assign_scene(project, 1, "/a")
        assign_scene(project, 3, "/b")
        assert project_progress(project) == {
            "scenes": 4, "assigned": 2, "percent": 50,
        }

    def test_empty_project(self):
        project = MovieProject(title="T", genre="", brief="", logline="")
        assert project_progress(project) == {
            "scenes": 0, "assigned": 0, "percent": 0,
        }

    def test_all_assigned_is_100(self):
        project = _project(3)
        for n in (1, 2, 3):
            assign_scene(project, n, f"/f{n}")
        assert project_progress(project)["percent"] == 100


def _report(name="clip_A.mp4", usable=0.8, notes=(), moments=()):
    return ClipReport(
        path=f"/footage/{name}", duration=30.0, usable_ratio=usable,
        notes=list(notes), moments=list(moments),
    )


def _labeled_moment(label="", tags=(), group=""):
    """A Moment carrying vision annotations, set like the annotator does."""
    moment = Moment(start=0.0, end=2.0, score=0.9)
    moment.label = label
    moment.tags = list(tags)
    moment.group = group
    return moment


def _scene(heading="EXT. WALDWEG - NIGHT", summary="Das Auto hält im Wald."):
    return MovieScene(number=1, heading=heading, summary=summary, action="a")


class TestCheckSceneFootage:
    def test_shape_is_stable(self):
        check = check_scene_footage(_scene(), [_report()])
        assert set(check) == {
            "score", "content_checked", "clips", "avg_usable", "findings",
        }
        assert isinstance(check["score"], float)
        assert 0.0 <= check["score"] <= 1.0

    def test_technical_only_without_vision(self):
        reports = [_report("a.mp4", 0.9), _report("b.mp4", 0.5)]
        check = check_scene_footage(_scene(heading="INT. AUTO - DAY"), reports)
        assert check["content_checked"] is False
        assert check["clips"] == 2
        assert check["avg_usable"] == pytest.approx(0.7)
        assert check["score"] == 0.5  # baseline: nothing verified either way
        assert any("2 clips" in f and "70% usable" in f for f in check["findings"])
        # one finding points at how to get content checks
        assert any(
            "monteur see" in f and "Let Claude watch" in f
            for f in check["findings"]
        )

    def test_no_clips_finding(self):
        check = check_scene_footage(_scene(), [])
        assert check["clips"] == 0
        assert check["avg_usable"] == 0.0
        assert any("No clips" in f for f in check["findings"])

    def test_mostly_unusable_clip_is_called_out(self):
        reports = [
            _report("shaky.mp4", 0.2, notes=["78% unusable: mostly shaky"]),
        ]
        check = check_scene_footage(_scene(heading="EXT. PASS - DAY"), reports)
        assert any(
            "shaky.mp4: 78% unusable: mostly shaky" in f
            for f in check["findings"]
        )

    def test_content_overlap_positive(self):
        moments = [
            _labeled_moment(
                "car stops on a forest track", ["wald", "auto", "nacht"]
            )
        ]
        check = check_scene_footage(_scene(), [_report(moments=moments)])
        assert check["content_checked"] is True
        mention = [f for f in check["findings"] if "footage mentions" in f]
        assert mention and "wald" in mention[0] and "auto" in mention[0]
        # +0.25 overlap, +0.15 ext (forest/wald outdoor), +0.1 night -> 1.0
        assert check["score"] == pytest.approx(1.0)

    def test_content_overlap_prefix_matches_german_plurals(self):
        # scene says "Wald", the tags say "waldweg" — prefix match, min 3
        scene = _scene(summary="Sie fahren durch den Wald.")
        moments = [_labeled_moment("track", ["waldweg"])]
        check = check_scene_footage(scene, [_report(moments=moments)])
        assert any(
            "footage mentions" in f and "wald" in f for f in check["findings"]
        )

    def test_content_mismatch_is_worded_carefully(self):
        moments = [_labeled_moment("beach volleyball", ["sand", "sonne"])]
        scene = _scene(
            heading="INT. KELLER - NIGHT", summary="Verhör im Keller."
        )
        check = check_scene_footage(scene, [_report(moments=moments)])
        assert check["content_checked"] is True
        assert any(
            "No overlap" in f and "what Claude saw" in f
            for f in check["findings"]
        )
        # mismatched content never scores above the baseline
        assert check["score"] <= 0.5

    def test_slug_words_do_not_count_as_content(self):
        # "night" in the tags must not read as overlap with the heading slug
        moments = [_labeled_moment("dark frame", ["night"])]
        scene = _scene(heading="EXT. MEER - NIGHT", summary="Wellen.")
        check = check_scene_footage(scene, [_report(moments=moments)])
        assert not any("footage mentions" in f for f in check["findings"])

    def test_ext_heading_with_outdoor_labels_is_consistent(self):
        moments = [_labeled_moment("mountain road", ["mountain", "road", "sky"])]
        scene = _scene(heading="EXT. PASS - DAY", summary="Fahrt über den Pass.")
        check = check_scene_footage(scene, [_report(moments=moments)])
        assert any(
            "lean outdoor" in f and "EXT." in f for f in check["findings"]
        )

    def test_int_heading_with_outdoor_labels_is_flagged(self):
        moments = [_labeled_moment("forest road", ["wald", "berg", "himmel"])]
        scene = _scene(heading="INT. KÜCHE - DAY", summary="Frühstück.")
        check = check_scene_footage(scene, [_report(moments=moments)])
        assert any(
            "heading says INT." in f and "lean outdoor" in f
            for f in check["findings"]
        )

    def test_day_night_wordlists_score(self):
        day_moments = [_labeled_moment("sunny meadow", ["sonnig", "wiese"])]
        scene = _scene(heading="EXT. WIESE - DAY", summary="Picknick.")
        check = check_scene_footage(scene, [_report(moments=day_moments)])
        assert any(
            "daytime" in f and "DAY heading" in f for f in check["findings"]
        )
        # +0.25 overlap (wiese) +0.15 ext +0.1 day
        assert check["score"] == pytest.approx(1.0)

    def test_night_heading_vs_daytime_labels_is_flagged(self):
        moments = [_labeled_moment("bright sunny street", ["sunny", "daylight"])]
        scene = _scene(heading="EXT. STRASSE - NIGHT", summary="Flucht.")
        check = check_scene_footage(scene, [_report(moments=moments)])
        assert any(
            "heading says NIGHT" in f and "daytime" in f
            for f in check["findings"]
        )

    def test_dark_footage_fits_a_night_scene(self):
        reports = [
            _report(
                "dark.mp4", 0.15, notes=["85% unusable: mostly too dark"],
                moments=[_labeled_moment("dark forest road", ["wald", "nacht"])],
            )
        ]
        scene = _scene(heading="EXT. WALDWEG - NIGHT", summary="Im Wald.")
        check = check_scene_footage(scene, reports)
        softened = [f for f in check["findings"] if "fits a NIGHT scene" in f]
        assert softened and "dark.mp4" in softened[0]
        # softened, not doubled: the raw unusable warning is gone
        assert not any("85% unusable" in f for f in check["findings"])

    def test_dark_footage_in_a_day_scene_stays_a_warning(self):
        reports = [
            _report("dark.mp4", 0.15, notes=["85% unusable: mostly too dark"]),
        ]
        scene = _scene(heading="EXT. WIESE - DAY", summary="Picknick.")
        check = check_scene_footage(scene, reports)
        assert any("85% unusable: mostly too dark" in f for f in check["findings"])
        assert not any("fits a NIGHT scene" in f for f in check["findings"])
