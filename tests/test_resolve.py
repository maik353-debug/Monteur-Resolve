from __future__ import annotations

import pytest

import fable.resolve as resolve
from fable.model import AUDIO, VIDEO
from fable.resolve import FableResolveError, ResolveBridge, connect


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


class FakeMediaPool:
    def __init__(self) -> None:
        self.imported_timelines: list[str] = []
        self.imported_media: list[str] = []
        self.fail_timeline_import = False

    def ImportTimelineFromFile(self, path: str):
        if self.fail_timeline_import:
            return None
        self.imported_timelines.append(path)
        return FakeTimeline(name="Imported")

    def ImportMedia(self, paths: list[str]):
        self.imported_media.extend(paths)
        return [object() for _ in paths]


class FakeProject:
    def __init__(
        self,
        name: str = "Fable Feature",
        timelines: list[FakeTimeline] | None = None,
        current: FakeTimeline | None = None,
    ) -> None:
        self._name = name
        self._timelines = timelines or []
        self._current = current if current is not None else (
            self._timelines[0] if self._timelines else None
        )
        self.media_pool = FakeMediaPool()

    def GetName(self) -> str:
        return self._name

    def GetTimelineCount(self) -> int:
        return len(self._timelines)

    def GetTimelineByIndex(self, index: int) -> FakeTimeline:
        return self._timelines[index - 1]

    def GetCurrentTimeline(self) -> FakeTimeline | None:
        return self._current

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

    module = importlib.import_module("fable.resolve")
    assert module is resolve


def test_connect_without_resolve_raises() -> None:
    with pytest.raises(FableResolveError) as excinfo:
        connect()
    assert "DaVinciResolveScript" in str(excinfo.value)


def test_connect_with_injected_app() -> None:
    bridge = connect(app=FakeResolve(FakeProject(timelines=[standard_timeline()])))
    assert isinstance(bridge, ResolveBridge)
    assert bridge.project_name() == "Fable Feature"


def test_connect_rejects_none_app_object() -> None:
    with pytest.raises(FableResolveError):
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
    with pytest.raises(FableResolveError):
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
    with pytest.raises(FableResolveError) as excinfo:
        bridge.read_timeline("Nope")
    assert "Nope" in str(excinfo.value)
    assert "Cut 1" in str(excinfo.value)


def test_read_timeline_without_current_raises() -> None:
    bridge, _ = make_bridge([], current=None)
    with pytest.raises(FableResolveError):
        bridge.read_timeline()


def test_no_current_project_raises() -> None:
    bridge = ResolveBridge(FakeResolve(None))
    with pytest.raises(FableResolveError):
        bridge.project_name()


def test_import_timeline_file() -> None:
    bridge, project = make_bridge([standard_timeline()])
    assert bridge.import_timeline_file("/edits/cut.edl") is True
    assert project.media_pool.imported_timelines == ["/edits/cut.edl"]


def test_import_timeline_file_failure_raises() -> None:
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.fail_timeline_import = True
    with pytest.raises(FableResolveError) as excinfo:
        bridge.import_timeline_file("/edits/broken.edl")
    assert "/edits/broken.edl" in str(excinfo.value)


def test_import_media_returns_count() -> None:
    bridge, project = make_bridge([standard_timeline()])
    count = bridge.import_media(["/media/a.mov", "/media/b.wav"])
    assert count == 2
    assert project.media_pool.imported_media == ["/media/a.mov", "/media/b.wav"]
