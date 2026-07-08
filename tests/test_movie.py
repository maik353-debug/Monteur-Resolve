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
