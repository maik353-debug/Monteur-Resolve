import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from fable.project import Project
from fable.web.server import FableHandler, _APP_HTML

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def server(tmp_path):
    handler = type("TestHandler", (FableHandler,), {"project": Project(tmp_path)})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
        assert "Fable Studio" in body


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
        assert "TITLE: Fable Assembly" in data["content"]

    def test_plan_without_takes_is_400(self, server):
        payload = self._payload() | {"takes": []}
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/assembly/plan", payload)
        assert exc_info.value.code == 400
