import json
import os
import socket
import threading
import time
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
