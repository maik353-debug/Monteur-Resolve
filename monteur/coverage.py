"""Coverage check: which shots are still missing — BEFORE the cut.

Monteur already rates the shots (:mod:`monteur.sift`) and knows what the
video should become (style + brief). This module turns that knowledge
around: instead of judging a finished plan it looks at the raw footage
and answers "what do I still have to film?" — a concrete shot list the
editor can take back out with the camera, while filming is still
possible. It is the PRE-CUT sibling of Director's Notes
(:mod:`monteur.director`), which reviews a planned cut afterwards.

Two functions, two layers:

* :func:`coverage_basics` — deterministic facts, no AI, always works:
  usable seconds vs the target length (the montage planner's own
  no-repeat rule: with repeats off a cut never outgrows the unique
  material, so a longer target means a shortened cut), role
  coverage (openers/closers/heroes from the vision roles — zero openers
  is a finding), scene-group variety (everything in one group is a
  finding) and the unusable share. Pure and testable.
* :func:`missing_shots` — the gap list. ONE completion through the AI
  seam :func:`monteur.ai.complete` (:data:`MISSING_SCHEMA`): the style's
  craft brief (:data:`monteur.compose.CRAFT_BRIEFS` — the same editorial
  grammar the composer cuts by), the editor's own brief, the
  :func:`coverage_basics` facts and the compact moment inventory
  (basename, window, score, vision fields — lean like the director's
  dossier). Text-only, so it runs over an API key OR the Claude Code
  CLI — with Claude Code there is **no extra API cost**. A
  :class:`monteur.ai.MonteurAIError` passes through unchanged; a
  parseable but structurally odd reply is repaired defensively (score
  clamped to 0-100, malformed entries dropped, the list capped at
  :data:`MISSING_LIMIT`).

Vision is an upgrade, not a gate: without annotations the prompt says the
material is unlabeled and the result's ``notes`` recommend a "Let Claude
watch your clips" scan — the same pattern the composer uses.
"""

from __future__ import annotations

import json
from pathlib import Path

from monteur import ai
from monteur import montage as _montage
from monteur.compose import CRAFT_BRIEFS, _has_vision
from monteur.sift import ClipReport

_EPS = 1e-6

#: A moment with at least this much hero strength counts as a hero shot
#: (the same threshold ``monteur find`` and the Studio's hero pill use).
_HERO_MIN = 0.5

#: How many missing-shot entries the validated result may carry — a shot
#: list longer than this stops being a plan and becomes homework.
MISSING_LIMIT = 10

#: The vision roles counted by :func:`coverage_basics`, in arc order.
_ROLES = ("opener", "build", "climax", "closer")

#: The structured-output contract for :func:`missing_shots`. The API
#: backend enforces it; the CLI backend gets it as an instruction — either
#: way the parsed dict is re-validated by :func:`_validate_missing`.
MISSING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "description": "one-line coverage verdict"},
        "coverage_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "have": {
            "type": "array",
            "items": {"type": "string"},
            "description": "what the material already delivers, short bullets",
        },
        "missing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "shot": {"type": "string", "description": "what to film, concrete"},
                    "why": {"type": "string", "description": "which role/act it serves"},
                    "priority": {"type": "string", "enum": ["must", "nice"]},
                    "tip": {
                        "type": "string",
                        "description": "how to film it: angle/length/light, one sentence",
                    },
                },
                "required": ["shot", "why", "priority", "tip"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "coverage_score", "have", "missing", "summary"],
}

#: The editorial stance of the coverage check — practical, filmable, honest.
_SYSTEM = (
    "You are a seasoned film editor — a Schnittmeister — doing a coverage "
    "check BEFORE the cut, while the editor can still go out and film. "
    "Judge the shot inventory against what the intended form needs: an "
    "opener that establishes place or subject, builds, a climax or hero "
    "moment for the drop, a closer that resolves, scene variety, and "
    "enough usable length. Name honestly what is already there, then list "
    "the missing shots concretely enough to put on a shot list. Be "
    "practical: only shots this editor can realistically film for this "
    "video — no fantasy gear, no invented locations. When the material is "
    "unlabeled, reason from counts, scores and durations without "
    "inventing content."
)


def _r(value: float) -> float:
    """Round for the facts/prompt — neither needs 15 decimals."""
    return round(float(value), 2)


def coverage_basics(
    reports: list[ClipReport],
    style: str = "auto",
    target_seconds: float | None = None,
) -> dict:
    """Deterministic coverage facts from the sifted (+vision) reports.

    No AI, no I/O — pure math over what the sift and an optional vision
    pass already know::

        {"style": str, "clips": int, "moments": int,
         "usable_seconds": float,          # deduplicated moment material
         "max_comfortable_seconds": float, # the no-repeat maximum (== usable)
         "unusable_share": float,          # 0..1 across all clip durations
         "roles": {"opener": n, "build": n, "climax": n, "closer": n},
         "heroes": int,                    # moments with hero >= 0.5
         "groups": int,                    # distinct scene groups
         "vision": bool,                   # any annotations at all
         "target_seconds": float,          # only when a target was given
         "repetition_risk": bool,          # only when a target was given
         "findings": [str, ...]}           # plain-language flags

    ``usable_seconds`` reuses the montage planner's own unique-material
    measure, and ``repetition_risk`` its no-repeat rule: a target longer
    than the usable material either shortens the cut (repeats off, the
    default) or visibly repeats footage (repeats on) —
    ``max_comfortable_seconds`` therefore equals the usable material,
    the honest maximum a repeat-free cut can reach. The role/group
    findings (zero openers, zero closers, no hero, everything one scene
    group) only fire when vision annotations exist — unlabeled material
    is unknown, not missing.
    """
    chosen = _montage.STYLES.get(style, _montage.STYLES["auto"])
    usable = _montage._unique_material(reports)
    vision = _has_vision(reports)

    roles = {role: 0 for role in _ROLES}
    heroes = 0
    groups: set[str] = set()
    moments = 0
    for report in reports:
        for m in report.moments:
            moments += 1
            if m.role in roles:
                roles[m.role] += 1
            if m.hero >= _HERO_MIN:
                heroes += 1
            if m.group:
                groups.add(m.group)

    total_duration = sum(r.duration for r in reports)
    usable_time = sum(r.usable_ratio * r.duration for r in reports)
    unusable_share = 0.0
    if total_duration > _EPS:
        unusable_share = min(1.0, max(0.0, 1.0 - usable_time / total_duration))

    facts: dict = {
        "style": chosen.key,
        "clips": len(reports),
        "moments": moments,
        "usable_seconds": _r(usable),
        "max_comfortable_seconds": _r(usable),
        "unusable_share": _r(unusable_share),
        "roles": roles,
        "heroes": heroes,
        "groups": len(groups),
        "vision": vision,
    }

    findings: list[str] = []
    if target_seconds is not None and target_seconds > _EPS:
        facts["target_seconds"] = _r(target_seconds)
        risk = target_seconds > usable + _EPS
        facts["repetition_risk"] = risk
        if risk:
            findings.append(
                f"only {usable:.0f}s of unique material for a "
                f"{target_seconds:.0f}s target — the cut shortens to the "
                "material (or repeats footage if repeats are allowed)"
            )
    if total_duration > _EPS and unusable_share >= 0.5:
        findings.append(
            f"{round(unusable_share * 100)}% of the footage is unusable "
            "(too dark, blurry or shaky)"
        )
    if vision:
        # Role/group gaps are only knowable when Claude actually looked.
        if roles["opener"] == 0:
            findings.append("no opener — nothing establishes place or subject")
        if roles["closer"] == 0:
            findings.append("no closer — nothing to end the cut on")
        if heroes == 0:
            findings.append("no hero shot — nothing strong enough for the drop")
        if len(groups) == 1 and moments >= 2:
            findings.append(
                "everything is one scene group — the cut will feel repetitive"
            )
    facts["findings"] = findings
    return facts


def _inventory(reports: list[ClipReport]) -> list[dict]:
    """The compact moment inventory for the prompt (lean like the dossier).

    One dict per moment: clip basename, window, score, and the vision
    fields (label/tags/role/hero/group) only when present — empty fields
    are omitted so an unseen inventory stays small and honest.
    """
    items: list[dict] = []
    for report in reports:
        for m in report.moments:
            item: dict = {
                "clip": Path(report.path).name,
                "start": _r(m.start),
                "end": _r(m.end),
                "score": _r(m.score),
            }
            if m.label:
                item["label"] = m.label
            if m.tags:
                item["tags"] = list(m.tags)
            if m.role:
                item["role"] = m.role
            if m.hero > _EPS:
                item["hero"] = _r(m.hero)
            if m.group:
                item["group"] = m.group
            if getattr(m, "daylight", ""):
                item["daylight"] = m.daylight
            items.append(item)
    return items


def _build_prompt(basics: dict, inventory: list[dict], style: str, brief: str) -> str:
    """The one coverage prompt: craft brief + editor's brief + facts + shots."""
    craft = CRAFT_BRIEFS.get(style, CRAFT_BRIEFS["auto"])
    parts = [
        f"STYLE: {basics['style']}\nCRAFT (how this form is cut):\n{craft}"
    ]
    if brief.strip():
        parts.append(
            "THE EDITOR'S BRIEF (what this video should become):\n"
            + brief.strip()
        )
    if basics.get("vision"):
        parts.append(
            "The inventory carries vision labels — judge coverage by "
            "MEANING: what the shots show and which roles they can fill."
        )
    else:
        parts.append(
            "No vision labels are available for this footage — judge "
            "coverage by counts, scores and durations; do not invent "
            "content. (A 'Let Claude watch your clips' scan would make "
            "this far sharper.)"
        )
    parts.append(
        "COVERAGE FACTS (computed from the scan):\n"
        + json.dumps(basics, ensure_ascii=False)
    )
    parts.append(
        "SHOT INVENTORY (every usable moment):\n"
        + json.dumps(inventory, ensure_ascii=False)
    )
    parts.append(
        "List what is still missing to cut this video well:\n"
        "- `have`: what the material already delivers — short, honest "
        "bullets;\n"
        "- `missing`: the shots to film BEFORE the cut, most important "
        "first — each `shot` concrete and filmable (subject, angle, "
        "moment), `why` names the role or act it serves, `priority` is "
        '"must" (the cut suffers without it) or "nice" (an upgrade), '
        "`tip` is ONE sentence on how to film it (angle, length, light); "
        f"at most {MISSING_LIMIT} entries, never shots the inventory "
        "already covers;\n"
        "- `coverage_score` is 0-100 (100 = everything the cut needs is "
        "already here);\n"
        "- `verdict` is one line; `summary` wraps up in 2-3 sentences."
    )
    return "\n\n".join(parts)


def _validate_missing(data) -> dict:
    """Defensively normalise a parsed coverage reply.

    Missing keys become sensible defaults, ``coverage_score`` is clamped
    to 0-100 (missing/non-numeric reads as a neutral 50), ``have`` entries
    are coerced to non-empty strings, and ``missing`` entries are dropped
    when they are not dicts or carry no ``shot``; an unknown ``priority``
    degrades to ``"nice"`` (a wrong urgency must not invent urgency). The
    list is capped at :data:`MISSING_LIMIT`.
    """
    if not isinstance(data, dict):
        data = {}
    try:
        score = int(data.get("coverage_score"))
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))
    have = [str(h).strip() for h in data.get("have") or [] if str(h).strip()]
    missing: list[dict] = []
    for raw in data.get("missing") or []:
        if not isinstance(raw, dict):
            continue
        shot = str(raw.get("shot") or "").strip()
        if not shot:
            continue
        priority = str(raw.get("priority") or "").strip().lower()
        if priority != "must":
            priority = "nice"
        missing.append(
            {
                "shot": shot,
                "why": str(raw.get("why") or "").strip(),
                "priority": priority,
                "tip": str(raw.get("tip") or "").strip(),
            }
        )
        if len(missing) >= MISSING_LIMIT:
            break
    return {
        "verdict": str(data.get("verdict") or ""),
        "coverage_score": score,
        "have": have,
        "missing": missing,
        "summary": str(data.get("summary") or ""),
    }


def missing_shots(
    reports: list[ClipReport],
    style: str = "auto",
    brief: str = "",
    target_seconds: float | None = None,
) -> dict:
    """Ask Claude which shots are still missing for this video.

    Builds the :func:`coverage_basics` facts and the compact inventory,
    sends ONE completion through :func:`monteur.ai.complete` with
    :data:`MISSING_SCHEMA` and the coverage system prompt, and returns
    the validated dict::

        {"verdict": str, "coverage_score": int 0-100, "have": [str],
         "missing": [{"shot", "why", "priority": "must"|"nice", "tip"}],
         "summary": str,
         "basics": <the coverage_basics dict>,   # the deterministic layer
         "notes": [str]}                          # e.g. the vision hint

    ``brief`` is the editor's own words ("what should this video
    become?"); ``style`` picks the craft brief the coverage is judged
    against. Raises :class:`monteur.ai.MonteurAIError` unchanged when no
    backend is reachable, the request fails, or the reply is not
    parseable JSON; a structurally odd but parseable reply is repaired by
    :func:`_validate_missing` instead of raising. Without vision labels
    the check still works (judged on counts and durations) and ``notes``
    recommends the "Let Claude watch your clips" scan.
    """
    basics = coverage_basics(reports, style, target_seconds)
    prompt = _build_prompt(basics, _inventory(reports), style, brief)
    raw = ai.complete(prompt, system=_SYSTEM, json_schema=MISSING_SCHEMA)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ai.MonteurAIError(
            f"the coverage check came back as unparseable JSON: {raw[:200]!r}"
        ) from exc
    result = _validate_missing(data)
    result["basics"] = basics
    notes: list[str] = []
    if not basics.get("vision"):
        notes.append(
            'no vision labels — run "Let Claude watch your clips" '
            "(monteur see) for a sharper shot list"
        )
    result["notes"] = notes
    return result
