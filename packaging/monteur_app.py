"""Entry point for the packaged Monteur desktop app (PyInstaller shell).

The app is split shell/payload (see ``monteur/payload.py``): this frozen shell
carries a baseline payload and, at startup, puts the NEWEST payload it can find
— the baseline, or a smaller one downloaded into ``~/.monteur/payloads/`` by the
in-app updater — on ``sys.path`` BEFORE importing anything from ``monteur``.
That's how an update takes effect without swapping the executable.

This bootstrap is deliberately tiny and dependency-free: it can't import
``monteur`` (that's the very thing it's choosing), so it reads the version
markers itself. Keep :func:`_choose_root` in sync with
``monteur.payload.choose_root`` — same rule, duplicated on purpose.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path


def _parse_version(text: str) -> tuple[int, ...]:
    s = str(text or "").strip().lstrip("vV")
    for sep in ("-", "+", " "):
        s = s.split(sep, 1)[0]
    parts: list[int] = []
    for chunk in s.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


def _payload_version(root: Path) -> str | None:
    """The version a payload tree declares, if it's a valid payload."""
    try:
        data = json.loads((root / "payload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = str(data.get("version") or "").strip() if isinstance(data, dict) else ""
    if not version or not (root / "monteur" / "__init__.py").is_file():
        return None
    return version


def _payloads_dir() -> Path:
    override = os.environ.get("MONTEUR_SETTINGS_PATH", "").strip()
    base = Path(override).parent if override else Path.home() / ".monteur"
    return base / "payloads"


def _choose_root(baseline_version: str, baseline_root: Path) -> Path:
    """The newest of {baseline, downloaded payloads}. Baseline wins ties."""
    best_root = baseline_root
    best_key = _parse_version(baseline_version)
    try:
        children = sorted(_payloads_dir().iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir():
            continue
        version = _payload_version(child)
        if version and _parse_version(version) > best_key:
            best_root, best_key = child, _parse_version(version)
    return best_root


def _activate_newest_payload() -> None:
    if not getattr(sys, "frozen", False):
        return  # a source run imports monteur normally
    baseline_root = Path(getattr(sys, "_MEIPASS", "."))
    baseline_version = _payload_version(baseline_root) or "0"
    root = _choose_root(baseline_version, baseline_root)
    # winning root first so `import monteur` resolves to it
    sys.path.insert(0, str(root))


def main() -> None:
    # a frozen build that spawns child processes must call this first, or each
    # child would re-launch the whole app
    multiprocessing.freeze_support()

    _activate_newest_payload()

    from monteur.web import serve_app

    serve_app()


if __name__ == "__main__":
    main()
