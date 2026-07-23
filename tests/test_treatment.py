"""Tests for the Regie-Vorschlag (monteur.treatment).

The AI seam (monteur.ai.complete) is monkeypatched; these exercise the
dossier/prompt, the normalisation of a reply, the graceful/strict failure
semantics and the treatment→brief fold.
"""

from __future__ import annotations

import json

import pytest

from monteur import ai, treatment
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import ClipReport, Moment


def make_reports(vision: bool = False) -> list[ClipReport]:
    reports = []
    for name in ("a", "b"):
        moments = [Moment(i * 10.0, i * 10.0 + 5.0, 0.8 - i * 0.05) for i in range(4)]
        reports.append(ClipReport(path=f"/f/{name}.mp4", duration=60.0, moments=moments))
    if vision:
        m = reports[0].moments[0]
        m.label = "sunrise over the pass"
        m.tags = ["sunrise", "mountains"]
        m.daylight = "golden"
        m.shot_size = "wide"
        m.hero = 0.9
    return reports


def make_music() -> MusicAnalysis:
    return MusicAnalysis(
        path="/m/song.wav", duration=40.0, tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[
            MusicSection(0.0, 13.0, 0.2, "low"),
            MusicSection(13.0, 27.0, 0.5, "mid"),
            MusicSection(27.0, 40.0, 0.9, "high"),
        ],
        drops=[27.0],
    )


def fake_complete(reply, calls=None):
    def _complete(prompt, *, system="", json_schema=None, **kwargs):
        if calls is not None:
            calls.append({"prompt": prompt, "system": system, "json_schema": json_schema})
        return reply if isinstance(reply, str) else json.dumps(reply)

    return _complete


def full_reply() -> dict:
    return {
        "format": "trailer", "style": "trailer", "energy": "varied",
        "mood": "episch", "platform": "youtube", "length_seconds": 45.0,
        "grade": "cinematic", "hook": "der Gipfel-Shot",
        "rationale": "Weite Landschaften wollen einen atmenden Bogen.",
    }


def test_dossier_summarizes_footage_music_and_brief(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(full_reply(), calls))
    treatment.propose_treatment(
        make_reports(vision=True), make_music(), brief="Alpen, episch"
    )
    prompt = calls[0]["prompt"]
    assert "FOOTAGE:" in prompt and "MUSIC:" in prompt
    assert "sunrise over the pass" in prompt  # a vision label reaches the dossier
    assert "120 bpm" in prompt and "1 drop" in prompt
    assert "Alpen, episch" in prompt
    assert calls[0]["json_schema"] == treatment.TREATMENT_SCHEMA


def test_dossier_flags_missing_vision_and_music(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(full_reply(), calls))
    treatment.propose_treatment(make_reports(vision=False), None, brief="")
    prompt = calls[0]["prompt"]
    assert "no vision labels yet" in prompt
    assert "MUSIC: none chosen yet" in prompt
    assert "(none given" in prompt


def test_propose_returns_a_normalized_treatment(monkeypatch):
    monkeypatch.setattr(ai, "complete", fake_complete(full_reply()))
    t = treatment.propose_treatment(make_reports(), make_music(), brief="x")
    assert t["format"] == "trailer"
    assert t["energy"] == "varied"
    assert t["length_seconds"] == 45.0
    assert set(t) == {
        "format", "style", "energy", "mood", "platform",
        "length_seconds", "grade", "hook", "rationale",
    }


def test_propose_normalizes_garbage_to_safe_values(monkeypatch):
    bad = {
        "format": "documentary", "style": "nonsense", "energy": "sleepy",
        "platform": "imax", "length_seconds": 9999, "grade": "psychedelic",
        "mood": "x" * 200, "rationale": "ok",
    }
    monkeypatch.setattr(ai, "complete", fake_complete(bad))
    t = treatment.propose_treatment(make_reports(), None)
    assert t["format"] == "montage"  # unknown -> montage
    assert t["style"] == "music_video"  # unknown -> the format's default style
    assert t["energy"] == "varied"  # unknown -> varied
    assert t["platform"] == "youtube"
    assert t["length_seconds"] == 0.0  # out of band -> unset
    assert t["grade"] == "neutral"
    assert len(t["mood"]) <= 60


def test_propose_degrades_gracefully_when_the_backend_is_down(monkeypatch):
    def boom(*a, **k):
        raise ai.MonteurAIError("no backend")

    monkeypatch.setattr(ai, "complete", boom)
    t = treatment.propose_treatment(make_reports(), None)
    assert t == pytest.approx(t)  # a dict is returned, not an exception
    assert t["format"] == "montage"
    assert "nicht verfügbar" in t["rationale"]


def test_propose_strict_raises(monkeypatch):
    def boom(*a, **k):
        raise ai.MonteurAIError("no backend")

    monkeypatch.setattr(ai, "complete", boom)
    with pytest.raises(ai.MonteurAIError):
        treatment.propose_treatment(make_reports(), None, strict=True)


def test_treatment_to_brief_weaves_the_directive():
    brief = treatment.treatment_to_brief(full_reply(), "Alpen Motorradtour")
    assert brief.startswith("REGIE: trailer, Stimmung episch.")
    assert "variables Tempo" in brief  # varied energy spelled out
    assert "Kalt öffnen auf: der Gipfel-Shot." in brief
    assert brief.rstrip().endswith("Alpen Motorradtour")  # user's words last


def test_treatment_to_brief_maps_each_energy():
    for energy, needle in (
        ("driving", "treibende"),
        ("calm", "atmend durchgehend"),
        ("varied", "variables Tempo"),
    ):
        t = dict(full_reply(), energy=energy)
        assert needle in treatment.treatment_to_brief(t)


def test_treatment_max_seconds():
    assert treatment.treatment_max_seconds(full_reply()) == 45.0
    assert treatment.treatment_max_seconds(dict(full_reply(), length_seconds=0)) is None
    assert treatment.treatment_max_seconds({}) is None
