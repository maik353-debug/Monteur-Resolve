import json
import os
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from monteur.project import Project
from monteur.web.server import (
    MonteurHandler,
    MonteurServer,
    _APP_HTML,
    _install_diagnostic_hooks,
    _restore_diagnostic_hooks,
    serve,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def server(tmp_path):
    handler = type("TestHandler", (MonteurHandler,), {"project": Project(tmp_path)})
    httpd = MonteurServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def _get(url):
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def _edl_payload(**extra):
    return {
        "filename": "sample.edl",
        "content": (FIXTURES / "sample.edl").read_text(),
        "fps": 25,
        **extra,
    }


class TestApi:
    def test_analyze_edl(self, server):
        data = _post(f"{server}/api/analyze", _edl_payload())
        assert data["stats"]["shot_count"] == 5
        assert data["stats"]["fps"] == 25

    def test_analyze_fcpxml(self, server):
        data = _post(
            f"{server}/api/analyze",
            {
                "filename": "sample.fcpxml",
                "content": (FIXTURES / "sample.fcpxml").read_text(),
            },
        )
        assert data["stats"]["shot_count"] > 0

    def test_analyze_edl_without_fps_is_400(self, server):
        payload = _edl_payload()
        del payload["fps"]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/analyze", payload)
        assert exc_info.value.code == 400
        assert "fps" in json.loads(exc_info.value.read())["error"]

    def test_compare(self, server):
        data = _post(
            f"{server}/api/compare", {"a": _edl_payload(), "b": _edl_payload()}
        )
        assert "verdict" in data["compare"]
        assert data["a"]["shot_count"] == data["b"]["shot_count"]

    def test_version_lifecycle(self, server):
        added = _post(f"{server}/api/versions", _edl_payload(label="rough v1"))
        vid = added["version"]["id"]
        assert added["version"]["label"] == "rough v1"

        versions = _get(f"{server}/api/versions")["versions"]
        assert [v["id"] for v in versions] == [vid]

        stats = _get(f"{server}/api/versions/{vid}")["stats"]
        assert stats["shot_count"] == 5

        request = urllib.request.Request(
            f"{server}/api/versions/{vid}", method="DELETE"
        )
        with urllib.request.urlopen(request) as response:
            assert json.loads(response.read())["ok"] is True
        assert _get(f"{server}/api/versions")["versions"] == []

    def test_unknown_route_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/nope")
        assert exc_info.value.code == 404

    def test_resolve_status_disconnected(self, server):
        data = _get(f"{server}/api/resolve/status")
        assert data["connected"] is False
        assert "error" in data

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_serves_app(self, server):
        with urllib.request.urlopen(f"{server}/") as response:
            body = response.read().decode()
        assert "Monteur Studio" in body


class TestAssemblyApi:
    def _payload(self):
        demo = Path(__file__).parent.parent / "examples" / "demo"
        return {
            "script": {
                "filename": "script.fountain",
                "content": (demo / "script.fountain").read_text(),
            },
            "takes": [
                {"filename": p.name, "content": p.read_text()}
                for p in sorted((demo / "takes").glob("*.srt"))
            ],
        }

    def test_plan(self, server):
        data = _post(f"{server}/api/assembly/plan", self._payload())
        assert data["coverage"] == 1.0
        assert len(data["plan"]["scenes"]) == 2
        assert data["screenplay"]["scenes"][0]["dialogue"]
        winner = data["plan"]["scenes"][0]["segments"][0]["take"]
        assert winner == "S1_T01"

    def test_plan_with_forced_take(self, server):
        payload = self._payload() | {"forced": {"0": "S1_T02"}}
        data = _post(f"{server}/api/assembly/plan", payload)
        assert data["plan"]["scenes"][0]["segments"][0]["take"] == "S1_T02"

    def test_export_edl(self, server):
        payload = self._payload() | {"format": "edl", "fps": 25}
        data = _post(f"{server}/api/assembly/export", payload)
        assert data["filename"].endswith(".edl")
        assert "TITLE: Monteur Assembly" in data["content"]

    def test_plan_without_takes_is_400(self, server):
        payload = self._payload() | {"takes": []}
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/assembly/plan", payload)
        assert exc_info.value.code == 400


def _wait_for_job(server, job_id, timeout=60.0, states=("done", "error", "cancelled")):
    """Poll GET /api/jobs/<id> until the job reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _get(f"{server}/api/jobs/{job_id}")
        if job["state"] in states:
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} still {job['state']!r} after {timeout}s")


class TestCreateApi:
    DEMO = "/tmp/claude-0/-home-user-Fable-tool/90401078-872b-52b4-9d55-214193ea4ea5/scratchpad/demo-footage"

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def _scan(self, server):
        data = _post(f"{server}/api/create/scan", {"folder": self.DEMO})
        assert isinstance(data["job"], str) and data["job"]
        return data["job"]

    def test_scan_job(self, server):
        job_id = self._scan(server)
        job = _wait_for_job(server, job_id)
        assert job["state"] == "done"
        assert job["kind"] == "scan"

        clips = job["result"]["clips"]
        assert len(clips) == 4
        by_name = {Path(c["path"]).name: c for c in clips}
        assert by_name["clip_B.mp4"]["usable_ratio"] < 1.0

        # Live per-clip progress: every clip fires a start and a done entry,
        # and done entries carry the clip's usable_ratio.
        stages = [p["stage"] for p in job["progress"]]
        assert stages.count("start") == 4
        assert stages.count("done") == 4
        done_entries = [p for p in job["progress"] if p["stage"] == "done"]
        assert all(0.0 <= p["usable_ratio"] <= 1.0 for p in done_entries)
        assert all(p["total"] == 4 for p in job["progress"])

    def test_build_reuses_scan_cache(self, server):
        scan_job = _wait_for_job(server, self._scan(server))
        assert scan_job["state"] == "done"

        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "travel", "fps": 25, "format": "edl"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "build"

        # The build must NOT sift again: the scan's reports are reused.
        assert {"stage": "cache", "name": "using previous scan"} in job["progress"]
        assert not any(p["stage"] in ("start", "done") for p in job["progress"])
        assert any(p["stage"] == "music" for p in job["progress"])

        result = job["result"]
        assert result["plan"]["cuts"] > 0
        assert result["plan"]["tempo"] > 0
        assert result["filename"].endswith(".edl")
        assert result["content"].startswith("TITLE:")
        assert any("travel" in n for n in result["plan"]["notes"])

    def test_build_forwards_pace(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "pace": 3, "format": "edl"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert any("cut pace ~3s" in n for n in job["result"]["plan"]["notes"])

    def test_build_forwards_canvas_and_transitions(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "canvas": "vertical", "transitions": "cuts", "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert 'width="1080"' in job["result"]["content"]
        assert 'height="1920"' in job["result"]["content"]
        assert any("hard cuts only" in n for n in job["result"]["plan"]["notes"])

    def test_build_unknown_style_is_error_job(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "vaporwave"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "valid styles" in job["message"]
        assert job["result"] is None

    def test_cancel_scan(self, server):
        job_id = self._scan(server)
        assert _post(f"{server}/api/jobs/{job_id}/cancel", {"why": "user"})["ok"] is True
        # A tiny demo dir may finish before the cancel lands — both terminal
        # states are acceptable; the endpoint contract must hold either way.
        job = _wait_for_job(server, job_id)
        assert job["state"] in ("cancelled", "done")
        if job["state"] == "done":
            assert len(job["result"]["clips"]) == 4
        else:
            assert job["result"] is None
        # Cancelling an already-finished job stays a no-op {"ok": true}.
        assert _post(f"{server}/api/jobs/{job_id}/cancel", {})["ok"] is True
        assert _get(f"{server}/api/jobs/{job_id}")["state"] == job["state"]

    def test_scan_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/scan", {})
        assert exc_info.value.code == 400

    def test_build_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/build", {"music": "song.wav"})
        assert exc_info.value.code == 400


class TestJobsApi:
    def test_unknown_job_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/jobs/deadbeef")
        assert exc_info.value.code == 404

    def test_cancel_unknown_job_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/jobs/deadbeef/cancel", {})
        assert exc_info.value.code == 404

    def test_scan_of_missing_folder_is_error_job(self, server):
        data = _post(f"{server}/api/create/scan", {"folder": "/no/such/folder"})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "not a directory" in job["message"]


def _fake_vision(fail_with=None):
    """A stand-in for monteur.vision, injected via sys.modules.

    The server resolves the vision module at CALL time with
    ``importlib.import_module("monteur.vision")`` inside the job thread —
    which honours ``sys.modules`` (unlike ``import a.b as c``, whose
    parent-attribute shortcut would keep returning the real module). So
    ``monkeypatch.setitem(sys.modules, "monteur.vision", fake)`` is a
    complete test hook: it works in the same process as the server threads
    and whether or not the real module exists on disk. No production code
    path changes for tests.
    """
    module = types.ModuleType("monteur.vision")

    class MonteurVisionError(RuntimeError):
        pass

    calls: list[int] = []

    def analyze_reports(reports, *, model=None, max_moments=48,
                        frame_height=360, progress=None, cache_path=None):
        calls.append(len(reports))
        if fail_with:
            raise MonteurVisionError(fail_with)
        total = len(reports)
        for i, report in enumerate(reports, start=1):
            name = Path(report.path).name
            if progress is not None:
                progress(i, total, name, "vision")
            for j, moment in enumerate(report.moments):
                moment.label = f"labeled moment {j} in {name}"
                moment.tags = ["outdoor", "demo"]
                moment.role = "opener" if j == 0 else ""
                moment.hero = 0.9 if j == 0 else 0.1
                moment.group = "demo"
        return [f"fake vision annotated {total} clips"]

    module.MonteurVisionError = MonteurVisionError
    module.analyze_reports = analyze_reports
    module.calls = calls
    return module


def _clear_scan_cache():
    from monteur.web import server as web_server

    with web_server._SCAN_CACHE_LOCK:
        web_server._SCAN_CACHE.clear()


class TestVisionApi:
    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()  # each test decides for itself whether a scan is cached

    def _scan_see(self, server):
        data = _post(f"{server}/api/create/scan", {"folder": self.DEMO, "see": True})
        return _wait_for_job(server, data["job"])

    def test_scan_with_see_annotates_clips(self, server, monkeypatch):
        fake = _fake_vision()
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        job = self._scan_see(server)
        assert job["state"] == "done"

        result = job["result"]
        assert result["vision_notes"] == ["fake vision annotated 4 clips"]
        assert "vision_error" not in result
        clips = result["clips"]
        assert len(clips) == 4
        assert any(clip["moments"] for clip in clips)
        for clip in clips:
            name = Path(clip["path"]).name
            for j, moment in enumerate(clip["moments"]):
                assert moment["label"] == f"labeled moment {j} in {name}"
                assert moment["tags"] == ["outdoor", "demo"]
                assert moment["role"] == ("opener" if j == 0 else "")
                assert moment["hero"] == (0.9 if j == 0 else 0.1)
                assert moment["group"] == "demo"

        vision_entries = [p for p in job["progress"] if p["stage"] == "vision"]
        assert len(vision_entries) == 4
        assert all(p["total"] == 4 for p in vision_entries)
        assert sorted(p["index"] for p in vision_entries) == [1, 2, 3, 4]
        assert {p["name"] for p in vision_entries} == {
            "clip_A.mp4", "clip_B.mp4", "clip_C.mp4", "clip_D.mp4",
        }

    def test_scan_with_see_vision_error_still_succeeds(self, server, monkeypatch):
        fake = _fake_vision(fail_with="anthropic package is not installed")
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        job = self._scan_see(server)
        assert job["state"] == "done"  # vision is an upgrade, not a gate

        result = job["result"]
        assert result["vision_error"] == "anthropic package is not installed"
        assert "vision_notes" not in result
        assert len(result["clips"]) == 4
        for clip in result["clips"]:  # the clips came through un-annotated
            for moment in clip["moments"]:
                assert not moment.get("label")
        assert not any(p["stage"] == "vision" for p in job["progress"])

    def test_scan_without_see_never_calls_vision(self, server, monkeypatch):
        fake = _fake_vision()
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        data = _post(f"{server}/api/create/scan", {"folder": self.DEMO})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert fake.calls == []
        assert "vision_notes" not in job["result"]
        assert "vision_error" not in job["result"]

    def test_build_with_see_reuses_annotated_scan(self, server, monkeypatch):
        fake = _fake_vision()
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        scan_job = self._scan_see(server)
        assert scan_job["state"] == "done"
        assert fake.calls == [4]

        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "see": True, "format": "edl"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        # Cache hit: the cached reports already carry the scan's annotations,
        # so vision ran exactly ONCE — during the scan.
        assert fake.calls == [4]
        assert {"stage": "cache", "name": "using previous scan"} in job["progress"]
        assert not any(p["stage"] == "vision" for p in job["progress"])
        assert "vision_error" not in job["result"]
        assert job["result"]["plan"]["cuts"] > 0

    def test_build_with_see_fresh_sift_runs_vision(self, server, monkeypatch):
        fake = _fake_vision()
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "see": True, "format": "edl"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert fake.calls == [4]  # cache miss -> vision ran before planning
        assert any(p["stage"] == "vision" for p in job["progress"])
        assert job["result"]["vision_notes"] == ["fake vision annotated 4 clips"]
        assert job["result"]["plan"]["cuts"] > 0

    def test_build_with_see_vision_error_still_builds(self, server, monkeypatch):
        fake = _fake_vision(fail_with="ANTHROPIC_API_KEY is not set")
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "see": True, "format": "edl"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"  # the build proceeds without vision
        result = job["result"]
        assert result["vision_error"] == "ANTHROPIC_API_KEY is not set"
        assert "vision_notes" not in result
        assert result["plan"]["cuts"] > 0
        assert result["content"].startswith("TITLE:")


class TestPickJobApi:
    """POST /api/create/pick — rank a folder of candidate songs."""

    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()  # each test decides for itself whether a scan is cached

    @pytest.fixture()
    def music_dir(self, tmp_path):
        import shutil

        songs = tmp_path / "songs"
        songs.mkdir()
        shutil.copy(Path(self.DEMO) / "song.wav", songs / "candidate.wav")
        return songs

    def _pick(self, server, music_dir, **extra):
        data = _post(
            f"{server}/api/create/pick",
            {"folder": self.DEMO, "music_dir": str(music_dir), **extra},
        )
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_pick_job_end_to_end(self, server, music_dir):
        job = self._pick(server, music_dir)
        assert job["state"] == "done"
        assert job["kind"] == "pick"

        ranking = job["result"]["ranking"]
        assert ranking
        top = ranking[0]
        assert top["name"] == "candidate.wav"
        assert top["path"].endswith("candidate.wav")
        assert 0.0 < top["score"] <= 1.0
        assert top["duration"] > 0
        assert top["parts"] and all(0.0 <= v <= 1.0 for v in top["parts"].values())
        assert top["reasons"]

        # Fresh sift: the usual per-clip entries, then one "song" entry per song.
        stages = [p["stage"] for p in job["progress"]]
        assert stages.count("start") == 4
        assert stages.count("done") == 4
        song_entries = [p for p in job["progress"] if p["stage"] == "song"]
        assert [p["name"] for p in song_entries] == ["candidate.wav"]
        assert song_entries[0]["index"] == 1 and song_entries[0]["total"] == 1

    def test_second_pick_reuses_scan_cache(self, server, music_dir):
        first = self._pick(server, music_dir)
        assert first["state"] == "done"

        job = self._pick(server, music_dir)
        assert job["state"] == "done"
        # The second pick must NOT sift again: cached reports are reused.
        assert {"stage": "cache", "name": "using previous scan"} in job["progress"]
        assert not any(p["stage"] in ("start", "done") for p in job["progress"])
        assert any(p["stage"] == "song" for p in job["progress"])
        assert job["result"]["ranking"]

    def test_pick_forwards_max_duration(self, server, music_dir):
        job = self._pick(server, music_dir, max_duration=1)
        assert job["state"] == "done"
        top = job["result"]["ranking"][0]
        assert any("covers the 1s target" in r for r in top["reasons"])

    def test_pick_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/pick", {"music_dir": "/somewhere"})
        assert exc_info.value.code == 400

    def test_pick_missing_music_dir_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/pick", {"folder": self.DEMO})
        assert exc_info.value.code == 400
        assert "music_dir" in json.loads(exc_info.value.read())["error"]

    def test_pick_nonexistent_music_dir_is_error_job(self, server):
        data = _post(
            f"{server}/api/create/pick",
            {"folder": self.DEMO, "music_dir": "/no/such/songs"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "is not a folder" in job["message"]
        assert job["result"] is None

    def test_pick_empty_music_dir_is_error_job(self, server, tmp_path):
        (tmp_path / "notes.txt").write_text("not audio")
        data = _post(
            f"{server}/api/create/pick",
            {"folder": self.DEMO, "music_dir": str(tmp_path)},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "no audio files" in job["message"]


class TestKitApi:
    """POST /api/create/kit — build's plan path + a publish kit on disk."""

    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def test_kit_job_end_to_end(self, server, tmp_path):
        kit_dir = tmp_path / "publish"
        data = _post(
            f"{server}/api/create/kit",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "travel", "fps": 25, "kit_dir": str(kit_dir)},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "kit"
        assert {"stage": "kit", "name": "writing publish kit"} in job["progress"]

        result = job["result"]
        assert result["kit_dir"] == str(kit_dir.resolve())
        assert result["notes"]
        assert any("publish kit" in n for n in result["notes"])

        # publish.md exists on disk AND is returned inline.
        on_disk = (kit_dir / "publish.md").read_text(encoding="utf-8")
        assert on_disk.startswith("# Publish kit")
        assert result["publish_md"] == on_disk

        # The demo clips are real media — thumbnails come back as base64 JPEGs.
        import base64

        assert result["thumbs"]
        assert len(result["thumbs"]) <= 6
        for thumb in result["thumbs"]:
            assert thumb["name"].endswith(".jpg")
            payload = base64.b64decode(thumb["data_b64"])
            assert payload[:2] == b"\xff\xd8"  # JPEG SOI
            assert (kit_dir / "thumbs" / thumb["name"]).is_file()

    def test_kit_reuses_scan_cache(self, server, tmp_path):
        scan = _post(f"{server}/api/create/scan", {"folder": self.DEMO})
        assert _wait_for_job(server, scan["job"])["state"] == "done"

        data = _post(
            f"{server}/api/create/kit",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "kit_dir": str(tmp_path / "kit")},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert {"stage": "cache", "name": "using previous scan"} in job["progress"]
        assert not any(p["stage"] in ("start", "done") for p in job["progress"])

    def test_kit_missing_kit_dir_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/kit",
                {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav"},
            )
        assert exc_info.value.code == 400
        assert "kit_dir" in json.loads(exc_info.value.read())["error"]

    def test_kit_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/kit", {"kit_dir": "/tmp/kit"})
        assert exc_info.value.code == 400

    def test_kit_unknown_style_is_error_job(self, server, tmp_path):
        data = _post(
            f"{server}/api/create/kit",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "vaporwave", "kit_dir": str(tmp_path / "kit")},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "valid styles" in job["message"]


class TestPickApi:
    @pytest.mark.skipif(
        bool(os.environ.get("DISPLAY")),
        reason="a display is available — a real dialog would open and block",
    )
    def test_pick_headless_soft_fallback(self, server):
        data = _post(f"{server}/api/pick", {"kind": "folder"})
        assert "path" not in data
        assert "paste the path" in data["error"]

    def test_pick_bad_kind_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/pick", {"kind": "clipboard"})
        assert exc_info.value.code == 400


def _host_port(url):
    """('http://127.0.0.1:1234') -> ('127.0.0.1', 1234)."""
    hostport = url.split("://", 1)[1]
    host, port = hostport.rsplit(":", 1)
    return host, int(port)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestCrashRobustness:
    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_serving_page_twice_survives(self, server):
        # Opening the page is exactly what crashed the real Windows process.
        # The server must survive serving it AND keep serving afterwards.
        with urllib.request.urlopen(f"{server}/") as r1:
            assert r1.status == 200
        with urllib.request.urlopen(f"{server}/") as r2:
            assert r2.status == 200

    def test_survives_aborted_connection(self, server):
        host, port = _host_port(server)
        # Send a partial/garbage request line, then slam the socket shut with a
        # hard RST (SO_LINGER 0) — this is what a browser closing a tab mid-load
        # looks like, and on Windows it raises ConnectionAbortedError in wfile.
        raw = socket.create_connection((host, port))
        raw.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER,
            __import__("struct").pack("ii", 1, 0),
        )
        raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")  # no blank line = partial
        raw.close()
        # A second, well-formed request must still be answered normally.
        data = _post(f"{server}/api/analyze", _edl_payload())
        assert data["stats"]["shot_count"] == 5

    def test_handle_error_does_not_raise(self, server):
        host, port = _host_port(server)
        # Build a throwaway server instance just to call handle_error on it.
        handler = type("H", (MonteurHandler,), {})
        httpd = MonteurServer(("127.0.0.1", _free_port()), handler)
        try:
            # Must be called from within an active exception context (it reads
            # sys.exc_info()); it must print and NOT raise.
            try:
                raise ConnectionAbortedError(10053, "aborted")
            except ConnectionAbortedError:
                httpd.handle_error(object(), ("127.0.0.1", 12345))
        finally:
            httpd.server_close()

    def test_server_is_hardened(self, server):
        assert MonteurServer.daemon_threads is True
        assert MonteurServer.allow_reuse_address is True

    def test_install_restore_hooks_roundtrip(self):
        orig_thread_hook = threading.excepthook
        orig_sys_hook = __import__("sys").excepthook
        prev = _install_diagnostic_hooks()
        try:
            assert threading.excepthook is not orig_thread_hook
            assert __import__("sys").excepthook is not orig_sys_hook
        finally:
            _restore_diagnostic_hooks(*prev)
        assert threading.excepthook is orig_thread_hook
        assert __import__("sys").excepthook is orig_sys_hook

    def test_serve_restores_threading_excepthook(self, tmp_path):
        original = threading.excepthook
        captured = {}
        ready = threading.Event()

        def grab(srv):
            captured["srv"] = srv

        t = threading.Thread(
            target=serve,
            kwargs={
                "port": _free_port(),
                "project_root": str(tmp_path),
                "open_browser": False,
                "ready": ready,
                "on_bind": grab,
            },
            daemon=True,
        )
        t.start()
        assert ready.wait(timeout=5)
        # While serving, our diagnostic hook is installed (not the original).
        assert threading.excepthook is not original
        captured["srv"].shutdown()
        t.join(timeout=5)
        assert not t.is_alive()
        # serve()'s finally block must have restored the original.
        assert threading.excepthook is original


# --- /api/movie/* — the Studio's Movie view -----------------------------------------


def _movie_project(n_scenes=3, folders=()):
    from monteur.movie import MovieProject, MovieScene

    scenes = []
    for i in range(n_scenes):
        scene = MovieScene(
            number=i + 1,
            heading="EXT. WALDWEG - NIGHT" if i % 2 else "INT. AUTO - NIGHT",
            summary=f"Szene {i + 1} treibt die Geschichte voran.",
            action="Scheinwerfer schneiden durch den Nebel.",
            shooting_tips=["Kamera tief halten", "2 Takes"],
            sound_notes="Motor separat aufnehmen.",
            cut_intent="ruhig halten",
        )
        if i < len(folders) and folders[i]:
            scene.folder = folders[i]
            scene.status = "assigned"
        scenes.append(scene)
    return MovieProject(
        title="Nachtfahrt", genre="thriller", brief="Wald und Auto, nachts",
        logline="Ein Fahrer, ein Wald, ein Geheimnis.", scenes=scenes,
    )


def _write_movie_project(project_dir, n_scenes=3, folders=()):
    from monteur.movie import save_project

    save_project(_movie_project(n_scenes, folders), project_dir)
    return str(project_dir)


class TestMovieApi:
    def test_load_missing_project_dir_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/movie/load", {})
        assert exc_info.value.code == 400
        assert "project_dir" in json.loads(exc_info.value.read())["error"]

    def test_load_without_movie_json_is_400_with_load_error(self, server, tmp_path):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/movie/load", {"project_dir": str(tmp_path)})
        assert exc_info.value.code == 400
        error = json.loads(exc_info.value.read())["error"]
        assert "movie.json" in error and "movie new" in error

    def test_load_happy_path(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj", folders=["/f1"])
        data = _post(f"{server}/api/movie/load", {"project_dir": project_dir})
        assert data["project"]["title"] == "Nachtfahrt"
        assert data["project"]["monteur_movie"] == 1
        assert len(data["project"]["scenes"]) == 3
        assert data["project"]["scenes"][0]["folder"] == "/f1"
        assert data["progress"] == {"scenes": 3, "assigned": 1, "percent": 33}

    def test_new_missing_brief_is_400(self, server, tmp_path):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/movie/new", {"project_dir": str(tmp_path)})
        assert exc_info.value.code == 400
        assert "brief" in json.loads(exc_info.value.read())["error"]

    def test_new_with_fake_generate_movie(self, server, tmp_path, monkeypatch):
        """The movie job resolves generate_movie at call time — patchable."""
        import monteur.movie as movie_module

        seen = {}

        def fake_generate(brief, genre="", model=None):
            seen["brief"], seen["genre"] = brief, genre
            return _movie_project()

        monkeypatch.setattr(movie_module, "generate_movie", fake_generate)
        project_dir = tmp_path / "neu"
        data = _post(
            f"{server}/api/movie/new",
            {"project_dir": str(project_dir), "brief": "Waldthriller",
             "genre": "thriller"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "movie"
        assert seen == {"brief": "Waldthriller", "genre": "thriller"}
        assert {"stage": "movie", "name": "drafting the screenplay"} in job["progress"]

        result = job["result"]
        assert result["project"]["title"] == "Nachtfahrt"
        assert result["progress"] == {"scenes": 3, "assigned": 0, "percent": 0}
        assert [Path(p).name for p in result["paths"]] == [
            "movie.json", "script.fountain", "shotlist.md",
        ]
        assert all(Path(p).is_file() for p in result["paths"])
        # and the saved project loads back through the load endpoint
        loaded = _post(f"{server}/api/movie/load", {"project_dir": str(project_dir)})
        assert loaded["project"]["title"] == "Nachtfahrt"

    def test_new_ai_error_is_job_error(self, server, tmp_path, monkeypatch):
        """A screenplay has no offline fallback — MonteurAIError fails the job."""
        import monteur.movie as movie_module
        from monteur.ai import MonteurAIError

        def fail(brief, genre="", model=None):
            raise MonteurAIError("install the AI extra: pip install 'monteur[ai]'")

        monkeypatch.setattr(movie_module, "generate_movie", fail)
        data = _post(
            f"{server}/api/movie/new",
            {"project_dir": str(tmp_path / "neu"), "brief": "Waldthriller"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "monteur[ai]" in job["message"]
        assert job["result"] is None

    def test_assign_persists_to_disk(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj")
        data = _post(
            f"{server}/api/movie/assign",
            {"project_dir": project_dir, "scene": 2, "folder": "/footage/wald"},
        )
        assert data["project"]["scenes"][1]["folder"] == "/footage/wald"
        assert data["project"]["scenes"][1]["status"] == "assigned"
        assert data["progress"] == {"scenes": 3, "assigned": 1, "percent": 33}

        # a fresh load reads the assignment back from movie.json on disk
        loaded = _post(f"{server}/api/movie/load", {"project_dir": project_dir})
        assert loaded["project"]["scenes"][1]["folder"] == "/footage/wald"
        assert loaded["progress"]["assigned"] == 1

    def test_assign_empty_folder_unassigns(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj", folders=["/f1"])
        data = _post(
            f"{server}/api/movie/assign",
            {"project_dir": project_dir, "scene": 1, "folder": ""},
        )
        assert data["project"]["scenes"][0]["status"] == "planned"
        assert data["progress"]["assigned"] == 0

    def test_assign_unknown_scene_is_400(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/movie/assign",
                {"project_dir": project_dir, "scene": 9, "folder": "/f"},
            )
        assert exc_info.value.code == 400
        assert "no scene 9" in json.loads(exc_info.value.read())["error"]

    def test_assign_bad_project_is_400(self, server, tmp_path):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/movie/assign",
                {"project_dir": str(tmp_path), "scene": 1, "folder": "/f"},
            )
        assert exc_info.value.code == 400


class TestMovieCheckApi:
    """POST /api/movie/check — sift a scene's folder and judge the match."""

    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def _project_with_demo(self, tmp_path):
        return _write_movie_project(tmp_path / "proj", folders=[self.DEMO])

    def _check(self, server, project_dir, scene=1, **extra):
        data = _post(
            f"{server}/api/movie/check",
            {"project_dir": project_dir, "scene": scene, **extra},
        )
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_check_end_to_end(self, server, tmp_path):
        project_dir = self._project_with_demo(tmp_path)
        job = self._check(server, project_dir)
        assert job["state"] == "done"
        assert job["kind"] == "scene-check"

        # the usual per-clip sift progress ran
        stages = [p["stage"] for p in job["progress"]]
        assert stages.count("start") == 4
        assert stages.count("done") == 4

        result = job["result"]
        assert result["scene"] == 1
        check = result["check"]
        assert check["clips"] == 4
        assert 0.0 <= check["avg_usable"] <= 1.0
        assert check["content_checked"] is False  # no vision annotations
        assert check["score"] == 0.5
        assert any("4 clips" in f for f in check["findings"])
        assert any("monteur see" in f for f in check["findings"])
        clips = result["clips"]
        assert {c["name"] for c in clips} == {
            "clip_A.mp4", "clip_B.mp4", "clip_C.mp4", "clip_D.mp4",
        }
        assert all(0.0 <= c["usable_ratio"] <= 1.0 for c in clips)
        assert "vision_error" not in result

    def test_second_check_reuses_scan_cache(self, server, tmp_path):
        project_dir = self._project_with_demo(tmp_path)
        first = self._check(server, project_dir)
        assert first["state"] == "done"

        job = self._check(server, project_dir)
        assert job["state"] == "done"
        assert {"stage": "cache", "name": "using previous scan"} in job["progress"]
        assert not any(p["stage"] in ("start", "done") for p in job["progress"])
        assert job["result"]["check"]["clips"] == 4

    def test_check_with_see_uses_vision_annotations(self, server, tmp_path, monkeypatch):
        fake = _fake_vision()
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        project_dir = self._project_with_demo(tmp_path)
        job = self._check(server, project_dir, see=True)
        assert job["state"] == "done"
        assert fake.calls == [4]
        assert any(p["stage"] == "vision" for p in job["progress"])
        check = job["result"]["check"]
        assert check["content_checked"] is True
        assert "vision_error" not in job["result"]

    def test_check_with_see_vision_error_still_checks(self, server, tmp_path, monkeypatch):
        fake = _fake_vision(fail_with="ANTHROPIC_API_KEY is not set")
        monkeypatch.setitem(sys.modules, "monteur.vision", fake)
        project_dir = self._project_with_demo(tmp_path)
        job = self._check(server, project_dir, see=True)
        assert job["state"] == "done"  # vision stays an upgrade, not a gate
        assert job["result"]["vision_error"] == "ANTHROPIC_API_KEY is not set"
        assert job["result"]["check"]["content_checked"] is False

    def test_check_unassigned_scene_is_error_job(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj")  # nothing assigned
        job = self._check(server, project_dir, scene=2)
        assert job["state"] == "error"
        assert "no footage folder assigned" in job["message"]

    def test_check_unknown_scene_is_error_job(self, server, tmp_path):
        project_dir = self._project_with_demo(tmp_path)
        job = self._check(server, project_dir, scene=99)
        assert job["state"] == "error"
        assert "no scene 99" in job["message"]

    def test_check_missing_scene_is_400(self, server, tmp_path):
        project_dir = self._project_with_demo(tmp_path)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/movie/check", {"project_dir": project_dir})
        assert exc_info.value.code == 400
