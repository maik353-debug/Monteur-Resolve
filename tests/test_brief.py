"""Tests for monteur/brief.py — natural-language montage briefs."""

from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

from monteur.ai import MonteurAIError
from monteur.brief import (
    BriefSettings,
    interpret_brief,
    interpret_brief_offline,
    merge_brief,
    resolve_brief,
)

# --- offline interpreter ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("90 sekunden", 90.0),
        ("90 seconds", 90.0),
        ("60s", 60.0),
        ("2 minuten", 120.0),
        ("2 minutes", 120.0),
        ("etwa 3 min bitte", 180.0),
        ("1:30", 90.0),
        ("maximal 1:05 lang", 65.0),
        ("1,5 minuten", 90.0),
        ("keine dauer genannt", None),
    ],
)
def test_offline_duration(text, expected):
    assert interpret_brief_offline(text).max_duration == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("unsere Reise durch Island", "travel"),
        ("a travel film", "travel"),
        ("Urlaub am Meer", "travel"),
        ("Hochzeit von Anna und Ben", "wedding"),
        ("wedding highlights", "wedding"),
        ("ein Musikvideo", "music_video"),
        ("cut it like a music video", "music_video"),
        ("ein Trailer", "trailer"),
        ("short teaser please", "trailer"),
        ("einfach schoen", "auto"),
    ],
)
def test_offline_style(text, expected):
    assert interpret_brief_offline(text).style == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("beste zuerst", "best_first"),
        ("best moments first", "best_first"),
        ("die besten Momente nach vorn", "best_first"),
        ("nur die Highlights", "best_first"),
        ("chronologisch bitte", "chronological"),
        ("", "chronological"),
    ],
)
def test_offline_order(text, expected):
    assert interpret_brief_offline(text).order == expected


def test_offline_energy_words_fall_back_to_music_video():
    settings = interpret_brief_offline("90 sekunden, energiegeladen")
    assert settings.style == "music_video"
    assert settings.max_duration == 90.0

    # energy alone, English
    assert interpret_brief_offline("fast and energetic").style == "music_video"


def test_offline_energy_does_not_override_explicit_style():
    settings = interpret_brief_offline("schneller Trailer")
    assert settings.style == "trailer"

    settings = interpret_brief_offline("fast-paced wedding film")
    assert settings.style == "wedding"


def test_offline_empty_text_yields_defaults():
    settings = interpret_brief_offline("")
    assert settings == BriefSettings(
        style="auto", order="chronological", max_duration=None,
        rationale=settings.rationale,
    )
    assert "no cues" in settings.rationale


def test_offline_rationale_lists_recognized_cues():
    rationale = interpret_brief_offline("2 minuten Hochzeit, beste zuerst").rationale
    assert "duration 120s" in rationale
    assert "wedding" in rationale
    assert "best_first" in rationale


# --- AI interpreter (mocked client) -----------------------------------------------


def _fake_client(payload: dict):
    block = mock.Mock()
    block.type = "text"
    block.text = json.dumps(payload)
    response = mock.Mock()
    response.content = [block]
    response.stop_reason = "end_turn"
    client = mock.Mock()
    client.messages.create.return_value = response
    return client


def test_interpret_brief_parses_structured_response():
    client = _fake_client(
        {
            "style": "music_video",
            "order": "best_first",
            "max_duration": 90,
            "rationale": "Energiegeladen und 90 Sekunden -> Musikvideo-Schnitt.",
        }
    )
    with mock.patch("monteur.brief._client", return_value=client):
        settings = interpret_brief("90 Sekunden, energiegeladen, beste zuerst")
    assert settings.style == "music_video"
    assert settings.order == "best_first"
    assert settings.max_duration == 90.0
    assert "Musikvideo" in settings.rationale

    kwargs = client.messages.create.call_args.kwargs
    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == {
        "style", "order", "max_duration", "rationale",
    }
    assert kwargs["max_tokens"] == 1024


def test_interpret_brief_invalid_style_falls_back_with_note():
    client = _fake_client(
        {"style": "vlog", "order": "best_first", "max_duration": None,
         "rationale": "vlog style"}
    )
    with mock.patch("monteur.brief._client", return_value=client):
        settings = interpret_brief("mach ein vlog")
    assert settings.style == "auto"
    assert settings.order == "best_first"
    assert "unknown style" in settings.rationale


def test_interpret_brief_missing_anthropic_raises():
    with mock.patch.dict(sys.modules, {"anthropic": None}):
        with pytest.raises(MonteurAIError, match=r"monteur\[ai\]"):
            interpret_brief("90 sekunden")


def test_interpret_brief_unparseable_text_raises():
    client = _fake_client({})
    client.messages.create.return_value.content[0].text = "not json"
    with mock.patch("monteur.brief._client", return_value=client):
        with pytest.raises(MonteurAIError, match="unparseable"):
            interpret_brief("90 sekunden")


# --- resolve_brief ----------------------------------------------------------------


def test_resolve_brief_falls_back_to_offline_on_ai_error():
    with mock.patch(
        "monteur.brief.interpret_brief", side_effect=MonteurAIError("no creds")
    ):
        settings = resolve_brief("90 sekunden hochzeit")
    assert settings.style == "wedding"
    assert settings.max_duration == 90.0
    assert settings.rationale.startswith("(offline interpretation) ")


def test_resolve_brief_use_ai_false_never_calls_ai():
    with mock.patch(
        "monteur.brief.interpret_brief",
        side_effect=AssertionError("AI must not be called"),
    ):
        settings = resolve_brief("2 minuten trailer", use_ai=False)
    assert settings.style == "trailer"
    assert settings.max_duration == 120.0
    assert settings.rationale.startswith("(offline interpretation) ")


def test_resolve_brief_prefers_ai_result():
    ai_settings = BriefSettings(style="travel", rationale="from the model")
    with mock.patch("monteur.brief.interpret_brief", return_value=ai_settings):
        assert resolve_brief("eine reise") is ai_settings


# --- CLI wiring -------------------------------------------------------------------


def test_build_parser_accepts_brief():
    from monteur.cli import build_parser

    args = build_parser().parse_args(
        ["create", "clips/", "song.mp3", "-o", "out.fcpxml",
         "--brief", "90 Sekunden, energiegeladen"]
    )
    assert args.brief == "90 Sekunden, energiegeladen"
    # defaults untouched at parse time — merging happens in cmd_create
    assert args.style == "auto"
    assert args.order == "chronological"
    assert args.max_duration is None


def test_merge_brief_explicit_flags_win():
    settings = BriefSettings(style="music_video", order="best_first", max_duration=90.0)
    # user passed --style travel explicitly; order/max-duration at defaults
    style, order, max_duration = merge_brief("travel", "chronological", None, settings)
    assert style == "travel"          # explicit flag wins over the brief
    assert order == "best_first"      # default -> taken from the brief
    assert max_duration == 90.0       # default -> taken from the brief


def test_merge_brief_all_defaults_takes_brief():
    settings = BriefSettings(style="wedding", order="best_first", max_duration=120.0)
    assert merge_brief("auto", "chronological", None, settings) == (
        "wedding", "best_first", 120.0,
    )


def test_merge_brief_all_explicit_ignores_brief():
    settings = BriefSettings(style="wedding", order="best_first", max_duration=120.0)
    assert merge_brief("trailer", "best_first", 30.0, settings) == (
        "trailer", "best_first", 30.0,
    )
