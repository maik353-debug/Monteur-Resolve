"""DaVinci Resolve integration for Monteur.

Monteur talks to Resolve through Blackmagic's scripting API. First enable
scripting in Resolve: Preferences > System > General > "External scripting
using" and set it to "Local" (or "Network" for remote control). Then run
Monteur one of three ways:

1. Resolve Console (Workspace > Console, select Py3): ``import monteur.resolve``
   works directly because Resolve preloads its scripting module; call
   ``monteur.resolve.connect()``.

2. Workspace > Scripts: drop a script into the Resolve scripts folder
   (e.g. macOS ``~/Library/Application Support/Blackmagic Design/DaVinci
   Resolve/Fusion/Scripts/Utility``) and it runs with the API available.

3. Externally, from any Python 3 process while Resolve is running. Set the
   three environment variables (macOS values shown; adjust per platform):

   RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
   RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
   PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules"

   On Windows the API lives under ``%PROGRAMDATA%\\Blackmagic Design\\DaVinci
   Resolve\\Support\\Developer\\Scripting`` and the lib is ``fusionscript.dll``
   in the Resolve install folder; on Linux the API is under
   ``/opt/resolve/Developer/Scripting`` and the lib is
   ``/opt/resolve/libs/Fusion/fusionscript.so``.

Typical use::

    from monteur.resolve import connect

    bridge = connect()
    timeline = bridge.read_timeline()
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType
from typing import Any

from monteur.model import AUDIO, VIDEO, Clip, Marker, Timeline

_MODULE_NAME = "DaVinciResolveScript"

_ENABLE_HINT = (
    'Make sure DaVinci Resolve is running and scripting is enabled: open '
    'Resolve Preferences > System > General and set "External scripting '
    'using" to "Local".'
)


class MonteurResolveError(RuntimeError):
    """Raised when the Resolve scripting API is unavailable or misbehaves."""


def _candidate_module_dirs() -> list[str]:
    dirs: list[str] = []
    api = os.environ.get("RESOLVE_SCRIPT_API")
    if api:
        dirs.append(os.path.join(api, "Modules"))
        dirs.append(api)
    lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if lib:
        dirs.append(os.path.dirname(lib))
    if sys.platform == "darwin":
        dirs.append(
            "/Library/Application Support/Blackmagic Design"
            "/DaVinci Resolve/Developer/Scripting/Modules"
        )
    elif sys.platform.startswith("win"):
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        dirs.append(
            os.path.join(
                program_data,
                "Blackmagic Design",
                "DaVinci Resolve",
                "Support",
                "Developer",
                "Scripting",
                "Modules",
            )
        )
    else:
        dirs.append("/opt/resolve/Developer/Scripting/Modules")
        dirs.append("/home/resolve/Developer/Scripting/Modules")
    return dirs


def find_scripting_module() -> ModuleType:
    """Locate and import ``DaVinciResolveScript``.

    Tries PYTHONPATH / already-importable locations first, then
    RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB, then the standard per-platform
    install paths. Raises MonteurResolveError with setup guidance if missing.
    """
    try:
        return importlib.import_module(_MODULE_NAME)
    except ImportError:
        pass
    searched = _candidate_module_dirs()
    for directory in searched:
        if not os.path.isfile(os.path.join(directory, _MODULE_NAME + ".py")):
            continue
        if directory not in sys.path:
            sys.path.append(directory)
        try:
            return importlib.import_module(_MODULE_NAME)
        except ImportError:
            continue
    raise MonteurResolveError(
        f"Could not locate the {_MODULE_NAME} module. Searched: "
        + ", ".join(searched)
        + ". Install DaVinci Resolve (free from blackmagicdesign.com), then "
        "point Monteur at its scripting API by setting RESOLVE_SCRIPT_API to "
        "the Developer/Scripting folder (and RESOLVE_SCRIPT_LIB to "
        "fusionscript.so/.dll), or add the Scripting/Modules folder to "
        "PYTHONPATH. " + _ENABLE_HINT
    )


def connect(app: Any | None = None) -> "ResolveBridge":
    """Connect to a running Resolve instance and return a ResolveBridge.

    Pass ``app`` to inject a pre-built Resolve app object (e.g. the ``resolve``
    global inside Resolve's Console, or a fake in tests).
    """
    if app is None:
        module = find_scripting_module()
        app = module.scriptapp("Resolve")
    if app is None:
        raise MonteurResolveError(
            "DaVinciResolveScript.scriptapp('Resolve') returned nothing — "
            "Resolve does not appear to be running. " + _ENABLE_HINT
        )
    return ResolveBridge(app)


class ResolveBridge:
    """Thin wrapper around the Resolve scripting API's current project."""

    def __init__(self, app: Any) -> None:
        if app is None:
            raise MonteurResolveError(
                "ResolveBridge requires a Resolve app object. " + _ENABLE_HINT
            )
        self.app = app

    def _project(self) -> Any:
        manager = self.app.GetProjectManager()
        if manager is None:
            raise MonteurResolveError(
                "Resolve returned no project manager. " + _ENABLE_HINT
            )
        project = manager.GetCurrentProject()
        if project is None:
            raise MonteurResolveError(
                "No project is open in Resolve — open or create a project "
                "in the Project Manager first."
            )
        return project

    def _media_pool(self) -> Any:
        pool = self._project().GetMediaPool()
        if pool is None:
            raise MonteurResolveError(
                f"Project {self.project_name()!r} returned no media pool."
            )
        return pool

    def project_name(self) -> str:
        return self._project().GetName()

    def list_timelines(self) -> list[str]:
        project = self._project()
        names: list[str] = []
        for index in range(1, int(project.GetTimelineCount()) + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline is not None:
                names.append(timeline.GetName())
        return names

    def current_timeline_name(self) -> str:
        return self._current_timeline().GetName()

    def _current_timeline(self) -> Any:
        timeline = self._project().GetCurrentTimeline()
        if timeline is None:
            raise MonteurResolveError(
                f"Project {self.project_name()!r} has no current timeline — "
                "open a timeline in the Edit page first."
            )
        return timeline

    def _timeline_by_name(self, name: str) -> Any:
        project = self._project()
        for index in range(1, int(project.GetTimelineCount()) + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline is not None and timeline.GetName() == name:
                return timeline
        raise MonteurResolveError(
            f"Timeline {name!r} not found in project {self.project_name()!r}. "
            f"Available timelines: {self.list_timelines()}"
        )

    def read_timeline(self, name: str | None = None) -> Timeline:
        """Convert a Resolve timeline (current, or named) to a model Timeline.

        Record positions are normalized to start at frame 0; the original
        timeline start frame (typically 01:00:00:00) is stored in
        ``timeline.metadata["record_start"]``.
        """
        raw = self._current_timeline() if name is None else self._timeline_by_name(name)
        fps = _parse_fps(raw)
        record_start = _record_start(raw)
        clips: list[Clip] = []
        for kind, prefix in ((VIDEO, "V"), (AUDIO, "A")):
            for track_index in range(1, int(raw.GetTrackCount(kind)) + 1):
                items = raw.GetItemListInTrack(kind, track_index) or []
                for item in items:
                    clips.append(
                        _convert_item(item, f"{prefix}{track_index}", kind, record_start)
                    )
        markers = _convert_markers(raw)
        return Timeline(
            name=raw.GetName(),
            fps=fps,
            clips=clips,
            markers=markers,
            metadata={"record_start": record_start},
        )

    def import_timeline_file(self, path: str) -> bool:
        pool = self._media_pool()
        timeline = pool.ImportTimelineFromFile(path)
        if timeline is None:
            raise MonteurResolveError(
                f"Resolve failed to import a timeline from {path!r} into "
                f"project {self.project_name()!r} — check that the file is a "
                "valid EDL/FCPXML/AAF and its media is available."
            )
        return True

    def import_media(self, paths: list[str]) -> int:
        pool = self._media_pool()
        items = pool.ImportMedia(paths)
        if items is None:
            raise MonteurResolveError(
                f"Resolve failed to import media into project "
                f"{self.project_name()!r}: {paths}"
            )
        return len(items)

    def add_markers(
        self, markers: list[Marker], timeline_name: str | None = None
    ) -> int:
        """Add markers to the current (or named) timeline; return the count added.

        Resolve's ``Timeline.AddMarker(frameId, ...)`` takes frames RELATIVE
        to the timeline start (even when the timeline starts at e.g.
        01:00:00:00), which matches our 0-based ``Marker.frame`` — so frames
        are passed through unchanged. Marker colors are mapped to the nearest
        Resolve marker color name ("Blue" when unknown). Markers Resolve
        rejects (e.g. duplicate frame) are skipped, not fatal.
        """
        if timeline_name is None:
            timeline = self._current_timeline()
        else:
            timeline = self._timeline_by_name(timeline_name)
            self._project().SetCurrentTimeline(timeline)
        added = 0
        for marker in markers:
            ok = timeline.AddMarker(
                int(marker.frame),
                _marker_color(marker.color),
                marker.name,
                marker.note,
                1,
                "",
            )
            if ok:
                added += 1
        return added

    def build_timeline_from_plan(
        self, plan, fps: float, name: str = "Monteur Montage"
    ) -> str:
        """Build a montage timeline in Resolve from a MontagePlan.

        Steps: import the distinct clip paths (plus the music) into the media
        pool, create an empty timeline (name uniquified with " 2", " 3", ...
        on a clash), append one video clip per plan entry in record order
        (Resolve appends back-to-back, matching the plan's gapless record
        ranges), then append the music as one audio clip. Returns the created
        timeline's name.

        Imported media-pool items are mapped back to file paths by, in order:
        ``GetClipProperty("File Path")``; positional order when Resolve
        returned exactly one item per requested path; and finally basename
        matching via ``GetName()``. An unmapped path raises
        MonteurResolveError.
        """
        entries = sorted(plan.entries, key=lambda e: e.record_start)
        paths: list[str] = []
        for entry in entries:
            if entry.clip_path not in paths:
                paths.append(entry.clip_path)
        if plan.music_path not in paths:
            paths.append(plan.music_path)

        pool = self._media_pool()
        items = pool.ImportMedia(paths)
        if not items:
            raise MonteurResolveError(
                f"Resolve imported no media into project "
                f"{self.project_name()!r} from: {paths}. Check that the files "
                "exist and are readable by Resolve."
            )
        by_path = _map_items_to_paths(paths, items)
        missing = [p for p in paths if by_path.get(p) is None]
        if missing:
            raise MonteurResolveError(
                f"Resolve did not return media pool items for: {missing}. "
                "The files may be missing, offline or unsupported."
            )

        existing = set(self.list_timelines())
        timeline_name = name
        suffix = 2
        while timeline_name in existing:
            timeline_name = f"{name} {suffix}"
            suffix += 1
        timeline = pool.CreateEmptyTimeline(timeline_name)
        if timeline is None:
            raise MonteurResolveError(
                f"Resolve failed to create timeline {timeline_name!r} in "
                f"project {self.project_name()!r}."
            )

        for entry in entries:
            clip_info = {
                "mediaPoolItem": by_path[entry.clip_path],
                "startFrame": int(round(entry.source_start * fps)),
                "endFrame": int(round(entry.source_end * fps)) - 1,
                "mediaType": 1,
            }
            if not pool.AppendToTimeline([clip_info]):
                raise MonteurResolveError(
                    f"Resolve failed to append {entry.clip_path!r} "
                    f"({entry.source_start:.2f}-{entry.source_end:.2f}s) to "
                    f"timeline {timeline_name!r}."
                )
        music_info = {
            "mediaPoolItem": by_path[plan.music_path],
            "startFrame": 0,
            "endFrame": int(round(plan.duration * fps)) - 1,
            "mediaType": 2,
        }
        if not pool.AppendToTimeline([music_info]):
            raise MonteurResolveError(
                f"Resolve failed to append the music {plan.music_path!r} to "
                f"timeline {timeline_name!r}."
            )
        return timeline_name


# Resolve's fixed marker color palette.
RESOLVE_MARKER_COLORS = (
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
)


def _marker_color(color: str) -> str:
    """Nearest Resolve marker color name for a model marker color.

    A color that already is a Resolve color name passes through verbatim
    (case-insensitively normalized); anything else becomes "Blue".
    """
    wanted = (color or "").strip().lower()
    for name in RESOLVE_MARKER_COLORS:
        if name.lower() == wanted:
            return name
    return "Blue"


def _map_items_to_paths(paths: list[str], items: list[Any]) -> dict[str, Any]:
    """Map requested file paths to imported media pool items.

    Strategy (documented on build_timeline_from_plan): exact
    GetClipProperty("File Path") matches first, then positional order when
    the counts line up, then basename matching via GetName(). Unmatched
    paths map to None.
    """
    by_path: dict[str, Any] = {path: None for path in paths}
    for item in items:
        getter = getattr(item, "GetClipProperty", None)
        if not callable(getter):
            continue
        try:
            file_path = getter("File Path")
        except Exception:
            continue
        if file_path in by_path and by_path[file_path] is None:
            by_path[file_path] = item
    if any(v is None for v in by_path.values()) and len(items) == len(paths):
        for path, item in zip(paths, items):
            if by_path[path] is None:
                by_path[path] = item
    remaining = [p for p, v in by_path.items() if v is None]
    if remaining:
        by_name: dict[str, Any] = {}
        for item in items:
            try:
                by_name.setdefault(item.GetName(), item)
            except Exception:
                continue
        for path in remaining:
            by_path[path] = by_name.get(os.path.basename(path))
    return by_path


def _parse_fps(raw_timeline: Any) -> float:
    setting = raw_timeline.GetSetting("timelineFrameRate")
    try:
        return float(setting)
    except (TypeError, ValueError):
        raise MonteurResolveError(
            f"Timeline {raw_timeline.GetName()!r} reported an unreadable "
            f"frame rate setting: {setting!r}"
        ) from None


def _record_start(raw_timeline: Any) -> int:
    get_start = getattr(raw_timeline, "GetStartFrame", None)
    if callable(get_start):
        return int(get_start())
    starts: list[int] = []
    for kind in (VIDEO, AUDIO):
        for track_index in range(1, int(raw_timeline.GetTrackCount(kind)) + 1):
            for item in raw_timeline.GetItemListInTrack(kind, track_index) or []:
                starts.append(int(item.GetStart()))
    return min(starts, default=0)


def _convert_item(item: Any, track: str, kind: str, record_start: int) -> Clip:
    start = int(item.GetStart())
    end = int(item.GetEnd())
    left = int(item.GetLeftOffset())
    duration = int(item.GetDuration())
    source_name = ""
    get_pool_item = getattr(item, "GetMediaPoolItem", None)
    if callable(get_pool_item):
        pool_item = get_pool_item()
        if pool_item is not None:
            source_name = pool_item.GetName()
    return Clip(
        name=item.GetName(),
        track=track,
        kind=kind,
        source_in=left,
        source_out=left + duration,
        record_in=start - record_start,
        record_out=end - record_start,
        source_name=source_name,
    )


def _convert_markers(raw_timeline: Any) -> list[Marker]:
    raw_markers = raw_timeline.GetMarkers() or {}
    markers = [
        Marker(
            frame=int(frame),
            name=info.get("name", ""),
            note=info.get("note", ""),
            color=info.get("color", ""),
        )
        for frame, info in raw_markers.items()
    ]
    markers.sort(key=lambda m: m.frame)
    return markers


# --- Resolve menu scripts ------------------------------------------------------

_ANALYZE_TIMELINE_SCRIPT = '''\
"""Monteur - Analyze Timeline.

Runs inside DaVinci Resolve (Workspace > Scripts > Utility, installed by
`monteur resolve install-scripts`). Reads the current timeline, prints its
pacing stats to the console and adds a red "Monteur: slow section" marker
at the start of every slow section.
"""


def _get_resolve():
    """Return the Resolve app object from whatever host runs this script."""
    try:
        return resolve  # provided as a global by Resolve's script host
    except NameError:
        pass
    try:
        return app.GetResolve()  # Fusion-style host
    except (NameError, AttributeError):
        pass
    try:
        return bmd.scriptapp("Resolve")
    except (NameError, AttributeError):
        return None


def main():
    try:
        from monteur.analysis import analyze_timeline
        from monteur.model import Marker, seconds_to_frames
        from monteur.resolve import MonteurResolveError, ResolveBridge
    except ImportError:
        print(
            "Monteur is not installed in Resolve's Python interpreter.\\n"
            "Install it for the Python 3 that Resolve uses, e.g.:\\n"
            "    python3 -m pip install monteur\\n"
            "then re-run this script."
        )
        return

    resolve_app = _get_resolve()
    if resolve_app is None:
        print("Could not reach the Resolve scripting API from this host.")
        return

    bridge = ResolveBridge(resolve_app)
    try:
        timeline = bridge.read_timeline()
        stats = analyze_timeline(timeline)
        print("Timeline : " + (stats.timeline_name or "-"))
        print(
            "Duration : {0:.1f}s at {1:g} fps".format(
                stats.duration_seconds, stats.fps
            )
        )
        print(
            "Shots    : {0}   Cuts: {1}".format(stats.shot_count, stats.cut_count)
        )
        print(
            "Shot len : avg {0:.2f}s  median {1:.2f}s  "
            "min {2:.2f}s  max {3:.2f}s".format(
                stats.avg_shot_seconds,
                stats.median_shot_seconds,
                stats.min_shot_seconds,
                stats.max_shot_seconds,
            )
        )
        markers = [
            Marker(
                frame=seconds_to_frames(section.start, timeline.fps),
                name="Monteur: slow section",
                note="avg shot {0:.1f}s".format(section.avg_shot_length),
                color="Red",
            )
            for section in stats.sections
            if section.label == "slow"
        ]
        added = bridge.add_markers(markers)
        print("Added {0} 'Monteur: slow section' marker(s).".format(added))
    except MonteurResolveError as exc:
        print("Monteur: {0}".format(exc))


main()
'''

_OPEN_STUDIO_SCRIPT = '''\
"""Monteur - Open Studio.

Runs inside DaVinci Resolve (Workspace > Scripts > Utility, installed by
`monteur resolve install-scripts`). Launches the Monteur Studio web app
(`monteur ui`) detached in the background and prints its URL.
"""

import os
import subprocess
import sys

URL = "http://127.0.0.1:8765"


def main():
    try:
        import monteur  # noqa: F401
    except ImportError:
        print(
            "Monteur is not installed in Resolve's Python interpreter.\\n"
            "Install it for the Python 3 that Resolve uses, e.g.:\\n"
            "    python3 -m pip install monteur\\n"
            "then re-run this script."
        )
        return
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    subprocess.Popen([sys.executable, "-m", "monteur.cli", "ui"], **kwargs)
    print("Monteur Studio starting at " + URL)


main()
'''

_SCRIPT_FILES = {
    "Monteur - Analyze Timeline.py": _ANALYZE_TIMELINE_SCRIPT,
    "Monteur - Open Studio.py": _OPEN_STUDIO_SCRIPT,
}

_LINUX_SYSTEM_SCRIPT_DIR = "/opt/resolve/Fusion/Scripts/Utility"


def _script_install_dirs() -> list[str]:
    """Resolve's Fusion Scripts/Utility folder(s) for this platform."""
    if sys.platform == "darwin":
        return [
            os.path.expanduser(
                "~/Library/Application Support/Blackmagic Design"
                "/DaVinci Resolve/Fusion/Scripts/Utility"
            )
        ]
    if sys.platform.startswith("win"):
        appdata = os.environ.get(
            "APPDATA", os.path.expanduser(os.path.join("~", "AppData", "Roaming"))
        )
        return [
            os.path.join(
                appdata,
                "Blackmagic Design",
                "DaVinci Resolve",
                "Support",
                "Fusion",
                "Scripts",
                "Utility",
            )
        ]
    dirs = [os.path.expanduser("~/.local/share/DaVinciResolve/Fusion/Scripts/Utility")]
    if os.path.isdir(_LINUX_SYSTEM_SCRIPT_DIR) and os.access(
        _LINUX_SYSTEM_SCRIPT_DIR, os.W_OK
    ):
        dirs.append(_LINUX_SYSTEM_SCRIPT_DIR)
    return dirs


def install_scripts(dry_run: bool = False) -> list[str]:
    """Install Monteur's launcher scripts into Resolve's scripts menu.

    Writes "Monteur - Analyze Timeline.py" and "Monteur - Open Studio.py"
    into Resolve's Fusion ``Scripts/Utility`` folder (created if needed);
    they appear under Workspace > Scripts > Utility after a Resolve restart.
    With ``dry_run=True`` nothing is written and the target paths are
    returned as-is. Returns the list of written (or would-be-written) paths.
    """
    written: list[str] = []
    for directory in _script_install_dirs():
        for filename, content in _SCRIPT_FILES.items():
            target = os.path.join(directory, filename)
            written.append(target)
            if dry_run:
                continue
            os.makedirs(directory, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
    return written
