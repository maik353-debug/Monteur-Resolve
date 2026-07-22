"""Persistent user settings for Monteur (stdlib only).

Monteur ships to editors who see a finished application, never a terminal —
so anything a user must be able to change (which way Claude is reached, the
Anthropic API key) needs a home outside environment variables. That home is
one plain JSON file:

    ~/.monteur/settings.json

Keys currently in use:

* ``ai_backend`` — ``"auto"`` (default), ``"api"`` or ``"claude-cli"``:
  how :mod:`monteur.ai` reaches Claude. Set from Studio's settings panel.
* ``api_key`` — the Anthropic API key pasted into Studio; ``""`` = none.
* ``resolve_python`` — path to the Python interpreter the isolated DaVinci
  Resolve worker runs under (Resolve's native module needs a 64-bit Python
  ~3.6–3.11); ``""`` = unset. Normally written by Studio's "Find a
  compatible Python" button, not by hand. ``MONTEUR_RESOLVE_PYTHON``
  still overrides it (see :func:`monteur.resolve._worker_python`).
* ``youtube_client_id`` / ``youtube_client_secret`` — the OAuth
  "Desktop app" client of the user's OWN Google Cloud project
  (:mod:`monteur.youtube`); ``""`` = not configured.
* ``youtube_refresh_token`` — the long-lived token from "Connect
  YouTube"; ``""`` = not connected. Another secret, hence the 0600 mode.
* ``youtube_channel`` — the channel title of the last upload, a pure
  display hint (written from the upload response, never fetched).

Why plain JSON: Monteur is a local single-user tool, the settings must be
trivially inspectable and hand-editable, and the stdlib parses it. The file
CAN hold the API key, so :func:`save_settings` chmods it to ``0o600``
(owner read/write only) on POSIX; Windows home directories are already
per-user. That is the honest local-app tradeoff — the key is exactly as
protected as the user's own files.

Writes are atomic (temp file + ``os.replace``), and unknown keys survive a
load/save round-trip, so older and newer Monteur versions can share the
file. Tests point ``MONTEUR_SETTINGS_PATH`` at a scratch file to stay out
of the real home directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: Environment variable overriding the settings file location (tests).
SETTINGS_PATH_ENV = "MONTEUR_SETTINGS_PATH"

#: Backend values the settings file may force (anything else means "auto").
_FORCED_BACKENDS = ("api", "claude-cli")


def settings_path() -> Path:
    """Where the settings live: ``$MONTEUR_SETTINGS_PATH`` or ``~/.monteur/settings.json``."""
    override = os.environ.get(SETTINGS_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".monteur" / "settings.json"


def load_settings() -> dict:
    """The settings dict; a missing or corrupt file is just ``{}``.

    Settings are a convenience, never a gate — a half-written or
    hand-mangled file must not take Monteur down, so every read failure
    (missing file, bad JSON, JSON that is not an object) degrades to the
    defaults.
    """
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(updates: dict) -> dict:
    """Merge ``updates`` into the settings file; returns the merged dict.

    Merge-and-write: existing keys not named in ``updates`` (including keys
    this Monteur version does not know) are preserved. The write is atomic
    — a temp file in the same directory replaced over the real one via
    ``os.replace`` — so a crash mid-save never leaves a torn file. On
    POSIX the file ends up mode ``0o600`` because it can hold the API key.
    """
    path = settings_path()
    settings = load_settings()
    settings.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(
        json.dumps(settings, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    if os.name == "posix":
        os.chmod(tmp, 0o600)  # the file can hold the API key
    os.replace(tmp, path)
    return settings


def ai_backend() -> str:
    """The stored backend choice: ``"auto"``, ``"api"`` or ``"claude-cli"``.

    Anything unknown (old file, hand edit) reads as ``"auto"`` — the safe
    default that :mod:`monteur.ai` resolves for itself.
    """
    value = load_settings().get("ai_backend", "")
    value = value.strip().lower() if isinstance(value, str) else ""
    return value if value in _FORCED_BACKENDS else "auto"


def api_key() -> str:
    """The stored Anthropic API key, or ``""`` when none is saved."""
    value = load_settings().get("api_key", "")
    return value.strip() if isinstance(value, str) else ""


def update_channel() -> str:
    """Which update channel to check: ``"dev"`` (every push) or ``"stable"``.

    Anything unknown reads as ``"stable"`` — the safe default; only an explicit
    ``"dev"`` opts into the every-push prereleases.
    """
    value = load_settings().get("update_channel", "")
    value = value.strip().lower() if isinstance(value, str) else ""
    return value if value == "dev" else "stable"


def _string_setting(key: str) -> str:
    """A stripped string setting, ``""`` for missing/non-string values."""
    value = load_settings().get(key, "")
    return value.strip() if isinstance(value, str) else ""


def youtube_client_id() -> str:
    """The stored Google OAuth Desktop-app client id (``""`` = unset)."""
    return _string_setting("youtube_client_id")


def youtube_client_secret() -> str:
    """The stored Google OAuth Desktop-app client secret (``""`` = unset)."""
    return _string_setting("youtube_client_secret")


def youtube_refresh_token() -> str:
    """The stored YouTube refresh token (``""`` = not connected)."""
    return _string_setting("youtube_refresh_token")


def youtube_channel() -> str:
    """The channel title hint from the last upload (``""`` = unknown)."""
    return _string_setting("youtube_channel")


def resolve_python() -> str:
    """The stored Resolve-worker Python path, or ``""`` when none is saved.

    Existence is deliberately NOT checked here — the reader decides how to
    treat a stale path (:func:`monteur.resolve._worker_python` silently
    falls back, Studio's settings panel shows it so it can be fixed).
    """
    value = load_settings().get("resolve_python", "")
    return value.strip() if isinstance(value, str) else ""
