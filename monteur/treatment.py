"""Regie-Vorschlag — Claude proposes a creative treatment from the footage.

The composer needs to be TOLD what kind of video to cut (format, pace, mood,
platform); told nothing, it falls back to a generic default. This module
removes the blank page: it reads the ALREADY-analysed footage (moments,
vision labels, daylight, motion, hero shots) and the music, and asks Claude
— as a creative director — to PROPOSE the single best treatment. The Studio
shows the proposal as editable chips the user confirms or tweaks; the
confirmed treatment then folds into the composer's brief (:func:`treatment_to_brief`)
and the build's ``style`` / ``platform`` / ``max_duration``.

So "say nothing" no longer means "generic default" — it means "Claude looks
at your material and suggests something grounded in it". Everything here is
pure over the reports + music (no video is re-decoded); the AI seam is
:func:`monteur.ai.complete`, monkeypatched in tests.
"""

from __future__ import annotations

import json
from collections import Counter

from monteur import ai
from monteur.montage import STYLES
from monteur.music import MusicAnalysis
from monteur.sift import ClipReport

#: Reasoning depth for the treatment call — a short creative-direction task
#: over an already-analysed dossier, so it does not need the deepest thinking
#: (and on the CLI backend the default "high" can reason for minutes).
TREATMENT_EFFORT = "medium"

#: The video formats the treatment can propose (the Studio's chips mirror these).
FORMATS = ("montage", "trailer", "informative", "one_shot", "mini_film")
#: The three pacing energies — each maps to a concrete composer directive.
ENERGIES = ("driving", "varied", "calm")
#: Delivery targets — the build's own platform presets (aspect + length cap).
#: ``youtube`` is 16:9 landscape; ``reel`` / ``short`` / ``tiktok`` are 9:16
#: vertical. Kept identical to :data:`monteur.montage.PLATFORMS` so a chosen
#: platform passes straight into the build's ``resolve_platform``.
PLATFORMS = ("youtube", "reel", "short", "tiktok")
#: Grade looks the color step understands as a starting point.
GRADES = ("neutral", "warm", "cool", "cinematic", "vibrant")

#: A format with no explicit style maps to this montage style key.
_FORMAT_STYLE = {
    "montage": "music_video",
    "trailer": "trailer",
    "informative": "auto",
    "one_shot": "auto",
    "mini_film": "travel",
}

#: Each energy, spelled out for the composer's own pace / pace_by_phase logic.
_ENERGY_BRIEF = {
    "driving": (
        "durchgehend schnelle, treibende Schnitte auf die Musik — konstante Energie"
    ),
    "varied": (
        "variables Tempo über den Bogen: ruhiger, atmender Einstieg mit langen "
        "Einstellungen, schneller Höhepunkt, wieder ruhiger Ausklang"
    ),
    "calm": (
        "ruhig und atmend durchgehend — lange Einstellungen, kein hektisches "
        "Schneiden, jeder Shot bekommt Zeit"
    ),
}

TREATMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "format": {
            "type": "string",
            "enum": list(FORMATS),
            "description": (
                "the kind of video: montage (cut to music), trailer (escalating "
                "with act titles), informative (text-driven / explainer), one_shot "
                "(a single flowing take feel), mini_film (a short narrative)"
            ),
        },
        "style": {
            "type": "string",
            "description": "a montage style key (auto/trailer/travel/wedding/music_video/short)",
        },
        "energy": {
            "type": "string",
            "enum": list(ENERGIES),
            "description": "pacing: driving (fast throughout), varied (slow-fast-slow arc), calm (slow throughout)",
        },
        "mood": {
            "type": "string",
            "description": "one or two words for the feeling (e.g. 'episch', 'kontemplativ', 'verspielt')",
        },
        "platform": {
            "type": "string",
            "enum": list(PLATFORMS),
            "description": "youtube (16:9 landscape), reel / short / tiktok (9:16 vertical)",
        },
        "length_seconds": {
            "type": "number",
            "description": "target length in seconds for this format/platform",
        },
        "grade": {
            "type": "string",
            "enum": list(GRADES),
            "description": "a starting color look",
        },
        "hook": {
            "type": "string",
            "description": "the single opening image — name the clip/moment that should cold-open the cut",
        },
        "rationale": {
            "type": "string",
            "description": "one honest sentence: why THIS treatment fits THIS footage",
        },
    },
    "required": ["format", "energy", "mood", "rationale"],
}

_SYSTEM = (
    "You are a creative director and senior editor. Given a pool of already-"
    "analysed footage, a piece of music and the client's brief, you propose the "
    "SINGLE best treatment for a social-media video (YouTube / Reels / Shorts) — "
    "the format, the montage style, the pacing energy, the mood, the delivery "
    "platform, the length, a starting grade, and which single image should "
    "cold-open the cut. Every choice is GROUNDED in what the footage actually is: "
    "landscapes want a calmer, breathing treatment; fast action wants driving "
    "cuts; a story with people wants a varied arc. Honour the brief when it says "
    "something; when it is thin, make a strong, opinionated proposal from the "
    "material rather than a safe generic default. Answer only with the structured "
    "treatment."
)


def _r(value: float) -> float:
    return round(float(value), 2)


def _dossier(reports: list[ClipReport], music: MusicAnalysis | None, brief: str) -> str:
    """A compact, honest summary of the pool + music + brief for the prompt."""
    clips = len(reports)
    moments = [m for r in reports for m in (r.moments or [])]
    total_moments = len(moments)
    footage = sum(float(getattr(r, "duration", 0.0) or 0.0) for r in reports)

    labels = [str(getattr(m, "label", "")).strip() for m in moments]
    labels = [x for x in labels if x]
    tags = [t for m in moments for t in (getattr(m, "tags", None) or [])]
    daylight = Counter(
        str(getattr(m, "daylight", "")).strip()
        for m in moments
        if str(getattr(m, "daylight", "")).strip()
    )
    shot_sizes = Counter(
        str(getattr(m, "shot_size", "")).strip()
        for m in moments
        if str(getattr(m, "shot_size", "")).strip()
    )
    heroes = sum(1 for m in moments if float(getattr(m, "hero", 0.0) or 0.0) >= 0.5)

    lines: list[str] = []
    lines.append(
        f"FOOTAGE: {clips} clip(s), {total_moments} good moment(s), "
        f"~{_r(footage)}s total."
    )
    if labels:
        sample = list(dict.fromkeys(labels))[:10]
        lines.append("what the moments show: " + "; ".join(sample))
    else:
        lines.append(
            "no vision labels yet — judge from motion/scores (a Claude watch "
            "would sharpen this)."
        )
    if tags:
        top = [t for t, _ in Counter(tags).most_common(8)]
        lines.append("recurring tags: " + ", ".join(top))
    if daylight:
        lines.append(
            "light: "
            + ", ".join(f"{k} ×{n}" for k, n in daylight.most_common())
        )
    if shot_sizes:
        lines.append(
            "shot sizes: "
            + ", ".join(f"{k} ×{n}" for k, n in shot_sizes.most_common())
        )
    if heroes:
        lines.append(f"hero moments (strong single images): {heroes}")

    if music is not None:
        tempo = float(getattr(music, "tempo", 0.0) or 0.0)
        dur = float(getattr(music, "duration", 0.0) or 0.0)
        bits = [f"MUSIC: ~{_r(dur)}s"]
        if tempo:
            bits.append(f"{tempo:.0f} bpm")
        drops = list(getattr(music, "drops", None) or [])
        if drops:
            bits.append(f"{len(drops)} drop(s)")
        sections = list(getattr(music, "sections", None) or [])
        if sections:
            arc = "→".join(str(getattr(s, "label", "")) for s in sections if getattr(s, "label", ""))
            if arc:
                bits.append(f"energy arc {arc}")
        lines.append(", ".join(bits) + ".")
    else:
        lines.append("MUSIC: none chosen yet.")

    brief = (brief or "").strip()
    lines.append(f"BRIEF: {brief}" if brief else "BRIEF: (none given — propose from the material)")
    return "\n".join(lines)


def _normalize(data: dict) -> dict:
    """Validate a parsed reply into a safe, complete treatment dict."""
    out: dict = {}
    fmt = str(data.get("format") or "montage")
    out["format"] = fmt if fmt in FORMATS else "montage"

    style = str(data.get("style") or "").strip()
    out["style"] = style if style in STYLES else _FORMAT_STYLE.get(out["format"], "auto")

    energy = str(data.get("energy") or "").strip()
    out["energy"] = energy if energy in ENERGIES else "varied"

    out["mood"] = str(data.get("mood") or "").strip()[:60]

    platform = str(data.get("platform") or "").strip()
    out["platform"] = platform if platform in PLATFORMS else "youtube"

    try:
        length = float(data.get("length_seconds"))
    except (TypeError, ValueError):
        length = 0.0
    out["length_seconds"] = round(length, 1) if 3.0 <= length <= 600.0 else 0.0

    grade = str(data.get("grade") or "").strip()
    out["grade"] = grade if grade in GRADES else "neutral"

    out["hook"] = str(data.get("hook") or "").strip()[:120]
    out["rationale"] = str(data.get("rationale") or "").strip()[:280]
    return out


def default_treatment() -> dict:
    """A neutral treatment for when no proposal could be made (offline/empty)."""
    return _normalize({})


def propose_treatment(
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    *,
    brief: str = "",
    strict: bool = False,
    on_text=None,
) -> dict:
    """Ask Claude for a creative treatment; return a normalized dict.

    ``reports`` / ``music`` are the project's own analysed pool (no re-decode).
    With ``strict=False`` (the default) an unreachable backend or unparseable
    reply degrades to :func:`default_treatment` with a ``rationale`` saying so —
    the Studio still shows editable chips. ``strict=True`` raises
    :class:`monteur.ai.MonteurAIError`.
    """
    prompt = (
        _dossier(reports, music, brief)
        + "\n\nPropose the single best treatment for this material as the "
        "structured object. Ground every field in the footage above."
    )
    try:
        raw = ai.complete(
            prompt, system=_SYSTEM, json_schema=TREATMENT_SCHEMA,
            effort=TREATMENT_EFFORT, on_delta=on_text,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("the reply is not a JSON object")
    except (ai.MonteurAIError, ValueError) as exc:
        if strict:
            raise ai.MonteurAIError(str(exc)) from exc
        fallback = default_treatment()
        fallback["rationale"] = f"Vorschlag nicht verfügbar ({exc}); neutraler Start."
        return fallback
    return _normalize(data)


def treatment_to_brief(treatment: dict, base_brief: str = "") -> str:
    """Fold a confirmed treatment into a crisp directive brief for the composer.

    Weaves the format, mood, pacing energy and hook into one directive the
    composer reads (it then sets pace / pace_by_phase / holds to match), then
    appends the user's own brief text so their words still lead.
    """
    t = _normalize(treatment) if treatment else default_treatment()
    parts: list[str] = []
    fmt = t["format"].replace("_", " ")
    lead = f"REGIE: {fmt}"
    if t["mood"]:
        lead += f", Stimmung {t['mood']}"
    parts.append(lead + ".")
    parts.append("Tempo: " + _ENERGY_BRIEF.get(t["energy"], _ENERGY_BRIEF["varied"]) + ".")
    if t["hook"]:
        parts.append(f"Kalt öffnen auf: {t['hook']}.")
    base = (base_brief or "").strip()
    if base:
        parts.append(base)
    return " ".join(parts)


def treatment_max_seconds(treatment: dict) -> float | None:
    """The treatment's target length as a ``max_duration`` (None if unset)."""
    t = _normalize(treatment) if treatment else default_treatment()
    return t["length_seconds"] or None
