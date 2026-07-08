"""Project version history.

A Monteur project is a directory containing a ``.monteur/`` folder. Every time a
cut is analyzed and saved, a snapshot of its pacing statistics is appended to
the project history — so an editor can watch the rhythm of a film evolve
across versions ("v3 was faster but v5 breathes better in act two").

Only derived statistics are stored, never media.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from monteur.analysis import PacingStats, Section, Shot

_DIRNAME = ".monteur"
_FILENAME = "versions.json"


class Project:
    """Version store rooted at a project directory."""

    def __init__(self, root: str | Path = "."):
        self.root = Path(root)

    @property
    def store_path(self) -> Path:
        return self.root / _DIRNAME / _FILENAME

    def _load(self) -> list[dict]:
        if not self.store_path.exists():
            return []
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"corrupt version store at {self.store_path}: {exc}")
        return data if isinstance(data, list) else []

    def _save(self, versions: list[dict]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(versions, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def add_version(
        self, stats: PacingStats, label: str = "", source_file: str = "",
        saved_at: str = "",
    ) -> dict:
        """Append a snapshot and return the stored entry (with its id)."""
        versions = self._load()
        entry = {
            "id": (max((v["id"] for v in versions), default=0)) + 1,
            "label": label or stats.timeline_name or f"v{len(versions) + 1}",
            "source_file": source_file,
            "saved_at": saved_at,
            "stats": asdict(stats),
        }
        versions.append(entry)
        self._save(versions)
        return entry

    def versions(self) -> list[dict]:
        """All snapshots, oldest first, without the heavy stats payload."""
        result = []
        for v in self._load():
            stats = v["stats"]
            result.append(
                {
                    "id": v["id"],
                    "label": v["label"],
                    "source_file": v.get("source_file", ""),
                    "saved_at": v.get("saved_at", ""),
                    "duration_seconds": stats["duration_seconds"],
                    "shot_count": stats["shot_count"],
                    "avg_shot_seconds": stats["avg_shot_seconds"],
                    "std_shot_seconds": stats["std_shot_seconds"],
                }
            )
        return result

    def get_stats(self, version_id: int) -> PacingStats:
        for v in self._load():
            if v["id"] == version_id:
                return _stats_from_dict(v["stats"])
        raise KeyError(f"no version with id {version_id}")

    def delete_version(self, version_id: int) -> None:
        versions = [v for v in self._load() if v["id"] != version_id]
        self._save(versions)


def _stats_from_dict(data: dict) -> PacingStats:
    return PacingStats(
        timeline_name=data["timeline_name"],
        fps=data["fps"],
        duration_seconds=data["duration_seconds"],
        shot_count=data["shot_count"],
        cut_count=data["cut_count"],
        avg_shot_seconds=data["avg_shot_seconds"],
        median_shot_seconds=data["median_shot_seconds"],
        min_shot_seconds=data["min_shot_seconds"],
        max_shot_seconds=data["max_shot_seconds"],
        std_shot_seconds=data["std_shot_seconds"],
        shots=[Shot(**s) for s in data["shots"]],
        pacing_curve=[tuple(p) for p in data["pacing_curve"]],
        histogram=[tuple(h) for h in data["histogram"]],
        longest_shots=[Shot(**s) for s in data["longest_shots"]],
        sections=[Section(**s) for s in data["sections"]],
    )
