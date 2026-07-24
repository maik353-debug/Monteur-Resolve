"""Learned editing preferences from the user's corrections (blueprint 4.3).

The closed loop's third turn: after Monteur cuts and the user FIXES a few
things in the Studio — swaps a slot for a closer shot, changes a dissolve
to a hard cut, drags a boundary longer — those corrections should teach
the next cut, not evaporate. This module is that memory.

It stores the ABSTRACT signal, never the literal edit. Not "clip B over
clip A in slot 7" (that never recurs) but the DIRECTION it implies —
"prefers close-ups in the climax", "fewer dissolves", "longer holds in
calm phases". One correction proves nothing; only a REPEATED signal
(:data:`_MIN_COUNT`) becomes active and folds into the next plan as a
small :class:`monteur.montage.CastingBias` tie-breaker — never over sync,
the drop, the rhythm order or zero-repeat. An empty store folds in
nothing, so a fresh user's cut is byte-identical to today.

The file lives beside drafts.json / settings.json and follows the same
rules (see :mod:`monteur.drafts`): stdlib-only, trivially inspectable,
atomic temp-file + ``os.replace`` writes, and every read failure degrades
to "nothing learned yet" instead of taking the Studio down. Tests point
``MONTEUR_PREFERENCES_PATH`` at a scratch file to stay out of the real
home directory. The store is inspectable (:func:`inspect`) and resettable
(:func:`reset`) — the Studio can show "what Monteur learned" and offer to
forget it.

The file shape is ``{"signals": {key: {"count": int, "last": iso}}}``
where ``key`` is ``"family::context::direction"`` — e.g.
``"shot_size::climax::close"`` or ``"transition::*::cut"``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from monteur.montage import _PREF_SHOT_WEIGHT, CastingBias

#: Environment variable overriding the preferences file location (tests).
PREFERENCES_PATH_ENV = "MONTEUR_PREFERENCES_PATH"

#: How many times a signal must repeat before it is ACTIVE (folds into a
#: plan). Conservative by design: one swap tips nothing — a preference is
#: a pattern, not a single click.
_MIN_COUNT = 2

#: The signal families this store understands. A recorded signal outside
#: these is kept (inspectable) but never actuated — forward tolerance.
_SHOT_SIZES = frozenset({"wide", "medium", "close"})


def preferences_path() -> Path:
    """Where preferences live: ``$MONTEUR_PREFERENCES_PATH`` or ``~/.monteur/preferences.json``."""
    override = os.environ.get(PREFERENCES_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".monteur" / "preferences.json"


def _key(family: str, context: str, direction: str) -> str:
    return f"{family}::{context or '*'}::{direction}"


def _load() -> dict:
    """The stored signals; any read failure degrades to an empty store.

    Preferences are a convenience, never a gate — a missing file, bad JSON
    or a wrong shape must not take the Studio down, so all degrade to
    "nothing learned yet".
    """
    try:
        data = json.loads(preferences_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"signals": {}}
    if not isinstance(data, dict) or not isinstance(data.get("signals"), dict):
        return {"signals": {}}
    signals = {
        str(k): v
        for k, v in data["signals"].items()
        if isinstance(v, dict) and isinstance(v.get("count"), int)
    }
    return {"signals": signals}


def _write(data: dict) -> None:
    """Atomically replace the store (temp file + ``os.replace``, like drafts)."""
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def record_signal(family: str, context: str, direction: str) -> dict:
    """Record ONE abstract correction signal; returns the updated store.

    ``family`` is the kind ("shot_size", "transition", "hold"), ``context``
    the where ("climax", a phase label, or "*" for any), ``direction`` the
    way it points ("close", "cut", "longer"). Increments the signal's
    count and re-stamps ``last`` (ISO-8601 UTC). Idempotent shape: an
    unknown family is still stored (inspectable) but simply never actuates.
    """
    family = str(family or "").strip()
    context = str(context or "*").strip() or "*"
    direction = str(direction or "").strip()
    if not family or not direction:
        raise ValueError("a preference signal needs a family and a direction")
    data = _load()
    key = _key(family, context, direction)
    entry = data["signals"].get(key) or {"count": 0}
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data["signals"][key] = entry
    _write(data)
    return data


def _active(data: dict | None = None) -> list[tuple[str, str, str, int]]:
    """Active signals as ``(family, context, direction, count)``, count-sorted.

    Active = repeated at least :data:`_MIN_COUNT` times. Deterministic
    order (count desc, then key) so the derived bias is reproducible.
    """
    data = data if data is not None else _load()
    out: list[tuple[str, str, str, int]] = []
    for key, entry in data["signals"].items():
        count = int(entry.get("count", 0))
        if count < _MIN_COUNT:
            continue
        parts = key.split("::")
        if len(parts) != 3:
            continue
        family, context, direction = parts
        out.append((family, context, direction, count))
    out.sort(key=lambda t: (-t[3], t[0], t[1], t[2]))
    return out


def casting_bias() -> CastingBias | None:
    """Fold the ACTIVE learned signals into a :class:`monteur.montage.CastingBias`.

    Returns None when nothing is active (the empty-store / fresh-user
    fallback — planning is then byte-identical to today). Deterministic
    given the store. Only the families the engine can actuate become bias:

    * ``shot_size`` (climax/phase, direction close/medium/wide) → a small
      per-phase casting bonus for that size;
    * ``avoid_shot_size`` (same shape) → the SAME weight with the opposite
      sign. Lifting a shot out of the cut is the honest inverse of casting
      one in: it says "not this size here", which the casting score can act
      on directly because :meth:`CastingBias.size_bonus` sums the weights;
    * ``transition`` direction ``cut`` → ``fewer_dissolves``;
    * ``shot_length`` direction ``shorter``/``longer`` → one pace notch
      faster/slower. Both directions active cancel out, which is the right
      answer: an editor who trims both ways has no pace preference.

    Other recorded signals stay inspectable but do not (yet) actuate.
    """
    active = _active()
    if not active:
        return None
    shot: list[tuple[str, str, float]] = []
    fewer = False
    notches = 0
    for family, context, direction, _count in active:
        if family == "shot_size" and direction in _SHOT_SIZES:
            shot.append((context, direction, _PREF_SHOT_WEIGHT))
        elif family == "avoid_shot_size" and direction in _SHOT_SIZES:
            shot.append((context, direction, -_PREF_SHOT_WEIGHT))
        elif family == "transition" and direction == "cut":
            fewer = True
        elif family == "shot_length" and direction == "shorter":
            notches -= 1
        elif family == "shot_length" and direction == "longer":
            notches += 1
    bias = CastingBias(
        shot_size=tuple(shot),
        fewer_dissolves=fewer,
        pace_notches=max(-1, min(1, notches)),
    )
    return None if bias.is_neutral() else bias


def inspect() -> dict:
    """A human-inspectable view of everything learned (the "what Monteur learned" panel).

    ``{"signals": [{family, context, direction, count, active}, ...],
    "active": <count of active signals>}`` — newest-strongest first.
    Read-only; never fails (an unreadable store reads as empty).
    """
    data = _load()
    rows: list[dict] = []
    for key, entry in data["signals"].items():
        parts = key.split("::")
        if len(parts) != 3:
            continue
        family, context, direction = parts
        count = int(entry.get("count", 0))
        rows.append(
            {
                "family": family,
                "context": context,
                "direction": direction,
                "count": count,
                "active": count >= _MIN_COUNT,
                "last": str(entry.get("last") or ""),
            }
        )
    rows.sort(key=lambda r: (not r["active"], -r["count"], r["family"]))
    return {"signals": rows, "active": sum(1 for r in rows if r["active"])}


def reset() -> bool:
    """Forget everything Monteur learned; True when a store existed.

    The "reset what Monteur learned" action. Removes the file (an absent
    store is already empty, so the next plan folds in nothing).
    """
    path = preferences_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Best-effort: an un-deletable file is neutralised by truncation.
        try:
            _write({"signals": {}})
            return True
        except OSError:
            return False
