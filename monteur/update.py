"""In-app updates for Monteur.

Checks GitHub Releases for a newer version, downloads the asset that matches
the running platform, stages it under ``~/.monteur/updates`` and applies it at
the next startup — the only safe moment to replace a running executable.

Everything that touches the network is injected (``fetch``), so the whole
module is testable offline. The actual binary swap only means anything for a
frozen build (PyInstaller ``sys.frozen``); running from a source checkout the
updater degrades to a clear "update with pip / git" message instead of
touching any files.

Design notes:
* Stdlib only — the core stays dependency-free (an app the user runs, not a
  library, but the ethos holds).
* A check never raises: any failure (offline, rate-limited, malformed JSON)
  comes back as ``UpdateInfo(available=False, error=...)`` so the UI degrades.
* "Install" == download + stage + a marker; the swap happens on the next
  launch via :func:`apply_pending`, because a process can't overwrite the
  executable it is currently running.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import monteur
from monteur.settings import settings_path

#: owner/repo the releases are published under (GitHub renamed the repo to
#: Monteur-Resolve). Overridable so a fork / test can point elsewhere.
DEFAULT_REPO = "maik353-debug/Monteur-Resolve"
REPO_ENV = "MONTEUR_UPDATE_REPO"

#: how long a network call may take before we give up and report "couldn't check"
HTTP_TIMEOUT = 8.0

Fetch = Callable[[str], bytes]


def release_repo() -> str:
    """The ``owner/repo`` to check — ``$MONTEUR_UPDATE_REPO`` or the default."""
    return (os.environ.get(REPO_ENV) or "").strip() or DEFAULT_REPO


def current_version() -> str:
    """The version this build reports (single source: ``monteur.__version__``)."""
    return str(getattr(monteur, "__version__", "0") or "0")


# ---------------------------------------------------------------------------
# version comparison — tolerant of a leading "v" and pre-release suffixes
# ---------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...]:
    """``"v1.2.3"`` / ``"1.2"`` / ``"1.2.0-rc1"`` -> a comparable int tuple.

    Reads the leading dotted-number run only, so a ``-rc1``/``+build`` suffix
    is ignored (a release tag compares by its numbers). Unparseable input is
    ``()`` — which is older than any real version.
    """
    s = str(text or "").strip().lstrip("vV")
    # drop a semver pre-release/build suffix (-rc1, +build.7) before the dots,
    # so build metadata can't leak numbers into the version tuple
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


def is_newer(candidate: str, current: str) -> bool:
    """Is ``candidate`` a strictly newer version than ``current``?

    Shorter tuples pad with zeros (``1.2`` == ``1.2.0``). An unparseable
    candidate is never newer.
    """
    cand = parse_version(candidate)
    cur = parse_version(current)
    if not cand:
        return False
    width = max(len(cand), len(cur))
    cand += (0,) * (width - len(cand))
    cur += (0,) * (width - len(cur))
    return cand > cur


# ---------------------------------------------------------------------------
# platform asset selection
# ---------------------------------------------------------------------------

def _platform_key(system: str | None = None) -> str:
    sysname = (system or sys.platform or "").lower()
    if sysname.startswith("win") or sysname == "win32":
        return "windows"
    if sysname == "darwin" or "mac" in sysname:
        return "macos"
    return "linux"


#: filename hints per platform, most-specific first
_ASSET_HINTS = {
    "windows": (".exe", ".msi", "windows", "win64", "win32", "-win"),
    "macos": (".dmg", ".pkg", "macos", "darwin", "-mac", ".app.zip"),
    "linux": (".appimage", "linux", "x86_64", ".tar.gz"),
}


def asset_for_platform(assets: list[dict], system: str | None = None) -> dict | None:
    """Pick the release asset that fits this platform, or ``None``.

    ``assets`` are GitHub asset dicts (``name`` + ``browser_download_url``).
    Hints are tried in order so ``.exe`` wins over a generic ``windows`` match.
    """
    key = _platform_key(system)
    hints = _ASSET_HINTS.get(key, ())
    for hint in hints:
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            if hint in name:
                return asset
    return None


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

@dataclass
class UpdateInfo:
    current: str
    latest: str = ""
    available: bool = False
    notes: str = ""
    url: str = ""            # the human release page
    download_url: str = ""   # the platform asset, if any
    asset_name: str = ""
    mode: str = "source"     # "frozen" (a packaged build) | "source"
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) one-file build."""
    return bool(getattr(sys, "frozen", False))


def _http_get(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    # GitHub's API rejects requests without a User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": "Monteur-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return resp.read()


def check(
    current: str | None = None,
    *,
    repo: str | None = None,
    fetch: Fetch | None = None,
    system: str | None = None,
) -> UpdateInfo:
    """Ask GitHub for the latest release and compare it to ``current``.

    Never raises: connectivity/parse failures come back as
    ``UpdateInfo(available=False, error=...)``.
    """
    cur = current or current_version()
    info = UpdateInfo(current=cur, mode="frozen" if is_frozen() else "source")
    getter = fetch or _http_get
    api = f"https://api.github.com/repos/{repo or release_repo()}/releases/latest"
    try:
        raw = getter(api)
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (urllib.error.URLError, OSError) as exc:
        info.error = f"couldn't reach the update server ({exc})"
        return info
    except (ValueError, TypeError) as exc:
        info.error = f"the update server sent something unexpected ({exc})"
        return info

    if not isinstance(data, dict) or not data.get("tag_name"):
        # 404 (no releases yet) or an unexpected shape — treat as "up to date"
        info.error = str(data.get("message") or "") if isinstance(data, dict) else ""
        return info

    info.latest = str(data.get("tag_name") or "").strip()
    info.notes = str(data.get("body") or "").strip()
    info.url = str(data.get("html_url") or "").strip()
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    asset = asset_for_platform(assets, system=system)
    if asset:
        info.download_url = str(asset.get("browser_download_url") or "")
        info.asset_name = str(asset.get("name") or "")
    info.available = is_newer(info.latest, cur)
    return info


# ---------------------------------------------------------------------------
# staging + pending marker
# ---------------------------------------------------------------------------

def updates_dir() -> Path:
    """Where downloaded builds are staged (next to the settings file)."""
    return settings_path().parent / "updates"


def _pending_path() -> Path:
    return updates_dir() / "pending.json"


def read_pending() -> dict | None:
    """The staged-update marker, or ``None`` if there's nothing pending."""
    try:
        data = json.loads(_pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("file"):
        return None
    return data


def write_pending(staged: Path, version: str) -> None:
    marker = _pending_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"file": str(staged), "version": str(version)}, indent=2),
        encoding="utf-8",
    )


def clear_pending() -> None:
    try:
        _pending_path().unlink()
    except OSError:
        pass


def download(
    info: UpdateInfo,
    *,
    fetch: Fetch | None = None,
    dest_dir: Path | None = None,
) -> Path:
    """Download ``info``'s platform asset into the staging dir and mark it pending.

    Returns the staged file path. Raises ``ValueError`` if there's no asset to
    download (e.g. a source-only release).
    """
    if not info.download_url or not info.asset_name:
        raise ValueError("this release has no downloadable build for your platform")
    getter = fetch or _http_get
    target_dir = dest_dir or updates_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / info.asset_name
    data = getter(info.download_url)
    staged.write_bytes(data if isinstance(data, (bytes, bytearray)) else bytes(data))
    write_pending(staged, info.latest or info.current)
    return staged


# ---------------------------------------------------------------------------
# applying a staged update at startup
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    applied: bool = False
    message: str = ""
    version: str = ""
    relaunch: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def apply_pending() -> ApplyResult | None:
    """If a staged update is waiting, put it in place. Call at startup.

    * Not frozen (source checkout): there's nothing to swap — return an
      advisory and leave files alone.
    * Frozen: move the staged build over the running executable. On Windows a
      running ``.exe`` can be renamed but not deleted, so the current exe is
      moved aside first; the caller may relaunch ``result.relaunch``.

    Returns ``None`` when nothing is pending.
    """
    pending = read_pending()
    if not pending:
        return None
    staged = Path(str(pending.get("file") or ""))
    version = str(pending.get("version") or "")
    if not staged.is_file():
        clear_pending()
        return ApplyResult(applied=False, message="the downloaded update went missing", version=version)

    if not is_frozen():
        # a dev/source run — don't touch anything, just tell the truth
        return ApplyResult(
            applied=False,
            version=version,
            message=(
                f"Monteur {version} was downloaded, but this is a source "
                "checkout — update with 'git pull' / 'pip install -U monteur' "
                "instead. (The staged build is ignored.)"
            ),
        )

    target = Path(sys.executable)
    try:
        backup = target.with_suffix(target.suffix + ".old")
        if backup.exists():
            backup.unlink()
        os.replace(target, backup)          # move the running exe aside
        os.replace(staged, target)          # the new build takes its place
    except OSError as exc:
        return ApplyResult(applied=False, version=version, message=f"could not install the update: {exc}")
    clear_pending()
    return ApplyResult(
        applied=True,
        version=version,
        message=f"Updated to Monteur {version}.",
        relaunch=[str(target)],
    )
