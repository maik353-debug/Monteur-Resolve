"""DaVinci Resolve integration for Fable.

Fable talks to Resolve through Blackmagic's scripting API. First enable
scripting in Resolve: Preferences > System > General > "External scripting
using" and set it to "Local" (or "Network" for remote control). Then run
Fable one of three ways:

1. Resolve Console (Workspace > Console, select Py3): ``import fable.resolve``
   works directly because Resolve preloads its scripting module; call
   ``fable.resolve.connect()``.

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

    from fable.resolve import connect

    bridge = connect()
    timeline = bridge.read_timeline()
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType
from typing import Any

from fable.model import AUDIO, VIDEO, Clip, Marker, Timeline

_MODULE_NAME = "DaVinciResolveScript"

_ENABLE_HINT = (
    'Make sure DaVinci Resolve is running and scripting is enabled: open '
    'Resolve Preferences > System > General and set "External scripting '
    'using" to "Local".'
)


class FableResolveError(RuntimeError):
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
    install paths. Raises FableResolveError with setup guidance if missing.
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
    raise FableResolveError(
        f"Could not locate the {_MODULE_NAME} module. Searched: "
        + ", ".join(searched)
        + ". Install DaVinci Resolve (free from blackmagicdesign.com), then "
        "point Fable at its scripting API by setting RESOLVE_SCRIPT_API to "
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
        raise FableResolveError(
            "DaVinciResolveScript.scriptapp('Resolve') returned nothing — "
            "Resolve does not appear to be running. " + _ENABLE_HINT
        )
    return ResolveBridge(app)


class ResolveBridge:
    """Thin wrapper around the Resolve scripting API's current project."""

    def __init__(self, app: Any) -> None:
        if app is None:
            raise FableResolveError(
                "ResolveBridge requires a Resolve app object. " + _ENABLE_HINT
            )
        self.app = app

    def _project(self) -> Any:
        manager = self.app.GetProjectManager()
        if manager is None:
            raise FableResolveError(
                "Resolve returned no project manager. " + _ENABLE_HINT
            )
        project = manager.GetCurrentProject()
        if project is None:
            raise FableResolveError(
                "No project is open in Resolve — open or create a project "
                "in the Project Manager first."
            )
        return project

    def _media_pool(self) -> Any:
        pool = self._project().GetMediaPool()
        if pool is None:
            raise FableResolveError(
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
            raise FableResolveError(
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
        raise FableResolveError(
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
            raise FableResolveError(
                f"Resolve failed to import a timeline from {path!r} into "
                f"project {self.project_name()!r} — check that the file is a "
                "valid EDL/FCPXML/AAF and its media is available."
            )
        return True

    def import_media(self, paths: list[str]) -> int:
        pool = self._media_pool()
        items = pool.ImportMedia(paths)
        if items is None:
            raise FableResolveError(
                f"Resolve failed to import media into project "
                f"{self.project_name()!r}: {paths}"
            )
        return len(items)


def _parse_fps(raw_timeline: Any) -> float:
    setting = raw_timeline.GetSetting("timelineFrameRate")
    try:
        return float(setting)
    except (TypeError, ValueError):
        raise FableResolveError(
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
