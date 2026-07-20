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
    """A Fusion tool; reg_id 'TextPlus' makes it the Text+ tool."""

    def __init__(self, reg_id: str = "TextPlus") -> None:
        self._reg_id = reg_id
        self.inputs: dict = {}

    def GetAttrs(self) -> dict:
        return {"TOOLS_RegID": self._reg_id}

    def SetInput(self, name: str, value) -> None:
        self.inputs[name] = value


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
        self.added_markers: list[tuple] = []
        self.fail_marker_frames: set[int] = set()
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
        assert key == "timelineFrameRate"
        return self._fps

    def GetStartFrame(self) -> int:
        return self._start_frame

    def GetTrackCount(self, kind: str) -> int:
        return len(self._tracks[kind])

    def GetItemListInTrack(self, kind: str, index: int) -> list[FakeItem]:
        return self._tracks[kind][index - 1]

    def GetMarkers(self) -> dict:
        return self._markers


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
        self.import_media_result: list | None | str = "default"
        self.fail_append = False
        self.appended: list[dict] = []
        self.created_timeline_names: list[str] = []

    def ImportTimelineFromFile(self, path: str):
        if self.fail_timeline_import:
            return None
        self.imported_timelines.append(path)
        return FakeTimeline(name="Imported")

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
        self.appended.extend(clip_infos)
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

    def GetName(self) -> str:
        return self._name

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

    def GetProjectManager(self) -> FakeProjectManager:
        return self._manager


def make_bridge(
    timelines: list[FakeTimeline], current: FakeTimeline | None = None
) -> tuple[ResolveBridge, FakeProject]:
    project = FakeProject(timelines=timelines, current=current)
    return ResolveBridge(FakeResolve(project)), project


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
    name = bridge.build_timeline_from_plan(make_plan(), fps=24.0)
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
    bridge.build_timeline_from_plan(make_plan(), fps=25.0)
    frames = [(c["startFrame"], c["endFrame"]) for c in pool.appended]
    assert frames == [(25, 74), (15, 39), (125, 149), (0, 99)]


def test_build_timeline_from_plan_uniquifies_name() -> None:
    taken = FakeTimeline(name="Monteur Montage", start_frame=0)
    taken2 = FakeTimeline(name="Monteur Montage 2", start_frame=0)
    bridge, project = make_bridge([taken, taken2])
    name = bridge.build_timeline_from_plan(make_plan(), fps=24.0)
    assert name == "Monteur Montage 3"
    assert project.media_pool.created_timeline_names == ["Monteur Montage 3"]
    assert bridge.list_timelines() == [
        "Monteur Montage", "Monteur Montage 2", "Monteur Montage 3",
    ]


def test_build_timeline_from_plan_custom_name() -> None:
    bridge, _ = make_bridge([standard_timeline()])
    assert bridge.build_timeline_from_plan(make_plan(), 24.0, name="Holiday") == "Holiday"


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
    name = bridge.build_timeline_from_plan(make_plan(), fps=24.0)
    assert name == "Monteur Montage"
    assert pool.appended[0]["mediaPoolItem"].path == "/elsewhere/a.mov"
    assert pool.appended[3]["mediaPoolItem"].path == "/elsewhere/song.wav"


def test_build_timeline_import_none_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.import_media_result = None
    with pytest.raises(MonteurResolveError) as excinfo:
        bridge.build_timeline_from_plan(make_plan(), fps=24.0)
    assert "/media/a.mov" in str(excinfo.value)


def test_build_timeline_import_empty_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.import_media_result = []
    with pytest.raises(MonteurResolveError):
        bridge.build_timeline_from_plan(make_plan(), fps=24.0)


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
        bridge.build_timeline_from_plan(make_plan(), fps=24.0)
    assert "/music/song.wav" in str(excinfo.value)


def test_build_timeline_append_failure_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.fail_append = True
    with pytest.raises(MonteurResolveError) as excinfo:
        bridge.build_timeline_from_plan(make_plan(), fps=24.0)
    assert "append" in str(excinfo.value)


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
        {"start": 2.6, "duration": 2.0, "text": "the mountain pass"},
        {"start": 5.0, "duration": 2.0, "text": "Title"},
    ]


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


def test_build_timeline_from_plan_inserts_titles_with_dip_shift() -> None:
    bridge, project = make_bridge([standard_timeline()])
    plan = trailer_plan()
    titles = resolve.titles_from_plan(plan, texts=["ONE", "TWO"])
    bridge.build_timeline_from_plan(plan, fps=25.0, titles=titles)
    created = project._timelines[-1]  # the montage timeline (now current)
    assert created.inserted_fusion_titles == ["Text+", "Text+"]
    # A title track was added above the montage footage.
    assert created.added_tracks == ["video"]
    first, second = created.created_title_items
    # Entries are appended back-to-back, so the dips do not exist in Resolve:
    # title 1 stays at its own dip's start (2.6s -> frame 65); title 2 shifts
    # left by dip 1's 0.4s (5.0 - 0.4 = 4.6s -> frame 115).
    assert (first.start, first.end) == (65, 65 + 50)
    assert (second.start, second.end) == (115, 115 + 50)
    assert first._comp._tools[0].inputs["StyledText"] == "ONE"
    assert second._comp._tools[0].inputs["StyledText"] == "TWO"


def test_build_timeline_from_plan_without_titles_adds_none() -> None:
    bridge, project = make_bridge([standard_timeline()])
    bridge.build_timeline_from_plan(make_plan(), fps=24.0)
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
    assert "3.6" in result["error"] and "3.11" in result["error"]


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
    assert "3.11" in message


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
    assert "3.11" in d["verdict"]


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
    # The well-known path list is pure string building — 3.11 comes first.
    wellknown = resolve._windows_wellknown_pythons()
    assert wellknown[0].endswith(os.path.join("Python311", "python.exe"))
    assert all(p.endswith("python.exe") for p in wellknown)
    assert any("Python36" in p for p in wellknown)


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
        self, plan, fps, name="Monteur Montage", titles=None, warnings=None
    ):
        self.calls.append(
            {"plan": plan, "fps": fps, "name": name, "titles": titles}
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
        {"start": 2.6, "duration": 2.0, "text": "the mountain pass"},
        {"start": 5.0, "duration": 2.0, "text": "Title"},
    ]


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
    assert "3.6" in result["error"] and "3.11" in result["error"]


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


def _run_cmd_create_into_resolve(monkeypatch, plan, build_result):
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

    def fake_build(plan, fps, name="Monteur Montage", titles=None, timeout=180.0):
        calls.append({"plan": plan, "fps": fps, "name": name, "titles": titles})
        return build_result

    monkeypatch.setattr(resolve, "build_plan_isolated", fake_build)
    args = build_parser().parse_args(["create", "clips", "song.mp3", "--into-resolve"])
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
    out = capsys.readouterr().out
    assert "3 cuts -> Resolve timeline 'Monteur Montage' (8.0s at 25 fps)" in out
    assert "drag it onto the black gap at 4.6s." in out  # warnings are printed


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
