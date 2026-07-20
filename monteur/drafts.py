"""Persistent Create-wizard drafts for Monteur Studio (stdlib only).

The Studio's Create wizard lives entirely in the browser page: the footage
folder, the chosen song, every style/canvas/pace control and — most
importantly — the finished build result with its full ``plan_json``. Close
the tab or reload and all of it is gone, which makes "save this cut as
work-in-progress and continue tomorrow" impossible. This module is that
missing memory: one plain JSON file of draft records,

    ~/.monteur/drafts.json

next to the settings file and following the same rules (see
:mod:`monteur.settings`): stdlib-only, trivially inspectable, atomic
temp-file + ``os.replace`` writes so a crash mid-save never leaves a torn
file, and every read failure (missing file, bad JSON, wrong shape) degrades
to "no drafts" instead of taking the Studio down. Unlike settings the file
holds no secrets, so there is no 0600 chmod. Tests point
``MONTEUR_DRAFTS_PATH`` at a scratch file to stay out of the real home
directory.

The file shape is ``{"drafts": [record, ...]}``, newest first. A record is
what the Studio needs to restore the wizard completely:

* ``id`` — uuid4 hex, stamped by :func:`save_draft` when absent.
* ``name`` — the user's label (or "Auto-saved cut" for the autosave).
* ``saved_at`` — ISO-8601 UTC timestamp, stamped on every save.
* ``folder`` / ``music`` — the wizard's step-1/2 paths.
* ``settings`` — the build payload's control values (audio, style, canvas,
  fps, format, …) exactly as the browser sent them.
* ``plan_json`` — the FULL plan in the :func:`monteur.montage.plan_to_dict`
  save format; this is what makes a draft resumable without re-planning.
* ``summary`` — ``{"duration", "cuts", "style"}`` derived from the plan so
  the draft list can describe a cut without carrying the heavy plan.
* optionally ``pins``, ``review``, ``notes`` — whatever revision/director
  state the browser wants restored.

Two kinds of records share the store: NAMED drafts (explicit "Save draft"
clicks; capped at the :data:`MAX_DRAFTS` newest — a WIP store, not an
archive) and ONE autosave slot with the fixed id ``"autosave"``, written by
the server after every successful build/revise/apply so a browser reload
never loses the last good cut. The autosave replaces itself instead of
growing the list and does not count against the cap, but IS returned by
:func:`list_drafts` (flagged ``"autosave": true``) so the UI can label it.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

#: Environment variable overriding the drafts file location (tests).
DRAFTS_PATH_ENV = "MONTEUR_DRAFTS_PATH"

#: How many NAMED drafts the store keeps (oldest beyond this are dropped).
#: The autosave slot lives outside this cap.
MAX_DRAFTS = 20

#: The fixed id of the single autosave slot.
AUTOSAVE_ID = "autosave"

#: The keys :func:`list_drafts` copies into the light per-draft view.
_LIST_KEYS = ("id", "name", "saved_at", "folder", "music", "settings", "summary")

# plan_montage always writes a 'style "<key>": <name>' note into the plan
# (monteur.revise.style_from_plan reads the same note) — the one place a
# plan remembers which style built it.
_STYLE_NOTE_RE = re.compile(r'^style "([a-z_]+)"')


def drafts_path() -> Path:
    """Where the drafts live: ``$MONTEUR_DRAFTS_PATH`` or ``~/.monteur/drafts.json``."""
    override = os.environ.get(DRAFTS_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".monteur" / "drafts.json"


def _load_all() -> list[dict]:
    """Every stored record, newest first; any read failure is just ``[]``.

    Drafts are a convenience, never a gate — a half-written or hand-mangled
    file must not take the Studio down, so a missing file, bad JSON or a
    JSON shape that is not ``{"drafts": [dict, ...]}`` all degrade to "no
    drafts yet".
    """
    try:
        data = json.loads(drafts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    records = data.get("drafts")
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("id")]


def _write_all(records: list[dict]) -> None:
    """Atomically replace the store (temp file + ``os.replace``, like settings)."""
    path = drafts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(
        json.dumps({"drafts": records}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _summary_from_plan(plan_json: dict) -> dict:
    """``{"duration", "cuts", "style"}`` — the list view's one-line description."""
    style = "auto"
    for note in plan_json.get("notes") or []:
        match = _STYLE_NOTE_RE.match(str(note))
        if match:
            style = match.group(1)
            break
    try:
        duration = float(plan_json.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    entries = plan_json.get("entries")
    return {
        "duration": duration,
        "cuts": len(entries) if isinstance(entries, list) else 0,
        "style": style,
    }


def list_drafts() -> list[dict]:
    """The light draft list, newest first — WITHOUT the heavy ``plan_json``.

    Each entry carries ``id``, ``name``, ``saved_at``, ``folder``, ``music``,
    ``settings`` and ``summary`` (plus ``"autosave": true`` on the autosave
    slot) — everything the "Continue where you left off" panel shows. The
    full record, plan included, comes from :func:`load_draft`.
    """
    views = []
    for record in _load_all():
        view = {key: record.get(key) for key in _LIST_KEYS}
        if record.get("autosave") or record.get("id") == AUTOSAVE_ID:
            view["autosave"] = True
        views.append(view)
    views.sort(key=lambda v: str(v.get("saved_at") or ""), reverse=True)
    return views


def load_draft(draft_id: str) -> dict | None:
    """The full stored record (``plan_json`` included), or None when unknown."""
    for record in _load_all():
        if record.get("id") == draft_id:
            return dict(record)
    return None


def save_draft(record: dict) -> dict:
    """Validate, stamp and store ``record``; returns the stored record.

    The minimal resumable shape is enforced — ``folder`` and ``plan_json``
    are what a resume cannot work without, so their absence is a ValueError
    with a user-ready message (the web server surfaces it as a 400).

    Stamps ``id`` (uuid4 hex) when absent and ``saved_at`` (ISO-8601 UTC)
    always, upserts by id, and derives ``summary`` from the plan when the
    caller did not provide one. Named drafts are capped at the
    :data:`MAX_DRAFTS` newest — the oldest beyond that are dropped, because
    this is a WIP store, not an archive. A record with ``"autosave": true``
    instead REPLACES the single autosave slot (fixed id ``"autosave"``),
    which never counts against the cap.
    """
    if not isinstance(record, dict):
        raise ValueError("a draft must be a JSON object")
    if not str(record.get("folder") or "").strip():
        raise ValueError("a draft needs 'folder' (the footage folder it was cut from)")
    if not isinstance(record.get("plan_json"), dict):
        raise ValueError("a draft needs 'plan_json' (the plan a build result carries)")

    stored = dict(record)
    if stored.get("autosave"):
        stored["autosave"] = True
        stored["id"] = AUTOSAVE_ID
    elif not str(stored.get("id") or "").strip():
        stored["id"] = uuid.uuid4().hex
    else:
        stored["id"] = str(stored["id"])
    stored["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not isinstance(stored.get("summary"), dict):
        stored["summary"] = _summary_from_plan(stored["plan_json"])
    if not str(stored.get("name") or "").strip():
        stored["name"] = "Auto-saved cut" if stored.get("autosave") else "Draft cut"

    records = [r for r in _load_all() if r.get("id") != stored["id"]]
    records.insert(0, stored)  # newest first
    named = [r for r in records if r.get("id") != AUTOSAVE_ID]
    autosaves = [r for r in records if r.get("id") == AUTOSAVE_ID]
    _write_all(autosaves + named[:MAX_DRAFTS])
    return stored


def delete_draft(draft_id: str) -> bool:
    """Remove a draft (the autosave slot included); True when one was removed."""
    records = _load_all()
    kept = [r for r in records if r.get("id") != draft_id]
    if len(kept) == len(records):
        return False
    _write_all(kept)
    return True
