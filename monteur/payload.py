"""App payloads — the small, updatable half of the desktop app.

The packaged app is split like Electron's shell/app: a rarely-changing
**shell** (the PyInstaller executable = bootloader + Python + third-party
deps) and a frequently-changing **payload** (this ``monteur`` package +
``app.html``). A payload is a versioned tree::

    <payloads-dir>/<version>/
        payload.json      -> {"version": "0.2.0"}
        monteur/...        -> the package, incl. web/app.html

The shell's launcher puts the NEWEST available payload root on ``sys.path``
before importing anything from ``monteur``, so an update is just "drop a newer
payload tree on disk" — no executable swap, no admin prompt, KB not MB.

This module handles the on-disk side (list / verify / extract / pick newest).
It is pure and stdlib-only so it's testable offline and safe to run from the
frozen shell. The launcher carries its own tiny, independent copy of
:func:`choose_root` (it can't import this before ``sys.path`` is set) — keep
the two in sync.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from monteur.settings import settings_path
from monteur.update import parse_version

#: name of the per-payload version marker
MANIFEST = "payload.json"


def payloads_dir() -> Path:
    """Where downloaded payloads live (next to the settings file)."""
    return settings_path().parent / "payloads"


def read_payload_version(root: Path) -> str | None:
    """The version a payload tree declares, or ``None`` if it isn't one.

    A valid payload has ``<root>/payload.json`` with a ``version`` AND an
    importable ``<root>/monteur/__init__.py`` next to it.
    """
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = str(data.get("version") or "").strip() if isinstance(data, dict) else ""
    if not version or not (root / "monteur" / "__init__.py").is_file():
        return None
    return version


def installed_payloads(base: Path | None = None) -> list[tuple[str, Path]]:
    """``(version, root)`` for every valid payload under the payloads dir."""
    base = base or payloads_dir()
    found: list[tuple[str, Path]] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return found
    for child in entries:
        if not child.is_dir():
            continue
        version = read_payload_version(child)
        if version:
            found.append((version, child))
    return found


def choose_root(
    baseline_version: str,
    baseline_root: Path,
    base: Path | None = None,
) -> tuple[str, Path]:
    """Pick the newest payload to run: the built-in baseline or a downloaded one.

    Ties and unparseable versions fall back to the baseline, so a corrupt or
    downgrade payload can never shadow the shipped one.
    """
    best_version, best_root = baseline_version, baseline_root
    best_key = parse_version(baseline_version)
    for version, root in installed_payloads(base):
        key = parse_version(version)
        if key > best_key:
            best_version, best_root, best_key = version, root, key
    return best_version, best_root


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_sha256(data: bytes, expected: str) -> bool:
    """True when ``data`` matches ``expected`` (case-insensitive hex)."""
    want = str(expected or "").strip().lower()
    return bool(want) and sha256_of(data) == want


def extract_payload(zip_bytes: bytes, base: Path | None = None) -> tuple[str, Path]:
    """Unpack a payload zip into ``<payloads-dir>/<version>/`` atomically.

    The version is read from the payload's own ``payload.json`` (the zip is the
    source of truth). Returns ``(version, root)``. Raises ``ValueError`` if the
    zip isn't a valid payload.
    """
    base = base or payloads_dir()
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="monteur-payload-", dir=base))
    try:
        with zipfile.ZipFile(_as_seekable(zip_bytes)) as zf:
            _safe_extract(zf, staging)
        version = read_payload_version(staging)
        if not version:
            raise ValueError("not a valid Monteur payload (no payload.json / monteur package)")
        dest = base / version
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        staging.replace(dest)
        return version, dest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _as_seekable(zip_bytes: bytes):
    import io

    return io.BytesIO(zip_bytes)


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract, refusing any entry that would escape ``dest`` (zip-slip guard)."""
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"unsafe path in payload zip: {member!r}")
    zf.extractall(dest)
