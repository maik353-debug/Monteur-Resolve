"""Tests for the movie creator, stage 1 (monteur.movie)."""

from __future__ import annotations

import json

import pytest

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


def _use_api_client(monkeypatch, fake):
    """Route monteur.ai.complete to the API backend with a fake SDK client."""
    monkeypatch.setenv("MONTEUR_AI_BACKEND", "api")
    monkeypatch.setattr("monteur.ai._client", lambda: fake)
    return fake


def test_generate_movie_builds_validated_project(monkeypatch):
    fake = _use_api_client(monkeypatch, _FakeClient(_blueprint_json()))
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
    _use_api_client(monkeypatch, _FakeClient(payload))
    with pytest.raises(MonteurAIError, match="at least 3"):
        generate_movie("zu dünn")


def test_generate_movie_clamps_scene_count(monkeypatch):
    payload = _blueprint_json(30)
    _use_api_client(monkeypatch, _FakeClient(payload))
    project = generate_movie("episch")
    assert len(project.scenes) == 24
    assert any("clamped" in n for n in project.notes)


def test_render_fountain_is_assembly_compatible(monkeypatch):
    _use_api_client(monkeypatch, _FakeClient(_blueprint_json()))
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
    _use_api_client(monkeypatch, _FakeClient(_blueprint_json()))
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


# --- stage 3: the assembly engine ----------------------------------------------------


import shutil
from pathlib import Path

from monteur.movie import assemble_movie, parse_cut_intent, scene_duration_target

DEMO = Path(
    "/tmp/claude-0/-home-user-Fable-tool/90401078-872b-52b4-9d55-214193ea4ea5"
    "/scratchpad/demo-footage"
)
needs_demo = pytest.mark.skipif(not DEMO.is_dir(), reason="demo footage not available")


def _sift_report(path, duration=30.0, n_moments=6):
    """A synthetic sifted clip: n_moments 3s moments, chronological, scored."""
    moments = [
        Moment(start=4.0 * i, end=4.0 * i + 3.0, score=0.9 - 0.05 * i)
        for i in range(n_moments)
    ]
    return ClipReport(
        path=str(path), duration=duration, moments=moments, usable_ratio=0.9
    )


def _movie_scene(number, heading="EXT. WALDWEG - NIGHT", folder="", cut_intent="",
                 dialogue=(), action="a", summary=""):
    return MovieScene(
        number=number,
        heading=heading,
        summary=summary or f"Szene {number}",
        action=action,
        dialogue=list(dialogue),
        cut_intent=cut_intent,
        folder=folder,
        status="assigned" if folder else "planned",
    )


def _movie(scenes):
    return MovieProject(
        title="Nachtfahrt", genre="thriller", brief="b", logline="l", scenes=scenes
    )


@pytest.fixture(scope="module")
def demo_cache():
    """Sift the demo footage once for the whole module (it decodes video)."""
    from monteur.sift import sift_directory

    return {str(DEMO): sift_directory(str(DEMO))}


class TestSceneDurationTarget:
    def test_base_only(self):
        scene = _movie_scene(1, action="")
        assert scene_duration_target(scene) == 6.0

    def test_action_words_add_time(self):
        scene = _movie_scene(1, action="eins zwei drei vier fünf sechs sieben acht")
        assert scene_duration_target(scene) == 8.0  # 6 + 8/4

    def test_dialogue_lines_add_time(self):
        scene = _movie_scene(
            1,
            action="eins zwei drei vier fünf sechs sieben acht",
            dialogue=[DialogueLine("A", "hi"), DialogueLine("B", "ho")],
        )
        assert scene_duration_target(scene) == 13.0  # 6 + 2*2.5 + 2

    def test_action_contribution_is_capped_at_ten(self):
        scene = _movie_scene(1, action="wort " * 100)
        assert scene_duration_target(scene) == 16.0  # 6 + min(10, 25)

    def test_clamped_to_45(self):
        scene = _movie_scene(
            1,
            action="wort " * 100,
            dialogue=[DialogueLine("A", "x")] * 16,  # 6 + 40 + 10 = 56
        )
        assert scene_duration_target(scene) == 45.0

    def test_never_below_four(self):
        # the base alone is 6s, so the low clamp is a safety net
        assert scene_duration_target(_movie_scene(1, action="")) >= 4.0


class TestParseCutIntent:
    @pytest.mark.parametrize(
        ("intent", "expected"),
        [
            ("", (2.0, "cuts")),
            ("ruhig halten", (3.0, "cuts")),
            ("calm opening", (3.0, "cuts")),
            ("langsamer Aufbau", (3.0, "cuts")),
            ("slow build", (3.0, "cuts")),
            ("schnell geschnitten", (1.0, "cuts")),
            ("fast and punchy", (1.0, "cuts")),
            ("hektisch, atemlos", (1.0, "cuts")),
            ("snappy energy", (1.0, "cuts")),
            ("weiche Blende in die nächste Szene", (2.0, "dissolves")),
            ("dissolve to black", (2.0, "dissolves")),
            ("weich enden", (2.0, "dissolves")),
            ("harter Schnitt", (2.0, "cuts")),
            ("hard cut into the chase", (2.0, "cuts")),
            ("ruhig, weiche Blenden", (3.0, "dissolves")),
            # hard-cut words win over dissolve words
            ("harter Schnitt, keine Blende", (2.0, "cuts")),
            # calm wins when both pace cues appear
            ("ruhig, dann schnell", (3.0, "cuts")),
            # the stage-1 fixture intent
            ("ruhig halten, harter Schnitt in die nächste Szene", (3.0, "cuts")),
        ],
    )
    def test_table(self, intent, expected):
        assert parse_cut_intent(intent) == expected


class TestAssembleMovie:
    def test_nothing_assigned_is_value_error(self):
        project = _movie([_movie_scene(1), _movie_scene(2)])
        with pytest.raises(ValueError, match="no scene has footage assigned"):
            assemble_movie(project)

    def test_unknown_canvas_is_value_error(self):
        project = _movie([_movie_scene(1, folder="/footage")])
        with pytest.raises(ValueError, match="canvas.*hd"):
            assemble_movie(project, canvas="imax")

    def test_skipped_scene_note_and_summary(self):
        cache = {"/footage/auto": [_sift_report("/footage/auto/clip.mp4")]}
        project = _movie(
            [
                _movie_scene(1, heading="INT. AUTO - NIGHT", folder="/footage/auto"),
                _movie_scene(2, heading="EXT. WALDWEG - NIGHT"),
            ]
        )
        timeline, notes = assemble_movie(project, sift_cache=cache)
        assert notes[0].startswith("assembled 'Nachtfahrt': 1 of 2 scenes,")
        assert notes[0].endswith("at 25 fps")
        assert "scene lengths estimated from the script — trim in Resolve" in notes
        assert (
            "scene 2 (EXT. WALDWEG - NIGHT): no footage assigned — skipped" in notes
        )
        assert timeline.video_clips()  # scene 1 was assembled

    def test_take_files_restrict_scene_material(self):
        cache = {
            "/f": [
                _sift_report("/f/S01_T01.mp4"),
                _sift_report("/f/S01_T02.MP4"),
                _sift_report("/f/S11_T01.mp4"),
                _sift_report("/f/broll.mp4"),
            ]
        }
        project = _movie([_movie_scene(1, folder="/f")])
        timeline, notes = assemble_movie(project, sift_cache=cache)
        sources = {Path(c.source_file).name for c in timeline.video_clips()}
        assert sources <= {"S01_T01.mp4", "S01_T02.MP4"}  # S11/broll excluded
        assert any("2 take files named S01_T##" in n for n in notes)

    def test_take_restriction_matches_the_right_scene_number(self):
        cache = {
            "/f": [_sift_report("/f/S01_T01.mp4"), _sift_report("/f/S11_T01.mp4")]
        }
        project = _movie([_movie_scene(11, folder="/f")])
        timeline, _notes = assemble_movie(project, sift_cache=cache)
        sources = {Path(c.source_file).name for c in timeline.video_clips()}
        assert sources == {"S11_T01.mp4"}

    def test_without_take_files_everything_plays(self):
        cache = {
            "/f": [_sift_report("/f/morgen.mp4"), _sift_report("/f/abend.mp4")]
        }
        project = _movie(
            [_movie_scene(1, folder="/f", action="wort " * 60)]  # 21s target
        )
        timeline, notes = assemble_movie(project, sift_cache=cache)
        sources = {Path(c.source_file).name for c in timeline.video_clips()}
        assert sources == {"morgen.mp4", "abend.mp4"}
        assert not any("take file" in n for n in notes)

    def test_dissolve_intent_hands_over_between_scenes(self):
        cache = {
            "/a": [_sift_report("/a/x.mp4")],
            "/b": [_sift_report("/b/y.mp4")],
        }
        project = _movie(
            [
                _movie_scene(1, folder="/a", cut_intent="weiche Blende zur nächsten"),
                _movie_scene(2, folder="/b", cut_intent="harter Schnitt"),
            ]
        )
        timeline, _notes = assemble_movie(project, sift_cache=cache)
        clips = timeline.video_clips()
        scene2_start = timeline.markers[1].frame
        incoming = next(c for c in clips if c.record_in == scene2_start)
        assert incoming.metadata["transition"] == "dissolve"
        assert incoming.metadata["transition_frames"] > 0
        # the film's very first clip never dissolves in
        assert "transition" not in clips[0].metadata

    def test_hard_cut_between_scenes_by_default(self):
        cache = {
            "/a": [_sift_report("/a/x.mp4")],
            "/b": [_sift_report("/b/y.mp4")],
        }
        project = _movie(
            [_movie_scene(1, folder="/a"), _movie_scene(2, folder="/b")]
        )
        timeline, _notes = assemble_movie(project, sift_cache=cache)
        scene2_start = timeline.markers[1].frame
        incoming = next(
            c for c in timeline.video_clips() if c.record_in == scene2_start
        )
        assert "transition" not in incoming.metadata

    def test_dialogue_with_transcripts_recommends_assembly(self, tmp_path):
        clip = tmp_path / "take.mp4"
        clip.touch()
        (tmp_path / "take.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        cache = {str(tmp_path): [_sift_report(clip)]}
        project = _movie(
            [
                _movie_scene(
                    5, folder=str(tmp_path),
                    dialogue=[DialogueLine("LENA", "Halt an.")],
                )
            ]
        )
        _timeline, notes = assemble_movie(project, sift_cache=cache)
        assert (
            "scene 5 has dialogue and transcripts — consider 'monteur assembly' "
            "for line-accurate takes; assembled visually here" in notes
        )

    def test_dialogue_without_transcripts_stays_quiet(self, tmp_path):
        clip = tmp_path / "take.mp4"
        clip.touch()
        cache = {str(tmp_path): [_sift_report(clip)]}
        project = _movie(
            [
                _movie_scene(
                    5, folder=str(tmp_path),
                    dialogue=[DialogueLine("LENA", "Halt an.")],
                )
            ]
        )
        _timeline, notes = assemble_movie(project, sift_cache=cache)
        assert not any("monteur assembly" in n for n in notes)

    def test_sift_cache_reuse_and_population(self, monkeypatch):
        calls = []

        def fake_sift(folder, progress=None, cancel=None):
            calls.append(folder)
            return [_sift_report(f"{folder}/clip.mp4")]

        monkeypatch.setattr("monteur.sift.sift_directory", fake_sift)
        cache = {"/pre": [_sift_report("/pre/a.mp4")]}
        project = _movie(
            [
                _movie_scene(1, folder="/pre"),
                _movie_scene(2, folder="/neu"),
                _movie_scene(3, folder="/neu"),
            ]
        )
        _timeline, _notes = assemble_movie(project, sift_cache=cache)
        assert calls == ["/neu"]  # /pre reused; /neu sifted exactly once
        assert set(cache) == {"/pre", "/neu"}  # ... and added to the cache

    def test_progress_reports_scenes_and_forwards_sift_stages(self, monkeypatch):
        def fake_sift(folder, progress=None, cancel=None):
            report = _sift_report(f"{folder}/clip.mp4")
            if progress is not None:
                progress(1, 1, "clip.mp4", "start", None)
                progress(1, 1, "clip.mp4", "done", report)
            return [report]

        monkeypatch.setattr("monteur.sift.sift_directory", fake_sift)
        events = []
        project = _movie(
            [_movie_scene(1, heading="INT. AUTO - NIGHT", folder="/f"), _movie_scene(2)]
        )
        assemble_movie(
            project, progress=lambda i, t, n, s: events.append((i, t, n, s))
        )
        assert (1, 2, "INT. AUTO - NIGHT", "scene") in events
        assert (2, 2, "EXT. WALDWEG - NIGHT", "scene") in events  # skipped scenes too
        assert (1, 1, "clip.mp4", "start") in events
        assert (1, 1, "clip.mp4", "done") in events

    def test_broken_progress_callback_does_not_abort(self):
        cache = {"/f": [_sift_report("/f/x.mp4")]}
        project = _movie([_movie_scene(1, folder="/f")])

        def boom(*_args):
            raise RuntimeError("broken UI")

        timeline, _notes = assemble_movie(project, sift_cache=cache, progress=boom)
        assert timeline.video_clips()

    def test_unsiftable_folder_skips_scene_with_note(self, tmp_path):
        cache = {"/ok": [_sift_report("/ok/x.mp4")]}
        project = _movie(
            [
                _movie_scene(1, folder=str(tmp_path / "fehlt")),  # not a directory
                _movie_scene(2, folder="/ok"),
            ]
        )
        timeline, notes = assemble_movie(project, sift_cache=cache)
        assert any("scene 1" in n and "skipped" in n for n in notes)
        assert notes[0].startswith("assembled 'Nachtfahrt': 1 of 2 scenes")
        assert timeline.video_clips()

    def test_short_footage_shortens_scene_with_note(self):
        # one 3s moment against a 16s target: the repetition guard caps it
        cache = {"/f": [_sift_report("/f/x.mp4", n_moments=1)]}
        project = _movie([_movie_scene(1, folder="/f", action="wort " * 100)])
        timeline, notes = assemble_movie(project, sift_cache=cache)
        assert any("scene runs short" in n for n in notes)
        assert timeline.duration_seconds < 16.0


@needs_demo
class TestAssembleMovieDemo:
    """Geometry tests against a real sift of the demo footage."""

    def test_two_scene_film_tiles_contiguously(self, demo_cache):
        project = _movie(
            [
                _movie_scene(
                    1, heading="INT. AUTO - NIGHT", folder=str(DEMO),
                    cut_intent="ruhig", summary="Die Fahrt beginnt.",
                ),
                _movie_scene(
                    2, heading="EXT. WALDWEG - NIGHT", folder=str(DEMO),
                    cut_intent="schnell", summary="Der Wald schluckt das Licht.",
                ),
            ]
        )
        timeline, notes = assemble_movie(project, fps=25.0, sift_cache=demo_cache)

        # scenes tile the timeline contiguously: no gaps, no overlaps
        video = timeline.video_clips()
        assert video and video[0].record_in == 0
        for prev, nxt in zip(video, video[1:]):
            assert nxt.record_in == prev.record_out
        assert video[-1].record_out == timeline.duration

        # one Blue marker per scene start, note = the scene summary
        assert [m.name for m in timeline.markers] == [
            "Scene 1: INT. AUTO - NIGHT",
            "Scene 2: EXT. WALDWEG - NIGHT",
        ]
        assert all(m.color == "Blue" for m in timeline.markers)
        assert timeline.markers[0].frame == 0
        assert timeline.markers[0].note == "Die Fahrt beginnt."
        # scene 2 starts exactly where a video clip starts
        assert timeline.markers[1].frame in {c.record_in for c in video}

        # every video clip carries its own sound on A1, same ranges
        audio = timeline.audio_clips()
        assert {c.track for c in audio} == {"A1"}
        assert len(audio) == len(video)
        for v, a in zip(video, audio):
            assert (a.record_in, a.record_out) == (v.record_in, v.record_out)
            assert (a.source_in, a.source_out) == (v.source_in, v.source_out)
            assert a.source_file == v.source_file

        # the calm scene cuts slower than the fast scene
        split = timeline.markers[1].frame
        scene1 = [c for c in video if c.record_in < split]
        scene2 = [c for c in video if c.record_in >= split]
        asl1 = sum(c.duration for c in scene1) / len(scene1)
        asl2 = sum(c.duration for c in scene2) / len(scene2)
        assert asl1 > asl2

        assert notes[0].startswith("assembled 'Nachtfahrt': 2 of 2 scenes,")
        assert notes[0].endswith("at 25 fps")

    def test_take_files_from_real_footage(self, tmp_path, demo_cache):
        # cheap file copies: two demo clips become scene-1 takes, one stays b-roll
        clips = sorted(DEMO.glob("*.mp4"))
        shutil.copy(clips[0], tmp_path / "S01_T01.mp4")
        shutil.copy(clips[1], tmp_path / "S01_T02.mp4")
        shutil.copy(clips[2], tmp_path / "broll.mp4")
        project = _movie([_movie_scene(1, folder=str(tmp_path))])
        timeline, notes = assemble_movie(project, sift_cache={})
        sources = {Path(c.source_file).name for c in timeline.video_clips()}
        assert sources and sources <= {"S01_T01.mp4", "S01_T02.mp4"}
        assert any("take files named S01_T##" in n for n in notes)


# --- stage 3: CLI (movie assemble / movie status) -----------------------------------


def test_cli_movie_assemble_parses():
    from monteur.cli import build_parser, cmd_movie_assemble

    args = build_parser().parse_args(
        ["movie", "assemble", "proj", "-o", "film.fcpxml", "--fps", "24",
         "--canvas", "hd"]
    )
    assert args.func is cmd_movie_assemble
    assert args.project_dir == "proj"
    assert args.output == "film.fcpxml"
    assert args.fps == 24.0
    assert args.canvas == "hd"


def test_cli_movie_assemble_defaults():
    from monteur.cli import build_parser

    args = build_parser().parse_args(["movie", "assemble", "proj", "-o", "f.edl"])
    assert args.fps == 25.0
    assert args.canvas == "uhd"


def test_cli_movie_assemble_writes_film(tmp_path, monkeypatch, capsys):
    from monteur.cli import build_parser

    monkeypatch.setattr(
        "monteur.sift.sift_directory",
        lambda folder, progress=None, cancel=None: [
            _sift_report(f"{folder}/clip.mp4")
        ],
    )
    project = _movie([_movie_scene(1, folder=str(tmp_path / "footage"))])
    save_project(project, tmp_path / "proj")
    out = tmp_path / "film.fcpxml"
    args = build_parser().parse_args(
        ["movie", "assemble", str(tmp_path / "proj"), "-o", str(out)]
    )
    args.func(args)
    printed = capsys.readouterr().out
    assert out.is_file()
    assert "assembled 'Nachtfahrt': 1 of 1 scenes" in printed
    assert "[scene 1/1] EXT. WALDWEG - NIGHT" in printed


def test_cli_movie_assemble_missing_project_fails_cleanly(tmp_path, capsys):
    from monteur.cli import build_parser

    args = build_parser().parse_args(
        ["movie", "assemble", str(tmp_path), "-o", str(tmp_path / "f.fcpxml")]
    )
    with pytest.raises(SystemExit):
        args.func(args)
    assert "movie new" in capsys.readouterr().err


def test_cli_movie_assemble_nothing_assigned_fails_cleanly(tmp_path, capsys):
    from monteur.cli import build_parser

    save_project(_movie([_movie_scene(1)]), tmp_path / "proj")
    args = build_parser().parse_args(
        ["movie", "assemble", str(tmp_path / "proj"), "-o", str(tmp_path / "f.edl")]
    )
    with pytest.raises(SystemExit):
        args.func(args)
    assert "no scene has footage assigned" in capsys.readouterr().err


def test_cli_movie_status(tmp_path, capsys):
    from monteur.cli import build_parser, cmd_movie_status

    project = _movie(
        [_movie_scene(1, folder="/footage/auto"), _movie_scene(2)]
    )
    save_project(project, tmp_path / "proj")
    args = build_parser().parse_args(["movie", "status", str(tmp_path / "proj")])
    assert args.func is cmd_movie_status
    args.func(args)
    printed = capsys.readouterr().out
    assert "Nachtfahrt — 1/2 scenes assigned (50%)" in printed
    assert "[x]  1" in printed and "-> /footage/auto" in printed
    assert "[ ]  2" in printed


def test_cli_movie_status_missing_project_fails_cleanly(tmp_path, capsys):
    from monteur.cli import build_parser

    args = build_parser().parse_args(["movie", "status", str(tmp_path)])
    with pytest.raises(SystemExit):
        args.func(args)
    assert "movie new" in capsys.readouterr().err
