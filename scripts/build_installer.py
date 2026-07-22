#!/usr/bin/env python3
"""Compile the Windows installer with Inno Setup.

    python scripts/build_installer.py

Windows only — needs Inno Setup 6 (``iscc`` on PATH; get it from
https://jrsoftware.org/isdl.php). Run ``scripts/build_exe.py`` first so the
shell executable exists in ``dist/``.

Produces ``dist/Monteur-Setup-<version>.exe``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "packaging" / "monteur.iss"


def _version() -> str:
    text = (ROOT / "monteur" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0"


def main() -> int:
    if not sys.platform.startswith("win"):
        print("The installer is Windows-only (Inno Setup). Build it on Windows.",
              file=sys.stderr)
        return 1
    iscc = shutil.which("iscc") or shutil.which("ISCC")
    if not iscc:
        print("Inno Setup's 'iscc' isn't on PATH — install Inno Setup 6:\n"
              "    https://jrsoftware.org/isdl.php", file=sys.stderr)
        return 1

    version = _version()
    shell = ROOT / "dist" / f"Monteur-{version}-windows.exe"
    if not shell.exists():
        print(f"Shell not found: {shell}\nRun 'python scripts/build_exe.py' first.",
              file=sys.stderr)
        return 1

    print(f"Compiling the Monteur {version} installer…")
    result = subprocess.run(
        [iscc, f"/DMyAppVersion={version}", str(ISS)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        return result.returncode

    out = ROOT / "dist" / f"Monteur-Setup-{version}.exe"
    print(f"\nDone → {out}")
    print("Ship this installer. It never touches %USERPROFILE%\\.monteur.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
