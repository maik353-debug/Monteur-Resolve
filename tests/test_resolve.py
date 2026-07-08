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
        self.added_markers: list[tuple] = []
        self.fail_marker_frames: set[int] = set()

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
