#!/usr/bin/env python3
"""Code-signing helper for the Windows shell + installer.

Signing needs a real certificate, so this is opt-in: it signs only when one is
configured, and is a clean no-op otherwise (the build still succeeds, just
unsigned — Windows SmartScreen will warn "Unknown publisher" until you sign).

Configure via environment (either form):
  * MONTEUR_SIGN_PFX   = path to a .pfx/.p12 cert   + MONTEUR_SIGN_PASS = its password
  * MONTEUR_SIGN_SHA1  = a cert thumbprint already in the Windows cert store
Optional:
  * MONTEUR_SIGN_TS    = RFC-3161 timestamp URL (default DigiCert's)

Used by scripts/build_exe.py (signs the shell) and scripts/build_installer.py
(signs the setup). Can also be run directly:

    python scripts/sign.py dist/Monteur-0.1.0-windows.exe
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TS = "http://timestamp.digicert.com"


def signing_configured() -> bool:
    return bool(os.environ.get("MONTEUR_SIGN_PFX") or os.environ.get("MONTEUR_SIGN_SHA1"))


def _signtool() -> str | None:
    # signtool.exe ships with the Windows SDK; it's usually not on PATH
    found = shutil.which("signtool") or shutil.which("signtool.exe")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        if not base:
            continue
        for match in sorted(Path(base, "Windows Kits", "10", "bin").glob("**/x64/signtool.exe"), reverse=True):
            return str(match)
    return None


def sign_file(path: str | Path) -> bool:
    """Sign ``path`` in place. Returns True if signed, False if skipped.

    Skips (returns False, no error) when signing isn't configured or isn't
    possible here (not Windows / no signtool) — so an unsigned build still
    ships.
    """
    path = Path(path)
    if not signing_configured():
        print(f"(not signing {path.name} — no signing cert configured; see scripts/sign.py)")
        return False
    if not sys.platform.startswith("win"):
        print(f"(not signing {path.name} — code-signing runs on Windows only)")
        return False
    tool = _signtool()
    if not tool:
        print("(cannot sign — signtool.exe not found; install the Windows SDK)", file=sys.stderr)
        return False

    ts = os.environ.get("MONTEUR_SIGN_TS", DEFAULT_TS)
    cmd = [tool, "sign", "/fd", "sha256", "/tr", ts, "/td", "sha256"]
    if os.environ.get("MONTEUR_SIGN_SHA1"):
        cmd += ["/sha1", os.environ["MONTEUR_SIGN_SHA1"]]
    else:
        cmd += ["/f", os.environ["MONTEUR_SIGN_PFX"]]
        if os.environ.get("MONTEUR_SIGN_PASS"):
            cmd += ["/p", os.environ["MONTEUR_SIGN_PASS"]]
    cmd.append(str(path))

    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"signing {path} failed (signtool exit {result.returncode})")
    print(f"Signed → {path}")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/sign.py <file>", file=sys.stderr)
        raise SystemExit(2)
    sign_file(sys.argv[1])
