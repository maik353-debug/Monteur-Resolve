"""First-class Monteur Cut projects (stdlib only).

The unified model (see ``docs/ROADMAP.md`` — "Unified model"): everything is
ONE always-saved project. A project = a MEDIA POOL (files/folders referenced
from disk, Resolve-style — NEVER imported or moved) + DERIVED INFORMATION
(the plan, options, export history, notes; sift reports and version stats
attach later) + a regenerable proxy cache that stays in the shared
``~/.monteur/proxies`` (never duplicated into the bundle). The project stores
KNOWLEDGE, not video: "your files stay where they are."

A project is a FOLDER bundle::

    <root>/<project_id>/
        project.json      # the manifest (this module owns it)
        versions/         # per-project version snapshots (created lazily)
        exports/          # rendered outputs the user chose to keep here

``<root>`` is ``$MONTEUR_PROJECTS_PATH`` or ``~/.monteur/projects/`` — the
same env-override + home-directory convention as :mod:`monteur.drafts` and
:mod:`monteur.settings`, so tests point ``MONTEUR_PROJECTS_PATH`` at a
scratch directory and never touch a real home. Writes are atomic (temp file
+ ``os.replace``, like drafts), and every read failure (missing bundle, bad
JSON, wrong shape) degrades to "no such project" / a skipped summary instead
of taking the Studio down.

The manifest shape (``project.json``)::

    {
        "monteur_project": 1,          # PROJECT_FORMAT_VERSION
        "id": "<uuid4 hex>",
        "name": "My cut",
        "created_at": "2026-07-22T10:00:00Z",   # ISO-8601 UTC
        "modified_at": "2026-07-22T10:05:00Z",   # bumped on every save
        "media_pool": [                # referenced ABSOLUTE paths, never copied
            {"path": "/footage/trip", "kind": "folder", "added_at": "..."},
            {"path": "/music/song.mp3", "kind": "file", "added_at": "..."}
        ],
        "options": {...},              # the wizard/build options dict
        "plan": {...},                 # monteur.montage.plan_to_dict output;
                                       # ABSENT when the project has no plan yet
        "exports": [{"path": ..., "at": ...}, ...],
        "notes": [...],
        "migrated_from_draft": "<draft id>"   # present only on migrated projects
    }

Media is referenced by absolute path only. Nothing in this module ever
opens, copies, moves or modifies a pooled file or folder — the bundle holds
only ``project.json`` (plus the ``versions/`` / ``exports/`` subfolders the
user fills). :func:`delete_project` removes the bundle and NEVER the
referenced media.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Environment variable overriding the projects root (tests).
PROJECTS_PATH_ENV = "MONTEUR_PROJECTS_PATH"

#: Manifest schema version — bump when the saved shape changes incompatibly.
PROJECT_FORMAT_VERSION = 1

#: The manifest filename inside every bundle.
_MANIFEST_NAME = "project.json"

#: Subfolders a bundle owns (created on save; media never lands here).
_SUBFOLDERS = ("versions", "exports")


def projects_root() -> Path:
    """Where projects live: ``$MONTEUR_PROJECTS_PATH`` or ``~/.monteur/projects``."""
    override = os.environ.get(PROJECTS_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".monteur" / "projects"


def _bundle_dir(project_id: str) -> Path:
    return projects_root() / project_id


def _manifest_path(project_id: str) -> Path:
    return _bundle_dir(project_id) / _MANIFEST_NAME


def _now() -> str:
    """ISO-8601 UTC timestamp, second resolution (matches drafts' saved_at)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _abspath(path: str | Path) -> str:
    """A referenced path in absolute, ``~``-expanded form — never touched on disk."""
    return os.path.abspath(os.path.expanduser(str(path)))


@dataclass
class Project:
    """An in-memory Monteur Cut project — the manifest, loaded.

    ``plan`` is the :func:`monteur.montage.plan_to_dict` dict (or ``None``);
    it is written to the manifest only when set. ``media_pool`` entries are
    ``{"path", "kind", "added_at"}`` with absolute, referenced paths.
    """

    id: str
    name: str
    created_at: str
    modified_at: str
    media_pool: list[dict] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    plan: dict | None = None
    exports: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Editor's own per-clip notes, keyed by absolute clip path. Shown in the
    #: Clips review step and fed to the composer (Claude reads the note as
    #: the editor's instruction for that shot). Survives re-analysis because
    #: it is keyed by path, not by the sift report's mtime key.
    clip_notes: dict = field(default_factory=dict)
    #: Editor's own per-MOMENT notes, keyed by ``"<abs clip path>|<start>"``
    #: (start rounded to 0.01s). This is the finer-grained sibling of
    #: ``clip_notes``: the Moments review step annotates the individual
    #: stretches the sift extracts — the ones that actually land in the cut —
    #: and the composer reads each note as the editor's instruction for that
    #: exact moment. Survives re-analysis because it is keyed by path + time.
    moment_notes: dict = field(default_factory=dict)
    #: Lightweight index of saved cuts — each ``{"id","created_at","label",
    #: "shots","duration"}``. The full plan snapshot lives in
    #: ``versions/<id>.json``, so listing history never reads every plan.
    versions: list[dict] = field(default_factory=list)
    migrated_from_draft: str = ""
    #: Which tool made this project — "cut" (Create), "movie" or "analysis".
    #: Drives the Home recents badge and type filter. Default "cut": the
    #: Create workflow is the only writer today, and old manifests without
    #: the key load as cuts.
    type: str = "cut"

    @property
    def root(self) -> Path:
        """The bundle directory (``<root>/<id>``)."""
        return _bundle_dir(self.id)

    @property
    def manifest_path(self) -> Path:
        return _manifest_path(self.id)

    @property
    def has_plan(self) -> bool:
        return isinstance(self.plan, dict) and bool(self.plan)


# --- (de)serialization -------------------------------------------------------


def project_to_dict(project: Project) -> dict:
    """The JSON manifest for ``project`` (what ``project.json`` holds).

    ``plan`` and ``migrated_from_draft`` are written only when set, so a
    project without a plan (or one never migrated) serializes without those
    keys — the only-when-set discipline :func:`monteur.montage.plan_to_dict`
    uses, kept here too.
    """
    data = {
        "monteur_project": PROJECT_FORMAT_VERSION,
        "id": project.id,
        "name": project.name,
        "created_at": project.created_at,
        "modified_at": project.modified_at,
        "media_pool": [dict(entry) for entry in project.media_pool],
        "options": dict(project.options),
        "exports": [dict(export) for export in project.exports],
        "notes": list(project.notes),
    }
    if project.has_plan:
        data["plan"] = project.plan
    if project.versions:  # only-when-non-empty keeps historyless manifests unchanged
        data["versions"] = [dict(v) for v in project.versions]
    if project.clip_notes:  # only-when-non-empty: noteless manifests stay unchanged
        data["clip_notes"] = {str(k): str(v) for k, v in project.clip_notes.items()}
    if project.moment_notes:  # only-when-non-empty: keeps noteless manifests unchanged
        data["moment_notes"] = {str(k): str(v) for k, v in project.moment_notes.items()}
    if project.migrated_from_draft:
        data["migrated_from_draft"] = project.migrated_from_draft
    # only-when-not-default, so existing "cut" manifests stay byte-identical
    if project.type and project.type != "cut":
        data["type"] = project.type
    return data


def project_from_dict(data: dict) -> Project:
    """Rebuild a :class:`Project` from a manifest dict.

    Raises ValueError when the dict is not a Monteur project (missing or
    unsupported ``monteur_project`` version) or has no ``id`` — callers that
    want graceful degradation use :func:`load_project`, which swallows this.
    """
    if not isinstance(data, dict):
        raise ValueError("a project manifest must be a JSON object")
    version = data.get("monteur_project")
    if version is None:
        raise ValueError(
            "not a Monteur project: the 'monteur_project' version key is missing"
        )
    if version != PROJECT_FORMAT_VERSION:
        raise ValueError(
            f"unsupported project version {version!r}; this Monteur reads "
            f"version {PROJECT_FORMAT_VERSION}"
        )
    project_id = str(data.get("id") or "").strip()
    if not project_id:
        raise ValueError("a project manifest needs an 'id'")
    pool = [
        _normalize_pool_entry(entry)
        for entry in (data.get("media_pool") or [])
        if isinstance(entry, dict) and entry.get("path")
    ]
    plan = data.get("plan")
    return Project(
        id=project_id,
        name=str(data.get("name") or "Untitled project"),
        created_at=str(data.get("created_at") or ""),
        modified_at=str(data.get("modified_at") or ""),
        media_pool=pool,
        options=dict(data.get("options") or {}),
        plan=plan if isinstance(plan, dict) and plan else None,
        exports=[dict(e) for e in (data.get("exports") or []) if isinstance(e, dict)],
        notes=[str(n) for n in (data.get("notes") or [])],
        versions=[dict(v) for v in (data.get("versions") or []) if isinstance(v, dict) and v.get("id")],
        migrated_from_draft=str(data.get("migrated_from_draft") or ""),
        type=str(data.get("type") or "cut"),
        clip_notes={
            str(k): str(v)
            for k, v in (data.get("clip_notes") or {}).items()
        } if isinstance(data.get("clip_notes"), dict) else {},
        moment_notes={
            str(k): str(v)
            for k, v in (data.get("moment_notes") or {}).items()
        } if isinstance(data.get("moment_notes"), dict) else {},
    )


def _normalize_pool_entry(entry: dict) -> dict:
    """A clean ``{"path", "kind", "added_at"}`` media-pool entry."""
    kind = entry.get("kind")
    if kind not in ("file", "folder"):
        kind = _guess_kind(entry["path"])
    return {
        "path": _abspath(entry["path"]),
        "kind": kind,
        "added_at": str(entry.get("added_at") or ""),
    }


def _guess_kind(path: str | Path) -> str:
    """"folder" for a directory on disk, else "file" — a cheap stat, no read."""
    try:
        return "folder" if Path(_abspath(path)).is_dir() else "file"
    except OSError:
        return "file"


# --- persistence -------------------------------------------------------------


def _write_manifest(project: Project) -> None:
    """Atomically write ``project.json`` (temp file + ``os.replace``, like drafts)."""
    root = project.root
    root.mkdir(parents=True, exist_ok=True)
    for sub in _SUBFOLDERS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    path = project.manifest_path
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(
        json.dumps(project_to_dict(project), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def save_project(project: Project) -> Project:
    """Persist ``project`` (atomic), bumping ``modified_at``; returns it."""
    project.modified_at = _now()
    _write_manifest(project)
    return project


def create_project(
    name: str,
    *,
    options: dict | None = None,
    media_pool: list | None = None,
    plan: dict | None = None,
    notes: list | None = None,
    migrated_from_draft: str = "",
    project_id: str = "",
    type: str = "cut",
) -> Project:
    """Create, persist and return a new project.

    ``media_pool`` may be a list of path strings or of
    ``{"path", "kind"?}`` dicts — each is referenced by absolute path, never
    copied. ``plan`` is a :func:`monteur.montage.plan_to_dict` dict (or
    ``None``). The bundle (``project.json`` + empty ``versions/`` /
    ``exports/``) is written before returning.
    """
    now = _now()
    project = Project(
        id=str(project_id or "").strip() or uuid.uuid4().hex,
        name=str(name or "").strip() or "Untitled project",
        created_at=now,
        modified_at=now,
        media_pool=[],
        options=dict(options or {}),
        plan=plan if isinstance(plan, dict) and plan else None,
        exports=[],
        notes=[str(n) for n in (notes or [])],
        migrated_from_draft=str(migrated_from_draft or ""),
        type=str(type or "cut"),
    )
    for entry in media_pool or []:
        if isinstance(entry, dict) and entry.get("path"):
            _pool_append(project, entry["path"], entry.get("kind"))
        elif isinstance(entry, str) and entry.strip():
            _pool_append(project, entry)
    return save_project(project)


def load_project(project_id: str) -> Project | None:
    """The full project for ``project_id``, or ``None`` when missing/corrupt.

    A project is a convenience surface, never a gate: a missing bundle, bad
    JSON or a manifest of the wrong shape all degrade to ``None`` instead of
    raising — the Studio stays up.
    """
    path = _manifest_path(str(project_id or "").strip())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return project_from_dict(data)
    except ValueError:
        return None


def list_projects() -> list[dict]:
    """Lightweight per-project summaries, newest first (corrupt bundles skipped).

    Each entry: ``{"id", "name", "modified_at", "created_at", "pool_size",
    "has_plan"}`` — everything a project picker shows without the heavy plan.
    """
    root = projects_root()
    summaries: list[dict] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for bundle in entries:
        if not bundle.is_dir():
            continue
        project = load_project(bundle.name)
        if project is None:
            continue
        summary = {
            "id": project.id,
            "name": project.name,
            "created_at": project.created_at,
            "modified_at": project.modified_at,
            "pool_size": len(project.media_pool),
            "has_plan": project.has_plan,
            "type": project.type or "cut",
        }
        # a Movie pointer reopens by folder path (the movie lives in its own
        # bundle on disk, not here) — carry it so the recents card can route
        if project.type == "movie" and project.options.get("movie_path"):
            summary["movie_path"] = str(project.options.get("movie_path"))
        summaries.append(summary)
    summaries.sort(key=lambda s: str(s.get("modified_at") or ""), reverse=True)
    return summaries


def delete_project(project_id: str) -> bool:
    """Remove a project's bundle; True when one was removed.

    NEVER touches referenced media — the media pool holds absolute paths
    OUTSIDE the bundle, and only the ``<root>/<id>`` folder (manifest,
    versions/, exports/) is deleted.
    """
    bundle = _bundle_dir(str(project_id or "").strip())
    if not bundle.is_dir():
        return False
    shutil.rmtree(bundle)
    return True


# --- media pool --------------------------------------------------------------


def _pool_append(project: Project, path: str | Path, kind: str | None = None) -> dict | None:
    """Append a media-pool entry in place; None when already pooled (deduped).

    The path is stored absolute and referenced only — nothing is opened,
    copied or moved. ``kind`` is honored when valid, else guessed from disk.
    """
    abspath = _abspath(path)
    if any(entry.get("path") == abspath for entry in project.media_pool):
        return None
    if kind not in ("file", "folder"):
        kind = _guess_kind(abspath)
    entry = {"path": abspath, "kind": kind, "added_at": _now()}
    project.media_pool.append(entry)
    return entry


def add_to_pool(project: Project, path: str | Path, kind: str | None = None) -> dict | None:
    """Reference a file/folder in the project's media pool and save.

    Media is REFERENCED by absolute path, never copied or moved. Returns the
    new entry, or ``None`` when the path was already pooled (idempotent).
    """
    entry = _pool_append(project, path, kind)
    save_project(project)
    return entry


def remove_from_pool(project: Project, path: str | Path) -> bool:
    """Drop a referenced path from the pool and save; True when one was removed.

    Only the reference is removed — the file/folder on disk is untouched.
    """
    abspath = _abspath(path)
    kept = [entry for entry in project.media_pool if entry.get("path") != abspath]
    if len(kept) == len(project.media_pool):
        return False
    project.media_pool = kept
    save_project(project)
    return True


# --- version history (never lose a cut) --------------------------------------

#: keep at most this many snapshots per project (oldest dropped first)
_MAX_VERSIONS = 50


def _version_path(project: Project, version_id: str) -> Path:
    return project.root / "versions" / f"{version_id}.json"


def _plan_stats(plan: dict) -> dict:
    """Cheap summary of a plan for the history index (no montage import)."""
    entries = plan.get("entries") if isinstance(plan, dict) else None
    shots = len(entries) if isinstance(entries, list) else 0
    duration = 0.0
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict):
                try:
                    duration += float(e.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
    return {"shots": shots, "duration": round(duration, 2)}


def add_version(project: Project, plan: dict, label: str = "") -> dict | None:
    """Snapshot ``plan`` as a restorable cut. Returns the index entry.

    A no-op (returns ``None``) when ``plan`` is empty or identical to the most
    recent snapshot — autosave can call this freely without piling up dupes.
    The full plan lands in ``versions/<id>.json``; a slim entry goes into the
    manifest index. Oldest snapshots beyond :data:`_MAX_VERSIONS` are pruned.
    """
    if not isinstance(plan, dict) or not plan:
        return None
    if project.versions:
        last = load_version(project, project.versions[-1]["id"])
        if last == plan:
            return None  # nothing changed since the last cut
    version_id = uuid.uuid4().hex[:12]
    entry = {"id": version_id, "created_at": _now(), "label": str(label or "").strip()}
    entry.update(_plan_stats(plan))
    path = _version_path(project, version_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({**entry, "plan": plan}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    project.versions.append(entry)
    while len(project.versions) > _MAX_VERSIONS:
        dropped = project.versions.pop(0)
        try:
            _version_path(project, dropped["id"]).unlink()
        except OSError:
            pass
    save_project(project)
    return entry


def list_versions(project: Project) -> list[dict]:
    """The history index, newest first."""
    return list(reversed(project.versions))


def load_version(project: Project, version_id: str) -> dict | None:
    """The full plan of a snapshot, or ``None`` if it's missing."""
    try:
        data = json.loads(_version_path(project, version_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan = data.get("plan") if isinstance(data, dict) else None
    return plan if isinstance(plan, dict) and plan else None


def restore_version(project: Project, version_id: str) -> bool:
    """Make a past snapshot the project's current plan and save. True on success.

    The current plan is snapshotted first (labelled), so restoring is itself
    reversible — you never lose the cut you're leaving.
    """
    plan = load_version(project, version_id)
    if plan is None:
        return False
    if project.has_plan and project.plan != plan:
        add_version(project, project.plan, label="before restore")
    project.plan = plan
    save_project(project)
    return True


# --- analysis store: the sift + Claude labels live IN the project ------------
#
# Analysis (per-clip sift reports, carrying any Claude/vision labels) is part
# of the PROJECT, not a loose sidecar next to the footage. It lives in
# ``<root>/analysis.json`` keyed by ``abspath|mtime`` (the same key the sift
# sidecar uses), so a re-opened project builds from its OWN stored analysis and
# the storyboard never re-scans the media pool. Re-exported footage (mtime
# changed) drops out as stale — analyse it again.

#: The analysis store filename inside a bundle.
_ANALYSIS_NAME = "analysis.json"

#: Analysis store schema version.
ANALYSIS_FORMAT_VERSION = 1


def _analysis_path(project: Project) -> Path:
    return project.root / _ANALYSIS_NAME


def _load_analysis(project: Project) -> dict:
    """The raw ``{key: report_dict}`` store, or ``{}`` when absent/corrupt."""
    try:
        data = json.loads(_analysis_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("monteur_analysis") != ANALYSIS_FORMAT_VERSION:
        return {}
    clips = data.get("clips")
    return dict(clips) if isinstance(clips, dict) else {}


def save_reports(project: Project, reports: list) -> None:
    """Merge sift reports (with any Claude labels) into the project's store.

    Best-effort and atomic: keyed by ``abspath|mtime``, a re-analysed clip
    (new mtime) replaces its older entry, and a clip we cannot stat is skipped.
    This is what makes the analysis part of the project — the build reads it
    back with :func:`load_reports` and never re-scans the pool.
    """
    from monteur import sift

    if not reports:
        return
    store = _load_analysis(project)
    for report in reports:
        try:
            ab = os.path.abspath(report.path)
            key = f"{ab}|{os.path.getmtime(ab)}"
        except OSError:
            continue
        # drop any stale entry for the same clip (a different mtime)
        store = {
            k: v for k, v in store.items()
            if str(k).rpartition("|")[0] != ab
        }
        try:
            store[key] = sift.report_to_dict(report)
        except Exception:  # noqa: BLE001 — one bad report never blocks the rest
            continue
    root = project.root
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = _analysis_path(project)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(
            json.dumps(
                {"monteur_analysis": ANALYSIS_FORMAT_VERSION, "clips": store},
                ensure_ascii=False, indent=1,
            ) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass  # a store we cannot write degrades to "not analysed", never a crash


def load_reports(project: Project) -> list:
    """The project's FRESH stored sift reports (mtime-checked), pool order.

    Reports whose clip is gone or was re-exported (mtime changed) are skipped
    as stale. Returns an empty list when nothing is analysed yet — the build
    turns that into an "analyse your footage first" message, never a re-scan.
    """
    from monteur import sift

    store = _load_analysis(project)
    if not store:
        return []
    # pool order: pooled clip paths first (folders expanded is the web layer's
    # job, so here we order by the media-pool file entries we know, then any
    # remaining stored clips) — deterministic and stable for the build.
    pool_order = {
        os.path.abspath(str(e.get("path"))): i
        for i, e in enumerate(project.media_pool)
    }
    fresh: list = []
    for key, data in store.items():
        ab, _, mtime = str(key).rpartition("|")
        try:
            if not mtime or os.path.getmtime(ab) != float(mtime):
                continue
        except OSError:
            continue
        try:
            fresh.append((pool_order.get(ab, len(pool_order)), ab, sift.report_from_dict(data)))
        except Exception:  # noqa: BLE001 — skip a corrupt entry, keep the rest
            continue
    fresh.sort(key=lambda t: (t[0], t[1]))
    return [report for _, _, report in fresh]


def analyzed_count(project: Project) -> int:
    """How many of the project's clips have a fresh stored analysis (cheap)."""
    return len(load_reports(project))


# --- music store: the song's beat/energy analysis lives IN the project -------
#
# Music analysis (tempo, beats, energy sections — the grid montage cuts to) is
# part of the PROJECT, not a loose sidecar next to the song. It lives in
# ``<root>/music.json`` keyed by the song's ``abspath`` + ``mtime``, so a
# re-opened project builds from its OWN stored analysis and never re-runs the
# DSP. A re-exported song (mtime changed) drops out as stale — analyse it again.
# There is ONE song per project, so this store holds a single analysis, unlike
# the per-clip analysis store above.

#: The music store filename inside a bundle.
_MUSIC_NAME = "music.json"

#: Music store schema version.
MUSIC_FORMAT_VERSION = 1


def _music_path(project: Project) -> Path:
    return project.root / _MUSIC_NAME


def save_music(project: Project, music) -> None:
    """Persist the project's ONE music analysis to ``music.json``.

    Best-effort and atomic (temp file + ``os.replace``), keyed by the song's
    ``abspath`` + ``mtime``: a re-analysed song replaces the old entry, a song
    we cannot stat is skipped, and ``music is None`` is a no-op. This is what
    makes the music grid part of the project — the build reads it back with
    :func:`load_music` and never re-runs the DSP.
    """
    from dataclasses import asdict

    if music is None:
        return
    try:
        ab = os.path.abspath(music.path)
        mtime = os.path.getmtime(ab)
    except OSError:
        return  # a song we cannot stat is skipped, never a crash
    root = project.root
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = _music_path(project)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(
            json.dumps(
                {
                    "monteur_music": MUSIC_FORMAT_VERSION,
                    "path": ab,
                    "mtime": mtime,
                    "analysis": asdict(music),
                },
                ensure_ascii=False, indent=1,
            ) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass  # a store we cannot write degrades to "not analysed", never a crash


def load_music(project: Project, music_path):
    """The project's stored music analysis for ``music_path`` (mtime-checked).

    Returns the persisted :class:`~monteur.music.MusicAnalysis` ONLY when the
    stored path matches ``os.path.abspath(music_path)`` AND the stored mtime
    equals the song's current mtime; a re-exported song (mtime changed) is
    stale and yields ``None``. Missing, corrupt or mismatched → ``None`` — the
    build then re-analyses the song instead of crashing.
    """
    from monteur.music import MusicAnalysis, MusicSection

    try:
        data = json.loads(_music_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("monteur_music") != MUSIC_FORMAT_VERSION:
        return None
    ab = os.path.abspath(str(music_path))
    if str(data.get("path")) != ab:
        return None  # a different song → not ours
    try:
        if os.path.getmtime(ab) != float(data.get("mtime")):
            return None  # a re-export (mtime changed) → stale
    except (OSError, TypeError, ValueError):
        return None
    analysis = data.get("analysis")
    if not isinstance(analysis, dict):
        return None
    try:
        sections = [
            MusicSection(**s)
            for s in (analysis.get("sections") or [])
            if isinstance(s, dict)
        ]
        return MusicAnalysis(
            path=str(analysis.get("path") or ab),
            duration=float(analysis.get("duration") or 0.0),
            tempo=float(analysis.get("tempo") or 0.0),
            beats=list(analysis.get("beats") or []),
            sections=sections,
            downbeats=list(analysis.get("downbeats") or []),
            phrases=list(analysis.get("phrases") or []),
            drops=list(analysis.get("drops") or []),
            low_energy=list(analysis.get("low_energy") or []),
        )
    except (TypeError, ValueError):
        return None  # a corrupt analysis degrades to "not analysed"


# --- migration from drafts ---------------------------------------------------


def migrate_drafts() -> list[Project]:
    """Turn existing Create-wizard drafts into first-class projects.

    Reads ``~/.monteur/drafts.json`` (honoring ``MONTEUR_DRAFTS_PATH`` via
    :mod:`monteur.drafts`) and, for each draft not already migrated, creates
    a project carrying: the draft's ``folder`` (a ``"folder"`` media-pool
    entry) and ``music`` (a ``"file"`` entry, when set), the draft's
    ``settings`` as the project ``options``, and its saved ``plan_json`` as
    the project plan.

    IDEMPOTENT and LOSSLESS: each new project records
    ``migrated_from_draft: <draft id>``, and a re-run skips every draft whose
    id already appears there — so calling this repeatedly adds nothing new.
    ``drafts.json`` is only READ, never mutated or deleted; it stays as a
    safety copy. Returns the projects created THIS call (empty on a re-run).
    """
    from monteur import drafts as drafts_mod

    already = _already_migrated_draft_ids()
    created: list[Project] = []
    for summary in drafts_mod.list_drafts():
        draft_id = str(summary.get("id") or "").strip()
        if not draft_id or draft_id in already:
            continue
        record = drafts_mod.load_draft(draft_id)
        if not isinstance(record, dict):
            continue
        options = dict(record.get("settings") or {})
        plan = record.get("plan_json")
        plan = plan if isinstance(plan, dict) and plan else None
        project = create_project(
            record.get("name") or "Migrated cut",
            options=options,
            plan=plan,
            migrated_from_draft=draft_id,
        )
        folder = str(record.get("folder") or "").strip()
        if folder:
            _pool_append(project, folder, "folder")
        music = str(record.get("music") or "").strip()
        if music:
            _pool_append(project, music, "file")
        save_project(project)
        created.append(project)
        already.add(draft_id)
    return created


def _already_migrated_draft_ids() -> set[str]:
    """Draft ids already turned into projects (the idempotency key set)."""
    ids: set[str] = set()
    root = projects_root()
    try:
        bundles = list(root.iterdir())
    except OSError:
        return ids
    for bundle in bundles:
        if not bundle.is_dir():
            continue
        project = load_project(bundle.name)
        if project is not None and project.migrated_from_draft:
            ids.add(project.migrated_from_draft)
    return ids
