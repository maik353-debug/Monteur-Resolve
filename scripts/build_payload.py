#!/usr/bin/env python3
"""Build the distributable app payload (the small, updatable half).

    python scripts/build_payload.py

Writes to ``dist/``:
  * ``monteur-app-<version>.zip``          — the payload (monteur/ + payload.json)
  * ``monteur-app-<version>.zip.sha256``   — its checksum

Attach BOTH to the GitHub Release. The in-app updater downloads the zip,
verifies it against the ``.sha256``, and unpacks it into
``~/.monteur/payloads/<version>/``; the shell runs it on the next launch.

This is platform-independent — one payload works on every shell, because it's
just Python source + app.html. (A new *shell* executable is only needed when
the bundled Python/deps change.)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _version() -> str:
    """The build version: ``$MONTEUR_BUILD_VERSION`` (CI) or ``__version__``.

    CI stamps a monotone per-push version (e.g. ``0.1.<commit-count>``) so every
    push to the dev channel is strictly newer than the last.
    """
    override = os.environ.get("MONTEUR_BUILD_VERSION", "").strip().lstrip("vV")
    if override:
        return override
    text = (ROOT / "monteur" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0"


def main() -> int:
    version = _version()
    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    stage = ROOT / "build" / "payload_dist"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    shutil.copytree(
        ROOT / "monteur", stage / "monteur",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # stamp the version into the payload so the running app reports it
    init = stage / "monteur" / "__init__.py"
    init.write_text(
        re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"',
               init.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    (stage / "payload.json").write_text(
        json.dumps({"version": version}, indent=2), encoding="utf-8"
    )

    zip_path = dist / f"monteur-app-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_path = dist / f"monteur-app-{version}.zip.sha256"
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")

    print(f"Payload  → {zip_path}  ({zip_path.stat().st_size // 1024} KB)")
    print(f"Checksum → {sha_path}  ({digest[:16]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
