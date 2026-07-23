"""Watch mode: new footage auto-sifted, a report kept up to date.

``monteur watch <folder>`` scans a footage folder on an interval, sifts any
clip it hasn't triaged yet, and appends the verdict to a running report — so a
shoot dropped in overnight is triaged by morning. Which clips have been seen
(by path + mtime, so a re-export is re-triaged) lives in
``.monteur-watch.json``; the report is ``monteur-watch-report.md``.

The sift itself is injected (``sift=``), so the scan / state / report logic is
deterministic and fully testable offline — the heavy ffmpeg pass only runs in
the real command.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

STATE_FILE = ".monteur-watch.json"
REPORT_FILE = "monteur-watch-report.md"


def state_path(folder: str | Path) -> Path:
    return Path(folder) / STATE_FILE


def report_path(folder: str | Path) -> Path:
    return Path(folder) / REPORT_FILE


def _key(path: Path) -> str:
    try:
        return f"{path.resolve()}|{path.stat().st_mtime_ns}"
    except OSError:
        return f"{path.resolve()}|0"


def load_state(folder: str | Path) -> dict:
    try:
        data = json.loads(state_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seen": []}
    if not isinstance(data, dict) or not isinstance(data.get("seen"), list):
        return {"seen": []}
    return {"seen": [str(k) for k in data["seen"]]}


def save_state(folder: str | Path, state: dict) -> None:
    path = state_path(folder)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"seen": list(state.get("seen", []))}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def list_media(folder: str | Path) -> list[Path]:
    from monteur.media import MEDIA_EXTENSIONS

    try:
        return sorted(p for p in Path(folder).iterdir()
                      if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS)
    except OSError:
        return []


def pending(folder: str | Path, state: dict) -> list[Path]:
    """Media files in ``folder`` not yet triaged (by path + mtime)."""
    seen = set(state.get("seen", []))
    return [clip for clip in list_media(folder) if _key(clip) not in seen]


@dataclass
class WatchEntry:
    name: str
    usable_ratio: float
    flags: list[str]      # DARK / BLURRY / SHAKY present in the clip

    def line(self) -> str:
        pct = f"{round(self.usable_ratio * 100)}% usable"
        tail = f" · {', '.join(self.flags).lower()}" if self.flags else ""
        return f"- {self.name} — {pct}{tail}"


def _flags(report) -> list[str]:
    labels = {getattr(seg, "label", "") for seg in getattr(report, "segments", [])}
    return sorted(lbl for lbl in labels if lbl and lbl != "USABLE")


def run_pass(folder: str | Path, *, sift, state: dict | None = None, stamp: str = "") -> dict:
    """Triage every pending clip once; append to the report; update state.

    ``sift`` is a callable ``path -> ClipReport``. Returns
    ``{"entries": [WatchEntry...], "report": <path>}``. A clip that fails to
    sift is skipped this pass (it stays pending, so the next pass retries).
    """
    folder = Path(folder)
    state = state if state is not None else load_state(folder)
    seen = list(state.get("seen", []))
    entries: list[WatchEntry] = []
    for clip in pending(folder, state):
        try:
            report = sift(str(clip))
        except Exception:  # noqa: BLE001 — a bad clip must not stop the watch
            continue
        entries.append(WatchEntry(clip.name, float(getattr(report, "usable_ratio", 0.0)), _flags(report)))
        seen.append(_key(clip))
    state["seen"] = seen
    save_state(folder, state)
    if entries:
        _append_report(folder, entries, stamp)
    return {"entries": entries, "report": str(report_path(folder))}


def _append_report(folder: Path, entries: list[WatchEntry], stamp: str) -> None:
    path = report_path(folder)
    header = f"\n## {stamp}\n" if stamp else "\n"
    body = header + "\n".join(e.line() for e in entries) + "\n"
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    if not existing:
        existing = "# Monteur watch report\n\nNew footage, triaged as it arrives.\n"
    path.write_text(existing + body, encoding="utf-8")


def watch(
    folder: str | Path,
    *,
    interval: float = 300.0,
    once: bool = False,
    sift=None,
    sleep=time.sleep,
    log=print,
) -> None:
    """Watch ``folder``, triaging new clips every ``interval`` seconds.

    ``once`` runs a single pass and returns (handy for cron and for tests).
    """
    if sift is None:
        from monteur.sift import analyze_clip as sift  # noqa: N806

    folder = Path(folder)
    log(f"Watching {folder} for new footage (every {int(interval)}s). Ctrl+C to stop.")
    while True:
        result = run_pass(folder, sift=sift)
        for entry in result["entries"]:
            log(f"  triaged {entry.line()[2:]}")
        if result["entries"]:
            log(f"  report: {result['report']}")
        if once:
            return
        try:
            sleep(interval)
        except KeyboardInterrupt:
            log("Watch stopped.")
            return
