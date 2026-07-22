#!/usr/bin/env python3
"""Build the Monteur desktop app with PyInstaller.

    python scripts/build_exe.py

Runs the spec in ``packaging/monteur.spec`` and renames the result to include
the version and platform (e.g. ``Monteur-0.1.0-windows.exe``). Run it on the
OS you want to target — PyInstaller does not cross-compile, so a Windows
``.exe`` must be built on Windows.

Needs the build + app extras in the current environment:

    pip install -e '.[app,build]'
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "monteur.spec"


def _platform_tag() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _version() -> str:
    sys.path.insert(0, str(ROOT))
    import monteur

    return str(getattr(monteur, "__version__", "0"))


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller isn't installed. Install the build extras:\n"
            "    pip install -e '.[app,build]'",
            file=sys.stderr,
        )
        return 1

    # emit the distributable payload zip + checksum too (upload both, plus this
    # exe, to the GitHub Release)
    print("Building the app payload…")
    payload = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_payload.py")], cwd=str(ROOT))
    if payload.returncode != 0:
        return payload.returncode

    print(f"Building Monteur {_version()} shell for {_platform_tag()}…")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        return result.returncode

    dist = ROOT / "dist"
    exe = "Monteur.exe" if sys.platform.startswith("win") else "Monteur"
    built = dist / exe
    if not built.exists():
        print(f"Build finished but {built} is missing.", file=sys.stderr)
        return 1

    suffix = ".exe" if sys.platform.startswith("win") else ""
    final = dist / f"Monteur-{_version()}-{_platform_tag()}{suffix}"
    shutil.copy2(built, final)
    print(f"\nDone → {final}")
    print("Ship this single file — the target machine needs no Python.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
