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
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import monteur
from monteur.settings import settings_path

#: How long any single git subprocess may run before we give up.
GIT_TIMEOUT = 45.0

#: An injectable subprocess runner so the git path is testable without a real
#: repo (mirrors the module's "everything network/IO is injected" ethos).
GitRunner = Callable[..., "subprocess.CompletedProcess"]

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
    download_url: str = ""   # the platform executable asset (shell update), if any
    asset_name: str = ""
    payload_url: str = ""    # the small app-payload zip (the usual update)
    payload_name: str = ""
    sha256_url: str = ""     # the payload's checksum sibling asset
    kind: str = "none"       # what install would do: "payload" | "exe" | "none"
    mode: str = "source"     # "frozen" (a packaged build) | "source"
    channel: str = "stable"  # "stable" | "dev" — which release stream was checked
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


def _latest_release(getter: Fetch, repo: str, channel: str) -> dict | None:
    """The release to compare against for a channel.

    * ``stable`` → GitHub's ``/releases/latest`` (never a prerelease/draft).
    * ``dev`` → the newest non-draft release from ``/releases`` (prereleases
      included), since every-push builds are published as prereleases.
    """
    if channel == "dev":
        raw = getter(f"https://api.github.com/repos/{repo}/releases?per_page=20")
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        if not isinstance(data, list):
            return None
        for rel in data:  # GitHub returns newest first
            if isinstance(rel, dict) and rel.get("tag_name") and not rel.get("draft"):
                return rel
        return None
    raw = getter(f"https://api.github.com/repos/{repo}/releases/latest")
    data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    return data if isinstance(data, dict) else None


def check(
    current: str | None = None,
    *,
    repo: str | None = None,
    fetch: Fetch | None = None,
    system: str | None = None,
    channel: str | None = None,
) -> UpdateInfo:
    """Ask GitHub for the newest release on ``channel`` and compare to ``current``.

    Never raises: connectivity/parse failures come back as
    ``UpdateInfo(available=False, error=...)``.
    """
    cur = current or current_version()
    chan = (channel or "stable").lower()
    if chan != "dev":
        chan = "stable"
    info = UpdateInfo(current=cur, mode="frozen" if is_frozen() else "source", channel=chan)
    getter = fetch or _http_get
    try:
        data = _latest_release(getter, repo or release_repo(), chan)
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

    # the app-payload zip (the small, usual update) + its checksum sibling
    payload = payload_asset(assets)
    if payload:
        info.payload_url = str(payload.get("browser_download_url") or "")
        info.payload_name = str(payload.get("name") or "")
        sums = _sha_sibling(assets, info.payload_name)
        if sums:
            info.sha256_url = str(sums.get("browser_download_url") or "")

    # the platform executable (a rarer, full shell update)
    exe = asset_for_platform(assets, system=system)
    if exe:
        info.download_url = str(exe.get("browser_download_url") or "")
        info.asset_name = str(exe.get("name") or "")

    # frozen builds update by payload when one exists (no exe swap needed);
    # otherwise fall back to a full executable; source runs install nothing
    if info.mode == "frozen" and info.payload_url:
        info.kind = "payload"
    elif info.mode == "frozen" and info.download_url:
        info.kind = "exe"
    else:
        info.kind = "none"

    info.available = is_newer(info.latest, cur)
    return info


def payload_asset(assets: list[dict]) -> dict | None:
    """The app-payload zip in a release's assets (``monteur-app-*.zip``)."""
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.startswith("monteur-app") and name.endswith(".zip"):
            return asset
    return None


def _sha_sibling(assets: list[dict], payload_name: str) -> dict | None:
    want = (payload_name + ".sha256").lower()
    for asset in assets:
        if str(asset.get("name") or "").lower() == want:
            return asset
    return None


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


def install_payload(info: UpdateInfo, *, fetch: Fetch | None = None) -> str:
    """Download, verify and unpack the app payload. Returns the version.

    The new payload lands in ``~/.monteur/payloads/<version>/``; the launcher
    picks the newest at the next start — nothing is swapped now. If the release
    ships a ``.sha256`` sibling the download is verified against it and a
    mismatch raises (a truncated/tampered payload is never installed).
    """
    from monteur import payload as payload_mod

    if not info.payload_url:
        raise ValueError("this release has no app payload to install")
    getter = fetch or _http_get
    blob = getter(info.payload_url)
    blob = blob if isinstance(blob, (bytes, bytearray)) else bytes(blob)
    if info.sha256_url:
        raw = getter(info.sha256_url)
        text = (raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)).strip()
        expected = text.split()[0] if text else ""
        if not payload_mod.verify_sha256(blob, expected):
            raise ValueError("the downloaded update failed its checksum — not installed")
    version, _root = payload_mod.extract_payload(bytes(blob))
    return version


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


# --- git-checkout updates -----------------------------------------------------
#
# A source install (git clone + `pip install -e .`) can't swap an executable —
# but it doesn't need to: an update is just `git pull` + a restart. These drive
# that path so the whole thing happens in-app, no terminal required.


def _run_git(args: list[str], root: Path, runner: GitRunner | None = None):
    """Run one git command in ``root``; captured, text, bounded by GIT_TIMEOUT."""
    run = runner or subprocess.run
    return run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def git_root() -> Path | None:
    """The repository root when Monteur runs from a git checkout with ``git``
    available, else None (a frozen build or a plain wheel install)."""
    if is_frozen() or shutil.which("git") is None:
        return None
    start = Path(monteur.__file__).resolve().parent  # the monteur/ package dir
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def git_check(root: Path | None = None, *, runner: GitRunner | None = None) -> UpdateInfo:
    """Fetch and report how far the checkout is behind its upstream branch.

    ``UpdateInfo.mode == "git"``; ``available`` is True when the branch is
    behind its tracked upstream. Never raises — git failures (no upstream,
    offline, not a repo) come back as ``error``.
    """
    info = UpdateInfo(current=current_version(), mode="git", kind="git")
    root = root or git_root()
    if root is None:
        info.error = "not a git checkout (or git is not installed)"
        return info
    try:
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root, runner)
        info.notes = ""
        if branch.returncode == 0:
            info.asset_name = branch.stdout.strip()  # the branch name, for display
        fetched = _run_git(["fetch", "--quiet"], root, runner)
        if fetched.returncode != 0:
            info.error = (fetched.stderr or "git fetch failed").strip()
            return info
        counts = _run_git(
            ["rev-list", "--left-right", "--count", "HEAD...@{u}"], root, runner
        )
        if counts.returncode != 0:
            info.error = (
                (counts.stderr or "").strip()
                or "this branch has no upstream to compare against"
            )
            return info
        parts = counts.stdout.split()
        behind = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
        info.available = behind > 0
        if behind:
            subj = _run_git(["log", "-1", "--format=%s", "@{u}"], root, runner)
            top = subj.stdout.strip() if subj.returncode == 0 else ""
            info.latest = f"{behind} commit{'s' if behind != 1 else ''} behind"
            info.notes = top
            info.url = ""
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        info.error = f"could not check git: {exc}"
    return info


def git_pull(root: Path | None = None, *, runner: GitRunner | None = None) -> ApplyResult:
    """Fast-forward the checkout to its upstream (``git pull --ff-only``).

    SAFE by design: ``--ff-only`` never rewrites or discards local work — if
    the branch has diverged or the tree is dirty in a way that blocks a
    fast-forward, it refuses and returns git's own message so the user can
    resolve it, rather than clobbering anything. On success the running app
    still holds the OLD code, so the result asks for a restart.
    """
    root = root or git_root()
    if root is None:
        return ApplyResult(applied=False, message="not a git checkout")
    try:
        dirty = _run_git(["status", "--porcelain"], root, runner)
        if dirty.returncode == 0 and dirty.stdout.strip():
            return ApplyResult(
                applied=False,
                message=(
                    "you have uncommitted local changes — commit or stash them, "
                    "then update again (nothing was touched)"
                ),
            )
        pulled = _run_git(["pull", "--ff-only"], root, runner)
        if pulled.returncode != 0:
            return ApplyResult(
                applied=False,
                message=(
                    (pulled.stderr or pulled.stdout or "git pull failed").strip()
                    + " — resolve it, then update again (nothing was discarded)"
                ),
            )
        return ApplyResult(
            applied=True,
            version=current_version(),
            message="Pulled the latest code. Restart Monteur to finish updating.",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ApplyResult(applied=False, message=f"could not update: {exc}")
