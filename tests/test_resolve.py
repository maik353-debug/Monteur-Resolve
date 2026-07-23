from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

import monteur.resolve as resolve
from monteur.model import AUDIO, VIDEO, Clip, Marker, Timeline
from monteur.montage import MontageEntry, MontagePlan
from monteur.resolve import MonteurResolveError, ResolveBridge, connect


@pytest.fixture(autouse=True)
def _isolated_worker_config(tmp_path, monkeypatch):
    """_worker_python() now reads BOTH the env override and the settings
    file — every test gets a scratch settings path and a clean env so the
    developer's real ~/.monteur/settings.json (or shell) never leaks in.
    Tests that want an override set it explicitly on top of this."""
    monkeypatch.setenv(
        "MONTEUR_SETTINGS_PATH", str(tmp_path / "resolve-settings.json")
    )
    monkeypatch.delenv("MONTEUR_RESOLVE_PYTHON", raising=False)


class FakeItem:
    def __init__(
        self,
        name: str,
        start: int,
        end: int,
        left_offset: int = 0,
        media_name: str = "",
    ) -> None:
        self._name = name
        self._start = start
        self._end = end
        self._left_offset = left_offset
        self._media_name = media_name

    def GetName(self) -> str:
        return self._name

    def GetStart(self) -> int:
        return self._start

    def GetEnd(self) -> int:
        return self._end

    def GetLeftOffset(self) -> int:
        return self._left_offset

    def GetDuration(self) -> int:
        return self._end - self._start

    def GetMediaPoolItem(self):
        if not self._media_name:
            return None
        return FakeMediaPoolItem(self._media_name)


class FakeMediaPoolItem:
    def __init__(self, name: str) -> None:
        self._name = name

    def GetName(self) -> str:
        return self._name


class FakeTextTool:
    """A Fusion tool; reg_id 'TextPlus' makes it the Text+ tool.

    ``SetInput`` accepts Fusion's optional third ``frame`` argument; calls
    that pass a frame are recorded as keyframes so tests can assert an
    animation was scripted (a plain static title never passes a frame).
    """

    def __init__(self, reg_id: str = "TextPlus") -> None:
        self._reg_id = reg_id
        self.inputs: dict = {}
        self.keyframes: dict = {}

    def GetAttrs(self) -> dict:
        return {"TOOLS_RegID": self._reg_id}

    def SetInput(self, name: str, value, frame=None) -> None:
        if frame is None:
            self.inputs[name] = value
        else:
            self.keyframes.setdefault(name, []).append((frame, value))


class FakeComp:
    """A Fusion composition honoring GetToolList's optional type filter."""

    def __init__(self, tools: list) -> None:
        self._tools = tools

    def GetToolList(self, selected: bool = False, reg_id: str | None = None):
        tools = [
            t
            for t in self._tools
            if reg_id is None or t.GetAttrs().get("TOOLS_RegID") == reg_id
        ]
        return {i + 1: t for i, t in enumerate(tools)}


_DEFAULT_COMP = object()  # sentinel: build a comp with one Text+ tool


class FakeTitleItem:
    """A timeline item created by InsertFusionTitleIntoTimeline."""

    def __init__(self, comp: object = _DEFAULT_COMP) -> None:
        if comp is _DEFAULT_COMP:
            comp = FakeComp([FakeTextTool()])
        self._comp = comp
        self.start: int | None = None
        self.end: int | None = None

    def GetFusionCompByIndex(self, index: int):
        assert index == 1
        return self._comp

    def SetStart(self, frame: int) -> bool:
        self.start = frame
        return True

    def SetEnd(self, frame: int) -> bool:
        self.end = frame
        return True


class FakeTitleItemNoSetters:
    """An old-Resolve title item: no SetStart/SetEnd scripting support."""

    def __init__(self, comp) -> None:
        self._comp = comp

    def GetFusionCompByIndex(self, index: int):
        return self._comp


class FakeTimeline:
    def __init__(
        self,
        name: str = "Cut 1",
        fps: str = "24",
        start_frame: int = 86400,
        video_tracks: list[list[FakeItem]] | None = None,
        audio_tracks: list[list[FakeItem]] | None = None,
        markers: dict | None = None,
    ) -> None:
        self._name = name
        self._fps = fps
        self._start_frame = start_frame
        self._tracks = {
            "video": video_tracks or [],
            "audio": audio_tracks or [],
        }
        self._markers = markers or {}
        # Extra GetSetting values (e.g. timelineResolutionWidth/Height on a
        # timeline "imported" from an FCPXML file).
        self._settings: dict[str, str] = {}
        self.added_markers: list[tuple] = []
        self.fail_marker_frames: set[int] = set()
        # --- canvas behavior knobs (build_timeline_from_plan tests) ---
        self.settings_set: list[tuple[str, str]] = []  # recorded SetSetting calls
        self.set_setting_result = True  # False = Resolve refuses the setting
        # --- Fusion title behavior knobs (add_titles tests) ---
        self.inserted_fusion_titles: list[str] = []
        self.created_title_items: list = []
        self.title_items_queue: list = []  # preset items for the next inserts
        self.insert_title_returns_item = True  # False = legacy True return
        self.insert_title_result_override = "auto"  # e.g. None to fail inserts
        self.insert_places_item = True  # False: Resolve put it "somewhere else"
        self.raise_on_insert_title: Exception | None = None
        self.added_tracks: list[str] = []
        self.fail_add_track = False

    def AddTrack(self, kind: str) -> bool:
        if self.fail_add_track:
            return False
        self._tracks[kind].append([])
        self.added_tracks.append(kind)
        return True

    def InsertFusionTitleIntoTimeline(self, name: str):
        if self.raise_on_insert_title is not None:
            raise self.raise_on_insert_title
        if self.insert_title_result_override != "auto":
            return self.insert_title_result_override
        self.inserted_fusion_titles.append(name)
        item = (
            self.title_items_queue.pop(0)
            if self.title_items_queue
            else FakeTitleItem()
        )
        if self.insert_places_item:
            if not self._tracks["video"]:
                self._tracks["video"].append([])
            self._tracks["video"][-1].append(item)
        self.created_title_items.append(item)
        return item if self.insert_title_returns_item else True

    def AddMarker(
        self,
        frame: int,
        color: str,
        name: str,
        note: str,
        duration: int,
        custom_data: str = "",
    ) -> bool:
        if frame in self.fail_marker_frames:
            return False
        self.added_markers.append((frame, color, name, note, duration, custom_data))
        return True

    def GetName(self) -> str:
        return self._name

    def GetSetting(self, key: str) -> str:
        if key == "timelineFrameRate":
            return self._fps
        return str(self._settings.get(key, ""))

    def SetSetting(self, key: str, value: str) -> bool:
        self.settings_set.append((key, value))
        if self.set_setting_result and key == "timelineFrameRate":
            # An accepting Resolve honors the rate: read-back returns it.
            self._fps = value
        return self.set_setting_result

    def GetStartFrame(self) -> int:
        return self._start_frame

    def GetTrackCount(self, kind: str) -> int:
        return len(self._tracks[kind])

    def GetItemListInTrack(self, kind: str, index: int) -> list[FakeItem]:
        return self._tracks[kind][index - 1]

    def GetMarkers(self) -> dict:
        return self._markers


class FakeTimelineClip:
    """A timeline item that AppendToTimeline lands on the created timeline.

    Records SetProperty calls so the canvas tests can assert the cine
    presets set Scaling=1 ("scale full frame with crop") per clip.
    """

    def __init__(self, info: dict, set_property_result: bool = True) -> None:
        self.info = info
        self.properties: list[tuple[str, object]] = []
        self.set_property_result = set_property_result

    def SetProperty(self, key: str, value) -> bool:
        self.properties.append((key, value))
        return self.set_property_result


class FakePoolClip:
    """A media pool item as returned by ImportMedia."""

    def __init__(self, path: str, with_file_path: bool = True) -> None:
        self.path = path
        self._with_file_path = with_file_path

    def GetName(self) -> str:
        return os.path.basename(self.path)

    def GetClipProperty(self, key: str) -> str:
        if key == "File Path" and self._with_file_path:
            return self.path
        return ""


class FakeMediaPool:
    def __init__(self, project: "FakeProject | None" = None) -> None:
        self._project = project
        self.imported_timelines: list[str] = []
        self.imported_media: list[str] = []
        self.import_calls: list[list[str]] = []
        self.fail_timeline_import = False
        # Raised by ImportTimelineFromFile (hybrid must fall back cleanly).
        self.raise_on_timeline_import: Exception | None = None
        # True = an older Resolve: ImportTimelineFromFile returns a truthy
        # flag instead of the timeline object (the import becomes current).
        self.import_returns_bool = False
        self.import_rename: str | None = None  # Resolve renamed the import
        self.imported_timeline_fps: str | None = None  # read-back override
        self.imported_timeline_resolution: tuple[int, int] | None = None
        # Every ImportTimelineFromFile call: path, options, whether the file
        # existed AT CALL TIME and its byte size (the bridge deletes its temp
        # file afterwards, so this is the only honest record).
        self.timeline_import_calls: list[dict] = []
        self.imported_fcpxml: list[str] = []  # captured file content
        self.import_media_result: list | None | str = "default"
        self.fail_append = False
        # True = this Resolve build rejects positioned placement: any
        # clip_info carrying "recordFrame" returns None (gapless fallback).
        self.reject_record_placement = False
        self.appended: list[dict] = []
        self.created_timeline_names: list[str] = []
        # False = the appended clips' SetProperty refuses (canvas crop tests)
        self.append_clip_property_result = True

    def ImportTimelineFromFile(self, path: str, options: dict | None = None):
        # Capture the file BEFORE returning (or raising): the hybrid build
        # removes its temp FCPXML in a finally, so this is when it must be
        # read.
        existed = os.path.isfile(path)
        content = ""
        if existed:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        self.timeline_import_calls.append(
            {
                "path": path,
                "options": dict(options) if options is not None else None,
                "existed": existed,
                "size": len(content.encode("utf-8")),
            }
        )
        if self.raise_on_timeline_import is not None:
            raise self.raise_on_timeline_import
        if self.fail_timeline_import:
            return None
        self.imported_timelines.append(path)
        self.imported_fcpxml.append(content)
        timeline = self._timeline_from_fcpxml(content, options)
        if self._project is not None:
            self._project._timelines.append(timeline)
            self._project._current = timeline
        return True if self.import_returns_bool else timeline

    def _timeline_from_fcpxml(
        self, content: str, options: dict | None
    ) -> FakeTimeline:
        """Minimally "load" an FCPXML like Resolve would.

        Name from the timelineName option (unless the pool is told the
        import was renamed), frame rate and resolution from the <format>
        element, one video item per spine asset-clip and one audio item per
        connected (lane-carrying) asset-clip — enough for the finishing
        steps (fps read-back, titles, canvas crop) to run against it.
        """
        import xml.etree.ElementTree as ET
        from fractions import Fraction

        name = str((options or {}).get("timelineName") or "Imported")
        if self.import_rename:
            name = self.import_rename
        fps = "24"
        settings: dict[str, str] = {}
        video_items: list = []
        audio_items: list = []
        if content:
            root = ET.fromstring(content)
            fmt = root.find("resources/format")
            if fmt is not None:
                frame_dur = fmt.get("frameDuration", "")
                if frame_dur.endswith("s"):
                    fps = f"{float(1 / Fraction(frame_dur[:-1])):g}"
                if fmt.get("width"):
                    settings["timelineResolutionWidth"] = fmt.get("width")
                if fmt.get("height"):
                    settings["timelineResolutionHeight"] = fmt.get("height")
            spine = root.find(".//spine")
            for clip_el in spine.findall("asset-clip") if spine is not None else []:
                video_items.append(
                    FakeTimelineClip(
                        {"name": clip_el.get("name")},
                        set_property_result=self.append_clip_property_result,
                    )
                )
                for connected in clip_el.findall("asset-clip"):
                    audio_items.append(
                        FakeTimelineClip(
                            {
                                "name": connected.get("name"),
                                "lane": connected.get("lane"),
                            }
                        )
                    )
        if self.imported_timeline_fps is not None:
            fps = self.imported_timeline_fps
        if self.imported_timeline_resolution is not None:
            width, height = self.imported_timeline_resolution
            settings["timelineResolutionWidth"] = str(width)
            settings["timelineResolutionHeight"] = str(height)
        timeline = FakeTimeline(
            name=name,
            fps=fps,
            start_frame=0,
            video_tracks=[video_items],
            audio_tracks=[audio_items],
        )
        timeline._settings.update(settings)
        return timeline

    def ImportMedia(self, paths: list[str]):
        self.import_calls.append(list(paths))
        self.imported_media.extend(paths)
        if self.import_media_result != "default":
            return self.import_media_result
        return [FakePoolClip(path) for path in paths]

    def CreateEmptyTimeline(self, name: str):
        self.created_timeline_names.append(name)
        timeline = FakeTimeline(name=name, start_frame=0)
        if self._project is not None:
            self._project._timelines.append(timeline)
            self._project._current = timeline
        return timeline

    def AppendToTimeline(self, clip_infos: list[dict]):
        if self.fail_append:
            return None
        if self.reject_record_placement and any(
            "recordFrame" in info for info in clip_infos
        ):
            return None
        self.appended.extend(clip_infos)
        # Like Resolve: the appended clips become items on the current
        # timeline — video (mediaType 1, the default) on video track 1,
        # audio (mediaType 2) on audio track 1.
        current = self._project._current if self._project is not None else None
        if current is not None:
            for info in clip_infos:
                kind = "audio" if info.get("mediaType") == 2 else "video"
                if not current._tracks[kind]:
                    current._tracks[kind].append([])
                current._tracks[kind][0].append(
                    FakeTimelineClip(
                        info, set_property_result=self.append_clip_property_result
                    )
                )
        return list(clip_infos)


class FakeProject:
    def __init__(
        self,
        name: str = "Monteur Feature",
        timelines: list[FakeTimeline] | None = None,
        current: FakeTimeline | None = None,
    ) -> None:
        self._name = name
        self._timelines = timelines or []
        self._current = current if current is not None else (
            self._timelines[0] if self._timelines else None
        )
        self.media_pool = FakeMediaPool(self)
        self.set_current_calls: list[FakeTimeline] = []
        self.settings_set: list[tuple[str, str]] = []  # project-level SetSetting
        self.set_setting_result = True
        # --- render (Deliver) behavior knobs -------------------------------
        self.render_presets: list = []  # GetRenderPresetList payload
        self.load_preset_calls: list[str] = []
        self.load_preset_result = True
        self.render_formats: dict = {}  # {"desc": "extension"}
        self.render_codecs: dict = {}   # {"codec desc": "codec key"}
        self.codec_queries: list[str] = []
        self.format_codec_calls: list[tuple] = []
        self.set_format_codec_result = True
        self.render_settings_calls: list[dict] = []
        self.set_render_settings_result = True
        self.add_render_job_result: object = "render-job-1"
        self.start_rendering_calls: list[list] = []
        self.start_rendering_result = True
        # Percents reported while IsRenderingInProgress stays True; each
        # GetRenderJobStatus pops one. Empty -> rendering finished, and the
        # status becomes render_final_status.
        self.render_progress: list[int] = []
        self.render_final_status: dict = {
            "JobStatus": "Complete", "CompletionPercentage": 100,
        }

    def GetRenderPresetList(self):
        return list(self.render_presets)

    def LoadRenderPreset(self, name: str):
        self.load_preset_calls.append(name)
        return self.load_preset_result

    def GetRenderFormats(self):
        return dict(self.render_formats)

    def GetRenderCodecs(self, render_format: str):
        self.codec_queries.append(render_format)
        return dict(self.render_codecs)

    def SetCurrentRenderFormatAndCodec(self, render_format: str, codec: str):
        self.format_codec_calls.append((render_format, codec))
        return self.set_format_codec_result

    def SetRenderSettings(self, settings: dict):
        self.render_settings_calls.append(dict(settings))
        return self.set_render_settings_result

    def AddRenderJob(self):
        return self.add_render_job_result

    def StartRendering(self, job_ids: list):
        self.start_rendering_calls.append(list(job_ids))
        return self.start_rendering_result

    def IsRenderingInProgress(self):
        return bool(self.render_progress)

    def GetRenderJobStatus(self, job_id):
        if self.render_progress:
            percent = self.render_progress.pop(0)
            return {"JobStatus": "Rendering", "CompletionPercentage": percent}
        return dict(self.render_final_status)

    def GetName(self) -> str:
        return self._name

    def SetSetting(self, key: str, value: str) -> bool:
        self.settings_set.append((key, value))
        return self.set_setting_result

    def GetTimelineCount(self) -> int:
        return len(self._timelines)

    def GetTimelineByIndex(self, index: int) -> FakeTimeline:
        return self._timelines[index - 1]

    def GetCurrentTimeline(self) -> FakeTimeline | None:
        return self._current

    def SetCurrentTimeline(self, timeline: FakeTimeline) -> bool:
        self._current = timeline
        self.set_current_calls.append(timeline)
        return True

    def GetMediaPool(self) -> FakeMediaPool:
        return self.media_pool


class FakeProjectManager:
    def __init__(self, project: FakeProject | None) -> None:
        self._project = project

    def GetCurrentProject(self) -> FakeProject | None:
        return self._project


class FakeResolve:
    def __init__(self, project: FakeProject | None) -> None:
        self._manager = FakeProjectManager(project)
        self.opened_pages: list[str] = []

    def OpenPage(self, name: str) -> bool:
        self.opened_pages.append(name)
        return True

    def GetProjectManager(self) -> FakeProjectManager:
        return self._manager


def make_bridge(
    timelines: list[FakeTimeline], current: FakeTimeline | None = None
) -> tuple[ResolveBridge, FakeProject]:
    project = FakeProject(timelines=timelines, current=current)
    return ResolveBridge(FakeResolve(project)), project


def build_append(bridge: ResolveBridge, *args, **kwargs) -> str:
    """build_timeline_from_plan pinned to the clip-by-clip append flow.

    The historical build-flow tests below assert the append path's exact
    behavior (ImportMedia calls, AppendToTimeline frames, gapless
    fallbacks). That path is now ``mode="append"`` — still fully supported
    as the forced mode and as the hybrid build's automatic fallback — so
    these tests pin it explicitly and keep every assertion byte-identical.
    The hybrid default has its own test battery further down.
    """
    kwargs.setdefault("mode", "append")
    return bridge.build_timeline_from_plan(*args, **kwargs)


def standard_timeline() -> FakeTimeline:
    return FakeTimeline(
        name="Cut 1",
        fps="24",
        start_frame=86400,
        video_tracks=[
            [
                FakeItem("Scene 1A", 86400, 86520, left_offset=10, media_name="A001"),
                FakeItem("Scene 1B", 86520, 86700, left_offset=0),
            ],
            [FakeItem("Title", 86450, 86500)],
        ],
        audio_tracks=[[FakeItem("Dialog", 86400, 86700)]],
        markers={
            240: {"color": "Blue", "name": "Note here", "note": "trim this", "duration": 1},
            12: {"color": "Red", "name": "Start", "note": "", "duration": 1},
        },
    )


def test_import_is_safe_without_resolve() -> None:
    import importlib

    module = importlib.import_module("monteur.resolve")
    assert module is resolve


def test_connect_without_resolve_raises() -> None:
    with pytest.raises(MonteurResolveError) as excinfo:
        connect()
    assert "DaVinciResolveScript" in str(excinfo.value)


def test_connect_with_injected_app() -> None:
    bridge = connect(app=FakeResolve(FakeProject(timelines=[standard_timeline()])))
    assert isinstance(bridge, ResolveBridge)
    assert bridge.project_name() == "Monteur Feature"


def test_connect_rejects_none_app_object() -> None:
    with pytest.raises(MonteurResolveError):
        ResolveBridge(None)


def test_list_and_current_timeline_names() -> None:
    first = standard_timeline()
    second = FakeTimeline(name="Cut 2")
    bridge, _ = make_bridge([first, second], current=second)
    assert bridge.list_timelines() == ["Cut 1", "Cut 2"]
    assert bridge.current_timeline_name() == "Cut 2"


def test_read_timeline_normalizes_record_start() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    timeline = bridge.read_timeline()
    assert timeline.name == "Cut 1"
    assert timeline.fps == 24.0
    assert timeline.metadata["record_start"] == 86400

    v1 = timeline.track_clips("V1")
    assert [c.name for c in v1] == ["Scene 1A", "Scene 1B"]
    first, second = v1
    assert (first.record_in, first.record_out) == (0, 120)
    assert (second.record_in, second.record_out) == (120, 300)
    assert first.duration == 120


def test_read_timeline_source_ranges_and_names() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    timeline = bridge.read_timeline()
    first = timeline.track_clips("V1")[0]
    assert (first.source_in, first.source_out) == (10, 130)
    assert first.source_name == "A001"
    assert timeline.track_clips("V1")[1].source_name == ""


def test_read_timeline_track_naming_and_kinds() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    timeline = bridge.read_timeline()
    assert timeline.tracks() == ["V1", "V2", "A1"]
    title = timeline.track_clips("V2")[0]
    assert title.kind == VIDEO
    assert (title.record_in, title.record_out) == (50, 100)
    dialog = timeline.track_clips("A1")[0]
    assert dialog.kind == AUDIO
    assert (dialog.record_in, dialog.record_out) == (0, 300)


def test_read_timeline_markers_sorted() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    timeline = bridge.read_timeline()
    assert [(m.frame, m.name, m.note, m.color) for m in timeline.markers] == [
        (12, "Start", "", "Red"),
        (240, "Note here", "trim this", "Blue"),
    ]


def test_read_timeline_fps_parsing_fractional() -> None:
    fractional = FakeTimeline(name="NTSC", fps="23.976", start_frame=0)
    bridge, _ = make_bridge([fractional])
    assert bridge.read_timeline().fps == pytest.approx(23.976)


def test_read_timeline_bad_fps_raises() -> None:
    broken = FakeTimeline(name="Broken", fps="not-a-number")
    bridge, _ = make_bridge([broken])
    with pytest.raises(MonteurResolveError):
        bridge.read_timeline()


def test_read_timeline_by_name() -> None:
    first = standard_timeline()
    second = FakeTimeline(name="Cut 2", start_frame=0)
    bridge, _ = make_bridge([first, second], current=second)
    timeline = bridge.read_timeline("Cut 1")
    assert timeline.name == "Cut 1"
    assert timeline.metadata["record_start"] == 86400


def test_read_timeline_unknown_name_raises() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    with pytest.raises(MonteurResolveError) as excinfo:
        bridge.read_timeline("Nope")
    assert "Nope" in str(excinfo.value)
    assert "Cut 1" in str(excinfo.value)


def test_read_timeline_without_current_raises() -> None:
    bridge, _ = make_bridge([], current=None)
    with pytest.raises(MonteurResolveError):
        bridge.read_timeline()


def test_no_current_project_raises() -> None:
    bridge = ResolveBridge(FakeResolve(None))
    with pytest.raises(MonteurResolveError):
        bridge.project_name()


def test_import_timeline_file() -> None:
    bridge, project = make_bridge([standard_timeline()])
    assert bridge.import_timeline_file("/edits/cut.edl") is True
    assert project.media_pool.imported_timelines == ["/edits/cut.edl"]


def test_import_timeline_file_failure_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.fail_timeline_import = True
    with pytest.raises(MonteurResolveError) as excinfo:
        bridge.import_timeline_file("/edits/broken.edl")
    assert "/edits/broken.edl" in str(excinfo.value)


def test_import_media_returns_count() -> None:
    bridge, project = make_bridge([standard_timeline()])
    count = bridge.import_media(["/media/a.mov", "/media/b.wav"])
    assert count == 2
    assert project.media_pool.imported_media == ["/media/a.mov", "/media/b.wav"]


# --- add_markers --------------------------------------------------------------


def test_add_markers_passes_relative_frames_and_maps_colors() -> None:
    timeline = standard_timeline()  # starts at 86400: frames must NOT shift
    bridge, _ = make_bridge([timeline])
    added = bridge.add_markers(
        [
            Marker(frame=12, name="Start", note="n1", color="Red"),
            Marker(frame=240, name="Later", note="", color=""),
            Marker(frame=300, name="Odd", note="", color="Orange"),
            Marker(frame=360, name="Lower", note="", color="cyan"),
        ]
    )
    assert added == 4
    assert timeline.added_markers == [
        (12, "Red", "Start", "n1", 1, ""),
        (240, "Blue", "Later", "", 1, ""),
        (300, "Blue", "Odd", "", 1, ""),
        (360, "Cyan", "Lower", "", 1, ""),
    ]


def test_add_markers_counts_only_successes() -> None:
    timeline = standard_timeline()
    timeline.fail_marker_frames = {50}
    bridge, _ = make_bridge([timeline])
    added = bridge.add_markers(
        [Marker(frame=10), Marker(frame=50), Marker(frame=90)]
    )
    assert added == 2
    assert [m[0] for m in timeline.added_markers] == [10, 90]


def test_add_markers_switches_to_named_timeline() -> None:
    first = standard_timeline()
    second = FakeTimeline(name="Cut 2", start_frame=0)
    bridge, project = make_bridge([first, second], current=second)
    added = bridge.add_markers([Marker(frame=7, color="Green")], timeline_name="Cut 1")
    assert added == 1
    assert first.added_markers == [(7, "Green", "", "", 1, "")]
    assert second.added_markers == []
    assert project.set_current_calls == [first]
    assert project.GetCurrentTimeline() is first


def test_add_markers_unknown_timeline_raises() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    with pytest.raises(MonteurResolveError):
        bridge.add_markers([Marker(frame=1)], timeline_name="Nope")


# --- build_timeline_from_plan ---------------------------------------------------


def make_plan() -> MontagePlan:
    return MontagePlan(
        music_path="/music/song.wav",
        duration=4.0,
        entries=[
            MontageEntry(
                clip_path="/media/a.mov", source_start=1.0, source_end=3.0,
                record_start=0.0, record_end=2.0, score=1.0,
            ),
            MontageEntry(
                clip_path="/media/b.mov", source_start=0.6, source_end=1.6,
                record_start=2.0, record_end=3.0, score=0.5,
            ),
            MontageEntry(  # a.mov again: must be imported only once
                clip_path="/media/a.mov", source_start=5.0, source_end=6.0,
                record_start=3.0, record_end=4.0, score=0.8,
            ),
        ],
    )


def test_build_timeline_from_plan_at_24fps() -> None:
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    name = build_append(bridge, make_plan(), fps=24.0)
    assert name == "Monteur Montage"
    assert pool.created_timeline_names == ["Monteur Montage"]

    # Distinct paths, one ImportMedia call, music last.
    assert pool.import_calls == [["/media/a.mov", "/media/b.mov", "/music/song.wav"]]

    assert len(pool.appended) == 4
    video, music = pool.appended[:3], pool.appended[3]
    assert [(c["startFrame"], c["endFrame"], c["mediaType"]) for c in video] == [
        (24, 71, 1),   # 1.0-3.0 s
        (14, 37, 1),   # 0.6-1.6 s (round(0.6*24)=14, round(1.6*24)-1=37)
        (120, 143, 1),  # 5.0-6.0 s
    ]
    # Entries reference the pool item of their own clip path.
    assert video[0]["mediaPoolItem"].path == "/media/a.mov"
    assert video[1]["mediaPoolItem"].path == "/media/b.mov"
    assert video[2]["mediaPoolItem"].path == "/media/a.mov"
    assert video[0]["mediaPoolItem"] is video[2]["mediaPoolItem"]
    # The music append comes last: whole montage length as audio.
    assert music["mediaPoolItem"].path == "/music/song.wav"
    assert (music["startFrame"], music["endFrame"], music["mediaType"]) == (0, 95, 2)


def test_build_timeline_from_plan_at_25fps() -> None:
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    build_append(bridge, make_plan(), fps=25.0)
    frames = [(c["startFrame"], c["endFrame"]) for c in pool.appended]
    assert frames == [(25, 74), (15, 39), (125, 149), (0, 99)]


def test_build_timeline_from_plan_uniquifies_name() -> None:
    taken = FakeTimeline(name="Monteur Montage", start_frame=0)
    taken2 = FakeTimeline(name="Monteur Montage 2", start_frame=0)
    bridge, project = make_bridge([taken, taken2])
    name = build_append(bridge, make_plan(), fps=24.0)
    assert name == "Monteur Montage 3"
    assert project.media_pool.created_timeline_names == ["Monteur Montage 3"]
    assert bridge.list_timelines() == [
        "Monteur Montage", "Monteur Montage 2", "Monteur Montage 3",
    ]


def test_build_timeline_from_plan_custom_name() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    assert build_append(bridge, make_plan(), 24.0, name="Holiday") == "Holiday"


def test_build_timeline_falls_back_to_name_matching() -> None:
    # Items without a usable "File Path" property and one extra item, so
    # neither the property nor the positional strategy applies.
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    pool.import_media_result = [
        FakePoolClip("/elsewhere/a.mov", with_file_path=False),
        FakePoolClip("/elsewhere/b.mov", with_file_path=False),
        FakePoolClip("/elsewhere/song.wav", with_file_path=False),
        FakePoolClip("/elsewhere/extra.mov", with_file_path=False),
    ]
    name = build_append(bridge, make_plan(), fps=24.0)
    assert name == "Monteur Montage"
    assert pool.appended[0]["mediaPoolItem"].path == "/elsewhere/a.mov"
    assert pool.appended[3]["mediaPoolItem"].path == "/elsewhere/song.wav"


def test_build_timeline_import_none_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.import_media_result = None
    with pytest.raises(MonteurResolveError) as excinfo:
        build_append(bridge, make_plan(), fps=24.0)
    assert "/media/a.mov" in str(excinfo.value)


def test_build_timeline_import_empty_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.import_media_result = []
    with pytest.raises(MonteurResolveError):
        build_append(bridge, make_plan(), fps=24.0)


def test_build_timeline_unmatched_path_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.import_media_result = [
        FakePoolClip("/media/a.mov"),
        FakePoolClip("/media/b.mov"),
        # music missing and counts differ -> no positional or name match
        FakePoolClip("/media/unrelated.mov"),
        FakePoolClip("/media/unrelated2.mov"),
    ]
    with pytest.raises(MonteurResolveError) as excinfo:
        build_append(bridge, make_plan(), fps=24.0)
    assert "/music/song.wav" in str(excinfo.value)


def test_build_timeline_append_failure_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.fail_append = True
    with pytest.raises(MonteurResolveError) as excinfo:
        build_append(bridge, make_plan(), fps=24.0)
    assert "append" in str(excinfo.value)


# --- build_timeline_from_plan: canvas (resolution + cinemascope crop) -----------


def test_build_timeline_canvas_cine_sets_resolution_and_crop() -> None:
    bridge, project = make_bridge([standard_timeline()])
    warnings: list[str] = []
    name = build_append(bridge, 
        make_plan(), fps=24.0, canvas="cine-uhd", warnings=warnings
    )
    assert name == "Monteur Montage"
    created = project._timelines[-1]
    # Timeline-level custom settings, in order, with STRING values.
    assert created.settings_set == [
        ("useCustomSettings", "1"),
        ("timelineFrameRate", "24"),
        ("useCustomSettings", "1"),
        ("timelineResolutionWidth", "3840"),
        ("timelineResolutionHeight", "1608"),
    ]
    # The timeline took the settings, so the project level is never touched.
    assert project.settings_set == []
    # Every video-track-1 clip got Scaling=1 ("scale full frame with crop").
    video_items = created._tracks["video"][0]
    assert len(video_items) == 3
    assert all(item.properties == [("Scaling", 1)] for item in video_items)
    # The music item is audio — no Scaling there.
    audio_items = created._tracks["audio"][0]
    assert len(audio_items) == 1
    assert audio_items[0].properties == []
    assert warnings == []


def test_build_timeline_canvas_uhd_sets_resolution_and_fill() -> None:
    bridge, project = make_bridge([standard_timeline()])
    warnings: list[str] = []
    build_append(bridge, 
        make_plan(), fps=24.0, canvas="uhd", warnings=warnings
    )
    created = project._timelines[-1]
    assert created.settings_set == [
        ("useCustomSettings", "1"),
        ("timelineFrameRate", "24"),
        ("useCustomSettings", "1"),
        ("timelineResolutionWidth", "3840"),
        ("timelineResolutionHeight", "2160"),
    ]
    # Non-cine canvases FILL the frame (mode 3): mismatched footage must
    # never sit small in the middle or behind bars.
    for item in created._tracks["video"][0]:
        assert item.properties == [("Scaling", 3)]
    assert warnings == []


def test_build_timeline_without_canvas_touches_no_settings() -> None:
    bridge, project = make_bridge([standard_timeline()])
    build_append(bridge, make_plan(), fps=24.0)
    created = project._timelines[-1]
    # Only the frame-rate pin: no canvas means no resolution/scaling calls.
    assert created.settings_set == [
        ("useCustomSettings", "1"),
        ("timelineFrameRate", "24"),
    ]
    assert project.settings_set == []
    for item in created._tracks["video"][0]:
        assert item.properties == []


def test_build_timeline_unknown_canvas_raises_before_any_resolve_work() -> None:
    bridge, project = make_bridge([standard_timeline()])
    with pytest.raises(ValueError) as excinfo:
        build_append(bridge, make_plan(), fps=24.0, canvas="imax")
    message = str(excinfo.value)
    assert "unknown canvas 'imax'" in message
    assert "cine-uhd" in message  # the valid presets are listed
    # Validated up front: nothing was imported, no timeline was created.
    assert project.media_pool.import_calls == []
    assert project.media_pool.created_timeline_names == []


def test_build_timeline_canvas_falls_back_to_project_setting() -> None:
    class RefusesSettingsPool(FakeMediaPool):
        def CreateEmptyTimeline(self, name):
            timeline = super().CreateEmptyTimeline(name)
            timeline.set_setting_result = False  # timeline level refuses
            return timeline

    bridge, project = make_bridge([standard_timeline()])
    project.media_pool = RefusesSettingsPool(project)
    warnings: list[str] = []
    build_append(bridge, 
        make_plan(), fps=24.0, canvas="hd", warnings=warnings
    )
    # The project-level fallback took the resolution — no warning.
    assert project.settings_set == [
        ("timelineResolutionWidth", "1920"),
        ("timelineResolutionHeight", "1080"),
    ]
    assert warnings == []


def test_build_timeline_canvas_refused_everywhere_warns_and_succeeds() -> None:
    class RefusesSettingsPool(FakeMediaPool):
        def CreateEmptyTimeline(self, name):
            timeline = super().CreateEmptyTimeline(name)
            timeline.set_setting_result = False
            return timeline

    bridge, project = make_bridge([standard_timeline()])
    project.media_pool = RefusesSettingsPool(project)
    project.set_setting_result = False  # project level refuses too
    warnings: list[str] = []
    name = build_append(bridge, 
        make_plan(), fps=24.0, canvas="cine-uhd", warnings=warnings
    )
    assert name == "Monteur Montage"  # non-fatal by design
    resolution_warnings = [w for w in warnings if "3840x1608" in w]
    assert len(resolution_warnings) == 1
    assert "'cine-uhd'" in resolution_warnings[0]
    assert "Timeline resolution" in resolution_warnings[0]
    # The crop step still ran despite the resolution refusal.
    created = project._timelines[-1]
    assert all(
        item.properties == [("Scaling", 1)]
        for item in created._tracks["video"][0]
    )


def test_build_timeline_canvas_crop_failures_warn_once_summarized() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.append_clip_property_result = False  # items refuse
    warnings: list[str] = []
    name = build_append(bridge, 
        make_plan(), fps=24.0, canvas="cine", warnings=warnings
    )
    assert name == "Monteur Montage"
    # ONE summarized warning for all 3 refusing clips, not one per clip.
    crop_warnings = [w for w in warnings if "crop" in w]
    assert len(crop_warnings) == 1
    assert "3 of 3 clips" in crop_warnings[0]
    assert "Scale full frame with crop" in crop_warnings[0]


# --- build_timeline_from_plan: placed SFX elements (SfxCue.file) ------------------


def make_plan_with_elements() -> MontagePlan:
    from monteur.montage import SfxCue

    plan = make_plan()
    plan.sfx = [
        SfxCue(0.0, 2.0, "ambience", "outdoor ambience", "opening"),  # marker-only
        SfxCue(1.0, 0.8, "impact", "hit", "on the drop", file="/sfx/hit.wav"),
        SfxCue(2.5, 0.6, "whoosh", "whoosh", "fast cut", file="/sfx/whoosh.wav"),
    ]
    return plan


def test_build_timeline_places_filed_cues_on_the_sfx_track() -> None:
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    warnings: list[str] = []
    build_append(bridge, 
        make_plan_with_elements(), fps=24.0, warnings=warnings
    )
    assert warnings == []
    # entries+music import first (untouched), the element files separately
    assert pool.import_calls == [
        ["/media/a.mov", "/media/b.mov", "/music/song.wav"],
        ["/sfx/hit.wav", "/sfx/whoosh.wav"],
    ]
    sfx = pool.appended[4:]  # after 3 video entries + 1 music clip
    assert [
        (c["recordFrame"], c["startFrame"], c["endFrame"], c["mediaType"],
         c["trackIndex"])
        for c in sfx
    ] == [
        (24, 0, 18, 2, 2),   # impact at 1.0s for 0.8s (19 frames at 24fps)
        (60, 0, 13, 2, 2),   # whoosh at 2.5s for 0.6s
    ]
    assert sfx[0]["mediaPoolItem"].path == "/sfx/hit.wav"
    assert sfx[1]["mediaPoolItem"].path == "/sfx/whoosh.wav"


def test_build_timeline_sfx_track_is_a3_in_mix_mode() -> None:
    bridge, project = make_bridge([standard_timeline()])
    build_append(bridge,
        make_plan_with_elements(), fps=24.0, audio="mix"
    )
    appended = project.media_pool.appended
    # "mix" now ALSO places the clips' own camera sound on audio track 2
    # (the no-music/own-audio fix: append builds carry sound everywhere) —
    # the SFX elements sit above it on track 3, same layout as the file
    # exports (song A1, camera A2, SFX A3).
    camera = [
        c for c in appended
        if c.get("mediaType") == 2 and c.get("trackIndex") == 2
    ]
    assert len(camera) == 3  # one per plan entry
    sfx = [c for c in appended if c.get("trackIndex") == 3]
    assert [c["mediaPoolItem"].path for c in sfx] == [
        "/sfx/hit.wav", "/sfx/whoosh.wav",
    ]


def test_build_timeline_without_filed_cues_imports_once() -> None:
    # The compatibility bar: no filed cues -> exactly the old single import.
    bridge, project = make_bridge([standard_timeline()])
    build_append(bridge, make_plan(), fps=24.0)
    assert len(project.media_pool.import_calls) == 1


def test_build_timeline_sfx_append_failures_warn_once() -> None:
    class SfxRejectingPool(FakeMediaPool):
        def AppendToTimeline(self, clip_infos):
            if any(info.get("trackIndex", 1) >= 2 for info in clip_infos):
                return None  # this Resolve refuses the SFX track appends
            return super().AppendToTimeline(clip_infos)

    bridge, project = make_bridge([standard_timeline()])
    project.media_pool = SfxRejectingPool(project)
    warnings: list[str] = []
    name = build_append(bridge, 
        make_plan_with_elements(), fps=24.0, warnings=warnings
    )
    assert name == "Monteur Montage"  # per-cue failures NEVER fail the build
    sfx_warnings = [w for w in warnings if "sound-element" in w]
    assert len(sfx_warnings) == 1  # one summarized warning, not one per cue
    assert "2 of 2" in sfx_warnings[0]
    assert "hit.wav" in sfx_warnings[0]


def test_build_timeline_sfx_import_miss_warns_once() -> None:
    class ElementlessPool(FakeMediaPool):
        def ImportMedia(self, paths):
            if any(p.startswith("/sfx/") for p in paths):
                self.import_calls.append(list(paths))
                return []  # Resolve knows nothing about these files
            return super().ImportMedia(paths)

    bridge, project = make_bridge([standard_timeline()])
    project.media_pool = ElementlessPool(project)
    warnings: list[str] = []
    build_append(bridge, 
        make_plan_with_elements(), fps=24.0, warnings=warnings
    )
    assert len([w for w in warnings if "sound-element" in w]) == 1
    # the montage itself still landed: 3 entries + music
    assert len(project.media_pool.appended) == 4


def test_build_timeline_sfx_skipped_when_placement_rejected() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.reject_record_placement = True
    warnings: list[str] = []
    build_append(bridge, 
        make_plan_with_elements(), fps=24.0, warnings=warnings
    )
    # gapless appends would land the elements at the wrong times — skipped,
    # said so once, and their files were never imported
    assert any("sound-element" in w and "skipped" in w for w in warnings)
    assert len(project.media_pool.import_calls) == 1
    assert all(info.get("trackIndex", 1) == 1 or "trackIndex" not in info
               for info in project.media_pool.appended)


def make_no_music_plan_with_elements() -> MontagePlan:
    plan = make_plan_with_elements()
    plan.music_path = ""
    return plan


def test_build_append_no_music_carries_clip_sound_and_sfx() -> None:
    # Field bug: "built without music — the sound track is missing
    # entirely". The append build must place the clips' own sound on audio
    # track 1 and the SFX elements on track 2, and never try to import a
    # song that does not exist.
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    warnings: list[str] = []
    name = build_append(
        bridge, make_no_music_plan_with_elements(), fps=24.0, warnings=warnings
    )
    assert name == "Monteur Montage"
    assert warnings == []
    # no "" path, no song import — only the clips, then the element files
    assert pool.import_calls == [
        ["/media/a.mov", "/media/b.mov"],
        ["/sfx/hit.wav", "/sfx/whoosh.wav"],
    ]
    video = [c for c in pool.appended if c.get("mediaType") == 1]
    assert len(video) == 3
    camera = [
        c for c in pool.appended
        if c.get("mediaType") == 2 and c.get("trackIndex") == 1
    ]
    # the clips' own sound: one per entry, same source range and position
    assert [
        (c["startFrame"], c["endFrame"], c["recordFrame"]) for c in camera
    ] == [(v["startFrame"], v["endFrame"], v["recordFrame"]) for v in video]
    sfx = [c for c in pool.appended if c.get("trackIndex") == 2]
    assert [c["mediaPoolItem"].path for c in sfx] == [
        "/sfx/hit.wav", "/sfx/whoosh.wav",
    ]


def test_build_append_original_mode_skips_the_song() -> None:
    # audio="original" means NO song bed — even when the plan has music,
    # matching montage_to_timeline's layout (camera sound on A1, SFX A2).
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    build_append(bridge, make_plan_with_elements(), fps=24.0, audio="original")
    assert pool.import_calls[0] == ["/media/a.mov", "/media/b.mov"]
    assert not any(
        c["mediaPoolItem"].path == "/music/song.wav" for c in pool.appended
    )
    assert any(
        c.get("mediaType") == 2 and c.get("trackIndex") == 1
        for c in pool.appended
    )


def test_build_append_no_music_gapless_fallback_warns() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.reject_record_placement = True
    warnings: list[str] = []
    build_append(
        bridge, make_no_music_plan_with_elements(), fps=24.0, warnings=warnings
    )
    # gapless appends cannot place the camera sound or the elements — both
    # say so honestly, nothing raises
    assert any("clips' own sound was skipped" in w for w in warnings)
    assert any("sound-element" in w and "skipped" in w for w in warnings)


def test_hybrid_build_no_music_file_carries_all_audio() -> None:
    # The hybrid (default) path for a no-music plan: the written FCPXML
    # carries the clips' own sound and the SFX lane, and no song asset.
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    warnings: list[str] = []
    name = bridge.build_timeline_from_plan(
        make_no_music_plan_with_elements(), fps=25.0, warnings=warnings
    )
    assert name == "Monteur Montage"
    assert warnings == []
    assert len(pool.timeline_import_calls) == 1
    content = pool.imported_fcpxml[0]
    assert 'hasAudio="1"' in content  # clip sound folded into the asset-clips
    assert 'audioRole="effects"' in content  # the placed SFX element
    assert "song.wav" not in content  # no phantom song
    assert pool.appended == []  # nothing placed clip-by-clip


def test_worker_build_plan_forwards_audio(monkeypatch) -> None:
    import monteur._resolve_worker as _resolve_worker

    fake = _FakeBuildBridge()
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    response = _resolve_worker.handle(
        "build_plan", build_plan_request(audio="mix")
    )
    assert response["ok"] is True
    assert fake.calls[0]["audio"] == "mix"
    # and the default stays "music" for old requests without the key
    _resolve_worker.handle("build_plan", build_plan_request())
    assert fake.calls[1]["audio"] == "music"


# --- add_titles -------------------------------------------------------------------


def title_specs() -> list[dict]:
    return [
        {"start": 10.0, "duration": 2.5, "text": "ACT ONE"},
        {"start": 20.0, "duration": 2.0, "text": "ACT TWO"},
    ]


def test_add_titles_happy_path_sets_position_and_text() -> None:
    timeline = standard_timeline()  # two video tracks, starts at frame 86400
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(title_specs(), fps=24.0, warnings=warnings)
    assert added == 2
    assert warnings == []
    assert timeline.inserted_fusion_titles == ["Text+", "Text+"]
    assert timeline.added_tracks == []  # a track above the footage existed
    first, second = timeline.created_title_items
    # Placement is in absolute timeline frames: timeline start + t * fps.
    assert (first.start, first.end) == (86400 + 240, 86400 + 240 + 60)
    assert (second.start, second.end) == (86400 + 480, 86400 + 480 + 48)
    assert first._comp._tools[0].inputs["StyledText"] == "ACT ONE"
    assert second._comp._tools[0].inputs["StyledText"] == "ACT TWO"


def test_add_titles_adds_a_track_above_single_track_footage() -> None:
    timeline = FakeTimeline(
        name="Montage", start_frame=0, video_tracks=[[FakeItem("Scene", 0, 100)]]
    )
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        [{"start": 1.0, "duration": 2.0, "text": "T"}], fps=25.0, warnings=warnings
    )
    assert added == 1
    assert warnings == []
    assert timeline.added_tracks == ["video"]
    assert timeline.GetTrackCount("video") == 2
    item = timeline.created_title_items[0]
    assert timeline.GetItemListInTrack("video", 2) == [item]
    assert (item.start, item.end) == (25, 75)


def test_add_titles_add_track_failure_is_only_a_warning() -> None:
    timeline = FakeTimeline(
        name="Montage", start_frame=0, video_tracks=[[FakeItem("Scene", 0, 100)]]
    )
    timeline.fail_add_track = True
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        [{"start": 0.0, "duration": 2.0, "text": "T"}], fps=25.0, warnings=warnings
    )
    assert added == 1
    assert any("AddTrack" in w for w in warnings)


def test_add_titles_legacy_bool_return_finds_item_via_track_scan() -> None:
    timeline = standard_timeline()
    timeline.insert_title_returns_item = False  # old Resolve returns True
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        [{"start": 5.0, "duration": 3.0, "text": "SOON"}], fps=24.0, warnings=warnings
    )
    assert added == 1
    assert warnings == []
    item = timeline.created_title_items[0]
    assert (item.start, item.end) == (86400 + 120, 86400 + 120 + 72)
    assert item._comp._tools[0].inputs["StyledText"] == "SOON"


def test_add_titles_insert_returning_none_warns_and_continues() -> None:
    timeline = standard_timeline()
    timeline.insert_title_result_override = None
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(title_specs(), fps=24.0, warnings=warnings)
    assert added == 0
    assert len(warnings) == 2
    assert "InsertFusionTitleIntoTimeline" in warnings[0]
    assert "'ACT ONE'" in warnings[0] and "'ACT TWO'" in warnings[1]


def test_add_titles_item_not_found_counts_but_warns() -> None:
    timeline = standard_timeline()
    timeline.insert_title_returns_item = False
    timeline.insert_places_item = False  # Resolve put it somewhere invisible
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        [{"start": 2.0, "duration": 2.0, "text": "X"}], fps=24.0, warnings=warnings
    )
    assert added == 1  # the title exists, even if we could not reach it
    assert len(warnings) == 1
    assert "playhead" in warnings[0]


def test_add_titles_missing_text_tool_warns_and_continues() -> None:
    timeline = standard_timeline()
    timeline.title_items_queue = [
        FakeTitleItem(comp=FakeComp([FakeTextTool(reg_id="Merge")])),
        FakeTitleItem(),
    ]
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(title_specs(), fps=24.0, warnings=warnings)
    assert added == 2
    assert len(warnings) == 1
    assert "Text+" in warnings[0] and "'ACT ONE'" in warnings[0]
    # The non-title tool was never written to; the second title got its text.
    merge_tool = timeline.created_title_items[0]._comp._tools[0]
    assert merge_tool.inputs == {}
    second = timeline.created_title_items[1]
    assert second._comp._tools[0].inputs["StyledText"] == "ACT TWO"


def test_add_titles_missing_comp_warns() -> None:
    timeline = standard_timeline()
    timeline.title_items_queue = [FakeTitleItem(comp=None)]
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        [{"start": 1.0, "duration": 2.0, "text": "T"}], fps=24.0, warnings=warnings
    )
    assert added == 1
    assert any("Fusion composition" in w for w in warnings)


def test_add_titles_without_setters_still_sets_text() -> None:
    timeline = standard_timeline()
    comp = FakeComp([FakeTextTool()])
    timeline.title_items_queue = [FakeTitleItemNoSetters(comp)]
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        [{"start": 9.6, "duration": 2.0, "text": "FINALE"}], fps=24.0,
        warnings=warnings,
    )
    assert added == 1
    assert comp._tools[0].inputs["StyledText"] == "FINALE"
    assert any("drag" in w for w in warnings)


def test_add_titles_native_exception_becomes_monteur_error() -> None:
    timeline = standard_timeline()
    timeline.raise_on_insert_title = RuntimeError("fusion exploded")
    bridge, _ = make_bridge([timeline])
    with pytest.raises(MonteurResolveError) as excinfo:
        bridge.add_titles(title_specs(), fps=24.0)
    assert "fusion exploded" in str(excinfo.value)
    assert "0 of 2" in str(excinfo.value)


def test_add_titles_invalid_specs_are_skipped_with_warnings() -> None:
    timeline = standard_timeline()
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        ["not-a-dict", {"start": "soon", "duration": 2.0, "text": "T"}],
        fps=24.0,
        warnings=warnings,
    )
    assert added == 0
    assert len(warnings) == 2
    assert timeline.inserted_fusion_titles == []


def test_add_titles_empty_list_is_a_noop() -> None:
    timeline = standard_timeline()
    bridge, _ = make_bridge([timeline])
    assert bridge.add_titles([], fps=24.0) == 0
    assert timeline.inserted_fusion_titles == []


def test_add_titles_without_current_timeline_raises() -> None:
    bridge, _ = make_bridge([], current=None)
    with pytest.raises(MonteurResolveError):
        bridge.add_titles(title_specs(), fps=24.0)


def test_add_titles_default_text_is_title() -> None:
    timeline = standard_timeline()
    bridge, _ = make_bridge([timeline])
    added = bridge.add_titles([{"start": 0.0, "duration": 2.0}], fps=24.0)
    assert added == 1
    item = timeline.created_title_items[0]
    assert item._comp._tools[0].inputs["StyledText"] == "Title"


def test_add_titles_animation_keyframes_the_text_plus() -> None:
    # a picked animation keyframes a standard Text+ input; a static title
    # (anim "none"/absent) never touches a keyframed input
    timeline = standard_timeline()
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(
        [
            {"start": 0.0, "duration": 2.0, "text": "FADE", "anim": "fade"},
            {"start": 4.0, "duration": 2.0, "text": "SLIDE", "anim": "slide"},
            {"start": 8.0, "duration": 2.0, "text": "TYPE", "anim": "type"},
            {"start": 12.0, "duration": 2.0, "text": "STATIC", "anim": "none"},
        ],
        fps=24.0,
        warnings=warnings,
    )
    assert added == 4
    assert warnings == []  # the fake Text+ accepts the frame arg -> animated
    tools = [it._comp._tools[0] for it in timeline.created_title_items]
    assert "Opacity" in tools[0].keyframes   # fade
    assert "Center" in tools[1].keyframes    # slide
    assert "WriteOnEnd" in tools[2].keyframes  # typewriter
    assert tools[3].keyframes == {}          # static — no animation scripted


def test_apply_title_anim_unsupported_host_is_false_not_raise() -> None:
    # a Text+ whose SetInput rejects the frame arg keeps a static title:
    # the helper returns False (caller then leaves a note), never raises
    def no_keyframes(name, value, frame=None):
        if frame is not None:
            raise TypeError("this host cannot animate inputs by frame")

    tool = FakeTextTool()
    tool.SetInput = no_keyframes  # type: ignore[method-assign]
    assert resolve._apply_title_anim(tool, FakeComp([tool]), "slide", 2.0, 24.0) is False
    # "none" is static everywhere — no attempt, no keyframes
    plain = FakeTextTool()
    assert resolve._apply_title_anim(plain, FakeComp([plain]), "none", 2.0, 24.0) is False
    assert plain.keyframes == {}


# --- titles_from_plan --------------------------------------------------------------


def trailer_plan() -> MontagePlan:
    return MontagePlan(
        music_path="/music/epic.wav",
        duration=8.0,
        entries=[
            MontageEntry(
                clip_path="/media/a.mov", source_start=0.0, source_end=2.6,
                record_start=0.0, record_end=2.6, score=1.0,
            ),
            MontageEntry(
                clip_path="/media/b.mov", source_start=1.0, source_end=3.0,
                record_start=3.0, record_end=5.0, score=0.9,
                label="the mountain pass",
            ),
            MontageEntry(
                clip_path="/media/c.mov", source_start=0.0, source_end=2.4,
                record_start=5.6, record_end=8.0, score=0.8,
            ),
        ],
        dips=[(2.6, 0.4), (5.0, 0.6)],
    )


def test_titles_from_plan_uses_dips_and_following_labels() -> None:
    titles = resolve.titles_from_plan(trailer_plan())
    assert titles == [
        {"start": 2.6, "duration": 2.0, "text": "the mountain pass", "anim": "none"},
        {"start": 5.0, "duration": 2.0, "text": "Title", "anim": "none"},
    ]


def test_titles_from_plan_carries_title_anims() -> None:
    plan = trailer_plan()
    plan.title_anims = ["slide"]  # only the first dip has an explicit anim
    titles = resolve.titles_from_plan(plan)
    assert titles[0]["anim"] == "slide"
    assert titles[1]["anim"] == "none"  # unset -> none


def test_titles_from_plan_explicit_texts_win() -> None:
    titles = resolve.titles_from_plan(trailer_plan(), texts=["ACT ONE", "ACT TWO"])
    assert [t["text"] for t in titles] == ["ACT ONE", "ACT TWO"]


def test_titles_from_plan_partial_texts_fall_back() -> None:
    titles = resolve.titles_from_plan(trailer_plan(), texts=["ACT ONE"])
    assert [t["text"] for t in titles] == ["ACT ONE", "Title"]
    titles = resolve.titles_from_plan(trailer_plan(), texts=["", "ACT TWO"])
    assert [t["text"] for t in titles] == ["the mountain pass", "ACT TWO"]


def test_titles_from_plan_no_dips_is_empty() -> None:
    assert resolve.titles_from_plan(make_plan()) == []


def test_titles_from_plan_minimum_duration() -> None:
    plan = trailer_plan()
    plan.dips = [(2.6, 0.4), (5.0, 3.5)]
    durations = [t["duration"] for t in resolve.titles_from_plan(plan)]
    assert durations == [2.0, 3.5]  # short dips stretched, long dips kept


def test_build_timeline_from_plan_inserts_titles_at_plan_time() -> None:
    bridge, project = make_bridge([standard_timeline()])
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    build_append(bridge, plan, fps=25.0, titles=titles)
    created = project._timelines[-1]  # the montage timeline (now current)
    assert created.inserted_fusion_titles == ["Text+", "Text+"]
    # A title track was added above the montage footage.
    assert created.added_tracks == ["video"]
    first, second = created.created_title_items
    # recordFrame placement keeps the dips as REAL black gaps, so the titles
    # land at their plan-time positions unshifted (2.6s -> 65, 5.0s -> 125).
    assert (first.start, first.end) == (65, 65 + 50)
    assert (second.start, second.end) == (125, 125 + 50)
    assert first._comp._tools[0].inputs["StyledText"] == "ONE"
    assert second._comp._tools[0].inputs["StyledText"] == "TWO"


def test_build_timeline_gapless_fallback_shifts_titles_and_warns() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.reject_record_placement = True
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    warnings: list[str] = []
    build_append(bridge, plan, fps=25.0, titles=titles, warnings=warnings)
    # Every appended clip fell back to the gapless form (no recordFrame).
    assert all("recordFrame" not in c for c in project.media_pool.appended)
    created = project._timelines[-1]
    first, second = created.created_title_items
    # Gapless: title 1 stays at its own dip's start (2.6s -> 65); title 2
    # shifts left by dip 1's 0.4s (5.0 - 0.4 = 4.6s -> frame 115).
    assert (first.start, first.end) == (65, 65 + 50)
    assert (second.start, second.end) == (115, 115 + 50)
    assert any("black title gaps" in w for w in warnings)


def test_build_timeline_uses_clip_native_frame_space() -> None:
    # The field bug: DJI clips run 50 fps with a time-of-day start timecode.
    # Source frames must be clip-fps frames anchored at the clip's Start
    # property — timeline-fps frames made Resolve clamp every cut to a
    # sliver (67 uniform 0.1s clips out of a 31s montage).
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    dji_start = 2_803_200  # 15:34:24 time-of-day at 50 fps

    def import_media(paths):
        pool.import_calls.append(list(paths))
        items = []
        for path in paths:
            item = FakePoolClip(path)
            if path.endswith(".mov"):
                props = {"FPS": "50", "Start": str(dji_start)}
                item.GetClipProperty = lambda key, _p=props, _i=item: _p.get(
                    key, _i.path if key == "File Path" else ""
                )
            items.append(item)
        return items

    pool.ImportMedia = import_media
    build_append(bridge, make_plan(), fps=25.0)
    video = pool.appended[:3]
    # 1.0-3.0s at 50 fps from the TC anchor; record positions at 25 fps.
    assert (video[0]["startFrame"], video[0]["endFrame"]) == (
        dji_start + 50, dji_start + 149,
    )
    assert (video[1]["startFrame"], video[1]["endFrame"]) == (
        dji_start + 30, dji_start + 79,
    )
    assert [c["recordFrame"] for c in video] == [0, 50, 75]
    assert all(c["trackIndex"] == 1 for c in video)
    # The music has no FPS/Start properties: timeline-fps frames from zero.
    music = pool.appended[3]
    assert (music["startFrame"], music["endFrame"]) == (0, 99)
    assert music["recordFrame"] == 0


def test_build_timeline_music_starts_at_the_plans_song_window() -> None:
    # The plan cuts to the song's BEST window (music_start), not its intro.
    # Playing the song from 0 put every cut beside the beat — the second
    # real-footage field bug.
    bridge, project = make_bridge([standard_timeline()])
    plan = make_plan()
    plan.music_start = 42.0
    build_append(bridge, plan, fps=25.0)
    music = project.media_pool.appended[-1]
    assert music["mediaType"] == 2
    # 42.0s into the song at 25 fps (audio has no FPS property -> timeline
    # fps), for the plan's 4.0s duration; placed at record 0.
    assert (music["startFrame"], music["endFrame"]) == (1050, 1149)
    assert music["recordFrame"] == 0


def test_build_timeline_tiles_without_one_frame_gaps() -> None:
    # Beat-aligned record decimals used to round source and record
    # independently, leaving 1-frame black slivers between the cuts.
    bridge, project = make_bridge([standard_timeline()])
    plan = MontagePlan(
        music_path="/music/song.wav",
        duration=3.72,
        entries=[
            MontageEntry(
                clip_path="/media/a.mov", source_start=0.0, source_end=1.24,
                record_start=0.0, record_end=1.24, score=1.0,
            ),
            MontageEntry(
                clip_path="/media/a.mov", source_start=4.0, source_end=5.24,
                record_start=1.24, record_end=2.48, score=0.9,
            ),
            MontageEntry(
                clip_path="/media/a.mov", source_start=7.0, source_end=8.24,
                record_start=2.48, record_end=3.72, score=0.8,
            ),
        ],
    )
    build_append(bridge, plan, fps=25.0)
    video = project.media_pool.appended[:3]
    # Record positions and source lengths tile exactly: each item's record
    # frame plus its frame count is the next item's record frame.
    positions = [c["recordFrame"] for c in video]
    lengths = [c["endFrame"] - c["startFrame"] + 1 for c in video]
    assert positions == [0, 31, 62]
    assert lengths == [31, 31, 31]
    assert positions[0] + lengths[0] == positions[1]
    assert positions[1] + lengths[1] == positions[2]


def test_build_timeline_repositions_at_the_timelines_real_fps() -> None:
    # The field bug behind "cuts beside the beat, Text+ past the end": a
    # 50 fps project ignores the requested 25 fps timeline rate. The build
    # must trust the READ-BACK rate and place everything in that currency.
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    original_create = pool.CreateEmptyTimeline

    def create(name):
        timeline = original_create(name)
        timeline.set_setting_result = False  # project default wins...
        timeline._fps = "50"  # ...and the timeline actually runs at 50
        return timeline

    pool.CreateEmptyTimeline = create
    warnings: list[str] = []
    build_append(bridge, make_plan(), fps=25.0, warnings=warnings)
    video = pool.appended[:3]
    # Record positions and source frames in 50 fps: 0/2/3 s -> 0/100/150.
    assert [c["recordFrame"] for c in video] == [0, 100, 150]
    assert (video[0]["startFrame"], video[0]["endFrame"]) == (50, 149)
    music = pool.appended[3]
    assert (music["startFrame"], music["endFrame"]) == (0, 199)
    assert any("50 fps" in w for w in warnings)


def test_build_timeline_record_base_uses_timeline_start_frame() -> None:
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    original_create = pool.CreateEmptyTimeline

    def create(name):
        timeline = original_create(name)
        timeline._start_frame = 90000  # 01:00:00:00 at 25 fps
        return timeline

    pool.CreateEmptyTimeline = create
    build_append(bridge, make_plan(), fps=25.0)
    assert [c["recordFrame"] for c in pool.appended] == [
        90000, 90050, 90075, 90000,
    ]


def test_build_timeline_from_plan_without_titles_adds_none() -> None:
    bridge, project = make_bridge([standard_timeline()])
    build_append(bridge, make_plan(), fps=24.0)
    assert project._timelines[-1].inserted_fusion_titles == []


# --- install_scripts ------------------------------------------------------------


SCRIPT_NAMES = ("Monteur - Analyze Timeline.py", "Monteur - Open Studio.py")


def test_install_scripts_dry_run_macos(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = resolve.install_scripts(dry_run=True)
    assert len(paths) == 2
    for path in paths:
        assert path.startswith(
            str(tmp_path / "Library" / "Application Support" / "Blackmagic Design")
        )
        assert os.path.join("Fusion", "Scripts", "Utility") in path
        assert not os.path.exists(path)
    assert sorted(os.path.basename(p) for p in paths) == sorted(SCRIPT_NAMES)


def test_install_scripts_dry_run_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = str(tmp_path / "AppData" / "Roaming")
    monkeypatch.setenv("APPDATA", appdata)
    paths = resolve.install_scripts(dry_run=True)
    assert len(paths) == 2
    for path in paths:
        assert path.startswith(
            os.path.join(appdata, "Blackmagic Design", "DaVinci Resolve", "Support")
        )
        assert os.path.join("Fusion", "Scripts", "Utility") in path
        assert not os.path.exists(path)


def test_install_scripts_dry_run_linux(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = resolve.install_scripts(dry_run=True)
    home_dir = str(
        tmp_path / ".local" / "share" / "DaVinciResolve" / "Fusion" / "Scripts" / "Utility"
    )
    assert [p for p in paths if p.startswith(home_dir)] == [
        os.path.join(home_dir, name) for name in SCRIPT_NAMES
    ]
    # /opt/resolve is only targeted when it already exists and is writable.
    for path in paths:
        if not path.startswith(home_dir):
            assert path.startswith("/opt/resolve/")


def test_install_scripts_writes_valid_python(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = [p for p in resolve.install_scripts() if str(tmp_path) in p]
    assert len(paths) == 2
    for path in paths:
        assert os.path.isfile(path)
        content = open(path, encoding="utf-8").read()
        compile(content, path, "exec")  # must be valid Python
        assert "pip install monteur" in content  # ImportError guidance
    analyze = open(
        os.path.join(os.path.dirname(paths[0]), "Monteur - Analyze Timeline.py"),
        encoding="utf-8",
    ).read()
    assert "add_markers" in analyze
    assert "Monteur: slow section" in analyze
    assert '"Red"' in analyze
    studio = open(
        os.path.join(os.path.dirname(paths[0]), "Monteur - Open Studio.py"),
        encoding="utf-8",
    ).read()
    assert "subprocess.Popen" in studio
    assert "monteur.cli" in studio


def test_install_scripts_dry_run_writes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    resolve.install_scripts(dry_run=True)
    assert list(tmp_path.rglob("*.py")) == []


# --- Timeline (de)serialization -------------------------------------------------


def sample_timeline() -> Timeline:
    return Timeline(
        name="Roundtrip",
        fps=23.976,
        clips=[
            Clip(
                name="Scene 1A", track="V1", kind=VIDEO,
                source_in=10, source_out=130, record_in=0, record_out=120,
                source_name="A001", metadata={"note": "hero shot"},
            ),
            Clip(
                name="Dialog", track="A2", kind=AUDIO,
                source_in=5, source_out=305, record_in=10, record_out=310,
            ),
        ],
        markers=[
            Marker(frame=12, name="Start", note="", color="Red"),
            Marker(frame=240, name="Note here", note="trim this", color="Blue"),
        ],
        metadata={"record_start": 86400, "misc": [1, 2, 3]},
    )


def test_timeline_dict_roundtrip_exact() -> None:
    original = sample_timeline()
    # Prove the intermediate dict is genuinely JSON-safe.
    as_dict = json.loads(json.dumps(resolve._timeline_to_dict(original)))
    rebuilt = resolve._timeline_from_dict(as_dict)

    assert rebuilt.name == original.name
    assert rebuilt.fps == pytest.approx(original.fps)
    assert rebuilt.metadata == original.metadata

    assert len(rebuilt.clips) == len(original.clips)
    for got, want in zip(rebuilt.clips, original.clips):
        assert (
            got.name, got.track, got.kind,
            got.source_in, got.source_out, got.record_in, got.record_out,
            got.source_name, got.metadata,
        ) == (
            want.name, want.track, want.kind,
            want.source_in, want.source_out, want.record_in, want.record_out,
            want.source_name, want.metadata,
        )

    assert [(m.frame, m.name, m.note, m.color) for m in rebuilt.markers] == [
        (m.frame, m.name, m.note, m.color) for m in original.markers
    ]


# --- Isolated (crash-safe) layer ------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["python", "_resolve_worker.py"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


STATUS_PAYLOAD = {
    "connected": True,
    "project": "Monteur Feature",
    "timelines": ["Cut 1", "Cut 2"],
    "current": "Cut 2",
}


def test_worker_python_defaults_to_sys_executable(monkeypatch) -> None:
    monkeypatch.delenv("MONTEUR_RESOLVE_PYTHON", raising=False)
    assert resolve._worker_python() == sys.executable


def test_worker_python_honors_env(monkeypatch) -> None:
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", "/opt/py311/bin/python3.11")
    assert resolve._worker_python() == "/opt/py311/bin/python3.11"


def test_status_isolated_success(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        # Launched by FILE PATH (not -m) so a bare interpreter without Monteur
        # installed still works: [interpreter, <path>/_resolve_worker.py, "status"]
        assert cmd[0] == resolve._worker_python()
        assert cmd[1].endswith("_resolve_worker.py")
        assert cmd[2] == "status"
        return _completed(0, json.dumps(STATUS_PAYLOAD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated()
    assert result["connected"] is True
    assert result["project"] == "Monteur Feature"
    assert result["timelines"] == ["Cut 1", "Cut 2"]
    assert result["current"] == "Cut 2"


def test_status_isolated_clean_nonzero_is_worker_error(monkeypatch) -> None:
    # A clean nonzero exit (e.g. the worker interpreter couldn't run the helper)
    # must NOT be mislabelled as a Resolve native crash.
    def fake_run(cmd, **kwargs):
        return _completed(1, "", stderr="ModuleNotFoundError: No module named 'x'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated()
    assert result["connected"] is False
    assert result["reason"] == "worker-error"
    assert "ModuleNotFoundError" in result["error"]


def test_status_isolated_native_crash_does_not_raise(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(-1073741819, "")  # 0xC0000005 access violation

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated()  # must NOT raise
    assert result["connected"] is False
    assert result["reason"] == "crash"
    assert "MONTEUR_RESOLVE_PYTHON" in result["error"]
    assert "3.10" in result["error"] and "3.12" in result["error"]


def test_status_isolated_windows_unsigned_crash_code(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(3221225477, "")  # 0xC0000005 as a large unsigned int

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated()
    assert result["connected"] is False
    assert result["reason"] == "crash"


def test_status_isolated_timeout(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1.0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated(timeout=3.0)
    assert result["connected"] is False
    assert result["reason"] == "timeout"


def test_status_isolated_no_interpreter(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file", cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated()
    assert result["connected"] is False
    assert result["reason"] == "no-interpreter"
    assert "MONTEUR_RESOLVE_PYTHON" in result["error"]


def test_status_isolated_bad_output(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(0, "this is not json {{{")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated()
    assert result["connected"] is False
    assert result["reason"] == "bad-output"


def test_status_isolated_passes_through_handled_failure(monkeypatch) -> None:
    # Worker exits 0 with a clean handled failure (Resolve not running).
    payload = {"connected": False, "error": "Resolve is not running."}

    def fake_run(cmd, **kwargs):
        return _completed(0, json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.resolve_status_isolated()
    assert result["connected"] is False
    assert result["error"] == "Resolve is not running."
    assert "reason" not in result  # a clean failure, not a crash/timeout


def test_read_timeline_isolated_success(monkeypatch) -> None:
    original = sample_timeline()
    payload = {"ok": True, "timeline": resolve._timeline_to_dict(original)}

    def fake_run(cmd, **kwargs):
        assert cmd[2] == "read_timeline"
        return _completed(0, json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    timeline = resolve.read_timeline_isolated()
    assert isinstance(timeline, Timeline)
    assert timeline.name == "Roundtrip"
    assert timeline.fps == pytest.approx(23.976)
    assert timeline.metadata["record_start"] == 86400

    v1 = timeline.track_clips("V1")[0]
    assert (v1.source_in, v1.source_out, v1.record_in, v1.record_out) == (
        10, 130, 0, 120,
    )
    assert v1.source_name == "A001"
    a2 = timeline.track_clips("A2")[0]
    assert a2.kind == AUDIO
    assert (a2.record_in, a2.record_out) == (10, 310)
    assert [(m.frame, m.color) for m in timeline.markers] == [
        (12, "Red"), (240, "Blue"),
    ]


def test_read_timeline_isolated_native_crash_raises(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(-1073741819, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(MonteurResolveError) as excinfo:
        resolve.read_timeline_isolated()
    message = str(excinfo.value)
    assert "MONTEUR_RESOLVE_PYTHON" in message
    assert "3.12" in message


def test_read_timeline_isolated_handled_failure_raises(monkeypatch) -> None:
    payload = {"ok": False, "error": "No current timeline."}

    def fake_run(cmd, **kwargs):
        return _completed(0, json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(MonteurResolveError) as excinfo:
        resolve.read_timeline_isolated()
    assert "No current timeline." in str(excinfo.value)


# --- Worker module (monteur._resolve_worker) ------------------------------------


def status_bridge() -> ResolveBridge:
    first = standard_timeline()
    second = FakeTimeline(name="Cut 2", start_frame=0)
    project = FakeProject(timelines=[first, second], current=second)
    return ResolveBridge(FakeResolve(project))


def test_worker_handle_status_success(monkeypatch) -> None:
    from monteur import _resolve_worker

    monkeypatch.setattr(resolve, "connect", lambda app=None: status_bridge())
    response = _resolve_worker.handle("status", {})
    assert response["connected"] is True
    assert response["project"] == "Monteur Feature"
    assert response["timelines"] == ["Cut 1", "Cut 2"]
    assert response["current"] == "Cut 2"


def test_worker_handle_status_error_is_clean(monkeypatch) -> None:
    from monteur import _resolve_worker

    def boom(app=None):
        raise MonteurResolveError("Resolve is not running.")

    monkeypatch.setattr(resolve, "connect", boom)
    response = _resolve_worker.handle("status", {})
    assert response == {"connected": False, "error": "Resolve is not running."}


def test_worker_handle_read_timeline_success(monkeypatch) -> None:
    from monteur import _resolve_worker

    bridge = ResolveBridge(FakeResolve(FakeProject(timelines=[standard_timeline()])))
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    response = _resolve_worker.handle("read_timeline", {"name": None})
    assert response["ok"] is True
    rebuilt = resolve._timeline_from_dict(response["timeline"])
    assert rebuilt.name == "Cut 1"
    assert rebuilt.metadata["record_start"] == 86400
    assert rebuilt.track_clips("V1")[0].name == "Scene 1A"


def test_worker_handle_read_timeline_error_is_clean(monkeypatch) -> None:
    from monteur import _resolve_worker

    def boom(app=None):
        raise MonteurResolveError("No current timeline.")

    monkeypatch.setattr(resolve, "connect", boom)
    response = _resolve_worker.handle("read_timeline", {"name": None})
    assert response == {"ok": False, "error": "No current timeline."}


def test_worker_main_exits_zero_and_writes_json(monkeypatch, capsys) -> None:
    from monteur import _resolve_worker

    monkeypatch.setattr(resolve, "connect", lambda app=None: status_bridge())
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = _resolve_worker.main(["status"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["connected"] is True
    assert data["current"] == "Cut 2"


def test_worker_main_reads_stdin_request(monkeypatch, capsys) -> None:
    from monteur import _resolve_worker

    captured: dict = {}

    def fake_handle(command, request):
        captured["command"] = command
        captured["request"] = request
        return {"ok": True, "timeline": {}}

    monkeypatch.setattr(_resolve_worker, "handle", fake_handle)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"name": "Cut 7"})))
    code = _resolve_worker.main(["read_timeline"])
    assert code == 0
    assert captured == {"command": "read_timeline", "request": {"name": "Cut 7"}}


def test_worker_main_normal_exception_is_not_a_crash(monkeypatch, capsys) -> None:
    from monteur import _resolve_worker

    def boom(command, request):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(_resolve_worker, "handle", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = _resolve_worker.main(["status"])
    assert code == 0  # exit 0: a normal error must NOT look like a native crash
    data = json.loads(capsys.readouterr().out)
    assert "error" in data
    assert "kaboom" in data["error"]


def test_diagnose_verdict_connected(monkeypatch) -> None:
    monkeypatch.setattr(
        resolve, "_run_worker",
        lambda cmd, timeout=25.0, request=None: (
            (True, {"python_version": "3.11.9", "bits": 64, "module_dir": "/x"})
            if cmd == "info"
            else (True, {"connected": True, "project": "Film", "timelines": [], "current": None})
        ),
    )
    d = resolve.diagnose()
    assert "working" in d["verdict"].lower()
    assert d["info"]["bits"] == 64


def test_diagnose_verdict_crash_too_new(monkeypatch) -> None:
    def fake_worker(cmd, timeout=25.0, request=None):
        if cmd == "info":
            return True, {"python_version": "3.14.0", "bits": 64, "module_dir": "/x"}
        return False, {"error": resolve._CRASH_MESSAGE, "reason": "crash"}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", "/py314")
    d = resolve.diagnose()
    assert "too new" in d["verdict"].lower()
    assert "3.10" in d["verdict"] and "3.12" in d["verdict"]


def test_diagnose_verdict_clean_not_connected(monkeypatch) -> None:
    def fake_worker(cmd, timeout=25.0, request=None):
        if cmd == "info":
            return True, {"python_version": "3.11.9", "bits": 64, "module_dir": "/x"}
        return True, {"connected": False, "error": "Resolve is not running."}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)
    d = resolve.diagnose()
    assert "loaded Resolve's module fine" in d["verdict"]
    assert "not running" in d["verdict"].lower()


def test_diagnose_crash_verdict_points_at_studio_settings(monkeypatch) -> None:
    # The product rule: end users fix this INSIDE the app. The verdict leads
    # with the settings-panel button; the env var is only an advanced note.
    def fake_worker(cmd, timeout=25.0, request=None):
        if cmd == "info":
            return True, {"python_version": "3.13.2", "bits": 64, "module_dir": "/x"}
        return False, {"error": resolve._CRASH_MESSAGE, "reason": "crash"}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)
    d = resolve.diagnose()
    assert "Find a compatible Python" in d["verdict"]
    assert "settings (gear)" in d["verdict"]
    assert "Advanced: MONTEUR_RESOLVE_PYTHON" in d["verdict"]


def test_diagnose_reports_interpreter_source(tmp_path, monkeypatch) -> None:
    from monteur.settings import save_settings

    def fake_worker(cmd, timeout=25.0, request=None):
        if cmd == "info":
            return True, {"python_version": "3.11.9", "bits": 64, "module_dir": "/x"}
        return True, {"connected": False, "error": "Resolve is not running."}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)

    # default: Monteur's own interpreter
    d = resolve.diagnose()
    assert d["interpreter_source"] == "default"
    assert d["worker_interpreter"] == sys.executable
    assert "Monteur's own Python" in d["verdict"]

    # a saved settings path (existing file) wins over the default...
    saved = tmp_path / "python311"
    saved.write_text("")
    save_settings({"resolve_python": str(saved)})
    d = resolve.diagnose()
    assert d["interpreter_source"] == "settings"
    assert d["worker_interpreter"] == str(saved)
    assert "saved in Monteur's settings" in d["verdict"]

    # ...and the env override wins over everything.
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", "/py311-env")
    d = resolve.diagnose()
    assert d["interpreter_source"] == "env"
    assert d["worker_interpreter"] == "/py311-env"
    assert "MONTEUR_RESOLVE_PYTHON environment variable" in d["verdict"]


# --- Crash forensics: env report, install location, staged load test ------------


@pytest.fixture(autouse=True)
def _clean_resolve_env(monkeypatch):
    """The forensics read RESOLVE_SCRIPT_API/LIB and PYTHONPATH — keep the
    developer's (or CI's) real values out of every test; tests that want a
    value set it explicitly on top of this."""
    monkeypatch.delenv("RESOLVE_SCRIPT_API", raising=False)
    monkeypatch.delenv("RESOLVE_SCRIPT_LIB", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)


def test_env_report_quoting_and_existence(tmp_path, monkeypatch) -> None:
    api_dir = tmp_path / "Scripting"
    api_dir.mkdir()
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(api_dir))
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", '"C:\\nope\\fusionscript.dll"')
    good = tmp_path / "Modules"
    good.mkdir()
    gone = tmp_path / "gone"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(good), str(gone)]))

    report = resolve._env_report()
    assert report["RESOLVE_SCRIPT_API"] == {
        "value": str(api_dir), "quoted": False, "exists": True,
    }
    lib = report["RESOLVE_SCRIPT_LIB"]
    assert lib["quoted"] is True  # the classic copy-pasted-from-the-docs quotes
    assert lib["exists"] is False  # checked AFTER stripping the quotes
    pythonpath = report["PYTHONPATH"]
    assert pythonpath["exists"] is None  # a path LIST — per-entry instead
    assert pythonpath["missing"] == [str(gone)]
    # unset variables report cleanly, never crash the report
    assert report["MONTEUR_RESOLVE_PYTHON"] == {
        "value": None, "quoted": False, "exists": None,
    }


def test_env_report_single_sided_quote_counts(monkeypatch) -> None:
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", 'C:\\x\\fusionscript.dll"')
    assert resolve._env_report()["RESOLVE_SCRIPT_LIB"]["quoted"] is True


def test_fusionscript_candidates_prefer_env_and_strip_quotes(
    tmp_path, monkeypatch
) -> None:
    lib = tmp_path / "fusionscript.so"
    lib.write_bytes(b"")
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", f'"{lib}"')
    candidates = resolve._fusionscript_candidates()
    assert candidates[0] == str(lib)  # quotes stripped, env first
    assert len(candidates) >= 2  # the per-OS default is still listed
    assert resolve._locate_fusionscript() == str(lib)


def test_locate_fusionscript_none_when_nothing_exists(monkeypatch) -> None:
    assert resolve._locate_fusionscript() is None  # container has no Resolve


def test_worker_info_reports_env_and_install(tmp_path, monkeypatch) -> None:
    from monteur import _resolve_worker

    lib = tmp_path / "fusionscript.so"
    lib.write_bytes(b"")
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", f'"{lib}"')  # quoted but real
    response = _resolve_worker.handle("info", {})
    env = response["env"]
    assert set(env) == set(resolve._DIAG_ENV_VARS)
    assert env["RESOLVE_SCRIPT_LIB"]["quoted"] is True
    assert env["RESOLVE_SCRIPT_LIB"]["exists"] is True
    install = response["resolve_install"]
    assert install["library"] == str(lib)
    assert str(lib) in install["searched"]
    # the pre-existing info fields survive
    assert response["python_version"] == "%d.%d.%d" % sys.version_info[:3]
    assert response["bits"] in (32, 64)


def test_worker_info_reports_missing_install(monkeypatch) -> None:
    from monteur import _resolve_worker

    response = _resolve_worker.handle("info", {})
    assert response["resolve_install"]["library"] is None
    assert response["resolve_install"]["searched"]  # the per-OS default path


def _load_test_lines(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_worker_load_test_no_library_is_clean_terminal(monkeypatch, capsys) -> None:
    from monteur import _resolve_worker

    monkeypatch.setattr(resolve, "_locate_fusionscript", lambda: None)
    assert _resolve_worker.main(["load_test"]) == 0
    lines = _load_test_lines(capsys)
    assert len(lines) == 1
    assert lines[0]["stage"] == "locate"
    assert lines[0]["ok"] is False
    assert "does not appear to be installed" in lines[0]["error"]


def test_worker_load_test_full_run_with_fake_module(
    tmp_path, monkeypatch, capsys
) -> None:
    import ctypes
    from types import SimpleNamespace

    from monteur import _resolve_worker

    lib = tmp_path / "fusionscript.so"
    lib.write_bytes(b"")
    monkeypatch.setattr(resolve, "_locate_fusionscript", lambda: str(lib))
    monkeypatch.setattr(ctypes, "CDLL", lambda path: object())
    fake_module = SimpleNamespace(scriptapp=lambda name: object())
    monkeypatch.setattr(resolve, "find_scripting_module", lambda: fake_module)

    assert _resolve_worker.main(["load_test"]) == 0
    lines = _load_test_lines(capsys)
    assert [line["stage"] for line in lines] == [
        "locate", "dll-load", "import", "connect",
    ]
    assert lines[0] == {"stage": "locate", "ok": True, "path": str(lib)}
    assert all(line["ok"] for line in lines)


def test_worker_load_test_dll_failure_is_clean(tmp_path, monkeypatch, capsys) -> None:
    # A text file is not a loadable library: the REAL ctypes.CDLL raises a
    # clean OSError, which must become an ok:false stage line, not a crash.
    from monteur import _resolve_worker

    lib = tmp_path / "fusionscript.so"
    lib.write_text("definitely not a shared library")
    monkeypatch.setattr(resolve, "_locate_fusionscript", lambda: str(lib))

    assert _resolve_worker.main(["load_test"]) == 0
    lines = _load_test_lines(capsys)
    assert [line["stage"] for line in lines] == ["locate", "dll-load"]
    assert lines[1]["ok"] is False
    assert lines[1]["error"]


def test_worker_load_test_import_failure_is_clean(
    tmp_path, monkeypatch, capsys
) -> None:
    import ctypes

    from monteur import _resolve_worker

    lib = tmp_path / "fusionscript.so"
    lib.write_bytes(b"")
    monkeypatch.setattr(resolve, "_locate_fusionscript", lambda: str(lib))
    monkeypatch.setattr(ctypes, "CDLL", lambda path: object())

    def boom():
        raise MonteurResolveError("Could not locate the DaVinciResolveScript module.")

    monkeypatch.setattr(resolve, "find_scripting_module", boom)
    assert _resolve_worker.main(["load_test"]) == 0
    lines = _load_test_lines(capsys)
    assert [line["stage"] for line in lines] == ["locate", "dll-load", "import"]
    assert lines[2]["ok"] is False
    assert "DaVinciResolveScript" in lines[2]["error"]


def test_worker_load_test_connect_none_is_ok_false(
    tmp_path, monkeypatch, capsys
) -> None:
    import ctypes
    from types import SimpleNamespace

    from monteur import _resolve_worker

    lib = tmp_path / "fusionscript.so"
    lib.write_bytes(b"")
    monkeypatch.setattr(resolve, "_locate_fusionscript", lambda: str(lib))
    monkeypatch.setattr(ctypes, "CDLL", lambda path: object())
    monkeypatch.setattr(
        resolve, "find_scripting_module",
        lambda: SimpleNamespace(scriptapp=lambda name: None),
    )
    assert _resolve_worker.main(["load_test"]) == 0
    lines = _load_test_lines(capsys)
    connect = lines[-1]
    assert connect["stage"] == "connect"
    assert connect["ok"] is False
    assert "running" in connect["error"]


def test_worker_load_test_connect_exception_is_clean(
    tmp_path, monkeypatch, capsys
) -> None:
    import ctypes
    from types import SimpleNamespace

    from monteur import _resolve_worker

    def bad_scriptapp(name):
        raise RuntimeError("IPC pipe broke")

    lib = tmp_path / "fusionscript.so"
    lib.write_bytes(b"")
    monkeypatch.setattr(resolve, "_locate_fusionscript", lambda: str(lib))
    monkeypatch.setattr(ctypes, "CDLL", lambda path: object())
    monkeypatch.setattr(
        resolve, "find_scripting_module",
        lambda: SimpleNamespace(scriptapp=bad_scriptapp),
    )
    assert _resolve_worker.main(["load_test"]) == 0
    connect = _load_test_lines(capsys)[-1]
    assert connect == {
        "stage": "connect", "ok": False, "error": "RuntimeError: IPC pipe broke",
    }


# --- load_test_isolated: parsing full and PARTIAL stage streams -----------------


def test_load_test_isolated_full_clean_run(monkeypatch) -> None:
    lines = [
        {"stage": "locate", "ok": True, "path": "/lib/fusionscript.so"},
        {"stage": "dll-load", "ok": True},
        {"stage": "import", "ok": True},
        {"stage": "connect", "ok": False, "error": "not running"},
    ]

    def fake_run(cmd, **kwargs):
        assert cmd[0] == resolve._worker_python()
        assert cmd[1].endswith("_resolve_worker.py")
        assert cmd[2] == "load_test"
        return _completed(0, "\n".join(json.dumps(line) for line in lines) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated()
    assert report["stages"] == lines
    assert report["crashed_at"] is None
    assert report["reason"] is None


def test_load_test_isolated_partial_output_pinpoints_crash(monkeypatch) -> None:
    # The child hard-crashed DURING dll-load: only the locate line (plus a
    # truncated fragment) made it out. crashed_at = the stage AFTER the last
    # completed one.
    stdout = (
        json.dumps({"stage": "locate", "ok": True, "path": "/x/fusionscript.dll"})
        + "\n"
        + '{"stage": "dll-'  # truncated mid-write by the crash
    )

    def fake_run(cmd, **kwargs):
        return _completed(-1073741819, stdout)  # 0xC0000005

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated()
    assert [s["stage"] for s in report["stages"]] == ["locate"]
    assert report["reason"] == "crash"
    assert report["crashed_at"] == "dll-load"


def test_load_test_isolated_crash_after_import(monkeypatch) -> None:
    stdout = "\n".join(
        json.dumps(line)
        for line in [
            {"stage": "locate", "ok": True, "path": "/x"},
            {"stage": "dll-load", "ok": True},
            {"stage": "import", "ok": True},
        ]
    )

    def fake_run(cmd, **kwargs):
        return _completed(3221225477, stdout)  # unsigned 0xC0000005

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated()
    assert report["crashed_at"] == "connect"
    assert report["reason"] == "crash"


def test_load_test_isolated_crash_with_no_output(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(-11, "")  # POSIX SIGSEGV, nothing written

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated()
    assert report["stages"] == []
    assert report["crashed_at"] == "locate"  # died before the first stage


def test_load_test_isolated_clean_nonzero_is_worker_error(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(1, "", stderr="SyntaxError: whatever")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated()
    assert report["reason"] == "worker-error"
    assert report["crashed_at"] is None


def test_load_test_isolated_no_interpreter(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file", cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated()
    assert report == {"stages": [], "crashed_at": None, "reason": "no-interpreter"}


def test_load_test_isolated_timeout_keeps_partial_stages(monkeypatch) -> None:
    partial = json.dumps({"stage": "locate", "ok": True, "path": "/x"}) + "\n"

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5.0, output=partial)

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated(timeout=5.0)
    assert report["reason"] == "timeout"
    assert [s["stage"] for s in report["stages"]] == ["locate"]


def test_load_test_isolated_skips_garbage_lines(monkeypatch) -> None:
    stdout = 'not json\n{"no_stage": 1}\n' + json.dumps(
        {"stage": "locate", "ok": True, "path": "/x"}
    )

    def fake_run(cmd, **kwargs):
        return _completed(0, stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = resolve.load_test_isolated()
    assert [s["stage"] for s in report["stages"]] == ["locate"]


# --- Verdict synthesis: naming the exact crash cause ----------------------------


def _env_entry(value=None, quoted=False, exists=None, missing=None) -> dict:
    entry = {"value": value, "quoted": quoted, "exists": exists}
    if missing is not None:
        entry["missing"] = missing
    return entry


def _diag_info(env_overrides=None, library="C:/Resolve/fusionscript.dll") -> dict:
    """A worker `info` payload: compatible 3.11.9 64-bit, clean env, library
    found at the default spot — overridable per test."""
    env = {name: _env_entry() for name in resolve._DIAG_ENV_VARS}
    env.update(env_overrides or {})
    return {
        "python_version": "3.11.9",
        "bits": 64,
        "module_dir": "/modules",
        "env": env,
        "resolve_install": {
            "library": library,
            "searched": ["C:/Resolve/fusionscript.dll"],
        },
    }


def _fake_crash_diag(monkeypatch, info, load_test) -> None:
    """diagnose() with a faked crash status probe, `info` and load test."""

    def fake_worker(cmd, timeout=25.0, request=None):
        if cmd == "info":
            return True, info
        return False, {"error": resolve._CRASH_MESSAGE, "reason": "crash"}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)
    monkeypatch.setattr(
        resolve,
        "load_test_isolated",
        lambda timeout=25.0, interpreter=None: load_test,
    )


_DLL_CRASH = {
    "stages": [{"stage": "locate", "ok": True, "path": "C:/Resolve/fusionscript.dll"}],
    "crashed_at": "dll-load",
    "reason": "crash",
}


def test_diagnose_crash_quoted_env_var_is_named(monkeypatch) -> None:
    info = _diag_info(
        {
            "RESOLVE_SCRIPT_LIB": _env_entry(
                '"C:\\Resolve\\fusionscript.dll"', quoted=True, exists=True
            )
        }
    )
    _fake_crash_diag(monkeypatch, info, _DLL_CRASH)
    d = resolve.diagnose()
    assert "RESOLVE_SCRIPT_LIB" in d["verdict"]
    assert "quotation marks" in d["verdict"]
    assert "Start menu" in d["verdict"]
    assert "3.10" not in d["verdict"]  # no version guessing — the env var IS the cause
    assert d["load_test"] == _DLL_CRASH  # the report carries the staged evidence


def test_diagnose_crash_stale_env_path_recommends_deleting(monkeypatch) -> None:
    info = _diag_info(
        {"RESOLVE_SCRIPT_API": _env_entry("C:\\old-resolve", exists=False)}
    )
    _fake_crash_diag(monkeypatch, info, _DLL_CRASH)
    verdict = resolve.diagnose()["verdict"]
    assert "RESOLVE_SCRIPT_API" in verdict
    assert "doesn't exist" in verdict
    assert "Delete the stale variable" in verdict


def test_diagnose_crash_stale_resolve_pythonpath_entry(monkeypatch) -> None:
    gone = "C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Modules"
    info = _diag_info(
        {"PYTHONPATH": _env_entry("C:\\stuff;" + gone, missing=[gone])}
    )
    _fake_crash_diag(monkeypatch, info, _DLL_CRASH)
    verdict = resolve.diagnose()["verdict"]
    assert "PYTHONPATH" in verdict
    assert gone in verdict


def test_diagnose_crash_unrelated_pythonpath_entry_is_ignored(monkeypatch) -> None:
    # A missing entry that has nothing to do with Resolve must NOT hijack the
    # verdict — the staged pinpoint (dll-load) speaks instead.
    info = _diag_info(
        {"PYTHONPATH": _env_entry("C:\\my-tools", missing=["C:\\my-tools"])}
    )
    _fake_crash_diag(monkeypatch, info, _DLL_CRASH)
    verdict = resolve.diagnose()["verdict"]
    assert "PYTHONPATH" not in verdict
    assert "update DaVinci Resolve" in verdict


def test_diagnose_crash_at_dll_load_default_install(monkeypatch) -> None:
    # THE field case: correct 64-bit 3.11.9, clean env, library at the
    # default spot — and it still crashes. Verdict: the Resolve release
    # doesn't support this Python; update Resolve, THEN try 3.10.
    _fake_crash_diag(monkeypatch, _diag_info(), _DLL_CRASH)
    verdict = resolve.diagnose()["verdict"]
    assert "3.11.9" in verdict
    assert "update DaVinci Resolve" in verdict
    assert "Studio updates are free" in verdict
    assert "3.10" in verdict
    assert "C:/Resolve/fusionscript.dll" in verdict


def test_diagnose_crash_at_import_mentions_version_mix(monkeypatch) -> None:
    load = {
        "stages": [
            {"stage": "locate", "ok": True, "path": "C:/Resolve/fusionscript.dll"},
            {"stage": "dll-load", "ok": True},
        ],
        "crashed_at": "import",
        "reason": "crash",
    }
    _fake_crash_diag(monkeypatch, _diag_info(), load)
    verdict = resolve.diagnose()["verdict"]
    assert "DaVinciResolveScript" in verdict
    assert "different Resolve version" in verdict
    assert "RESOLVE_SCRIPT_API" in verdict


def test_diagnose_crash_at_connect(monkeypatch) -> None:
    load = {
        "stages": [
            {"stage": "locate", "ok": True, "path": "C:/Resolve/fusionscript.dll"},
            {"stage": "dll-load", "ok": True},
            {"stage": "import", "ok": True},
        ],
        "crashed_at": "connect",
        "reason": "crash",
    }
    _fake_crash_diag(monkeypatch, _diag_info(), load)
    verdict = resolve.diagnose()["verdict"]
    assert "crashed while connecting" in verdict
    assert "Restart DaVinci Resolve" in verdict


def test_diagnose_crash_library_not_found(monkeypatch) -> None:
    load = {
        "stages": [{"stage": "locate", "ok": False, "error": "No fusionscript"}],
        "crashed_at": None,
        "reason": None,
    }
    _fake_crash_diag(monkeypatch, _diag_info(library=None), load)
    verdict = resolve.diagnose()["verdict"]
    assert "could not find Resolve's scripting library" in verdict
    assert "default folder" in verdict


def test_diagnose_too_new_python_still_wins_over_env_issues(monkeypatch) -> None:
    # A 3.14 interpreter is the certain crash cause — the env checks and the
    # staged pinpoint must not water that verdict down.
    info = _diag_info(
        {"RESOLVE_SCRIPT_LIB": _env_entry('"C:\\x"', quoted=True, exists=False)}
    )
    info["python_version"] = "3.14.0"
    _fake_crash_diag(monkeypatch, info, _DLL_CRASH)
    verdict = resolve.diagnose()["verdict"]
    assert "too new" in verdict.lower()
    assert "quotation marks" not in verdict


def test_diagnose_runs_load_test_only_on_crash(monkeypatch) -> None:
    calls: list[float] = []

    def fake_worker(cmd, timeout=25.0, request=None):
        if cmd == "info":
            return True, _diag_info()
        return True, {"connected": False, "error": "Resolve is not running."}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)
    monkeypatch.setattr(
        resolve,
        "load_test_isolated",
        lambda timeout=25.0, interpreter=None: calls.append(timeout),
    )
    d = resolve.diagnose()
    assert calls == []  # clean not-connected: no crash, no load test
    assert d["load_test"] is None


def test_diagnose_clean_not_connected_mentions_studio(monkeypatch) -> None:
    def fake_worker(cmd, timeout=25.0, request=None):
        if cmd == "info":
            return True, _diag_info()
        return True, {"connected": False, "error": "Resolve is not running."}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)
    verdict = resolve.diagnose()["verdict"]
    assert "loaded Resolve's module fine" in verdict
    assert "DaVinci Resolve Studio" in verdict  # external scripting needs Studio


def test_diagnose_no_interpreter_quoted_env_value(monkeypatch) -> None:
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", '"C:\\Python311\\python.exe"')

    def fake_worker(cmd, timeout=25.0, request=None):
        return False, {"error": "could not launch", "reason": "no-interpreter"}

    monkeypatch.setattr(resolve, "_run_worker", fake_worker)
    verdict = resolve.diagnose()["verdict"]
    assert "quotation marks" in verdict
    assert "MONTEUR_RESOLVE_PYTHON" in verdict


# --- Worker interpreter choice (env > settings > default) ------------------------


def test_worker_python_prefers_env_over_settings(tmp_path, monkeypatch) -> None:
    from monteur.settings import save_settings

    saved = tmp_path / "saved-python"
    saved.write_text("")
    save_settings({"resolve_python": str(saved)})
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", "/opt/env/python3.11")
    assert resolve._worker_python() == "/opt/env/python3.11"
    assert resolve._worker_python_source() == ("/opt/env/python3.11", "env")


def test_worker_python_uses_saved_settings_path(tmp_path) -> None:
    from monteur.settings import save_settings

    saved = tmp_path / "python311"
    saved.write_text("")
    save_settings({"resolve_python": str(saved)})
    assert resolve._worker_python() == str(saved)
    assert resolve._worker_python_source() == (str(saved), "settings")


def test_worker_python_ignores_stale_settings_path(tmp_path) -> None:
    from monteur.settings import save_settings

    save_settings({"resolve_python": str(tmp_path / "uninstalled" / "python.exe")})
    # The file is gone (Python uninstalled) — fall back silently, no error.
    assert resolve._worker_python() == sys.executable
    assert resolve._worker_python_source() == (sys.executable, "default")


def test_run_worker_uses_worker_python_by_default(tmp_path, monkeypatch) -> None:
    # Every isolated call (status/info/read_timeline/build_plan) goes through
    # _run_worker without an explicit interpreter — the settings choice must
    # reach them all.
    from monteur.settings import save_settings

    saved = tmp_path / "python311"
    saved.write_text("")
    save_settings({"resolve_python": str(saved)})
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[0])
        return _completed(0, json.dumps({"connected": False, "error": "closed"}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolve.resolve_status_isolated()
    assert seen == [str(saved)]


# --- Interpreter discovery (_candidate_pythons) ----------------------------------


def test_candidate_pythons_order(tmp_path, monkeypatch) -> None:
    from monteur.settings import save_settings

    env_py = tmp_path / "env-python"
    env_py.write_text("")
    saved_py = tmp_path / "saved-python"
    saved_py.write_text("")
    which_py = tmp_path / "python3.11"
    which_py.write_text("")
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", str(env_py))
    save_settings({"resolve_python": str(saved_py)})
    monkeypatch.setattr(
        resolve.shutil,
        "which",
        lambda name: str(which_py) if name == "python3.11" else None,
    )
    candidates = resolve._candidate_pythons()
    # env override first, then the saved setting, then PATH, then sys.executable
    assert candidates[0] == str(env_py)
    assert candidates[1] == str(saved_py)
    assert candidates[2] == str(which_py)
    assert candidates[-1] == sys.executable


def test_candidate_pythons_dedupes_symlinks_and_missing(tmp_path, monkeypatch) -> None:
    from monteur.settings import save_settings

    real = tmp_path / "python3.11"
    real.write_text("")
    alias = tmp_path / "alias-python"
    alias.symlink_to(real)
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", str(alias))
    save_settings({"resolve_python": str(real)})  # same interpreter, other name
    monkeypatch.setattr(resolve.shutil, "which", lambda name: None)
    candidates = resolve._candidate_pythons()
    assert candidates[0] == str(alias)
    assert str(real) not in candidates  # resolves to the same realpath
    # a nonexistent env path never survives the existence check
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", str(tmp_path / "nope"))
    save_settings({"resolve_python": ""})
    assert resolve._candidate_pythons() == [sys.executable]


def test_candidate_pythons_is_capped(tmp_path, monkeypatch) -> None:
    files = []
    for index in range(30):
        path = tmp_path / f"python-{index}"
        path.write_text("")
        files.append(str(path))
    names = iter(files)
    monkeypatch.setattr(
        resolve.shutil, "which", lambda name: next(names, None)
    )
    monkeypatch.setattr(resolve, "_WHICH_NAMES", tuple(f"p{i}" for i in range(30)))
    candidates = resolve._candidate_pythons()
    assert len(candidates) <= resolve._MAX_CANDIDATES


def test_windows_helpers_are_safe_off_windows() -> None:
    # The registry scan needs winreg (Windows-only) and must degrade to [].
    assert resolve._windows_registry_pythons() == []
    # The well-known path list is pure string building — preference order
    # 3.11 (safest), then 3.12 (now supported), then 3.10, then older.
    wellknown = resolve._windows_wellknown_pythons()
    assert wellknown[0].endswith(os.path.join("Python311", "python.exe"))
    joined = os.pathsep.join(wellknown)
    assert joined.index("Python311") < joined.index("Python312")
    assert joined.index("Python312") < joined.index("Python310")
    assert all(p.endswith("python.exe") for p in wellknown)
    assert any("Python36" in p for p in wellknown)


def test_which_names_prefer_311_then_312_then_310() -> None:
    # PATH discovery mirrors the well-known order: 3.11 > 3.12 > 3.10.
    assert resolve._WHICH_NAMES[:3] == ("python3.11", "python3.12", "python3.10")


def test_parse_py_launcher_output() -> None:
    text = (
        "Installed Pythons found by py Launcher for Windows\n"
        r" -V:3.13 *        C:\Users\me\AppData\Local\Programs\Python\Python313\python.exe" + "\n"
        r" -3.11-64         C:\Python311\python.exe" + "\n"
        " -3.10-32         no path shown\n"
    )
    assert resolve._parse_py_launcher_output(text) == [
        r"C:\Users\me\AppData\Local\Programs\Python\Python313\python.exe",
        r"C:\Python311\python.exe",
    ]
    assert resolve._parse_py_launcher_output("") == []
    assert resolve._parse_py_launcher_output("garbage\nlines") == []


# --- Probing one interpreter (probe_resolve_python) ------------------------------


def _fake_probe_worker(monkeypatch, info_result, status_result=None):
    """Install a _run_worker fake; returns the list of commands it saw."""
    calls: list[str] = []

    def fake(cmd, timeout, request=None, interpreter=None):
        calls.append(cmd)
        if cmd == "info":
            return info_result
        assert status_result is not None, "status must not be attempted"
        return status_result

    monkeypatch.setattr(resolve, "_run_worker", fake)
    return calls


def test_probe_too_new_short_circuits_without_native_load(monkeypatch) -> None:
    calls = _fake_probe_worker(
        monkeypatch, (True, {"python_version": "3.13.1", "bits": 64})
    )
    result = resolve.probe_resolve_python("/py313")
    assert result == {
        "ok": False, "reason": "incompatible", "version": "3.13.1", "bits": 64,
    }
    assert calls == ["info"]  # the crashing status probe was never attempted


def test_probe_32bit_short_circuits_without_native_load(monkeypatch) -> None:
    calls = _fake_probe_worker(
        monkeypatch, (True, {"python_version": "3.10.9", "bits": 32})
    )
    result = resolve.probe_resolve_python("/py310-32")
    assert result == {
        "ok": False, "reason": "incompatible", "version": "3.10.9", "bits": 32,
    }
    assert calls == ["info"]


def test_probe_unlaunchable_interpreter(monkeypatch) -> None:
    _fake_probe_worker(
        monkeypatch,
        (False, {"reason": "no-interpreter", "error": "could not launch"}),
    )
    result = resolve.probe_resolve_python("/nope")
    assert result["ok"] is False
    assert result["reason"] == "no-interpreter"


def test_probe_crash_is_reported(monkeypatch) -> None:
    calls = _fake_probe_worker(
        monkeypatch,
        (True, {"python_version": "3.11.9", "bits": 64}),
        (False, {"error": resolve._CRASH_MESSAGE, "reason": "crash"}),
    )
    result = resolve.probe_resolve_python("/py311-broken")
    assert result["ok"] is False
    assert result["reason"] == "crash"
    assert result["version"] == "3.11.9"
    assert calls == ["info", "status"]


def test_probe_loaded_but_resolve_closed_is_ok(monkeypatch) -> None:
    # A compatible interpreter with Resolve closed is a FIND — the whole
    # point is remembering it for when Resolve is running.
    _fake_probe_worker(
        monkeypatch,
        (True, {"python_version": "3.11.9", "bits": 64}),
        (True, {"connected": False, "error": "Resolve is not running."}),
    )
    result = resolve.probe_resolve_python("/py311")
    assert result["ok"] is True
    assert result["connected"] is False
    assert result["version"] == "3.11.9"


def test_probe_connected(monkeypatch) -> None:
    _fake_probe_worker(
        monkeypatch,
        (True, {"python_version": "3.10.11", "bits": 64}),
        (True, {"connected": True, "project": "Film", "timelines": []}),
    )
    result = resolve.probe_resolve_python("/py310")
    assert result["ok"] is True
    assert result["connected"] is True
    assert result["project"] == "Film"


def test_probe_passes_interpreter_through(monkeypatch) -> None:
    seen: list[str | None] = []

    def fake(cmd, timeout, request=None, interpreter=None):
        seen.append(interpreter)
        return True, {"python_version": "3.13.0", "bits": 64}

    monkeypatch.setattr(resolve, "_run_worker", fake)
    resolve.probe_resolve_python("/some/python")
    assert seen == ["/some/python"]


# --- Walking the candidates (find_resolve_python) --------------------------------


def test_find_stops_at_first_ok_and_saves_nothing(monkeypatch) -> None:
    from monteur.settings import resolve_python as saved_python

    monkeypatch.setattr(
        resolve, "_candidate_pythons", lambda: ["/py313", "/py311", "/py310"]
    )

    def fake_probe(path, timeout=10.0):
        if path == "/py313":
            return {"ok": False, "reason": "incompatible",
                    "version": "3.13.0", "bits": 64}
        if path == "/py311":
            return {"ok": True, "connected": False,
                    "version": "3.11.9", "bits": 64}
        raise AssertionError("probing must stop at the first ok result")

    monkeypatch.setattr(resolve, "probe_resolve_python", fake_probe)
    report = resolve.find_resolve_python()
    assert report["found"] == "/py311"
    assert report["connected"] is False
    assert [p["path"] for p in report["probed"]] == ["/py313", "/py311"]
    assert report["probed"][0]["reason"] == "incompatible"
    assert report["probed"][1]["ok"] is True
    # find_resolve_python only LOOKS — persisting is the detect endpoint's job.
    assert saved_python() == ""


def test_find_connected_result(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "_candidate_pythons", lambda: ["/py311"])
    monkeypatch.setattr(
        resolve,
        "probe_resolve_python",
        lambda path, timeout=10.0: {
            "ok": True, "connected": True, "version": "3.11.9",
            "bits": 64, "project": "Film",
        },
    )
    report = resolve.find_resolve_python()
    assert report["found"] == "/py311"
    assert report["connected"] is True


def test_find_none_found_reports_every_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        resolve, "_candidate_pythons", lambda: ["/py313", "/py312"]
    )
    monkeypatch.setattr(
        resolve,
        "probe_resolve_python",
        lambda path, timeout=10.0: {
            "ok": False, "reason": "incompatible", "version": "3.13.0", "bits": 64,
        },
    )
    report = resolve.find_resolve_python()
    assert report["found"] is None
    assert report["connected"] is False
    assert [p["path"] for p in report["probed"]] == ["/py313", "/py312"]


def test_find_with_no_candidates(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "_candidate_pythons", lambda: [])
    report = resolve.find_resolve_python()
    assert report == {"found": None, "connected": False, "probed": []}


# --- build_plan: isolated (crash-safe) timeline building -------------------------


class _FakeBuildBridge:
    """A connect() stand-in that records build_timeline_from_plan calls.

    Emits the preset ``warn`` messages into the caller's ``warnings`` list —
    exactly what the real bridge does for non-fatal title placement problems.
    """

    def __init__(
        self, result: str = "Monteur Montage", warn: list[str] | None = None
    ) -> None:
        self.result = result
        self.warn = list(warn or [])
        self.calls: list[dict] = []

    def build_timeline_from_plan(
        self, plan, fps, name="Monteur Montage", titles=None, canvas=None,
        warnings=None, audio="music", mode="hybrid",
    ):
        self.calls.append(
            {
                "plan": plan, "fps": fps, "name": name, "titles": titles,
                "canvas": canvas, "audio": audio, "mode": mode,
            }
        )
        if warnings is not None:
            warnings.extend(self.warn)
        return self.result


def build_plan_request(plan=None, **overrides) -> dict:
    from monteur.montage import plan_to_dict

    request = {
        "plan": plan_to_dict(plan if plan is not None else make_plan()),
        "fps": 24.0,
        "name": "Monteur Montage",
        "titles": None,
        "canvas": None,
        # The historical worker tests assert the append flow's exact
        # behavior; the hybrid default has its own wire tests.
        "mode": "append",
    }
    request.update(overrides)
    return request


def test_worker_handle_build_plan_round_trip_real_bridge(monkeypatch) -> None:
    # Full chain: JSON payload -> plan_from_dict -> ResolveBridge (fakes).
    from monteur import _resolve_worker

    bridge, project = make_bridge([standard_timeline()])
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    response = _resolve_worker.handle(
        "build_plan", build_plan_request(name="Holiday")
    )
    assert response == {"ok": True, "timeline": "Holiday", "warnings": []}
    pool = project.media_pool
    assert pool.created_timeline_names == ["Holiday"]
    # The deserialized plan built the same appends as the in-process path.
    assert pool.import_calls == [["/media/a.mov", "/media/b.mov", "/music/song.wav"]]
    assert [(c["startFrame"], c["endFrame"]) for c in pool.appended] == [
        (24, 71), (14, 37), (120, 143), (0, 95),
    ]


def test_worker_handle_build_plan_titles_and_warnings_real_bridge(monkeypatch) -> None:
    # Titles reach the created timeline; add_titles' warnings travel back in
    # the payload. The created timeline refuses inserts, so every title warns.
    from monteur import _resolve_worker

    bridge, project = make_bridge([standard_timeline()])

    class RefusingPool(FakeMediaPool):
        def CreateEmptyTimeline(self, name):
            timeline = super().CreateEmptyTimeline(name)
            timeline.insert_title_result_override = None  # soft insert failure
            return timeline

    project.media_pool = RefusingPool(project)
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    response = _resolve_worker.handle(
        "build_plan", build_plan_request(plan=plan, fps=25.0, titles=titles)
    )
    assert response["ok"] is True
    assert len(response["warnings"]) == 2
    assert "'ONE'" in response["warnings"][0]
    assert "'TWO'" in response["warnings"][1]


def test_worker_handle_build_plan_inserts_titles(monkeypatch) -> None:
    from monteur import _resolve_worker

    bridge, project = make_bridge([standard_timeline()])
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    response = _resolve_worker.handle(
        "build_plan", build_plan_request(plan=plan, fps=25.0, titles=titles)
    )
    assert response["ok"] is True
    assert response["warnings"] == []
    created = project._timelines[-1]
    assert created.inserted_fusion_titles == ["Text+", "Text+"]
    texts = [i._comp._tools[0].inputs["StyledText"] for i in created.created_title_items]
    assert texts == ["ONE", "TWO"]


def test_worker_handle_build_plan_records_titles_arg(monkeypatch) -> None:
    from monteur import _resolve_worker

    fake = _FakeBuildBridge(result="Cut Together", warn=["title 1: check it"])
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    titles = [{"start": 2.6, "duration": 2.0, "text": "ACT ONE"}]
    response = _resolve_worker.handle(
        "build_plan",
        build_plan_request(fps=30.0, name="Trailer", titles=titles),
    )
    assert response == {
        "ok": True, "timeline": "Cut Together", "warnings": ["title 1: check it"],
    }
    call = fake.calls[0]
    assert call["fps"] == 30.0
    assert call["name"] == "Trailer"
    assert call["titles"] == titles
    assert [e.clip_path for e in call["plan"].entries] == [
        "/media/a.mov", "/media/b.mov", "/media/a.mov",
    ]


def test_worker_handle_build_plan_forwards_canvas(monkeypatch) -> None:
    from monteur import _resolve_worker

    fake = _FakeBuildBridge()
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    response = _resolve_worker.handle(
        "build_plan", build_plan_request(canvas="cine-uhd")
    )
    assert response["ok"] is True
    assert fake.calls[0]["canvas"] == "cine-uhd"


def test_worker_handle_build_plan_canvas_defaults_to_none(monkeypatch) -> None:
    # Old callers' payloads (no "canvas" key) keep working: canvas=None.
    from monteur import _resolve_worker

    fake = _FakeBuildBridge()
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    request = build_plan_request()
    del request["canvas"]
    response = _resolve_worker.handle("build_plan", request)
    assert response["ok"] is True
    assert fake.calls[0]["canvas"] is None


def test_worker_handle_build_plan_unknown_canvas_is_clean(monkeypatch) -> None:
    # The real bridge raises ValueError for an unknown preset; the worker
    # answers with a clean ok:false payload, never a crash-looking exit.
    from monteur import _resolve_worker

    bridge, _ = make_bridge([standard_timeline()])
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    response = _resolve_worker.handle(
        "build_plan", build_plan_request(canvas="imax")
    )
    assert response["ok"] is False
    assert "unknown canvas 'imax'" in response["error"]
    assert "cine-uhd" in response["error"]


def test_worker_main_build_plan_canvas_wire_round_trip(monkeypatch, capsys) -> None:
    # The real wire with a canvas: stdin JSON -> real fake bridge -> the
    # created timeline got the resolution AND the cine crop scaling.
    from monteur import _resolve_worker

    bridge, project = make_bridge([standard_timeline()])
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps(build_plan_request(canvas="cine-uhd"))),
    )
    code = _resolve_worker.main(["build_plan"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"ok": True, "timeline": "Monteur Montage", "warnings": []}
    created = project._timelines[-1]
    assert created.settings_set == [
        ("useCustomSettings", "1"),
        ("timelineFrameRate", "24"),
        ("useCustomSettings", "1"),
        ("timelineResolutionWidth", "3840"),
        ("timelineResolutionHeight", "1608"),
    ]
    assert all(
        item.properties == [("Scaling", 1)]
        for item in created._tracks["video"][0]
    )


def test_worker_handle_build_plan_bad_plan_is_clean(monkeypatch) -> None:
    from monteur import _resolve_worker

    fake = _FakeBuildBridge()
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    response = _resolve_worker.handle(
        "build_plan", {"plan": {"not": "a plan"}, "fps": 24.0}
    )
    assert response["ok"] is False
    assert "monteur_plan" in response["error"]  # plan_from_dict's message
    assert fake.calls == []  # never reached Resolve


def test_worker_handle_build_plan_missing_fps_is_clean(monkeypatch) -> None:
    from monteur import _resolve_worker

    fake = _FakeBuildBridge()
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    request = build_plan_request()
    del request["fps"]
    response = _resolve_worker.handle("build_plan", request)
    assert response["ok"] is False
    assert "fps" in response["error"]
    assert fake.calls == []


def test_worker_handle_build_plan_resolve_error_is_clean(monkeypatch) -> None:
    from monteur import _resolve_worker

    def boom(app=None):
        raise MonteurResolveError("Resolve is not running.")

    monkeypatch.setattr(resolve, "connect", boom)
    response = _resolve_worker.handle("build_plan", build_plan_request())
    assert response == {"ok": False, "error": "Resolve is not running."}


def test_worker_main_build_plan_wire_round_trip(monkeypatch, capsys) -> None:
    # The real wire: JSON on stdin, one JSON object on stdout, exit 0.
    from monteur import _resolve_worker

    bridge, project = make_bridge([standard_timeline()])
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(build_plan_request()))
    )
    code = _resolve_worker.main(["build_plan"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"ok": True, "timeline": "Monteur Montage", "warnings": []}
    assert project.media_pool.created_timeline_names == ["Monteur Montage"]


# --- build_plan_isolated ----------------------------------------------------------


def test_build_plan_isolated_success(monkeypatch) -> None:
    from monteur.montage import plan_from_dict

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return _completed(
            0,
            json.dumps(
                {"ok": True, "timeline": "Monteur Montage 2", "warnings": ["w1"]}
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.build_plan_isolated(make_plan(), fps=24.0)
    assert result == {"ok": True, "timeline": "Monteur Montage 2", "warnings": ["w1"]}
    # Launched by FILE PATH like every other isolated command.
    assert captured["cmd"][0] == resolve._worker_python()
    assert captured["cmd"][1].endswith("_resolve_worker.py")
    assert captured["cmd"][2] == "build_plan"
    sent = json.loads(captured["input"])
    assert sent["fps"] == 24.0
    assert sent["name"] == "Monteur Montage"
    assert sent["titles"] is None
    assert sent["canvas"] is None
    # The serialized plan is a faithful plan_to_dict payload.
    rebuilt = plan_from_dict(sent["plan"])
    assert [e.clip_path for e in rebuilt.entries] == [
        "/media/a.mov", "/media/b.mov", "/media/a.mov",
    ]
    assert rebuilt.duration == 4.0


def test_build_plan_isolated_honors_worker_python(monkeypatch) -> None:
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", "/opt/py311/bin/python3.11")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(0, json.dumps({"ok": True, "timeline": "T", "warnings": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolve.build_plan_isolated(make_plan(), fps=25.0)
    assert captured["cmd"][0] == "/opt/py311/bin/python3.11"


def test_build_plan_isolated_sends_titles(monkeypatch) -> None:
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs.get("input")
        return _completed(0, json.dumps({"ok": True, "timeline": "T", "warnings": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolve.build_plan_isolated(plan, fps=25.0, name="Trailer", titles=titles)
    sent = json.loads(captured["input"])
    assert sent["name"] == "Trailer"
    assert sent["titles"] == [
        {"start": 2.6, "duration": 2.0, "text": "the mountain pass", "anim": "none"},
        {"start": 5.0, "duration": 2.0, "text": "Title", "anim": "none"},
    ]


def test_build_plan_isolated_sends_canvas(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs.get("input")
        return _completed(0, json.dumps({"ok": True, "timeline": "T", "warnings": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolve.build_plan_isolated(make_plan(), fps=25.0, canvas="cine-uhd")
    sent = json.loads(captured["input"])
    assert sent["canvas"] == "cine-uhd"


def test_build_plan_isolated_worker_clean_error(monkeypatch) -> None:
    payload = {"ok": False, "error": "No project is open in Resolve."}

    def fake_run(cmd, **kwargs):
        return _completed(0, json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.build_plan_isolated(make_plan(), fps=24.0)
    assert result["ok"] is False
    assert result["error"] == "No project is open in Resolve."
    assert "reason" not in result  # a handled Resolve error, not a crash


def test_build_plan_isolated_native_crash_never_raises(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(-1073741819, "")  # 0xC0000005 access violation

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.build_plan_isolated(make_plan(), fps=24.0)  # must NOT raise
    assert result["ok"] is False
    assert result["reason"] == "native-crash"
    assert "MONTEUR_RESOLVE_PYTHON" in result["error"]
    assert "3.10" in result["error"] and "3.12" in result["error"]


def test_build_plan_isolated_windows_unsigned_crash_code(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(3221225477, "")  # 0xC0000005 as unsigned

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.build_plan_isolated(make_plan(), fps=24.0)
    assert result["ok"] is False
    assert result["reason"] == "native-crash"


def test_build_plan_isolated_timeout_passes_through(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1.0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.build_plan_isolated(make_plan(), fps=24.0, timeout=5.0)
    assert result["ok"] is False
    assert result["reason"] == "timeout"


def test_build_plan_isolated_bad_output(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return _completed(0, "not json at all")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = resolve.build_plan_isolated(make_plan(), fps=24.0)
    assert result["ok"] is False
    assert result["reason"] == "bad-output"


# --- CLI: create --into-resolve uses the isolated path ----------------------------


def _run_cmd_create_into_resolve(monkeypatch, plan, build_result, extra_args=()):
    """Run ``monteur create <folder> song.mp3 --into-resolve`` with the whole
    pipeline faked out; returns the recorded build_plan_isolated calls."""
    import types

    import monteur.montage
    import monteur.music
    import monteur.sift
    from monteur.cli import build_parser, cmd_create

    monkeypatch.setattr(monteur.sift, "list_media", lambda folder: ["a.mov"])
    monkeypatch.setattr(
        monteur.sift, "sift_directory", lambda folder, progress=None: []
    )
    monkeypatch.setattr(
        monteur.music,
        "analyze_music",
        lambda path: types.SimpleNamespace(tempo=100.0, beats=[], duration=8.0),
    )
    monkeypatch.setattr(
        monteur.montage, "plan_montage", lambda reports, music, **kwargs: plan
    )
    calls: list[dict] = []

    def fake_build(
        plan, fps, name="Monteur Montage", titles=None, canvas=None,
        audio="music", timeout=180.0,
    ):
        calls.append(
            {
                "plan": plan, "fps": fps, "name": name, "titles": titles,
                "canvas": canvas, "audio": audio,
            }
        )
        return build_result

    monkeypatch.setattr(resolve, "build_plan_isolated", fake_build)
    args = build_parser().parse_args(
        ["create", "clips", "song.mp3", "--into-resolve", *extra_args]
    )
    cmd_create(args)
    return calls


def test_cli_into_resolve_uses_isolated_build_with_titles(monkeypatch, capsys) -> None:
    plan = trailer_plan()
    calls = _run_cmd_create_into_resolve(
        monkeypatch,
        plan,
        {
            "ok": True,
            "timeline": "Monteur Montage",
            "warnings": ["title 2 ('Title'): drag it onto the black gap at 4.6s."],
        },
    )
    assert len(calls) == 1
    assert calls[0]["plan"] is plan
    assert calls[0]["fps"] == 25.0  # the create default
    # The plan has dips, so titles are derived via titles_from_plan.
    assert calls[0]["titles"] == resolve.titles_from_plan(plan)
    # --canvas was not given, so the create default rides along.
    assert calls[0]["canvas"] == "uhd"
    out = capsys.readouterr().out
    assert "3 cuts -> Resolve timeline 'Monteur Montage' (8.0s at 25 fps)" in out
    assert "drag it onto the black gap at 4.6s." in out  # warnings are printed


def test_cli_into_resolve_forwards_chosen_canvas(monkeypatch, capsys) -> None:
    plan = trailer_plan()
    calls = _run_cmd_create_into_resolve(
        monkeypatch,
        plan,
        {"ok": True, "timeline": "Monteur Montage", "warnings": []},
        extra_args=["--canvas", "cine-uhd"],
    )
    assert calls[0]["canvas"] == "cine-uhd"


def test_cli_into_resolve_no_dips_passes_no_titles(monkeypatch, capsys) -> None:
    calls = _run_cmd_create_into_resolve(
        monkeypatch,
        make_plan(),
        {"ok": True, "timeline": "Monteur Montage", "warnings": []},
    )
    assert calls[0]["titles"] is None
    out = capsys.readouterr().out
    assert "Resolve timeline 'Monteur Montage'" in out


def test_cli_into_resolve_failure_exits_with_error(monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run_cmd_create_into_resolve(
            monkeypatch,
            make_plan(),
            {"ok": False, "error": resolve._CRASH_MESSAGE, "reason": "native-crash"},
        )
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "MONTEUR_RESOLVE_PYTHON" in err  # the crash hint reaches the user


# --- Worker `render` command (streamed Deliver drive) ----------------------------


def render_project() -> FakeProject:
    """A FakeProject ready to render: presets, a 3-step progress, success."""
    project = FakeProject(
        timelines=[standard_timeline(), FakeTimeline(name="Cut 2", start_frame=0)]
    )
    project.render_presets = [
        "H.264 Master", "YouTube - 2160p", "YouTube - 1080p", "ProRes Master",
    ]
    project.render_progress = [25, 50, 75]
    return project


def run_render_worker(monkeypatch, capsys, project, request):
    """Run the worker's `render` command against a FakeProject.

    Returns ``(lines, app)`` — the parsed emitted JSON lines and the
    FakeResolve app (for OpenPage assertions). Poll sleeps are zeroed.
    """
    from monteur import _resolve_worker

    monkeypatch.setattr(_resolve_worker, "_RENDER_POLL_SECONDS", 0)
    app = FakeResolve(project)
    monkeypatch.setattr(resolve, "connect", lambda a=None: ResolveBridge(app))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    code = _resolve_worker.main(["render"])
    assert code == 0
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    return lines, app


def render_request(tmp_path, **overrides) -> dict:
    request = {
        "timeline": None,
        "target_dir": str(tmp_path / "renders"),
        "name": "holiday",
        "preset": "2160p",
    }
    request.update(overrides)
    return request


def test_worker_render_happy_path_with_preset(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    request = render_request(tmp_path)
    lines, app = run_render_worker(monkeypatch, capsys, project, request)
    # prepare -> progress x4 (25/50/75 while rendering, 100 from the final
    # status read) -> done, all as separate flushed lines.
    assert lines[0] == {"stage": "prepare", "ok": True, "preset": "YouTube - 2160p"}
    progress = [l["percent"] for l in lines if l["stage"] == "progress"]
    assert progress == [25, 50, 75, 100]
    done = lines[-1]
    assert done["stage"] == "done" and done["ok"] is True
    assert done["path"] == os.path.join(str(tmp_path / "renders"), "holiday")
    assert isinstance(done["seconds"], (int, float))
    # The Deliver workflow really ran: page, preset, settings, job, start.
    assert app.opened_pages == ["deliver"]
    assert project.load_preset_calls == ["YouTube - 2160p"]
    assert project.render_settings_calls == [
        {
            "SelectAllFrames": True,
            "TargetDir": str(tmp_path / "renders"),
            "CustomName": "holiday",
        }
    ]
    assert project.start_rendering_calls == [["render-job-1"]]
    assert (tmp_path / "renders").is_dir()  # created with parents


def test_worker_render_null_preset_defaults_to_2160p(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path, preset=None)
    )
    assert lines[0]["preset"] == "YouTube - 2160p"


def test_worker_render_1080p_preset(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path, preset="1080p")
    )
    assert lines[0] == {"stage": "prepare", "ok": True, "preset": "YouTube - 1080p"}
    assert project.load_preset_calls == ["YouTube - 1080p"]


def test_worker_render_loose_preset_match(tmp_path, monkeypatch, capsys) -> None:
    # "verify against the list at runtime, match loosely by substring":
    # a differently-worded YouTube preset still wins over a plain one.
    project = render_project()
    project.render_presets = ["Custom Master", "My 2160p Export", "YouTube 2160p UHD"]
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[0]["preset"] == "YouTube 2160p UHD"


def test_worker_render_preset_match_without_youtube(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.render_presets = ["My 2160p Master"]
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[0]["preset"] == "My 2160p Master"


def test_worker_render_dict_preset_list(tmp_path, monkeypatch, capsys) -> None:
    # Some Resolve versions return dicts from GetRenderPresetList.
    project = render_project()
    project.render_presets = [{"RenderPresetName": "YouTube - 2160p"}]
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[0]["preset"] == "YouTube - 2160p"


def test_worker_render_fallback_to_format_codec(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.render_presets = ["ProRes Master"]  # nothing matches 2160p
    project.render_formats = {"QuickTime": "mov", "MP4": "mp4"}
    project.render_codecs = {"H.264": "H264", "H.265": "H265"}
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[0] == {"stage": "prepare", "ok": True, "preset": "mp4/H264"}
    assert project.format_codec_calls == [("mp4", "H264")]
    assert project.codec_queries == ["mp4"]
    # In fallback mode the extension is knowable — the path carries it.
    assert lines[-1]["path"].endswith(os.path.join("renders", "holiday.mp4"))


def test_worker_render_load_preset_refusal_falls_back(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.load_preset_result = False  # the preset exists but won't load
    project.render_formats = {"MP4": "mp4"}
    project.render_codecs = {"H.264 Best": "H264_Main"}
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[0]["preset"] == "mp4/H264_Main"


def test_worker_render_no_preset_and_no_format_is_clean(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.render_presets = []
    project.render_formats = {}
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert len(lines) == 1
    assert lines[0]["stage"] == "prepare" and lines[0]["ok"] is False
    assert "no mp4 render format" in lines[0]["error"]
    assert project.start_rendering_calls == []


def test_worker_render_no_h264_codec_is_clean(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.render_presets = []
    project.render_formats = {"MP4": "mp4"}
    project.render_codecs = {"VP9": "VP9"}
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[0]["ok"] is False
    assert "H.264" in lines[0]["error"]


def test_worker_render_selects_named_timeline(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path, timeline="Cut 2")
    )
    assert lines[-1]["stage"] == "done"
    assert [t.GetName() for t in project.set_current_calls] == ["Cut 2"]


def test_worker_render_unknown_timeline_is_clean(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path, timeline="Nope")
    )
    assert len(lines) == 1
    assert lines[0]["stage"] == "prepare" and lines[0]["ok"] is False
    assert "'Nope'" in lines[0]["error"]
    assert "Cut 1" in lines[0]["error"]  # the available timelines are listed


def test_worker_render_add_render_job_refusal_is_clean(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.add_render_job_result = None
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[-1]["stage"] == "prepare" and lines[-1]["ok"] is False
    assert "AddRenderJob" in lines[-1]["error"]
    assert project.start_rendering_calls == []


def test_worker_render_start_rendering_refusal_is_clean(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.start_rendering_result = False
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    # prepare succeeded; the failure is a terminal render-stage line.
    assert lines[0]["ok"] is True
    assert lines[-1]["stage"] == "render" and lines[-1]["ok"] is False
    assert "StartRendering" in lines[-1]["error"]


def test_worker_render_failed_job_status_is_clean(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.render_progress = [40]
    project.render_final_status = {"JobStatus": "Failed", "Error": "Disk full"}
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[-1]["stage"] == "render" and lines[-1]["ok"] is False
    assert "'Failed'" in lines[-1]["error"]
    assert "Disk full" in lines[-1]["error"]


def test_worker_render_settings_refusal_is_clean(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.set_render_settings_result = False
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    assert lines[-1]["stage"] == "prepare" and lines[-1]["ok"] is False
    assert "SetRenderSettings" in lines[-1]["error"]


def test_worker_render_progress_only_when_changed(tmp_path, monkeypatch, capsys) -> None:
    project = render_project()
    project.render_progress = [25, 25, 25, 50]
    lines, _ = run_render_worker(
        monkeypatch, capsys, project, render_request(tmp_path)
    )
    progress = [l["percent"] for l in lines if l["stage"] == "progress"]
    assert progress == [25, 50, 100]  # duplicates are silent


def test_worker_render_status_without_percent_still_finishes(
    tmp_path, monkeypatch, capsys
) -> None:
    # A Resolve version whose job status carries no CompletionPercentage:
    # no progress lines, but the render still completes cleanly.
    class NoPercentProject(FakeProject):
        def __init__(self):
            super().__init__(timelines=[standard_timeline()])
            self.render_presets = ["YouTube - 2160p"]
            self._polls = 2

        def IsRenderingInProgress(self):
            self._polls -= 1
            return self._polls > 0

        def GetRenderJobStatus(self, job_id):
            return {"JobStatus": "Complete"}

    lines, _ = run_render_worker(
        monkeypatch, capsys, NoPercentProject(), render_request(tmp_path)
    )
    assert [l["stage"] for l in lines] == ["prepare", "done"]
    assert lines[-1]["ok"] is True


def test_worker_render_creates_target_dir_with_parents(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / "deep" / "nested" / "renders"
    lines, _ = run_render_worker(
        monkeypatch, capsys, render_project(),
        render_request(tmp_path, target_dir=str(target)),
    )
    assert lines[-1]["stage"] == "done"
    assert target.is_dir()


def test_worker_render_unwritable_target_dir_is_clean(tmp_path, monkeypatch, capsys) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file")
    lines, _ = run_render_worker(
        monkeypatch, capsys, render_project(),
        render_request(tmp_path, target_dir=str(blocked)),
    )
    assert len(lines) == 1
    assert lines[0]["stage"] == "prepare" and lines[0]["ok"] is False
    assert "could not create the render folder" in lines[0]["error"]


def test_worker_render_unknown_quality_is_clean(tmp_path, monkeypatch, capsys) -> None:
    lines, _ = run_render_worker(
        monkeypatch, capsys, render_project(),
        render_request(tmp_path, preset="720p"),
    )
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert "unknown render preset '720p'" in lines[0]["error"]


def test_worker_render_resolve_not_running_is_clean(tmp_path, monkeypatch, capsys) -> None:
    from monteur import _resolve_worker

    monkeypatch.setattr(_resolve_worker, "_RENDER_POLL_SECONDS", 0)

    def boom(app=None):
        raise MonteurResolveError("Resolve is not running.")

    monkeypatch.setattr(resolve, "connect", boom)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(render_request(tmp_path)))
    )
    assert _resolve_worker.main(["render"]) == 0
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert lines == [
        {"stage": "prepare", "ok": False, "error": "Resolve is not running."}
    ]


# --- _stream_worker / render_isolated --------------------------------------------


def _fake_stream_child(tmp_path, monkeypatch, body: str) -> None:
    """Point _WORKER_PATH at a tiny stand-in script for real-subprocess tests."""
    script = tmp_path / "fake_stream_worker.py"
    script.write_text(body, encoding="utf-8")
    monkeypatch.setattr(resolve, "_WORKER_PATH", str(script))


_HAPPY_RENDER_CHILD = """\
import json, sys, time
sys.stdin.read()
print(json.dumps({"stage": "prepare", "ok": True, "preset": "YouTube - 2160p"}), flush=True)
print("this line is not JSON and must be skipped", flush=True)
for percent in (10, 60, 100):
    time.sleep(0.05)
    print(json.dumps({"stage": "progress", "percent": percent}), flush=True)
print(json.dumps({"stage": "done", "ok": True, "path": "/out/x.mp4", "seconds": 1.5}), flush=True)
"""


def test_stream_worker_streams_lines_while_running(tmp_path, monkeypatch) -> None:
    _fake_stream_child(tmp_path, monkeypatch, _HAPPY_RENDER_CHILD)
    seen: list[dict] = []
    ok, lines, failure = resolve._stream_worker(
        "render", 30.0, request={"x": 1}, on_line=seen.append
    )
    assert ok is True and failure == {}
    assert seen == lines  # every parsed line reached the callback, in order
    assert [l["stage"] for l in lines] == [
        "prepare", "progress", "progress", "progress", "done",
    ]  # the garbage line was skipped


def test_render_isolated_happy_path_streams_progress(tmp_path, monkeypatch) -> None:
    _fake_stream_child(tmp_path, monkeypatch, _HAPPY_RENDER_CHILD)
    percents: list[int] = []
    result = resolve.render_isolated(
        None, "/out", "x", preset="2160p", progress=percents.append
    )
    assert percents == [10, 60, 100]  # live, increasing, ints
    assert result == {
        "ok": True,
        "path": "/out/x.mp4",
        "seconds": 1.5,
        "preset": "YouTube - 2160p",
    }


def test_render_isolated_sends_request_on_stdin(tmp_path, monkeypatch) -> None:
    _fake_stream_child(
        tmp_path,
        monkeypatch,
        """\
import json, sys
request = json.loads(sys.stdin.read())
print(json.dumps({"stage": "prepare", "ok": True, "preset": request["preset"]}), flush=True)
print(json.dumps({"stage": "done", "ok": True,
                  "path": request["target_dir"] + "/" + request["name"],
                  "seconds": 0.1}), flush=True)
""",
    )
    result = resolve.render_isolated("Cut 7", "/renders", "movie", preset="1080p")
    assert result["ok"] is True
    assert result["path"] == "/renders/movie"
    assert result["preset"] == "1080p"


def test_render_isolated_clean_failure_line(tmp_path, monkeypatch) -> None:
    _fake_stream_child(
        tmp_path,
        monkeypatch,
        """\
import json, sys
sys.stdin.read()
print(json.dumps({"stage": "prepare", "ok": False,
                  "error": "No project is open in Resolve."}), flush=True)
""",
    )
    result = resolve.render_isolated(None, "/out", "x")
    assert result == {"ok": False, "error": "No project is open in Resolve."}


def test_render_isolated_timeout_kills_child_and_says_render_continues(
    tmp_path, monkeypatch
) -> None:
    _fake_stream_child(
        tmp_path,
        monkeypatch,
        """\
import json, sys, time
sys.stdin.read()
print(json.dumps({"stage": "progress", "percent": 5}), flush=True)
time.sleep(30)
""",
    )
    percents: list[int] = []
    started = __import__("time").monotonic()
    result = resolve.render_isolated(
        None, "/out", "x", timeout=0.7, progress=percents.append
    )
    assert __import__("time").monotonic() - started < 10
    assert percents == [5]  # the streamed line arrived before the kill
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert "continues inside Resolve" in result["error"]


def test_render_isolated_no_terminal_line_is_bad_output(tmp_path, monkeypatch) -> None:
    _fake_stream_child(
        tmp_path,
        monkeypatch,
        """\
import json, sys
sys.stdin.read()
print(json.dumps({"stage": "progress", "percent": 50}), flush=True)
""",
    )
    result = resolve.render_isolated(None, "/out", "x")
    assert result["ok"] is False
    assert result["reason"] == "bad-output"


def test_render_isolated_progress_exception_propagates_and_kills_child(
    tmp_path, monkeypatch
) -> None:
    # The cooperative-cancel seam: a raising progress callback aborts the
    # stream (the child is killed first) and the exception reaches the
    # caller — Studio's render job uses exactly this to honour Cancel.
    _fake_stream_child(
        tmp_path,
        monkeypatch,
        """\
import json, sys, time
sys.stdin.read()
print(json.dumps({"stage": "progress", "percent": 5}), flush=True)
time.sleep(30)
print(json.dumps({"stage": "done", "ok": True, "path": "/x", "seconds": 30.0}), flush=True)
""",
    )

    class Cancelled(Exception):
        pass

    def cancelling_progress(percent: int) -> None:
        raise Cancelled("stop watching")

    started = __import__("time").monotonic()
    with pytest.raises(Cancelled):
        resolve.render_isolated(None, "/out", "x", progress=cancelling_progress)
    assert __import__("time").monotonic() - started < 10  # child did not linger


class _FakeStreamPopen:
    """A Popen stand-in for exit-code classification tests (portable)."""

    def __init__(self, lines: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = io.StringIO(lines)
        self.stderr = io.StringIO(stderr)
        self.stdin = io.StringIO()
        self.returncode: int | None = None
        self._rc = returncode

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = self._rc


def test_stream_worker_native_crash_is_classified(monkeypatch) -> None:
    fake = _FakeStreamPopen(
        json.dumps({"stage": "prepare", "ok": True, "preset": "P"}) + "\n",
        returncode=-1073741819,  # 0xC0000005 access violation
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    ok, lines, failure = resolve._stream_worker("render", 5.0)
    assert ok is False
    assert failure["reason"] == "crash"
    assert lines and lines[0]["stage"] == "prepare"  # partial stream survives


def test_render_isolated_native_crash_never_raises(monkeypatch) -> None:
    fake = _FakeStreamPopen("", returncode=3221225477)  # unsigned 0xC0000005
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    result = resolve.render_isolated(None, "/out", "x")
    assert result["ok"] is False
    assert result["reason"] == "native-crash"
    assert "3.10" in result["error"] and "3.12" in result["error"]


def test_stream_worker_clean_nonzero_is_worker_error(monkeypatch) -> None:
    # stderr goes to the spool file _stream_worker passes in (not a pipe) —
    # the fake writes there like a real child would.
    fake = _FakeStreamPopen("", returncode=1)

    def fake_popen(cmd, **kwargs):
        kwargs["stderr"].write("Traceback...\nImportError: nope\n")
        return fake

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ok, lines, failure = resolve._stream_worker("render", 5.0)
    assert ok is False
    assert failure["reason"] == "worker-error"
    assert "ImportError: nope" in failure["error"]


def test_stream_worker_missing_interpreter(monkeypatch) -> None:
    def raise_missing(*args, **kwargs):
        raise FileNotFoundError("no such interpreter")

    monkeypatch.setattr(subprocess, "Popen", raise_missing)
    ok, lines, failure = resolve._stream_worker("render", 5.0)
    assert ok is False
    assert failure["reason"] == "no-interpreter"
    assert "Find a compatible Python" in failure["error"]


def test_stream_worker_uses_worker_python(monkeypatch) -> None:
    monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", "/opt/py311/bin/python3.11")
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeStreamPopen(
            json.dumps({"stage": "done", "ok": True, "path": "/x", "seconds": 0.1})
            + "\n"
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ok, _lines, _failure = resolve._stream_worker("render", 5.0)
    assert ok is True
    assert captured["cmd"][0] == "/opt/py311/bin/python3.11"
    assert captured["cmd"][1].endswith("_resolve_worker.py")
    assert captured["cmd"][2] == "render"


# --- CLI: monteur resolve render ---------------------------------------------------


def _run_cli_render(monkeypatch, result, argv):
    from monteur.cli import main

    calls: list[dict] = []

    def fake_render(
        timeline, target_dir, name, preset=None, timeout=7200.0, progress=None
    ):
        calls.append(
            {
                "timeline": timeline, "target_dir": target_dir, "name": name,
                "preset": preset,
            }
        )
        if progress is not None:
            progress(50)
        return dict(result)

    monkeypatch.setattr(resolve, "render_isolated", fake_render)
    main(argv)
    return calls


def test_cli_resolve_render_happy_path(monkeypatch, capsys) -> None:
    calls = _run_cli_render(
        monkeypatch,
        {"ok": True, "path": "/out/v.mp4", "seconds": 12.0,
         "preset": "YouTube - 2160p"},
        ["resolve", "render", "--out", "/out", "--name", "v",
         "--preset", "2160p", "--timeline", "Cut 1"],
    )
    assert calls == [
        {"timeline": "Cut 1", "target_dir": "/out", "name": "v", "preset": "2160p"}
    ]
    out = capsys.readouterr().out
    assert "\rRendering… 50%" in out  # streamed percents redraw one line
    assert "Your video is ready: /out/v.mp4 in 12s" in out
    assert "YouTube - 2160p" in out


def test_cli_resolve_render_defaults(monkeypatch, capsys) -> None:
    calls = _run_cli_render(
        monkeypatch,
        {"ok": True, "path": "/r/monteur_render", "seconds": 3.0, "preset": "P"},
        ["resolve", "render", "--out", "/r"],
    )
    assert calls == [
        {"timeline": None, "target_dir": "/r", "name": "monteur_render",
         "preset": None}
    ]


def test_cli_resolve_render_requires_out(monkeypatch, capsys) -> None:
    from monteur.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["resolve", "render"])
    assert excinfo.value.code == 1
    assert "--out" in capsys.readouterr().err


def test_cli_resolve_render_failure_exits_with_error(monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run_cli_render(
            monkeypatch,
            {"ok": False, "error": "Resolve is not running."},
            ["resolve", "render", "--out", "/out"],
        )
    assert excinfo.value.code == 1
    assert "Resolve is not running." in capsys.readouterr().err


# --- Windows PythonCore registry census -------------------------------------------


class _FakeRegKey:
    def __init__(self, kind: str, payload) -> None:
        self.kind = kind  # "core" or "install"
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """A winreg shim. hives = {"HKLM": {"3.11": install_path_or_None}, ...}."""

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200

    def __init__(self, hives, broken_tags=()) -> None:
        self._hives = hives
        self._broken = set(broken_tags)

    def OpenKey(self, root, path, reserved, access):
        if root in self._hives and path == r"SOFTWARE\Python\PythonCore":
            return _FakeRegKey("core", self._hives[root])
        if isinstance(root, _FakeRegKey) and root.kind == "core":
            tag = path[: -len(r"\InstallPath")]
            if tag in self._broken or tag not in root.payload:
                raise OSError("no InstallPath")
            return _FakeRegKey("install", root.payload[tag])
        raise OSError("not found")

    def QueryInfoKey(self, key):
        return (len(key.payload), 0, 0)

    def EnumKey(self, key, index):
        return sorted(key.payload)[index]

    def QueryValue(self, key, name):
        if key.payload is None:
            raise OSError("no default value")
        return key.payload


def test_census_merges_hives_and_dedupes_views() -> None:
    shim = _FakeWinreg(
        {
            "HKLM": {"3.11": "C:\\P311\\", "3.14": "C:\\Python314\\"},
            "HKCU": {"3.10": None},
        }
    )
    census = resolve._pythoncore_census(shim)
    # Three registry views are scanned but every (hive, tag) appears once.
    assert sorted((e["hive"], e["version"]) for e in census) == [
        ("HKCU", "3.10"), ("HKLM", "3.11"), ("HKLM", "3.14"),
    ]
    by_tag = {e["version"]: e for e in census}
    assert by_tag["3.14"]["path"] == "C:\\Python314\\"
    assert by_tag["3.10"]["path"] is None  # InstallPath without a value


def test_census_tolerates_broken_install_keys() -> None:
    shim = _FakeWinreg({"HKLM": {"3.13": "C:\\P313\\"}}, broken_tags={"3.13"})
    census = resolve._pythoncore_census(shim)
    assert census == [{"version": "3.13", "hive": "HKLM", "path": None}]


def test_registered_pythons_is_guarded() -> None:
    class _Exploding:
        HKEY_LOCAL_MACHINE = "HKLM"
        HKEY_CURRENT_USER = "HKCU"

        def __getattr__(self, name):
            raise RuntimeError("registry on fire")

    assert resolve._registered_pythons(_Exploding()) == []
    if not sys.platform.startswith("win"):
        # Off Windows the real winreg import fails -> [].
        assert resolve._registered_pythons() == []


def test_registry_highest_per_hive_skips_garbage() -> None:
    census = [
        {"version": "3.11", "hive": "HKLM", "path": None},
        {"version": "3.14", "hive": "HKLM", "path": "C:\\Python314\\"},
        {"version": "not-a-version", "hive": "HKLM", "path": None},
        {"version": "3.12", "hive": "HKCU", "path": None},
    ]
    assert resolve._registry_highest(census) == "3.14"
    assert resolve._registry_highest(census, hive="HKCU") == "3.12"
    assert resolve._registry_highest([]) is None


# --- The registry-conflict verdict ------------------------------------------------


def _field_info(worker="3.11.6", highest="3.14", platform="win32", census=None):
    if census is None:
        census = [
            {"version": "3.11", "hive": "HKLM", "path": "C:\\P311\\"},
            {"version": highest, "hive": "HKLM", "path": "C:\\Python314\\"},
        ]
    return {
        "platform": platform,
        "python_version": worker,
        "bits": 64,
        "registered_pythons": census,
        "registry_highest": highest,
    }


def test_registry_conflict_fires_on_the_field_scenario() -> None:
    # Worker 3.11, HKLM-highest 3.14, hard crash at import: the exact case
    # that burned a real user for hours. The verdict must name the
    # mechanism, the version, its path and both fixes.
    verdict = resolve._registry_conflict_verdict(
        _field_info(), {"crashed_at": "import"}
    )
    assert "highest Python registered" in verdict
    assert "3.14" in verdict and "C:\\Python314\\" in verdict
    assert "uninstall" in verdict.lower()
    assert r"PythonCore\3.14" in verdict
    assert "3.10" in verdict  # names the actually-working range


def test_registry_conflict_mid_range_mismatch_fires() -> None:
    # Worker 3.10, highest 3.11: not "too new" in absolute terms, but the
    # registry still wins over the worker -> mismatch copy.
    verdict = resolve._registry_conflict_verdict(
        _field_info(worker="3.10.10", highest="3.11"),
        {"crashed_at": "dll-load"},
    )
    assert "does not match the worker" in verdict


def test_registry_conflict_mentions_higher_hkcu_python() -> None:
    census = [
        {"version": "3.13", "hive": "HKLM", "path": None},
        {"version": "3.14", "hive": "HKCU", "path": None},
    ]
    verdict = resolve._registry_conflict_verdict(
        _field_info(highest="3.13", census=census), {"crashed_at": "import"}
    )
    assert "HKEY_CURRENT_USER" in verdict


def test_registry_conflict_stays_silent_when_not_applicable() -> None:
    fire = {"crashed_at": "import"}
    # The highest registered version IS the worker's own -> release advice
    # territory, not this rule.
    assert resolve._registry_conflict_verdict(
        _field_info(worker="3.14.3", highest="3.14"), fire
    ) == ""
    # Not Windows.
    assert resolve._registry_conflict_verdict(
        _field_info(platform="linux"), fire
    ) == ""
    # No census (registry unreadable).
    assert resolve._registry_conflict_verdict(
        _field_info(census=[]) | {"registry_highest": None}, fire
    ) == ""
    # Crash pinpointed elsewhere.
    assert resolve._registry_conflict_verdict(_field_info(), {}) == ""
    assert resolve._registry_conflict_verdict(
        _field_info(), {"crashed_at": "connect"}
    ) == ""


def test_crash_verdict_prefers_registry_conflict_over_release_advice() -> None:
    verdict = resolve._diagnosis_verdict(
        "settings",
        _field_info(),
        {"connected": False, "reason": "crash"},
        load_test={"crashed_at": "import"},
    )
    assert "highest Python registered" in verdict
    assert "update DaVinci Resolve" not in verdict


# --- Probe short-circuit boundary after the 3.12 allowance ------------------------


def test_probe_312_is_probed_not_rejected(monkeypatch) -> None:
    calls = _fake_probe_worker(
        monkeypatch,
        (True, {"python_version": "3.12.4", "bits": 64}),
        (True, {"connected": False, "error": "Resolve is not running."}),
    )
    result = resolve.probe_resolve_python("/py312")
    assert result["ok"] is True
    assert calls == ["info", "status"]  # 3.12 gets the real probe now


# --- playhead-first title placement & SFX track creation --------------------------


def test_add_titles_moves_playhead_before_insert() -> None:
    # Resolve 21 field case: items cannot be repositioned via scripting,
    # but Fusion titles insert AT the playhead — so the playhead must be
    # moved to the title's spot BEFORE the insert.
    timeline = standard_timeline()
    calls: list[str] = []
    timeline.SetCurrentTimecode = lambda tc: calls.append(f"tc:{tc}") or True
    original_insert = timeline.InsertFusionTitleIntoTimeline
    timeline.InsertFusionTitleIntoTimeline = (
        lambda name: calls.append("insert") or original_insert(name)
    )
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    bridge.add_titles(title_specs(), fps=24.0, warnings=warnings)
    # playhead move precedes each insert; absolute frames: 86400 + t*24
    assert calls[0].startswith("tc:") and calls[1] == "insert"
    assert calls[2].startswith("tc:") and calls[3] == "insert"
    from monteur.model import format_timecode

    assert calls[0] == f"tc:{format_timecode(86400 + 240, 24.0)}"
    assert calls[2] == f"tc:{format_timecode(86400 + 480, 24.0)}"


def test_add_titles_no_drag_warning_when_playhead_moved() -> None:
    # Repositioning unavailable (no SetStart/SetEnd on the item) but the
    # playhead was moved first: the title IS in place — no drag warning.
    timeline = standard_timeline()
    timeline.SetCurrentTimecode = lambda tc: True
    timeline.title_items_queue = [
        FakeTitleItemNoSetters(FakeComp([FakeTextTool()])),
        FakeTitleItemNoSetters(FakeComp([FakeTextTool()])),
    ]
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    added = bridge.add_titles(title_specs(), fps=24.0, warnings=warnings)
    assert added == 2
    assert not any("drag" in w for w in warnings)


def test_add_titles_drag_warning_without_playhead_support() -> None:
    # Neither playhead moving nor repositioning: the honest warning stays.
    timeline = standard_timeline()
    timeline.title_items_queue = [
        FakeTitleItemNoSetters(FakeComp([FakeTextTool()])),
    ]
    bridge, _ = make_bridge([timeline])
    warnings: list[str] = []
    bridge.add_titles(
        [{"start": 9.6, "duration": 2.0, "text": "T"}], fps=24.0,
        warnings=warnings,
    )
    assert any("drag" in w for w in warnings)


def test_build_creates_the_sfx_audio_track() -> None:
    # Field case: element clips silently missing because only A1 existed —
    # an explicit trackIndex never creates the track.
    bridge, project = make_bridge([standard_timeline()])
    from monteur.montage import SfxCue

    plan = make_plan()
    plan.sfx = [
        SfxCue(time=1.0, duration=0.5, kind="impact", query="hit",
               note="drop", file="/sfx/hit.wav"),
    ]
    build_append(bridge, plan, fps=24.0)
    created = project._timelines[-1]
    # One audio track existed (the music append created it); the SFX
    # placement added the second.
    assert "audio" in created.added_tracks
    assert created.GetTrackCount("audio") >= 2


# --- hybrid build: FCPXML import + API finishing ----------------------------------


def hybrid_plan() -> MontagePlan:
    """A plan exercising everything only the timeline FILE can carry.

    A dissolve into the second entry, head/tail black fades, the trailer
    dips (real gaps) and a placed SFX element (its own audio lane).
    """
    from monteur.montage import SfxCue

    plan = trailer_plan()
    plan.fade_in = 0.5
    plan.fade_out = 1.0
    plan.entries[1].transition = 0.5
    plan.sfx = [
        SfxCue(1.0, 0.8, "impact", "hit", "on the drop", file="/sfx/hit.wav"),
    ]
    return plan


def test_hybrid_build_imports_the_written_fcpxml() -> None:
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    warnings: list[str] = []
    name = bridge.build_timeline_from_plan(
        hybrid_plan(), fps=25.0, warnings=warnings
    )
    assert name == "Monteur Montage"
    assert warnings == []
    # ONE timeline-file import, with the documented options.
    assert len(pool.timeline_import_calls) == 1
    call = pool.timeline_import_calls[0]
    assert call["options"]["timelineName"] == "Monteur Montage"
    assert call["options"]["importSourceClips"] is True
    # Best-effort media linking: the folder of the first clip.
    assert call["options"]["sourceClipsPath"] == "/media"
    # The temp FCPXML existed (non-empty) at import time and is gone now.
    assert call["existed"] is True
    assert call["size"] > 0
    assert not os.path.exists(call["path"])
    # NOTHING was placed clip-by-clip: no media imports, no appends, no
    # empty timeline — the file already carries the montage, the music
    # AND the placed SFX element (no double-placing).
    assert pool.import_calls == []
    assert pool.appended == []
    assert pool.created_timeline_names == []


def test_hybrid_fcpxml_carries_dissolves_fades_and_audio_lanes() -> None:
    import xml.etree.ElementTree as ET

    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    bridge.build_timeline_from_plan(hybrid_plan(), fps=25.0)
    # The fake captured the file content before the bridge deleted it.
    root = ET.fromstring(pool.imported_fcpxml[0])
    spine = root.find(".//spine")
    # Head fade, the plan's dissolve, tail fade — in spine order.
    transitions = spine.findall("transition")
    assert [t.get("name") for t in transitions] == ["Cross Dissolve"] * 3
    # The montage's clips are in the spine, with real black gaps: the head
    # fade shift, the two trailer dips, and the tail fade gap.
    assert len(spine.findall("asset-clip")) == 3
    assert len(spine.findall("gap")) == 4
    # All audio lanes made it: the music bed on lane -1 (role "music") and
    # the placed SFX element on lane -2 (role "effects").
    connected = spine.findall("asset-clip/asset-clip")
    lanes = {c.get("lane"): c.get("audioRole") for c in connected}
    assert lanes == {"-1": "music", "-2": "effects"}


def test_hybrid_titles_land_unshifted_at_plan_time() -> None:
    bridge, project = make_bridge([standard_timeline()])
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    name = bridge.build_timeline_from_plan(plan, fps=25.0, titles=titles)
    assert name == "Monteur Montage"
    imported = project._timelines[-1]
    assert imported.GetName() == "Monteur Montage"
    # The bridge made the import current before finishing it.
    assert project.GetCurrentTimeline() is imported
    assert imported.inserted_fusion_titles == ["Text+", "Text+"]
    first, second = imported.created_title_items
    # The imported timeline HAS the real gaps: titles land at their
    # plan-time positions UNSHIFTED (2.6s -> 65, 5.0s -> 125 at 25 fps).
    assert (first.start, first.end) == (65, 65 + 50)
    assert (second.start, second.end) == (125, 125 + 50)
    assert first._comp._tools[0].inputs["StyledText"] == "ONE"
    assert second._comp._tools[0].inputs["StyledText"] == "TWO"


def test_hybrid_canvas_verifies_resolution_and_always_crops() -> None:
    bridge, project = make_bridge([standard_timeline()])
    warnings: list[str] = []
    bridge.build_timeline_from_plan(
        trailer_plan(), fps=25.0, canvas="cine-uhd", warnings=warnings
    )
    imported = project._timelines[-1]
    # The FCPXML format element already carried 3840x1608 — verified, so
    # the resolution is never re-set (no SetSetting fights, no fallback).
    assert imported.GetSetting("timelineResolutionWidth") == "3840"
    assert imported.GetSetting("timelineResolutionHeight") == "1608"
    assert imported.settings_set == []
    assert project.settings_set == []
    # The per-clip crop is what the file CANNOT carry: always applied.
    video_items = imported._tracks["video"][0]
    assert len(video_items) == 3
    assert all(item.properties == [("Scaling", 1)] for item in video_items)
    assert warnings == []


def test_hybrid_canvas_sets_resolution_when_import_differs() -> None:
    bridge, project = make_bridge([standard_timeline()])
    # This Resolve ignored the file's format element (or a project default
    # won): the verify step sees the mismatch and sets it explicitly.
    project.media_pool.imported_timeline_resolution = (1920, 1080)
    bridge.build_timeline_from_plan(trailer_plan(), fps=25.0, canvas="cine-uhd")
    imported = project._timelines[-1]
    assert imported.settings_set == [
        ("useCustomSettings", "1"),
        ("timelineResolutionWidth", "3840"),
        ("timelineResolutionHeight", "1608"),
    ]


def test_hybrid_refusal_falls_back_to_append_with_one_warning() -> None:
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    pool.fail_timeline_import = True  # ImportTimelineFromFile returns None
    warnings: list[str] = []
    name = bridge.build_timeline_from_plan(
        make_plan(), fps=24.0, warnings=warnings
    )
    assert name == "Monteur Montage"
    # The append flow ran, unchanged: media imported, clips + music appended.
    assert pool.import_calls == [["/media/a.mov", "/media/b.mov", "/music/song.wav"]]
    assert len(pool.appended) == 4
    assert pool.created_timeline_names == ["Monteur Montage"]
    # Exactly ONE honest warning about the degradation.
    fallback = [w for w in warnings if "refused the timeline file import" in w]
    assert len(fallback) == 1
    assert "clip-by-clip" in fallback[0]
    assert "dissolves and fades" in fallback[0]
    # The hybrid attempt really offered a non-empty file, then cleaned up.
    call = pool.timeline_import_calls[0]
    assert call["existed"] is True and call["size"] > 0
    assert not os.path.exists(call["path"])


def test_hybrid_import_exception_falls_back_to_append() -> None:
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    pool.raise_on_timeline_import = RuntimeError("importer exploded")
    warnings: list[str] = []
    name = bridge.build_timeline_from_plan(
        make_plan(), fps=24.0, warnings=warnings
    )
    assert name == "Monteur Montage"
    assert len(pool.appended) == 4  # clip-by-clip build took over
    assert len([w for w in warnings if "refused the timeline file import" in w]) == 1
    # The temp file is removed on the exception path too.
    call = pool.timeline_import_calls[0]
    assert call["existed"] is True
    assert not os.path.exists(call["path"])


def test_hybrid_import_returning_true_uses_current_timeline() -> None:
    # Older Resolve builds return only a truthy flag; the import becomes
    # the current timeline and the finishing steps run against that.
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.import_returns_bool = True
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    name = bridge.build_timeline_from_plan(plan, fps=25.0, titles=titles)
    assert name == "Monteur Montage"
    imported = project._timelines[-1]
    assert imported.inserted_fusion_titles == ["Text+", "Text+"]
    assert project.media_pool.appended == []


def test_hybrid_returns_the_renamed_timeline_name() -> None:
    # Resolve may rename an import; the REAL name is what callers get back.
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.import_rename = "Monteur Montage 1"
    name = bridge.build_timeline_from_plan(make_plan(), fps=24.0)
    assert name == "Monteur Montage 1"


def test_hybrid_uniquifies_the_timeline_name() -> None:
    taken = FakeTimeline(name="Monteur Montage", start_frame=0)
    bridge, project = make_bridge([taken])
    name = bridge.build_timeline_from_plan(make_plan(), fps=24.0)
    assert name == "Monteur Montage 2"
    options = project.media_pool.timeline_import_calls[0]["options"]
    assert options["timelineName"] == "Monteur Montage 2"


def test_hybrid_fps_mismatch_trusts_the_import_and_warns() -> None:
    bridge, project = make_bridge([standard_timeline()])
    # The imported timeline reports 50 fps although the plan says 25: the
    # import wins — titles are placed in the timeline's own currency.
    project.media_pool.imported_timeline_fps = "50"
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    warnings: list[str] = []
    bridge.build_timeline_from_plan(
        plan, fps=25.0, titles=titles, warnings=warnings
    )
    mismatch = [w for w in warnings if "50 fps" in w]
    assert len(mismatch) == 1
    assert "trusting the import" in mismatch[0]
    imported = project._timelines[-1]
    first, second = imported.created_title_items
    assert (first.start, first.end) == (130, 130 + 100)  # 2.6s/2.0s at 50 fps
    assert (second.start, second.end) == (250, 250 + 100)
    # No SetSetting fight: the rate was only read back, never written.
    assert ("timelineFrameRate", "25") not in imported.settings_set


def test_mode_append_never_touches_the_file_import() -> None:
    # mode="append" forced = exactly today's clip-by-clip behavior; the
    # whole historical append battery above runs pinned to it, and the file
    # import is never even attempted.
    bridge, project = make_bridge([standard_timeline()])
    warnings: list[str] = []
    name = bridge.build_timeline_from_plan(
        make_plan(), fps=24.0, warnings=warnings, mode="append"
    )
    assert name == "Monteur Montage"
    assert project.media_pool.timeline_import_calls == []
    assert warnings == []
    assert len(project.media_pool.appended) == 4


def test_unknown_build_mode_raises_before_any_resolve_work() -> None:
    bridge, project = make_bridge([standard_timeline()])
    with pytest.raises(ValueError) as excinfo:
        bridge.build_timeline_from_plan(make_plan(), fps=24.0, mode="teleport")
    assert "unknown build mode 'teleport'" in str(excinfo.value)
    assert project.media_pool.timeline_import_calls == []
    assert project.media_pool.import_calls == []
    assert project.media_pool.created_timeline_names == []


# --- hybrid build: worker + isolated wire format -----------------------------------


def test_worker_build_plan_mode_defaults_to_hybrid(monkeypatch) -> None:
    # Backward-compatible wire: an old request WITHOUT a "mode" key builds
    # hybrid — the default upgrade reaches every caller automatically.
    from monteur import _resolve_worker

    fake = _FakeBuildBridge()
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    request = build_plan_request()
    del request["mode"]
    response = _resolve_worker.handle("build_plan", request)
    assert response["ok"] is True
    assert fake.calls[0]["mode"] == "hybrid"
    # An explicit null means the same thing.
    request = build_plan_request(mode=None)
    _resolve_worker.handle("build_plan", request)
    assert fake.calls[1]["mode"] == "hybrid"


def test_worker_build_plan_forwards_mode(monkeypatch) -> None:
    from monteur import _resolve_worker

    fake = _FakeBuildBridge()
    monkeypatch.setattr(resolve, "connect", lambda app=None: fake)
    response = _resolve_worker.handle("build_plan", build_plan_request(mode="append"))
    assert response["ok"] is True
    assert fake.calls[0]["mode"] == "append"


def test_worker_build_plan_unknown_mode_is_clean(monkeypatch) -> None:
    from monteur import _resolve_worker

    bridge, _ = make_bridge([standard_timeline()])
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    response = _resolve_worker.handle(
        "build_plan", build_plan_request(mode="teleport")
    )
    assert response["ok"] is False
    assert "unknown build mode 'teleport'" in response["error"]


def test_worker_main_build_plan_hybrid_wire_round_trip(monkeypatch, capsys) -> None:
    # The real wire with the hybrid default: stdin JSON (no "mode" key) ->
    # real fake bridge -> the FCPXML was imported, nothing was appended.
    from monteur import _resolve_worker

    bridge, project = make_bridge([standard_timeline()])
    monkeypatch.setattr(resolve, "connect", lambda app=None: bridge)
    request = build_plan_request()
    del request["mode"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    code = _resolve_worker.main(["build_plan"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"ok": True, "timeline": "Monteur Montage", "warnings": []}
    pool = project.media_pool
    assert len(pool.timeline_import_calls) == 1
    assert pool.appended == []
    assert pool.created_timeline_names == []


def test_build_plan_isolated_sends_mode(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs.get("input")
        return _completed(0, json.dumps({"ok": True, "timeline": "T", "warnings": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolve.build_plan_isolated(make_plan(), fps=25.0)
    assert json.loads(captured["input"])["mode"] == "hybrid"  # the default
    resolve.build_plan_isolated(make_plan(), fps=25.0, mode="append")
    assert json.loads(captured["input"])["mode"] == "append"


# --- music through the dips (append build) ------------------------------------------


def test_build_append_music_spans_dips_as_one_clip() -> None:
    """A smash-to-black dip is a record gap on V1; the music append must
    stay ONE clip covering the whole montage — the song carries the title
    card, no surface may cut it at the dip."""
    bridge, project = make_bridge([standard_timeline()])
    pool = project.media_pool
    plan = MontagePlan(
        music_path="/music/song.wav",
        duration=4.4,
        entries=[
            MontageEntry(
                clip_path="/media/a.mov", source_start=1.0, source_end=3.0,
                record_start=0.0, record_end=2.0, score=1.0,
            ),
            # record gap 2.0..2.4 — the dip
            MontageEntry(
                clip_path="/media/b.mov", source_start=0.6, source_end=2.6,
                record_start=2.4, record_end=4.4, score=0.5,
            ),
        ],
        dips=[(2.0, 0.4)],
    )
    build_append(bridge, plan, fps=25.0)
    music = [
        c for c in pool.appended if c["mediaPoolItem"].path == "/music/song.wav"
    ]
    assert len(music) == 1  # ONE continuous bed, never per-gap pieces
    # full montage length (4.4s at 25 fps), positioned at record 0 — the
    # bed bridges the V1 gap the dip leaves at record 2.0..2.4
    assert (music[0]["startFrame"], music[0]["endFrame"]) == (0, 109)
    assert music[0]["recordFrame"] == 0
    # the video entries really leave the gap (record positions in frames)
    video = [c for c in pool.appended if c["mediaType"] == 1]
    assert [c["recordFrame"] for c in video] == [0, 60]
