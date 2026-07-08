"""Natural-language direction for automatic montages.

The user describes the cut they want in one sentence — e.g. ``"90 Sekunden,
energiegeladen, ruhig enden"`` or ``"a two minute wedding film, best moments
first"`` — and Monteur maps it to montage parameters (style, order, maximum
duration).

Two interpreters are provided:

* :func:`interpret_brief` — asks Claude (structured output, guaranteed JSON)
  to translate the brief. Needs the ``anthropic`` package and credentials,
  like everything in :mod:`monteur.ai`.
* :func:`interpret_brief_offline` — a dependency-free keyword fallback for
  German and English. Deliberately rough: it recognizes obvious duration,
  style, and order cues and ignores everything else.

:func:`resolve_brief` picks between them: AI when available, offline
otherwise (with the rationale prefixed ``"(offline interpretation) "``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from monteur.ai import DEFAULT_MODEL, MonteurAIError, _client

#: Montage style keys — mirrors monteur.montage.STYLES (kept as a literal so
#: this module stays importable without the media stack).
VALID_STYLES = ("auto", "travel", "wedding", "music_video", "trailer")
VALID_ORDERS = ("chronological", "best_first")

_DEFAULT_STYLE = "auto"
_DEFAULT_ORDER = "chronological"


@dataclass
class BriefSettings:
    """Montage parameters derived from a one-sentence editorial brief."""

    style: str = _DEFAULT_STYLE
    order: str = _DEFAULT_ORDER
    max_duration: float | None = None
    rationale: str = ""  # one sentence explaining the mapping, for display


# --- AI interpretation ----------------------------------------------------------

_BRIEF_SYSTEM = (
    "You translate a film editor's brief (German or English) into montage "
    "settings for an automatic first cut. Styles: 'auto' follows the song's "
    "energy; 'travel' is a chronological journey arc; 'wedding' is warm and "
    "unhurried; 'music_video' is fast, beat-driven cutting; 'trailer' builds "
    "tension toward a climax. Order 'chronological' keeps footage order, "
    "'best_first' puts the strongest material on the loudest music. "
    "max_duration is the cap in seconds, or null for the full song. Pick the "
    "closest match; when nothing fits, keep the defaults (auto, "
    "chronological, null). Explain your choices briefly in the language the "
    "brief is written in."
)

_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "style": {"type": "string", "enum": list(VALID_STYLES)},
        "order": {"type": "string", "enum": list(VALID_ORDERS)},
        "max_duration": {"type": ["number", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["style", "order", "max_duration", "rationale"],
    "additionalProperties": False,
}


def _validated(data: dict) -> BriefSettings:
    """Turn a (model-produced) dict into BriefSettings, defaulting on mismatch."""
    notes: list[str] = []
    style = data.get("style", _DEFAULT_STYLE)
    if style not in VALID_STYLES:
        notes.append(f"unknown style {style!r} — using '{_DEFAULT_STYLE}'")
        style = _DEFAULT_STYLE
    order = data.get("order", _DEFAULT_ORDER)
    if order not in VALID_ORDERS:
        notes.append(f"unknown order {order!r} — using '{_DEFAULT_ORDER}'")
        order = _DEFAULT_ORDER
    max_duration = data.get("max_duration")
    if max_duration is not None:
        try:
            max_duration = float(max_duration)
        except (TypeError, ValueError):
            notes.append(f"unusable max_duration {max_duration!r} — ignoring it")
            max_duration = None
        else:
            if max_duration <= 0:
                notes.append("non-positive max_duration — ignoring it")
                max_duration = None
    rationale = str(data.get("rationale", ""))
    if notes:
        rationale = (rationale + " " if rationale else "") + f"({'; '.join(notes)})"
    return BriefSettings(
        style=style, order=order, max_duration=max_duration, rationale=rationale
    )


def interpret_brief(text: str, model: str = DEFAULT_MODEL) -> BriefSettings:
    """Translate an editorial brief into montage settings via Claude.

    Uses structured output (a JSON schema on ``output_config.format``), so
    the response is guaranteed to be valid JSON matching the schema. Raises
    :class:`monteur.ai.MonteurAIError` when the ``anthropic`` package is
    missing or the API call fails.
    """
    client = _client()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_BRIEF_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _BRIEF_SCHEMA}},
            messages=[{"role": "user", "content": f"EDITOR'S BRIEF:\n{text}"}],
        )
    except MonteurAIError:
        raise
    except Exception as exc:  # pragma: no cover - network/auth failures
        raise MonteurAIError(f"Claude API request failed: {exc}") from exc
    if getattr(response, "stop_reason", None) == "refusal":
        raise MonteurAIError("The request was declined by the model's safety system.")
    raw = "".join(b.text for b in response.content if b.type == "text")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MonteurAIError(
            f"Claude returned unparseable brief settings: {raw[:200]!r}"
        ) from exc
    return _validated(data)


# --- Offline interpretation -------------------------------------------------------

_MMSS_RE = re.compile(r"\b(\d{1,3}):([0-5]\d)\b")
_MINUTES_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*min(?:\.|uten?|utes?|s)?\b", re.I)
_SECONDS_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:sek(?:\.|unden?)?|sec(?:\.|onds?)?|s)\b", re.I)

_STYLE_KEYWORDS = (
    ("travel", ("reise", "travel", "urlaub")),
    ("wedding", ("hochzeit", "wedding")),
    ("music_video", ("musikvideo", "music video", "musik-video")),
    ("trailer", ("trailer", "teaser")),
)
_ORDER_RE = re.compile(r"\bbest(?:e[nsr]?)?\b|\bhighlights?\b", re.I)
_ENERGY_WORDS = ("schnell", "fast", "energiegeladen", "energetic", "energisch")


def _num(raw: str) -> float:
    return float(raw.replace(",", "."))


def interpret_brief_offline(text: str) -> BriefSettings:
    """Keyword fallback (German + English) — rough by design, no dependencies.

    Recognizes: durations like "90 sekunden/seconds", "60s", "2 minuten/
    minutes/min", "1:30"; style keywords (reise/travel/urlaub -> travel,
    hochzeit/wedding -> wedding, musikvideo/music video -> music_video,
    trailer/teaser -> trailer); "beste zuerst"/"best"/"highlights" ->
    best_first; energy words (schnell/fast/energiegeladen/energetic) ->
    music_video, but only when no explicit style keyword matched. Anything
    else is silently ignored — for nuance, use the AI interpreter.
    """
    t = text.lower()
    recognized: list[str] = []
    settings = BriefSettings()

    match = _MMSS_RE.search(t)
    if match:
        settings.max_duration = int(match.group(1)) * 60 + int(match.group(2))
        recognized.append(f"duration {settings.max_duration:.0f}s ({match.group(0)!r})")
    else:
        match = _MINUTES_RE.search(t)
        if match:
            settings.max_duration = _num(match.group(1)) * 60
            recognized.append(
                f"duration {settings.max_duration:.0f}s ({match.group(0)!r})"
            )
        else:
            match = _SECONDS_RE.search(t)
            if match:
                settings.max_duration = _num(match.group(1))
                recognized.append(
                    f"duration {settings.max_duration:.0f}s ({match.group(0)!r})"
                )

    for style, keywords in _STYLE_KEYWORDS:
        hit = next((k for k in keywords if k in t), None)
        if hit:
            settings.style = style
            recognized.append(f"style {style} (keyword {hit!r})")
            break

    if _ORDER_RE.search(t):
        settings.order = "best_first"
        recognized.append("order best_first (best/highlights)")

    if settings.style == _DEFAULT_STYLE:
        energy = next((w for w in _ENERGY_WORDS if w in t), None)
        if energy:
            settings.style = "music_video"
            recognized.append(f"style music_video (energy word {energy!r})")

    if recognized:
        settings.rationale = "recognized: " + "; ".join(recognized)
    else:
        settings.rationale = "no cues recognized — keeping the defaults"
    return settings


# --- Resolution & merging ---------------------------------------------------------


def resolve_brief(text: str, use_ai: bool = True) -> BriefSettings:
    """Interpret a brief with Claude when possible, offline otherwise.

    Tries :func:`interpret_brief` when ``use_ai`` is true; any
    :class:`MonteurAIError` (missing package, missing credentials, request
    failure) falls back to :func:`interpret_brief_offline` with the rationale
    prefixed ``"(offline interpretation) "``.
    """
    if use_ai:
        try:
            return interpret_brief(text)
        except MonteurAIError:
            pass
    settings = interpret_brief_offline(text)
    settings.rationale = "(offline interpretation) " + settings.rationale
    return settings


def merge_brief(
    args_style: str,
    args_order: str,
    args_max_duration: float | None,
    settings: BriefSettings,
    defaults: tuple[str, str, float | None] = (_DEFAULT_STYLE, _DEFAULT_ORDER, None),
) -> tuple[str, str, float | None]:
    """Merge explicit CLI values with brief-derived settings.

    Explicit flags win: a value that differs from its default was passed by
    the user and is kept; values still at their default are overridden by
    the brief. Returns ``(style, order, max_duration)``.
    """
    default_style, default_order, default_max = defaults
    style = args_style if args_style != default_style else settings.style
    order = args_order if args_order != default_order else settings.order
    max_duration = (
        args_max_duration
        if args_max_duration != default_max
        else settings.max_duration
    )
    return style, order, max_duration
