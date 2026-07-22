import json
import os
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
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

from _demo import DEMO as _DEMO_FOOTAGE

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def _proxy_cache_dir(tmp_path_factory):
    """ONE proxy cache for the whole test session: every scan job kicks the
    background proxy transcodes, and sharing the cache means the demo clips
    are transcoded once and every later scan's proxies job is a near-instant
    skip-when-fresh pass instead of 4 fresh ffmpeg runs per test. The env
    var is set at SESSION scope (os.environ directly) because the proxies
    job outlives its test: a daemon thread must never see the variable
    flicker off between tests and write into the real ~/.monteur/proxies."""
    cache = tmp_path_factory.mktemp("proxy-cache")
    previous = os.environ.get("MONTEUR_PROXIES_PATH")
    os.environ["MONTEUR_PROXIES_PATH"] = str(cache)
    yield cache
    if previous is None:
        os.environ.pop("MONTEUR_PROXIES_PATH", None)
    else:
        os.environ["MONTEUR_PROXIES_PATH"] = previous


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch, _proxy_cache_dir):
    """Every web test gets scratch settings AND drafts files — the server
    reads monteur.settings per request and autosaves drafts after builds,
    and tests must never touch (or depend on) the developer's real
    ~/.monteur/settings.json or ~/.monteur/drafts.json. The proxy cache is
    isolated too (session-scoped — see _proxy_cache_dir): scans kick real
    background transcodes, and those must never land in ~/.monteur/proxies."""
    monkeypatch.setenv(
        "MONTEUR_SETTINGS_PATH", str(tmp_path / "web-settings.json")
    )
    monkeypatch.setenv(
        "MONTEUR_DRAFTS_PATH", str(tmp_path / "web-drafts.json")
    )
    # First-class projects (blueprint: unified project model): the server
    # lists/creates/migrates projects under this root — tests must never
    # touch (or depend on) the developer's real ~/.monteur/projects.
    monkeypatch.setenv(
        "MONTEUR_PROJECTS_PATH", str(tmp_path / "web-projects")
    )
    # Learned preferences (blueprint 4.3): the server folds them into every
    # build and records correction signals on /api/plan/adjust — tests must
    # never touch (or depend on) the developer's real preferences.json.
    monkeypatch.setenv(
        "MONTEUR_PREFERENCES_PATH", str(tmp_path / "web-preferences.json")
    )
    monkeypatch.setenv("MONTEUR_PROXIES_PATH", str(_proxy_cache_dir))


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
    DEMO = str(_DEMO_FOOTAGE)

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

    def test_build_without_music_carries_clip_sound(self, server):
        # Field bug: "built without music — the sound track is missing
        # entirely". Without an explicit audio mode, a no-music build
        # resolves to "original": the clips' own sound rides in the export
        # instead of the job failing on a missing song.
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "max_duration": 10, "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        result = job["result"]
        assert result["plan_json"]["music_path"] == ""
        assert 'hasAudio="1"' in result["content"]  # the clips' own sound
        assert "song.wav" not in result["content"]  # no phantom song

    def test_build_forwards_music_window(self, server):
        # The adaptive-window override passes through untouched; the
        # engine validates, snaps to the song's own grid and notes it.
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "travel", "music_window": [5.0, 0],
             "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        plan_json = job["result"]["plan_json"]
        assert 3.5 <= plan_json["music_in"] <= 6.5  # 5s snapped to the grid
        assert any(
            "music window" in n and "your setting" in n
            for n in plan_json["notes"]
        )

    def test_series_builds_distinct_shorts(self, server):
        # One tour -> several Shorts, each a full plan, no moment repeated.
        data = _post(
            f"{server}/api/create/series",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav", "series": 2},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "series"
        shorts = job["result"]["shorts"]
        assert 1 <= len(shorts) <= 2  # honest degradation if too few seeds
        seen = set()
        for i, short in enumerate(shorts):
            assert short["index"] == i
            assert short["plan_json"]["monteur_plan"]  # a real save-plan
            assert short["cuts"] >= 1 and short["duration"] > 0
            assert "clip_path" in short["seed"]
            # the headline promise: no source moment appears in two shorts
            for e in short["plan_json"]["entries"]:
                key = (e["clip_path"], round(e["source_start"], 2), round(e["source_end"], 2))
                assert key not in seen, "a moment repeated across the series"
                seen.add(key)
        if len(shorts) == 2:
            assert shorts[0]["seed"]["clip_path"] != shorts[1]["seed"]["clip_path"] \
                or shorts[0]["seed"]["start"] != shorts[1]["seed"]["start"]

    def test_series_needs_at_least_two(self, server):
        data = _post(
            f"{server}/api/create/series",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav", "series": 1},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "at least 2" in job["message"]

    def test_build_forwards_music_flow(self, server):
        # "continuous" passes through untouched — the engine plans zero
        # deliberate silences and serializes without the music_gaps key.
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "trailer", "sfx": True,
             "music_flow": "continuous", "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        plan_json = job["result"]["plan_json"]
        assert "music_gaps" not in plan_json
        assert not any(n.startswith("silence:") for n in plan_json["notes"])

    def test_build_unknown_music_flow_is_error_job(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "music_flow": "sometimes"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "unknown music_flow" in job["message"]

    def test_build_music_window_without_music_is_error_job(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "max_duration": 10,
             "music_window": [2.0, 0]},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "music_window needs music" in job["message"]

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

    def test_build_autosaves_the_draft_slot(self, server):
        """A successful build fills the drafts autosave slot — the whole
        point of reload-safety: after the browser comes back, GET
        /api/drafts offers the last good cut, plan_json retrievable."""
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "travel", "fps": 25, "canvas": "hd", "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"

        drafts = _get(f"{server}/api/drafts")["drafts"]
        autos = [d for d in drafts if d.get("autosave")]
        assert len(autos) == 1
        auto = autos[0]
        assert auto["id"] == "autosave"
        assert auto["folder"] == self.DEMO
        assert auto["music"] == f"{self.DEMO}/song.wav"
        assert auto["settings"]["style"] == "travel"
        assert auto["settings"]["canvas"] == "hd"
        assert auto["summary"]["cuts"] == job["result"]["plan"]["cuts"]
        assert auto["summary"]["style"] == "travel"
        assert "plan_json" not in auto  # the list stays light

        # The full record carries the plan — and it is the build's plan.
        full = _get(f"{server}/api/drafts/autosave")
        assert full["plan_json"] == job["result"]["plan_json"]

        # The stored plan exports to a valid timeline WITHOUT re-planning.
        exported = _post(
            f"{server}/api/create/export",
            {"plan_json": full["plan_json"], "fps": 25,
             "audio": "music", "canvas": "hd", "format": "fcpxml"},
        )
        assert exported["filename"].endswith(".fcpxml")
        assert "<fcpxml" in exported["content"]
        assert exported["plan"]["cuts"] == job["result"]["plan"]["cuts"]
        assert exported["plan_json"]["entries"] == full["plan_json"]["entries"]

    def test_revise_autosaves_the_revised_plan(self, server):
        build = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "format": "fcpxml"},
        )
        build_job = _wait_for_job(server, build["job"])
        assert build_job["state"] == "done"
        revise = _post(
            f"{server}/api/create/revise",
            {"plan_json": build_job["result"]["plan_json"],
             "folder": self.DEMO, "brief": "calmer second half"},
        )
        revise_job = _wait_for_job(server, revise["job"])
        assert revise_job["state"] == "done"
        # The autosave slot now holds the REVISED plan, not the build's.
        full = _get(f"{server}/api/drafts/autosave")
        assert full["plan_json"] == revise_job["result"]["plan_json"]


class TestArrangeApi:
    """The Arrange step's server side: payload validation, forwarding to
    plan_montage, and drafts persistence of the arrangement."""

    DEMO = str(_DEMO_FOOTAGE)

    def _payload(self, **extra):
        return {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
                "format": "edl", **extra}

    # -- request-time validation (no footage needed: 400 before any job) --

    def test_arrangement_must_be_a_list(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/build",
                  {"folder": "/x", "arrangement": {"clip": "a.mp4"}})
        assert exc_info.value.code == 400
        assert "must be a list" in exc_info.value.read().decode()

    def test_arrangement_scene_needs_clip_and_numeric_start(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/build",
                  {"folder": "/x", "arrangement": [{"start": 1.0}]})
        assert exc_info.value.code == 400
        assert "missing 'clip'" in exc_info.value.read().decode()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/build",
                  {"folder": "/x",
                   "arrangement": [{"clip": "a.mp4", "start": "soon"}]})
        assert exc_info.value.code == 400
        assert "'start' must be a number" in exc_info.value.read().decode()

    def test_arrangement_rejects_unknown_transition_and_sfx(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/build",
                  {"folder": "/x",
                   "arrangement": [
                       {"clip": "a.mp4", "start": 0,
                        "after": {"transition": "wipe"}}]})
        assert exc_info.value.code == 400
        assert "cut, dissolve, smash" in exc_info.value.read().decode()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/build",
                  {"folder": "/x",
                   "arrangement": [{"clip": "a.mp4", "start": 0, "sfx": "boom"}]})
        assert exc_info.value.code == 400
        assert "impact, whoosh, riser" in exc_info.value.read().decode()

    def test_kit_validates_the_arrangement_too(self, server, tmp_path):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/kit",
                  {"folder": "/x", "kit_dir": str(tmp_path),
                   "arrangement": "not-a-list"})
        assert exc_info.value.code == 400

    def test_empty_arrangement_is_simply_dropped(self, server):
        # [] / null means "not arranged" — the build must not 400 on it
        # (it fails later on the bogus folder, as any build would).
        data = _post(f"{server}/api/create/build",
                     {"folder": "/nonexistent-folder", "arrangement": []})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "arrangement" not in job["message"]

    # -- forwarding into the engine (needs the demo footage) --

    @pytest.fixture()
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def test_build_forwards_arrangement_and_reports_it(self, server, _needs_demo_media):
        data = _post(
            f"{server}/api/create/build",
            self._payload(
                max_duration=20,
                arrangement=[
                    {"clip": "clip_C.mp4", "start": 0.0,
                     "after": {"transition": "smash"}, "end": 4.0,
                     "label": "display extras ride along"},
                    {"clip": "clip_A.mp4", "start": 0.0,
                     "after": {"transition": "dissolve"}},
                    {"clip": "clip_D.mp4", "start": 0.0},
                ],
            ),
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        plan_json = job["result"]["plan_json"]
        entries = sorted(plan_json["entries"], key=lambda e: e["record_start"])
        assert Path(entries[0]["clip_path"]).name == "clip_C.mp4"
        assert Path(entries[1]["clip_path"]).name == "clip_A.mp4"
        assert Path(entries[2]["clip_path"]).name == "clip_D.mp4"
        notes = "\n".join(job["result"]["plan"]["notes"])
        assert "arrangement:" in notes
        assert "follow your order" in notes
        assert plan_json["dips"], "the smash boundary must dip to black"

    def test_unknown_arranged_clip_is_a_job_error_naming_it(self, server, _needs_demo_media):
        data = _post(
            f"{server}/api/create/build",
            self._payload(arrangement=[{"clip": "ghost.mp4", "start": 0.0}]),
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "ghost.mp4" in job["message"]

    def test_build_autosave_remembers_the_arrangement(self, server, _needs_demo_media):
        arrangement = [{"clip": "clip_A.mp4", "start": 0.0,
                        "after": {"transition": "cut"}}]
        data = _post(
            f"{server}/api/create/build",
            self._payload(arrangement=arrangement),
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        full = _get(f"{server}/api/drafts/autosave")
        assert full["settings"]["arrangement"] == arrangement

    def test_drafts_round_trip_the_arrangement(self, server):
        arrangement = [
            {"clip": "/footage/a.mp4", "start": 2.0, "end": 5.0,
             "label": "the opener", "after": {"transition": "smash"},
             "sfx": "impact"},
        ]
        stored = _post(
            f"{server}/api/drafts",
            {"name": "arranged cut", "folder": "/footage",
             "settings": {"style": "auto", "arrangement": arrangement},
             "plan_json": {"monteur_plan": 1, "music_path": "", "duration": 1.0,
                           "entries": [], "notes": [], "dips": [], "sfx": []}},
        )
        full = _get(f"{server}/api/drafts/{stored['id']}")
        assert full["settings"]["arrangement"] == arrangement


class TestPreferences:
    """The learned-preference endpoints (blueprint 4.3). The store is
    isolated by the autouse _isolated_settings fixture (MONTEUR_PREFERENCES_PATH)."""

    def test_empty_store_inspects_clean(self, server):
        assert _get(f"{server}/api/preferences") == {"signals": [], "active": 0}

    def test_signal_record_inspect_and_reset(self, server):
        # One signal is inactive; a repeat activates it.
        _post(f"{server}/api/preferences/signal",
              {"family": "shot_size", "context": "climax", "direction": "close"})
        one = _post(f"{server}/api/preferences/signal",
                    {"family": "shot_size", "context": "climax", "direction": "close"})
        assert one["active"] == 1
        assert one["signals"][0]["count"] == 2
        assert _post(f"{server}/api/preferences/reset", {}) == {"reset": True}
        assert _get(f"{server}/api/preferences")["active"] == 0

    def test_signal_requires_family_and_direction(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/api/preferences/signal", {"family": "shot_size"})
        assert exc.value.code == 400

    def test_adjust_to_hard_cut_learns_fewer_dissolves(self, server):
        # A /api/plan/adjust that turns a dissolving boundary into a hard cut
        # records the abstract "fewer dissolves" signal (twice -> active).
        def _adjust():
            plan = {"monteur_plan": 1, "music_path": "/m.wav", "duration": 4.0,
                    "entries": [
                        {"clip_path": "/a.mp4", "source_start": 0.0, "source_end": 2.0,
                         "record_start": 0.0, "record_end": 2.0, "score": 1.0},
                        {"clip_path": "/b.mp4", "source_start": 0.0, "source_end": 2.0,
                         "record_start": 2.0, "record_end": 4.0, "score": 1.0,
                         "transition": 0.5}],
                    "notes": [], "dips": [], "sfx": []}
            try:
                _post(f"{server}/api/plan/adjust",
                      {"plan_json": plan, "slot": 1, "transition": "cut"})
            except urllib.error.HTTPError:
                pass  # the render may fail without real media; the signal is recorded first
        _adjust()
        _adjust()
        view = _get(f"{server}/api/preferences")
        assert any(
            s["family"] == "transition" and s["direction"] == "cut" and s["active"]
            for s in view["signals"]
        )


def _step3_html(html):
    """The Storyboard step's markup (between its section tag and step 4's)."""
    return html.split('id="cre-step-3"', 1)[1].split('id="cre-step-4"', 1)[0]


def _step4_html(html):
    """The Color step's markup (between its section tag and step 5's)."""
    return html.split('id="cre-step-4"', 1)[1].split('id="cre-step-5"', 1)[0]


def _step5_html(html):
    """The Your-cut step's markup (between its section tag and the inspector)."""
    return html.split('id="cre-step-5"', 1)[1].split('<aside class="inspector"', 1)[0]


class TestWizardStepsUi:
    """Static asserts on app.html: the DATA-DRIVEN wizard and its homes.

    1 Footage / 2 Options / 3 Storyboard / 4 Color / 5 Your cut. The step
    strip, the "Step N of M" line, the page-bar ticks and the show/hide
    loop all derive from the WIZ_STEPS list — no step number is hardcoded.
    The creative tools live in the storyboard, the grade is its own Color
    page, and the harvest lives in the last page.
    """

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_step_strip_is_data_driven(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        # the single source of truth: the WIZ_STEPS list, in order
        assert "var WIZ_STEPS = [" in html
        order = [html.index('key: "%s"' % k) for k in
                 ("footage", "options", "storyboard", "color", "cut")]
        assert order == sorted(order), "WIZ_STEPS out of order"
        for label in ('"Footage"', '"Options"', '"Storyboard"',
                      '"Look & colour"', '"Your cut"'):
            assert label in html, label
        # the bar is RENDERED from the list, not hardcoded spans
        assert "function renderWizBar" in html
        assert 'id="wbar-"' not in html  # no static wbar spans
        # count + progress derive from WIZ_STEPS.length — no "of 4"/"of 5"
        assert '"Step " + n + " of " + WIZ_STEPS.length' in html
        assert '" of 4 — "' not in html and '" of 5 — "' not in html
        # the old conditional Arrange step is gone (dissolved into step 3)
        assert "wbar-2b" not in html
        assert 'id="cre-step-2b"' not in html
        assert "Arrange the story myself" not in html
        # the five step sections, in order
        assert html.index('id="cre-step-1"') < html.index('id="cre-step-2"') \
            < html.index('id="cre-step-3"') < html.index('id="cre-step-4"') \
            < html.index('id="cre-step-5"')
        # entering the storyboard runs the build — the working draft
        assert 'creShowStep(creStepIndex("storyboard"), true);\n  startBuild(null);' in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_storyboard_step_holds_the_creative_tools(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        step3 = _step3_html(html)
        for needle in (
            # story header + music-entry note + preview at the top
            'id="cre-sb-story"',
            'id="cre-sb-music"',
            'id="cre-preview-btn"',
            # the strip and the board
            'id="cre-strip"',
            'id="cre-sb-board"',
            # the order editor and the coverage hook
            'id="cre-arrange-tools"',
            "Add / reorder scenes",
            'id="cre-missing"',
            # revise + director's notes iterate the draft here
            'id="cre-rev-brief"',
            'id="cre-dir-btn"',
            'id="cre-dir-apply"',
            # the path onward — the storyboard now leads to the Color page
            'id="cre-next-3"',
            "Continue to colour",
        ):
            assert needle in step3, needle
        # harvest controls do NOT live in the storyboard
        for absent in (
            'id="cre-resolve-btn"',
            'id="cre-export-btn"',
            'id="cre-download"',
            'id="cre-kit-btn"',
            'id="cre-save-draft"',
            'id="yt-x-block"',
        ):
            assert absent not in step3, absent
        # the music-entry note comes from the plan notes
        assert "function sbMusicLine" in html
        assert "music enters|music window" in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_color_is_its_own_page(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        step4 = _step4_html(html)
        for needle in (
            'id="cre-h4"',
            "Look &amp; colour",
            # the grade controls live here now
            'id="cre-color"',
            'id="cre-look-chips"',
            'id="cg-brightness"',
            'id="cre-color-frame"',
            # navigation: back to storyboard, on to the cut
            'id="cre-back-4"',
            "Back to storyboard",
            'id="cre-next-4"',
            "Continue to your cut",
        ):
            assert needle in step4, needle
        # the Color page is JUST the grade — no harvest, no storyboard
        for absent in (
            'id="cre-export-btn"',
            'id="cre-resolve-btn"',
            'id="cre-sb-board"',
            'id="cre-final-tiles"',
        ):
            assert absent not in step4, absent

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_last_page_is_a_calm_harvest_page(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        step5 = _step5_html(html)
        for needle in (
            # summary tiles + the story line
            'id="cre-final-tiles"',
            'id="cre-final-story"',
            # Resolve build (with the render row), export, downloads, upload
            'id="cre-resolve-btn"',
            'id="cre-render-block"',
            'id="cre-export-block"',
            'id="cre-download"',
            'id="yt-r-block"',
            'id="yt-x-block"',
            # publish kit + drafts
            'id="cre-kit-btn"',
            'id="cre-save-draft"',
            'id="cre-draft-name"',
            # the way back — to the Color page
            'id="cre-back-5"',
            "Back to colour",
        ):
            assert needle in step5, needle
        # NO storyboard, NO revise, NO director block, NO grade in the harvest
        for absent in (
            'id="cre-sb-board"',
            'id="cre-strip"',
            'id="cre-rev-brief"',
            'id="cre-dir-btn"',
            'id="cre-preview-btn"',
            'id="cre-arr-palette"',
            'id="cre-color"',
            'id="cre-look-chips"',
        ):
            assert absent not in step5, absent

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_palette_and_order_lane_live_in_the_storyboard(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        step3 = _step3_html(html)
        for needle in (
            'id="cre-arr-palette"',
            'id="cre-arr-seq"',
            'id="cre-arr-filter"',   # find-search as the palette filter
            'id="cre-arr-count"',    # the live counter
            'id="cre-arr-apply"',    # reorder/add -> rebuild with the order
        ):
            assert needle in step3, needle
        # board order -> arrangement: derived live from the plan's entries
        assert "function seqFromPlan" in html
        assert "function arrEnsureSeq" in html
        assert "cre.arrange.dirty" in html
        assert "/api/thumb?clip=" in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_dip_titles_are_editable(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            "function startDipTitleEdit",
            "function applyDipTitle",
            "sb-dip-input",
            "dip: dipIndex, title: text",
            '"/api/plan/adjust"',
        ):
            assert needle in html, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_missing_hook_reads_the_step_1_coverage_result(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert "covState.last = cov" in html
        assert "function renderMissingHook" in html
        # no coverage yet -> a calm pointer back to step 1
        assert "Check my coverage in step 1" in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_resume_lands_in_the_storyboard(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert "creShowStep(3, false);" in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_build_payload_and_drafts_carry_the_arrangement(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert "body.arrangement = cre.arrange.seq.map" in html
        # drafts: saved with the settings, restored on resume
        assert '"arrangement"].forEach' in html
        assert "Array.isArray(s.arrangement)" in html


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


def _tiny_plan_json(style="travel"):
    """A small but REAL plan dict, via the production serializer."""
    from monteur.montage import MontageEntry, MontagePlan, plan_to_dict

    plan = MontagePlan(
        music_path=None,
        duration=4.0,
        entries=[
            MontageEntry("a.mp4", 0.0, 2.0, 0.0, 2.0, 0.9),
            MontageEntry("b.mp4", 1.0, 3.0, 2.0, 4.0, 0.8),
        ],
        notes=[f'style "{style}": Some style'],
    )
    return plan_to_dict(plan)


def _delete(url):
    request = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


class TestDraftsApi:
    """The /api/drafts endpoints — the Create wizard's WIP memory."""

    def _record(self, **extra):
        record = {
            "name": "trip wip",
            "folder": "/footage/trip",
            "music": "/music/song.mp3",
            "settings": {"style": "travel", "fps": 25, "canvas": "uhd"},
            "plan_json": _tiny_plan_json(),
            "pins": [1.0],
        }
        record.update(extra)
        return record

    def test_list_starts_empty(self, server):
        assert _get(f"{server}/api/drafts") == {"drafts": []}

    def test_save_list_load_delete_lifecycle(self, server):
        stored = _post(f"{server}/api/drafts", self._record())
        assert stored["id"] and stored["saved_at"]
        assert stored["summary"] == {"duration": 4.0, "cuts": 2, "style": "travel"}

        drafts = _get(f"{server}/api/drafts")["drafts"]
        assert [d["id"] for d in drafts] == [stored["id"]]
        assert "plan_json" not in drafts[0]  # the list stays light
        assert drafts[0]["name"] == "trip wip"
        assert drafts[0]["settings"]["canvas"] == "uhd"

        full = _get(f"{server}/api/drafts/{stored['id']}")
        assert full["plan_json"] == _tiny_plan_json()
        assert full["pins"] == [1.0]

        assert _delete(f"{server}/api/drafts/{stored['id']}") == {"deleted": True}
        assert _get(f"{server}/api/drafts") == {"drafts": []}

    def test_save_upserts_by_id(self, server):
        stored = _post(f"{server}/api/drafts", self._record())
        _post(f"{server}/api/drafts", self._record(id=stored["id"], name="renamed"))
        drafts = _get(f"{server}/api/drafts")["drafts"]
        assert [d["name"] for d in drafts] == ["renamed"]

    def test_save_missing_folder_is_400(self, server):
        record = self._record()
        del record["folder"]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/drafts", record)
        assert exc_info.value.code == 400
        assert "folder" in json.loads(exc_info.value.read())["error"]

    def test_save_missing_plan_json_is_400(self, server):
        record = self._record()
        del record["plan_json"]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/drafts", record)
        assert exc_info.value.code == 400
        assert "plan_json" in json.loads(exc_info.value.read())["error"]

    def test_load_unknown_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/drafts/nope")
        assert exc_info.value.code == 404

    def test_delete_unknown_reports_false(self, server):
        assert _delete(f"{server}/api/drafts/nope") == {"deleted": False}

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_draft_ui(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        # step 1's "Continue where you left off" / draft-resume panel is a
        # draft-era relic: the Media workspace replaced it, and you open
        # projects from Home. It must be GONE — markup and handler alike.
        assert 'id="cre-drafts"' not in html
        assert 'id="cre-drafts-list"' not in html
        assert "Continue where you left off" not in html
        assert "function renderDrafts" not in html
        # step 3: the Save-draft controls next to the download bar still stand
        assert 'id="cre-save-draft"' in html
        assert 'id="cre-draft-name"' in html
        assert "Save draft" in html
        # persistence flipped to first-class projects: the client speaks the
        # projects store (not /api/drafts) plus the plan -> file export
        assert "/api/projects" in html
        assert "/api/create/export" in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_media_page_is_a_three_panel_workspace(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        # the Footage step is now a Resolve-style 3-panel workspace
        for needle in (
            'class="wiz-step ws-step"',   # step 1 fills the window
            'id="cre-explorer-list"',      # left: file explorer
            'id="cre-explorer-crumb"',     # ...with a breadcrumb
            'id="cre-pool-grid"',          # centre: the media pool (drop target)
            'id="cre-pool-drop"',
            'id="ws-video"',               # right: the mini-player
            'id="ws-insp-path"',           # ...and the inspector (on-disk path)
            'id="ws-status"',              # the knowledge status bar
            '/api/browse/list',            # the Explorer's listing endpoint
        ):
            assert needle in html, needle
        # the staged actions live in the toolbar
        assert 'id="cre-scan-btn"' in html
        assert 'id="cre-see-btn"' in html
        # the "never moved / referenced" reassurance line is GONE (self-evident)
        assert 'id="cre-pool-local"' not in html
        assert "never moved or copied" not in html


class TestProjectsApi:
    """The /api/projects endpoints — first-class Cut projects."""

    def test_list_starts_empty(self, server):
        assert _get(f"{server}/api/projects") == {"projects": []}

    def test_create_get_update_delete_lifecycle(self, server):
        created = _post(
            f"{server}/api/projects",
            {"name": "My cut", "options": {"style": "travel"}},
        )
        assert created["monteur_project"] == 1
        assert created["id"] and created["name"] == "My cut"
        assert created["options"] == {"style": "travel"}
        assert "plan" not in created  # only-when-set

        pid = created["id"]
        listed = _get(f"{server}/api/projects")["projects"]
        assert [p["id"] for p in listed] == [pid]
        assert listed[0]["pool_size"] == 0
        assert listed[0]["has_plan"] is False

        full = _get(f"{server}/api/projects/{pid}")
        assert full["id"] == pid

        updated = _post(
            f"{server}/api/projects/{pid}",
            {"name": "renamed", "plan": _tiny_plan_json(),
             "media_pool": [{"path": "/footage/trip", "kind": "folder"}]},
        )
        assert updated["name"] == "renamed"
        assert updated["plan"] == _tiny_plan_json()
        assert updated["media_pool"][0]["path"] == "/footage/trip"

        relisted = _get(f"{server}/api/projects")["projects"]
        assert relisted[0]["has_plan"] is True
        assert relisted[0]["pool_size"] == 1

        assert _delete(f"{server}/api/projects/{pid}") == {"deleted": True}
        assert _get(f"{server}/api/projects") == {"projects": []}

    def test_get_unknown_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/projects/nope")
        assert exc_info.value.code == 404

    def test_update_unknown_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/projects/nope", {"name": "x"})
        assert exc_info.value.code == 404

    def test_delete_unknown_reports_false(self, server):
        assert _delete(f"{server}/api/projects/nope") == {"deleted": False}

    def test_list_migrates_existing_drafts(self, server):
        # A draft saved through the drafts endpoint surfaces as a project on
        # the next GET /api/projects (lazy, idempotent migration).
        draft = _post(
            f"{server}/api/drafts",
            {
                "name": "trip wip",
                "folder": "/footage/trip",
                "music": "/music/song.mp3",
                "settings": {"style": "travel"},
                "plan_json": _tiny_plan_json(),
            },
        )
        first = _get(f"{server}/api/projects")["projects"]
        assert len(first) == 1
        assert first[0]["name"] == "trip wip"
        assert first[0]["has_plan"] is True
        pid = first[0]["id"]

        full = _get(f"{server}/api/projects/{pid}")
        assert full["migrated_from_draft"] == draft["id"]
        assert full["plan"] == _tiny_plan_json()

        # Idempotent: a second list adds no duplicate.
        again = _get(f"{server}/api/projects")["projects"]
        assert [p["id"] for p in again] == [pid]


class TestProjectPoolApi:
    """GET/POST /api/projects/<id>/pool — the media pool (Increment B).

    The pool RESOLVES the project's referenced files/folders into clip cards
    with cheap cached status, and add/remove only ever touches the REFERENCE
    list — never a byte of media on disk.
    """

    def _new_project(self, server):
        return _post(f"{server}/api/projects", {"name": "pool cut"})["id"]

    def test_pool_lists_clips_from_a_folder_entry_with_status_flags(
        self, server, tmp_path
    ):
        footage = tmp_path / "footage"
        footage.mkdir()
        (footage / "a.mp4").write_bytes(b"not really a video")
        (footage / "b.mov").write_bytes(b"nor is this one")
        (footage / "notes.txt").write_text("ignored — not media")

        pid = self._new_project(server)
        pool = _post(
            f"{server}/api/projects/{pid}/pool",
            {"add": {"path": str(footage), "kind": "folder"}},
        )
        names = sorted(c["name"] for c in pool["clips"])
        assert names == ["a.mp4", "b.mov"]  # the .txt is not media
        for clip in pool["clips"]:
            assert clip["kind"] == "file"
            assert clip["thumb"] is True
            # nothing scanned/proxied/labeled yet — cheap flags all read False
            assert clip["sifted"] is False
            assert clip["proxy_fresh"] is False
            assert clip["labeled"] is False
            assert Path(clip["path"]).is_absolute()
        # the folder entry reports how many clips it expands to
        assert pool["entries"] == [
            {"path": str(footage), "kind": "folder", "clip_count": 2}
        ]

    def test_pool_get_matches_post(self, server, tmp_path):
        footage = tmp_path / "shoot"
        footage.mkdir()
        (footage / "one.mp4").write_bytes(b"x")
        pid = self._new_project(server)
        _post(
            f"{server}/api/projects/{pid}/pool",
            {"add": {"path": str(footage), "kind": "folder"}},
        )
        got = _get(f"{server}/api/projects/{pid}/pool")
        assert [c["name"] for c in got["clips"]] == ["one.mp4"]

    def test_labeled_flag_reads_the_vision_cache(self, server, tmp_path):
        from monteur.vision import CACHE_FILENAME

        footage = tmp_path / "labeled"
        footage.mkdir()
        clip = footage / "hero.mp4"
        clip.write_bytes(b"x")
        # a .monteur-vision.json next to the footage marks the clip labeled;
        # the key discipline is "<abspath>|<mtime>|<window>|<model>"
        (footage / CACHE_FILENAME).write_text(
            json.dumps(
                {f"{clip.resolve()}|123.0|0.00-1.00|m": {"label": "a summit"}}
            )
        )
        pid = self._new_project(server)
        pool = _post(
            f"{server}/api/projects/{pid}/pool",
            {"add": {"path": str(footage), "kind": "folder"}},
        )
        assert pool["clips"][0]["labeled"] is True

    def test_add_then_remove_never_touches_the_media_file(self, server, tmp_path):
        # The whole promise of the pool: referencing local footage moves and
        # copies NOTHING. Create a real file, add it, remove it — and prove it
        # is byte-for-byte and mtime identical, and was never duplicated.
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        clip = media_dir / "precious.mp4"
        clip.write_bytes(b"the original pixels, untouched")
        before_bytes = clip.read_bytes()
        before_stat = clip.stat()
        before_files = sorted(p.name for p in media_dir.iterdir())

        pid = self._new_project(server)
        added = _post(
            f"{server}/api/projects/{pid}/pool",
            {"add": {"path": str(clip), "kind": "file"}},
        )
        assert [c["name"] for c in added["clips"]] == ["precious.mp4"]

        removed = _post(
            f"{server}/api/projects/{pid}/pool", {"remove": str(clip)}
        )
        assert removed["clips"] == []

        # the reference list changed; the FILE did not
        assert clip.read_bytes() == before_bytes
        assert clip.stat().st_mtime_ns == before_stat.st_mtime_ns
        assert clip.stat().st_size == before_stat.st_size
        # and no copy was made anywhere beside it
        assert sorted(p.name for p in media_dir.iterdir()) == before_files

    def test_add_updates_the_reference_list_only(self, server, tmp_path):
        clip = tmp_path / "ref.mp4"
        clip.write_bytes(b"x")
        pid = self._new_project(server)
        _post(
            f"{server}/api/projects/{pid}/pool",
            {"add": {"path": str(clip), "kind": "file"}},
        )
        manifest = _get(f"{server}/api/projects/{pid}")
        assert manifest["media_pool"][0]["path"] == str(clip)
        assert manifest["media_pool"][0]["kind"] == "file"

    def test_pool_of_unknown_project_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/projects/nope/pool")
        assert exc_info.value.code == 404

    def test_pool_update_needs_add_or_remove(self, server):
        pid = self._new_project(server)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/projects/{pid}/pool", {"bogus": True})
        assert exc_info.value.code == 400


class TestBrowseListApi:
    """GET /api/browse/list — the Media workspace Explorer's directory listing.

    Lists a folder's SUBFOLDERS and VIDEO FILES so the Explorer can navigate the
    disk and drag clips into the pool. Deterministic, offline, soft-failing.
    """

    def test_lists_folders_and_video_files(self, server, tmp_path):
        root = tmp_path / "rides"
        root.mkdir()
        (root / "RAW").mkdir()
        (root / "Drone").mkdir()
        (root / "clip_a.mp4").write_bytes(b"x")
        (root / "clip_b.mov").write_bytes(b"y")
        (root / "notes.txt").write_text("ignored — not media")
        (root / ".hidden.mp4").write_bytes(b"z")  # dotfiles skipped

        data = _get(f"{server}/api/browse/list?path={urllib.parse.quote(str(root))}")
        assert data["path"] == str(root)
        assert data["parent"] == str(tmp_path)
        assert [f["name"] for f in data["folders"]] == ["Drone", "RAW"]
        assert sorted(f["name"] for f in data["files"]) == ["clip_a.mp4", "clip_b.mov"]
        for entry in data["folders"] + data["files"]:
            assert Path(entry["path"]).is_absolute()

    def test_empty_path_defaults_to_home(self, server):
        data = _get(f"{server}/api/browse/list")
        assert data["path"] == os.path.abspath(os.path.expanduser("~"))
        assert "folders" in data and "files" in data

    def test_missing_directory_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/browse/list?path=/no/such/dir/anywhere")
        assert exc_info.value.code == 400


class TestProjectAnalyzeApi:
    """POST /api/projects/<id>/analyze + /see — the STAGED, subset flow.

    The pool's primary actions operate on a SELECTION: analyze only the chosen
    clips, then Claude-check only the good ones. Both are cancellable jobs the
    scan panel drives, and neither ever modifies a byte of the referenced media.
    """

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        # start from clean caches — a prior test's full DEMO scan would
        # otherwise leave every clip "sifted", hiding the subset behaviour
        _clear_scan_cache()

    def _project_with_demo(self, server):
        pid = _post(f"{server}/api/projects", {"name": "staged"})["id"]
        _post(
            f"{server}/api/projects/{pid}/pool",
            {"add": {"path": self.DEMO, "kind": "folder"}},
        )
        return pid

    def _pool_clip_paths(self, server, pid):
        return [c["path"] for c in _get(f"{server}/api/projects/{pid}/pool")["clips"]]

    def test_analyze_sifts_only_the_selected_clips(self, server):
        pid = self._project_with_demo(server)
        clips = self._pool_clip_paths(server, pid)
        assert len(clips) >= 3  # the demo footage has several clips
        chosen = clips[:2]

        before_mtime = {p: Path(p).stat().st_mtime_ns for p in clips}
        before_bytes = {p: Path(p).read_bytes() for p in chosen}

        data = _post(f"{server}/api/projects/{pid}/analyze", {"clips": chosen})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "scan"  # reuses the scan panel plumbing
        assert len(job["result"]["clips"]) == 2

        by_path = {
            c["path"]: c
            for c in _get(f"{server}/api/projects/{pid}/pool")["clips"]
        }
        # ONLY the selected clips are now sifted — the rest stay untouched
        for path in chosen:
            assert by_path[path]["sifted"] is True
            assert "usable_ratio" in by_path[path]
        for path in clips:
            if path not in chosen:
                assert by_path[path]["sifted"] is False

        # analyze reads frames but NEVER writes the media
        for path in clips:
            assert Path(path).stat().st_mtime_ns == before_mtime[path]
        for path in chosen:
            assert Path(path).read_bytes() == before_bytes[path]

    def test_analyze_rejects_clips_not_in_the_pool(self, server):
        pid = _post(f"{server}/api/projects", {"name": "empty"})["id"]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/projects/{pid}/analyze",
                {"clips": ["/nowhere/ghost.mp4"]},
            )
        assert exc_info.value.code == 400

    def test_analyze_needs_a_non_empty_clip_list(self, server):
        pid = self._project_with_demo(server)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/projects/{pid}/analyze", {"clips": []})
        assert exc_info.value.code == 400

    def test_see_runs_on_the_selected_subset(self, server):
        pid = self._project_with_demo(server)
        clips = self._pool_clip_paths(server, pid)
        data = _post(f"{server}/api/projects/{pid}/see", {"clips": clips[:1]})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert len(job["result"]["clips"]) == 1
        # with no ANTHROPIC_API_KEY in tests, vision soft-fails (an upgrade, not
        # a gate) — the sift still succeeded either way
        assert "vision_error" in job["result"] or "vision_notes" in job["result"]


class TestCreateExportApi:
    """POST /api/create/export — plan_json -> timeline file, synchronous."""

    def test_export_fcpxml_from_plan(self, server):
        data = _post(
            f"{server}/api/create/export",
            {"plan_json": _tiny_plan_json(), "fps": 25, "format": "fcpxml"},
        )
        assert data["filename"] == "monteur_montage.fcpxml"
        assert "<fcpxml" in data["content"]
        assert data["plan"]["cuts"] == 2
        assert data["plan"]["duration"] == 4.0
        assert data["plan"]["tempo"] == 0  # nothing re-listens to the song
        assert data["plan_json"]["entries"] == _tiny_plan_json()["entries"]

    def test_export_edl_and_canvas(self, server):
        data = _post(
            f"{server}/api/create/export",
            {"plan_json": _tiny_plan_json(), "fps": 25,
             "canvas": "vertical", "format": "edl"},
        )
        assert data["filename"] == "monteur_montage.edl"
        assert data["content"].startswith("TITLE:")

    def test_export_missing_plan_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/export", {"fps": 25})
        assert exc_info.value.code == 400

    def test_export_bad_plan_is_400_with_loader_message(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/export", {"plan_json": {"nope": 1}})
        assert exc_info.value.code == 400
        assert "monteur_plan" in json.loads(exc_info.value.read())["error"]

    def test_export_music_audio_without_music_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/export",
                {"plan_json": _tiny_plan_json(), "audio": "music"},
            )
        assert exc_info.value.code == 400


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
    # the per-clip cache (staged pool analysis) is a module global too — a
    # build reuses it when a folder is fully covered, so reset it alongside
    # the folder cache or a prior test's reports leak into "fresh sift" cases
    with web_server._CLIP_CACHE_LOCK:
        web_server._CLIP_CACHE.clear()


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


class TestMovieAssembleApi:
    """POST /api/movie/assemble — cut the whole film along the screenplay."""

    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def _project_with_demo(self, tmp_path):
        """A real 2-scene project, both scenes shooting from the demo folder."""
        return _write_movie_project(
            tmp_path / "proj", n_scenes=2, folders=[self.DEMO, self.DEMO]
        )

    def _assemble(self, server, project_dir, **extra):
        data = _post(
            f"{server}/api/movie/assemble", {"project_dir": project_dir, **extra}
        )
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_assemble_end_to_end(self, server, tmp_path):
        project_dir = self._project_with_demo(tmp_path)
        job = self._assemble(server, project_dir)
        assert job["state"] == "done"
        assert job["kind"] == "movie-assemble"

        # one "scene" progress entry per scene, name = the heading
        scenes = [p for p in job["progress"] if p["stage"] == "scene"]
        assert len(scenes) == 2
        assert [p["name"] for p in scenes] == [
            "INT. AUTO - NIGHT", "EXT. WALDWEG - NIGHT",
        ]
        # the shared folder was sifted exactly once, with per-clip progress
        stages = [p["stage"] for p in job["progress"]]
        assert stages.count("start") == 4
        assert stages.count("done") == 4

        result = job["result"]
        assert result["filename"] == "nachtfahrt.fcpxml"  # <title-slug>.fcpxml
        assert result["content"].startswith("<?xml")
        assert result["notes"]
        assert result["duration_seconds"] > 0
        assert result["scenes_used"] == 2

    def test_assemble_edl_format(self, server, tmp_path):
        project_dir = self._project_with_demo(tmp_path)
        job = self._assemble(server, project_dir, format="edl")
        assert job["state"] == "done"
        assert job["result"]["filename"] == "nachtfahrt.edl"
        assert job["result"]["content"].startswith("TITLE:")

    def test_assemble_unassigned_project_is_error_job(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj")  # nothing assigned
        job = self._assemble(server, project_dir)
        assert job["state"] == "error"
        assert "assign" in job["message"]
        assert job["result"] is None

    def test_assemble_bad_project_dir_is_error_job(self, server, tmp_path):
        job = self._assemble(server, str(tmp_path / "nope"))
        assert job["state"] == "error"
        assert "movie.json" in job["message"]

    def test_assemble_missing_project_dir_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/movie/assemble", {})
        assert exc_info.value.code == 400
        assert "project_dir" in json.loads(exc_info.value.read())["error"]

    def test_check_then_assemble_reuses_scan_cache(self, server, tmp_path):
        project_dir = self._project_with_demo(tmp_path)
        check = _post(
            f"{server}/api/movie/check", {"project_dir": project_dir, "scene": 1}
        )
        assert _wait_for_job(server, check["job"])["state"] == "done"

        job = self._assemble(server, project_dir)
        assert job["state"] == "done"
        # The check's sift is reused: the cache entry is announced and there
        # is no second per-clip start/done pass for the shared folder.
        assert {"stage": "cache", "name": "using previous scan"} in job["progress"]
        assert not any(p["stage"] in ("start", "done") for p in job["progress"])
        assert len([p for p in job["progress"] if p["stage"] == "scene"]) == 2
        assert job["result"]["scenes_used"] == 2
        assert job["result"]["duration_seconds"] > 0


# --- SFX toggle, revision loop, find and distill (the CLI-only quartet) --------


class TestSfxApi:
    """The build payload's "sfx" flag is forwarded to plan_montage."""

    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def _build(self, server, **extra):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "format": "edl", **extra},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        return job["result"]

    def test_build_forwards_sfx(self, server):
        result = self._build(server, sfx=True)
        assert any("sfx layer" in n for n in result["plan"]["notes"])
        assert result["plan_json"]["sfx"]  # the cues ride in the full plan too

    def test_build_without_sfx_plans_no_layer(self, server):
        result = self._build(server)
        assert not any("sfx layer" in n for n in result["plan"]["notes"])
        assert result["plan_json"]["sfx"] == []


class TestReviseApi:
    """POST /api/create/revise — the Studio's revision loop."""

    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def _build(self, server, **extra):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "format": "edl", **extra},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        return job["result"]

    def _revise(self, server, plan_json, brief, **extra):
        data = _post(
            f"{server}/api/create/revise",
            {"plan_json": plan_json, "folder": self.DEMO, "brief": brief,
             "format": "edl", **extra},
        )
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_build_result_carries_plan_json(self, server):
        result = self._build(server)
        plan_json = result["plan_json"]
        assert plan_json["monteur_plan"] == 1
        assert plan_json["entries"]
        assert plan_json["duration"] == result["plan"]["duration"]
        # ...and it round-trips through the real loader
        from monteur.montage import plan_from_dict

        plan = plan_from_dict(plan_json)
        assert len(plan.entries) == result["plan"]["cuts"]

    def test_revise_end_to_end(self, server):
        built = self._build(server)
        job = self._revise(server, built["plan_json"], "ruhiger")
        assert job["state"] == "done"
        assert job["kind"] == "revise"

        # the plan has music, so it was re-analyzed with a progress entry
        assert any(p["stage"] == "music" for p in job["progress"])

        result = job["result"]
        assert result["rationale"].startswith("recognized:")
        assert "calmer" in result["rationale"]
        assert result["filename"].endswith(".edl")
        assert result["content"].startswith("TITLE:")
        assert result["plan"]["cuts"] > 0
        # the revised plan differs and says what happened
        assert result["plan_json"] != built["plan_json"]
        assert any(n.startswith("revision:") for n in result["plan_json"]["notes"])
        assert any(n.startswith("revision:") for n in result["plan"]["notes"])

    def test_revise_keeps_pinned_shot(self, server):
        built = self._build(server)
        entry = built["plan_json"]["entries"][0]
        pin = (entry["record_start"] + entry["record_end"]) / 2.0
        job = self._revise(server, built["plan_json"], "ruhiger", pins=[pin])
        assert job["state"] == "done"
        result = job["result"]
        assert any("1 pinned shot kept" in n for n in result["plan_json"]["notes"])
        revised_first = result["plan_json"]["entries"][0]
        assert revised_first["record_start"] == entry["record_start"]
        assert revised_first["record_end"] == entry["record_end"]
        assert revised_first["clip_path"] == entry["clip_path"]

    def test_revise_chains(self, server):
        """A revised plan_json can be revised again (the loop loops)."""
        built = self._build(server)
        first = self._revise(server, built["plan_json"], "ruhiger")
        assert first["state"] == "done"
        second = self._revise(
            server, first["result"]["plan_json"], "harte schnitte"
        )
        assert second["state"] == "done"
        assert "transitions" in second["result"]["rationale"]

    def test_revise_bad_plan_json_is_job_error(self, server):
        job = self._revise(server, {"bogus": 1}, "ruhiger")
        assert job["state"] == "error"
        assert "not a Monteur plan" in job["message"]
        assert job["result"] is None

    def test_revise_bad_pins_is_job_error(self, server):
        built = self._build(server)
        job = self._revise(server, built["plan_json"], "ruhiger", pins=["soon"])
        assert job["state"] == "error"
        assert "pins" in job["message"]

    def test_revise_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/revise",
                {"plan_json": {"monteur_plan": 1}, "brief": "ruhiger"},
            )
        assert exc_info.value.code == 400

    def test_revise_missing_plan_json_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/revise",
                {"folder": self.DEMO, "brief": "ruhiger"},
            )
        assert exc_info.value.code == 400
        assert "plan_json" in json.loads(exc_info.value.read())["error"]

    def test_revise_missing_brief_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/revise",
                {"folder": self.DEMO, "plan_json": {"monteur_plan": 1}},
            )
        assert exc_info.value.code == 400
        assert "brief" in json.loads(exc_info.value.read())["error"]


class TestDirectorApi:
    """POST /api/create/direct + /api/create/direct/apply — director's notes."""

    DEMO = TestCreateApi.DEMO

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def _build(self, server, **extra):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "format": "edl", **extra},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        return job["result"]

    def test_direct_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/direct",
                {"plan_json": {"monteur_plan": 1}},
            )
        assert exc_info.value.code == 400
        assert "folder" in json.loads(exc_info.value.read())["error"]

    def test_direct_missing_plan_json_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/direct", {"folder": self.DEMO})
        assert exc_info.value.code == 400
        assert "plan_json" in json.loads(exc_info.value.read())["error"]

    def test_apply_missing_review_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/direct/apply",
                {"folder": self.DEMO, "plan_json": {"monteur_plan": 1}},
            )
        assert exc_info.value.code == 400
        assert "review" in json.loads(exc_info.value.read())["error"]

    def test_direct_happy_path(self, server, monkeypatch):
        built = self._build(server)
        canned = {
            "verdict": "tight cut", "score": 82,
            "praise": ["the opening establishes"], "issues": [],
            "summary": "ship it",
        }
        calls: dict = {}

        def fake_direct_cut(plan, reports, music=None, notes=""):
            calls.update(
                entries=len(plan.entries), reports=len(reports),
                has_music=music is not None, notes=notes,
            )
            return canned

        monkeypatch.setattr("monteur.director.direct_cut", fake_direct_cut)
        data = _post(
            f"{server}/api/create/direct",
            {"plan_json": built["plan_json"], "folder": self.DEMO,
             "notes": "for instagram"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "direct"

        result = job["result"]
        assert result["review"] == canned
        assert result["applied"] is False
        # the plan itself is returned UNCHANGED — apply is a separate step
        assert result["plan_json"] == built["plan_json"]
        assert calls["notes"] == "for instagram"
        assert calls["entries"] == built["plan"]["cuts"]
        assert calls["has_music"] is True  # the plan's own song was analyzed
        assert any(p["stage"] == "music" for p in job["progress"])
        assert any(p["stage"] == "direct" for p in job["progress"])

    def test_direct_ai_error_is_job_error(self, server, monkeypatch):
        built = self._build(server)
        from monteur.ai import MonteurAIError

        def boom(*args, **kwargs):
            raise MonteurAIError("no way to reach Claude found")

        monkeypatch.setattr("monteur.director.direct_cut", boom)
        data = _post(
            f"{server}/api/create/direct",
            {"plan_json": built["plan_json"], "folder": self.DEMO},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "no way to reach Claude" in job["message"]
        assert job["result"] is None

    def test_direct_bad_plan_json_is_job_error(self, server):
        data = _post(
            f"{server}/api/create/direct",
            {"plan_json": {"bogus": 1}, "folder": self.DEMO},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "not a Monteur plan" in job["message"]

    def test_apply_end_to_end(self, server):
        built = self._build(server)
        entries = built["plan_json"]["entries"]
        donor = next(
            (e for e in entries if e["clip_path"] != entries[0]["clip_path"]),
            None,
        )
        assert donor is not None, "demo build should use more than one clip"
        review = {
            "verdict": "", "score": 60, "praise": [], "summary": "",
            "issues": [
                {
                    "slots": [0], "kind": "weak_opening",
                    "problem": "p", "suggestion": "s",
                    "replacement": {
                        "clip": Path(donor["clip_path"]).name,
                        "start": donor["source_start"],
                        "end": donor["source_end"],
                    },
                }
            ],
        }
        data = _post(
            f"{server}/api/create/direct/apply",
            {"plan_json": built["plan_json"], "folder": self.DEMO,
             "review": review, "format": "edl"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "direct-apply"

        result = job["result"]
        assert result["applied"] is True
        assert result["filename"].endswith(".edl")
        assert result["content"].startswith("TITLE:")
        assert result["notes"] and result["notes"][0].startswith("slot 1:")

        new_entries = result["plan_json"]["entries"]
        assert len(new_entries) == len(entries)
        # the swapped slot changed clip but kept its record grid ...
        assert new_entries[0]["clip_path"] == donor["clip_path"]
        assert new_entries[0]["record_start"] == entries[0]["record_start"]
        assert new_entries[0]["record_end"] == entries[0]["record_end"]
        assert new_entries[0]["transition"] == entries[0]["transition"]
        # ... and every other entry is bit-identical (nothing re-planned)
        for old, new in zip(entries[1:], new_entries[1:]):
            assert old == new
        assert any(
            n.startswith("director:") for n in result["plan_json"]["notes"]
        )

    def test_apply_bad_review_shape_does_nothing_but_succeeds(self, server):
        """A review without issues still returns a valid (unchanged) cut."""
        built = self._build(server)
        data = _post(
            f"{server}/api/create/direct/apply",
            {"plan_json": built["plan_json"], "folder": self.DEMO,
             "review": {"issues": "not-a-list"}, "format": "edl"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["result"]["plan_json"]["entries"] == built["plan_json"]["entries"]

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_director_block(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert 'id="cre-direct"' in html
        assert 'id="cre-dir-btn"' in html
        assert 'id="cre-dir-apply"' in html
        assert "/api/create/direct" in html
        assert "/api/create/direct/apply" in html
        # the help copy explains the no-extra-cost angle and the vision link
        assert "no extra API cost" in html
        assert "Let Claude watch your clips" in html


class TestCoverageApi:
    """POST /api/coverage — the pre-cut shot list (monteur.coverage).

    ``missing_shots`` is resolved at CALL time inside the job thread, so
    monkeypatching it on monteur.coverage is enough — no AI backend runs.
    """

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def test_coverage_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/coverage", {"style": "trailer"})
        assert exc_info.value.code == 400
        assert "folder" in json.loads(exc_info.value.read())["error"]

    def test_coverage_happy_path(self, server, monkeypatch):
        canned = {
            "verdict": "thin on people", "coverage_score": 61,
            "have": ["road action"],
            "missing": [
                {"shot": "calm wide opener", "why": "the opener",
                 "priority": "must", "tip": "tripod, 10s hold"},
            ],
            "summary": "film the opener", "basics": {"vision": False},
            "notes": [],
        }
        calls: dict = {}

        def fake_missing_shots(reports, style="auto", brief="",
                               target_seconds=None):
            calls.update(reports=len(reports), style=style, brief=brief,
                         target=target_seconds)
            return canned

        monkeypatch.setattr("monteur.coverage.missing_shots", fake_missing_shots)
        data = _post(
            f"{server}/api/coverage",
            {"folder": self.DEMO, "style": "trailer",
             "brief": "epic alps trailer", "target": 45},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "coverage"
        assert job["result"] == {"coverage": canned}
        # the wizard's inputs are forwarded verbatim
        assert calls["style"] == "trailer"
        assert calls["brief"] == "epic alps trailer"
        assert calls["target"] == 45.0
        assert calls["reports"] > 0
        assert any(p["stage"] == "coverage" for p in job["progress"])

    def test_coverage_defaults_without_style_brief_target(self, server, monkeypatch):
        calls: dict = {}

        def fake_missing_shots(reports, style="auto", brief="",
                               target_seconds=None):
            calls.update(style=style, brief=brief, target=target_seconds)
            return {"verdict": "", "coverage_score": 50, "have": [],
                    "missing": [], "summary": "", "basics": {}, "notes": []}

        monkeypatch.setattr("monteur.coverage.missing_shots", fake_missing_shots)
        data = _post(f"{server}/api/coverage", {"folder": self.DEMO})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert calls == {"style": "auto", "brief": "", "target": None}

    def test_coverage_ai_error_is_job_error(self, server, monkeypatch):
        from monteur.ai import MonteurAIError

        def boom(*args, **kwargs):
            raise MonteurAIError("no way to reach Claude found")

        monkeypatch.setattr("monteur.coverage.missing_shots", boom)
        data = _post(f"{server}/api/coverage", {"folder": self.DEMO})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "no way to reach Claude" in job["message"]
        assert job["result"] is None

    def test_coverage_bad_target_is_job_error(self, server):
        data = _post(
            f"{server}/api/coverage", {"folder": self.DEMO, "target": "soon"}
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "target" in job["message"]

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_coverage_block(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert 'id="cre-coverage"' in html
        assert "Shot list — what's still missing?" in html
        assert 'id="cre-cov-brief"' in html
        assert 'id="cre-cov-btn"' in html
        assert "/api/coverage" in html
        # the MUST/NICE cards and the badge accents
        assert "cov-card must" in html
        assert "cov-card nice" in html
        assert "cov-badge must" in html
        assert "cov-badge nice" in html
        # one brief, two homes: the coverage input mirrors #cre-brief
        assert "setBriefText" in html
        # the calm after-the-list line pointing back at the rescan
        assert "Add the new clips to the same folder, then " in html
        # the help copy: runs over the Claude connection, sharpest with vision
        assert "no extra cost" in html
        assert "Let Claude watch your clips" in html


class TestAiCutApi:
    """Claude composes the cut (monteur.compose) behind "ai_cut": true.

    ``compose_montage`` is resolved at CALL time inside the job thread, so
    monkeypatching it on monteur.compose is enough — no AI backend runs.
    """

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def _payload(self, **extra):
        return {
            "folder": self.DEMO,
            "music": f"{self.DEMO}/song.wav",
            "format": "fcpxml",
            **extra,
        }

    def test_build_with_ai_cut_composes(self, server, monkeypatch):
        from monteur.montage import plan_montage

        calls = []

        def fake_compose(reports, music, **kwargs):
            calls.append(kwargs)
            plan = plan_montage(
                reports, music,
                style=kwargs.get("style", "auto"),
                order=kwargs.get("order", "chronological"),
            )
            plan.notes.append("story: ein Sommer in drei Akten")
            plan.notes.append("act 1: still beginnen")
            return plan

        monkeypatch.setattr("monteur.compose.compose_montage", fake_compose)
        data = _post(
            f"{server}/api/create/build",
            self._payload(ai_cut=True, brief="Alpen mit Freunden", style="travel"),
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"

        # the composer ran in Studio mode: strict (no silent downgrade),
        # with the wizard's brief
        assert len(calls) == 1
        assert calls[0]["strict"] is True
        assert calls[0]["brief"] == "Alpen mit Freunden"
        assert calls[0]["style"] == "travel"
        # the story and act notes surface on the result card
        notes = job["result"]["plan"]["notes"]
        assert "story: ein Sommer in drei Akten" in notes
        assert "act 1: still beginnen" in notes
        # a "compose" progress stage told the user what was happening
        assert any(p["stage"] == "compose" for p in job["progress"])

        # brief and ai_cut ride into the autosaved draft settings
        auto = _get(f"{server}/api/drafts/autosave")
        assert auto["settings"]["ai_cut"] is True
        assert auto["settings"]["brief"] == "Alpen mit Freunden"

    def test_explicit_ai_cut_failure_is_job_error(self, server, monkeypatch):
        from monteur.ai import MonteurAIError

        def fail_compose(reports, music, **kwargs):
            raise MonteurAIError(
                "No way to reach Claude found. Set ANTHROPIC_API_KEY or "
                "install Claude Code."
            )

        monkeypatch.setattr("monteur.compose.compose_montage", fail_compose)
        data = _post(f"{server}/api/create/build", self._payload(ai_cut=True))
        job = _wait_for_job(server, data["job"])
        # the user explicitly asked for the AI cut: no silent heuristic
        # downgrade — the job fails with the actionable AI message
        assert job["state"] == "error"
        assert "No way to reach Claude" in job["message"]
        assert job["result"] is None

    def test_build_without_ai_cut_never_composes(self, server, monkeypatch):
        def fail_compose(reports, music, **kwargs):  # pragma: no cover
            raise AssertionError("compose_montage must not run without ai_cut")

        monkeypatch.setattr("monteur.compose.compose_montage", fail_compose)
        data = _post(f"{server}/api/create/build", self._payload())
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert not any(p["stage"] == "compose" for p in job["progress"])

    def test_app_has_the_composer_controls(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        # the step-2 context textarea and the composer toggle
        assert 'id="cre-brief"' in html
        assert 'id="cre-ai-cut"' in html
        assert "Claude composes the cut" in html
        assert "What is this video?" in html
        # the payload carries both, and drafts restore them
        assert "ai_cut: $(\"cre-ai-cut\").checked" in html
        assert '"ai_cut", "brief"' in html
        # the auto-suggest reads the scan's vision annotations
        assert "scanHasVision" in html


def _write_vision_cache(folder, entries):
    """Write a real .monteur-vision.json next to dummy clips.

    Mirrors the helper pattern in test_find/test_mcp: the keys ARE
    vision._moment_key's format (abspath|mtime|start-end (2dp)|model), so
    monteur.find reads the cache exactly like a production one.
    """
    from monteur.vision import CACHE_FILENAME

    cache = {}
    for name, start, end, value in entries:
        clip = Path(folder) / name
        if not clip.exists():
            clip.write_bytes(b"not really a video")
        key = (
            f"{os.path.abspath(clip)}|{os.path.getmtime(clip)}"
            f"|{start:.2f}-{end:.2f}|claude-test-model"
        )
        cache[key] = value
    (Path(folder) / CACHE_FILENAME).write_text(
        json.dumps(cache), encoding="utf-8"
    )


class TestFindApi:
    """POST /api/find — instant, offline search of the vision cache."""

    def _seed(self, tmp_path):
        _write_vision_cache(tmp_path, [
            ("ride.mp4", 0.0, 4.0,
             {"label": "Kurve links am Hang", "tags": ["kurven", "wald"],
              "role": "action", "hero": 0.9, "group": "trail"}),
            ("ride.mp4", 10.0, 12.0,
             {"label": "Geradeaus im Flachen", "tags": ["gerade"],
              "role": "", "hero": 0.1, "group": "trail"}),
            ("camp.mp4", 2.0, 5.0,
             {"label": "Sonnenuntergang am See", "tags": ["abend"],
              "role": "closer", "hero": 0.6, "group": "camp"}),
        ])

    def test_find_happy_path(self, server, tmp_path):
        self._seed(tmp_path)
        data = _post(
            f"{server}/api/find", {"folder": str(tmp_path), "query": "kurve"}
        )
        assert "error" not in data
        shots = data["shots"]
        assert len(shots) == 1
        shot = shots[0]
        assert shot["clip_path"].endswith("ride.mp4")
        assert (shot["start"], shot["end"]) == (0.0, 4.0)
        assert shot["label"] == "Kurve links am Hang"
        assert shot["tags"] == ["kurven", "wald"]
        assert shot["hero"] == 0.9
        assert 0.0 < shot["relevance"] <= 1.0

    def test_find_hero_query(self, server, tmp_path):
        self._seed(tmp_path)
        data = _post(
            f"{server}/api/find", {"folder": str(tmp_path), "query": "hero"}
        )
        assert [s["hero"] for s in data["shots"]] == [0.9, 0.6]

    def test_find_respects_limit(self, server, tmp_path):
        self._seed(tmp_path)
        data = _post(
            f"{server}/api/find",
            {"folder": str(tmp_path), "query": "hero", "limit": 1},
        )
        assert len(data["shots"]) == 1

    def test_find_missing_cache_is_soft_error(self, server, tmp_path):
        data = _post(
            f"{server}/api/find", {"folder": str(tmp_path), "query": "kurve"}
        )
        assert "shots" not in data
        assert "monteur see" in data["error"]  # explains how to get annotations

    def test_find_empty_query_is_400(self, server, tmp_path):
        self._seed(tmp_path)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/find", {"folder": str(tmp_path), "query": "  "})
        assert exc_info.value.code == 400

    def test_find_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/find", {"query": "kurve"})
        assert exc_info.value.code == 400
        assert "folder" in json.loads(exc_info.value.read())["error"]


class TestDistillApi:
    """POST /api/create/distill — a finished cut becomes a short trailer."""

    def _timeline(self, **extra):
        return {
            "filename": "sample.edl",
            "content": (FIXTURES / "sample.edl").read_text(),
            "fps": 25,
            **extra,
        }

    def _distill(self, server, **extra):
        data = _post(
            f"{server}/api/create/distill",
            {"timeline": self._timeline(), **extra},
        )
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_distill_end_to_end(self, server):
        job = self._distill(server, target=8, format="edl")
        assert job["state"] == "done"
        assert job["kind"] == "distill"

        result = job["result"]
        assert result["filename"] == "monteur_trailer.edl"
        assert result["content"].startswith("TITLE:")
        assert result["plan"]["cuts"] > 0
        assert 0 < result["plan"]["duration"] <= 10  # ~the 8s target
        assert result["plan"]["tempo"] == 0  # no music given
        notes = result["plan"]["notes"]
        assert any("distilled from" in n for n in notes)
        # the fixture's sources are bare reels — the notes say so honestly
        assert any("not files on disk" in n for n in notes)

    def test_distill_fcpxml_output_and_canvas(self, server):
        job = self._distill(server, target=8, canvas="vertical")
        assert job["state"] == "done"
        result = job["result"]
        assert result["filename"] == "monteur_trailer.fcpxml"
        assert result["content"].startswith("<?xml")
        assert 'width="1080"' in result["content"]
        assert 'height="1920"' in result["content"]

    def test_distill_missing_timeline_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/distill", {"target": 8})
        assert exc_info.value.code == 400
        assert "timeline" in json.loads(exc_info.value.read())["error"]

    def test_distill_missing_content_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/distill",
                {"timeline": {"filename": "cut.edl", "fps": 25}},
            )
        assert exc_info.value.code == 400

    def test_distill_edl_without_fps_is_400(self, server):
        timeline = self._timeline()
        del timeline["fps"]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/distill", {"timeline": timeline})
        assert exc_info.value.code == 400
        assert "fps" in json.loads(exc_info.value.read())["error"]


class TestResolveBuildApi:
    """POST /api/create/resolve — build the plan straight into Resolve.

    ``build_plan_isolated`` is replaced at its import site: the job body
    does ``from monteur.resolve import build_plan_isolated`` at CALL time,
    so ``monkeypatch.setattr`` on :mod:`monteur.resolve` is a complete test
    hook — no running Resolve (and no worker child process) needed.
    ``titles_from_plan`` stays real: it is pure and Resolve-free.
    """

    def _plan_json(self, dips=False):
        """A real plan in the save format, exactly what a build result carries."""
        from monteur.montage import MontageEntry, MontagePlan, plan_to_dict

        plan = MontagePlan(
            music_path="/music/song.wav",
            duration=4.0,
            entries=[
                MontageEntry(
                    clip_path="/media/a.mov", source_start=1.0, source_end=3.0,
                    record_start=0.0, record_end=2.0, score=1.0,
                ),
                MontageEntry(
                    clip_path="/media/b.mov", source_start=0.5, source_end=2.5,
                    record_start=2.0, record_end=4.0, score=0.8,
                    label="the mountain pass",
                ),
            ],
            dips=[(2.0, 0.4)] if dips else [],
        )
        return plan_to_dict(plan)

    def _patch_build(self, monkeypatch, result):
        """Fake build_plan_isolated; returns the recorded calls."""
        import monteur.resolve as resolve_module

        calls = []

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
            return dict(result)

        monkeypatch.setattr(resolve_module, "build_plan_isolated", fake_build)
        return calls

    def _resolve(self, server, **payload):
        data = _post(f"{server}/api/create/resolve", payload)
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_resolve_build_happy_path(self, server, monkeypatch):
        calls = self._patch_build(
            monkeypatch, {"ok": True, "timeline": "Monteur Montage 3", "warnings": []}
        )
        plan_json = self._plan_json()
        job = self._resolve(server, plan_json=plan_json, fps=30)
        assert job["state"] == "done"
        assert job["kind"] == "resolve-build"
        assert job["result"] == {"timeline": "Monteur Montage 3", "warnings": []}
        assert {
            "stage": "resolve", "name": "building the timeline in Resolve"
        } in job["progress"]

        # The worker got the browser's plan back, faithfully round-tripped.
        from monteur.montage import plan_to_dict

        assert len(calls) == 1
        call = calls[0]
        assert plan_to_dict(call["plan"]) == plan_json
        assert call["fps"] == 30.0
        assert call["name"] == "Monteur Montage"  # the default timeline name
        assert call["titles"] is None  # no dips -> no titles
        assert call["canvas"] is None  # not sent -> project default

    def test_resolve_build_forwards_canvas(self, server, monkeypatch):
        # The UI sends the wizard's selected canvas key (buildInResolve's
        # body.canvas); the endpoint forwards it to build_plan_isolated so
        # the Resolve timeline is sized (and cine-cropped) like the file
        # download would be.
        calls = self._patch_build(
            monkeypatch, {"ok": True, "timeline": "Monteur Montage", "warnings": []}
        )
        job = self._resolve(
            server, plan_json=self._plan_json(), canvas="cine-uhd"
        )
        assert job["state"] == "done"
        assert calls[0]["canvas"] == "cine-uhd"

    def test_resolve_build_forwards_audio_for_the_sfx_track(self, server, monkeypatch):
        # The audio mode only picks the SFX track for placed sound elements
        # (A3 in "mix", A2 otherwise) — the endpoint forwards it, and old
        # payloads without the key keep the "music" default.
        calls = self._patch_build(
            monkeypatch, {"ok": True, "timeline": "Monteur Montage", "warnings": []}
        )
        job = self._resolve(server, plan_json=self._plan_json(), audio="mix")
        assert job["state"] == "done"
        assert calls[0]["audio"] == "mix"
        job = self._resolve(server, plan_json=self._plan_json())
        assert job["state"] == "done"
        assert calls[1]["audio"] == "music"

    def test_resolve_build_no_music_plan_defaults_to_original(
        self, server, monkeypatch
    ):
        # A plan without music must not be built with a phantom song bed:
        # without an explicit mode the endpoint resolves to "original"
        # (clip sound on A1, SFX on A2).
        calls = self._patch_build(
            monkeypatch, {"ok": True, "timeline": "Monteur Montage", "warnings": []}
        )
        plan_json = self._plan_json()
        plan_json["music_path"] = ""
        job = self._resolve(server, plan_json=plan_json)
        assert job["state"] == "done"
        assert calls[0]["audio"] == "original"

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_sends_canvas_with_resolve_build(self):
        # No JS harness here, so assert on the source: buildInResolve's
        # request body carries the wizard's canvas, and the cine help note
        # tells users the Resolve build applies the crop for them.
        source = _APP_HTML.read_text(encoding="utf-8")
        assert "canvas: built.canvas || canvasKey()" in source
        assert "Monteur applies that crop setting for you" in source

    def test_resolve_build_dips_plan_sends_titles_and_warnings(
        self, server, monkeypatch
    ):
        calls = self._patch_build(
            monkeypatch,
            {"ok": True, "timeline": "Trailer",
             "warnings": ["title 1 overlaps the next clip"]},
        )
        job = self._resolve(
            server, plan_json=self._plan_json(dips=True), name="Trailer"
        )
        assert job["state"] == "done"
        assert job["result"]["timeline"] == "Trailer"
        # Non-fatal title placement warnings are surfaced to the browser.
        assert job["result"]["warnings"] == ["title 1 overlaps the next clip"]

        # A plan WITH dips gets its act titles, derived by the real
        # titles_from_plan from the plan the job reconstructed.
        from monteur.resolve import titles_from_plan

        assert calls[0]["name"] == "Trailer"
        assert calls[0]["titles"] == titles_from_plan(calls[0]["plan"])
        assert calls[0]["titles"]  # the dip really yielded a title spec

    def test_resolve_build_failure_is_job_error_with_worker_message(
        self, server, monkeypatch
    ):
        message = (
            "Resolve scripting crashed the worker process — set "
            "MONTEUR_RESOLVE_PYTHON to a Resolve-compatible Python"
        )
        self._patch_build(
            monkeypatch, {"ok": False, "error": message, "reason": "native-crash"}
        )
        job = self._resolve(server, plan_json=self._plan_json())
        assert job["state"] == "error"
        assert job["message"] == message  # verbatim — it already names the fix
        assert job["result"] is None

    def test_resolve_build_missing_plan_json_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/resolve", {"fps": 25})
        assert exc_info.value.code == 400
        assert "plan_json" in json.loads(exc_info.value.read())["error"]

    def test_resolve_build_bad_plan_json_is_job_error(self, server):
        job = self._resolve(server, plan_json={"bogus": 1})
        assert job["state"] == "error"
        assert "not a Monteur plan" in job["message"]
        assert job["result"] is None

    def test_resolve_build_empty_plan_is_job_error(self, server):
        from monteur.montage import MontagePlan, plan_to_dict

        empty = plan_to_dict(MontagePlan(music_path="", duration=0.0))
        job = self._resolve(server, plan_json=empty)
        assert job["state"] == "error"
        assert "no entries" in job["message"]


class TestResolveRenderApi:
    """POST /api/resolve/render — render the built timeline to a video file.

    ``render_isolated`` is replaced at its import site (the job body does
    ``from monteur.resolve import render_isolated`` at CALL time), so
    ``monkeypatch.setattr`` on :mod:`monteur.resolve` is a complete test
    hook — no running Resolve, no worker child process.
    """

    def _patch_render(self, monkeypatch, result, percents=()):
        """Fake render_isolated; feeds ``percents`` through the progress
        callback before returning ``result``. Returns the recorded calls."""
        import monteur.resolve as resolve_module

        calls = []

        def fake_render(
            timeline, target_dir, name, preset=None, timeout=7200.0,
            progress=None,
        ):
            calls.append(
                {
                    "timeline": timeline, "target_dir": target_dir,
                    "name": name, "preset": preset,
                }
            )
            if progress is not None:
                for percent in percents:
                    progress(percent)
            return dict(result)

        monkeypatch.setattr(resolve_module, "render_isolated", fake_render)
        return calls

    def _render(self, server, **payload):
        data = _post(f"{server}/api/resolve/render", payload)
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_render_happy_path_with_live_progress(self, server, monkeypatch):
        calls = self._patch_render(
            monkeypatch,
            {"ok": True, "path": "/renders/holiday.mp4", "seconds": 12.5,
             "preset": "YouTube - 2160p"},
            percents=(30, 80),
        )
        job = self._render(
            server, timeline="Monteur Montage", target_dir="/renders",
            name="holiday", preset="2160p",
        )
        assert job["state"] == "done"
        assert job["kind"] == "resolve-render"
        assert job["result"] == {
            "path": "/renders/holiday.mp4",
            "seconds": 12.5,
            "preset": "YouTube - 2160p",
        }
        # The worker's percent stream landed as job-progress entries — the
        # shape the Studio job panel renders as a determinate bar.
        assert {
            "stage": "resolve", "name": "starting the render in Resolve"
        } in job["progress"]
        rendered = [p for p in job["progress"] if p["stage"] == "render"]
        assert [p["percent"] for p in rendered] == [30, 80]
        assert calls == [
            {"timeline": "Monteur Montage", "target_dir": "/renders",
             "name": "holiday", "preset": "2160p"}
        ]

    def test_render_defaults(self, server, monkeypatch):
        calls = self._patch_render(
            monkeypatch,
            {"ok": True, "path": "/r/monteur_render", "seconds": 1.0,
             "preset": "YouTube - 2160p"},
        )
        job = self._render(server, target_dir="/r")
        assert job["state"] == "done"
        # No timeline (current one), no name (monteur_render), no preset
        # (the worker's own 2160p default).
        assert calls == [
            {"timeline": None, "target_dir": "/r", "name": "monteur_render",
             "preset": None}
        ]

    def test_render_failure_is_job_error_with_worker_message(
        self, server, monkeypatch
    ):
        message = (
            "Resolve reported the render job as 'Failed': Disk full"
        )
        self._patch_render(monkeypatch, {"ok": False, "error": message})
        job = self._render(server, target_dir="/renders")
        assert job["state"] == "error"
        assert job["message"] == message  # verbatim — it already names the fix
        assert job["result"] is None

    def test_render_missing_target_dir_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/resolve/render", {"name": "x"})
        assert exc_info.value.code == 400
        assert "target_dir" in json.loads(exc_info.value.read())["error"]

    def test_render_bad_preset_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/resolve/render",
                {"target_dir": "/r", "preset": "720p"},
            )
        assert exc_info.value.code == 400
        assert "preset" in json.loads(exc_info.value.read())["error"]

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_render_row(self):
        # No JS harness here, so assert on the source (the established
        # pattern): the render row lives inside the Resolve-build success
        # block, with the folder/name/quality controls, live percent
        # handling in the job panel, the highlighted ready line, the calm
        # Deliver-engine note — and honest cancel copy (Resolve keeps
        # rendering; Monteur only stops watching).
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-render-block"',
            'id="cre-render-dir"',
            'id="cre-browse-render"',
            'id="cre-render-name"',
            'id="cre-render-preset"',
            'id="cre-render-btn"',
            'id="cre-render-panel"',
            'id="cre-render-done"',
            '<option value="2160p" selected>',
            '<option value="1080p">',
            '"/api/resolve/render"',
            'p.stage === "render"',
            "Your video is ready: ",
            "Resolve&rsquo;s own Deliver engine",
            "Resolve itself may still be rendering",
        ):
            assert needle in source, needle
        # The render row is nested in the Resolve-build success block (which
        # is hidden until a build succeeds), so it only ever shows after a
        # successful build: it appears after the block opens and before the
        # next sibling (the export bar) begins.
        result_block = source.split('id="cre-resolve-result"', 1)[1]
        assert result_block.index('id="cre-render-block"') < result_block.index(
            'class="export-bar"'
        )


class TestSettingsApi:
    """The AI connection settings endpoints (backend choice + API key).

    The autouse _isolated_settings fixture already points
    MONTEUR_SETTINGS_PATH at a scratch file; this fixture additionally
    strips machine-level state (env credentials, forced backend, a real
    `claude` on PATH) so the assertions are deterministic everywhere.
    """

    @pytest.fixture(autouse=True)
    def _clean_machine(self, monkeypatch):
        import monteur.ai as ai

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("MONTEUR_AI_BACKEND", raising=False)
        monkeypatch.delenv("MONTEUR_RESOLVE_PYTHON", raising=False)
        monkeypatch.setattr(ai, "_cli_path", lambda: None)
        self.ai = ai
        self.monkeypatch = monkeypatch

    def _raw_get(self, server):
        with urllib.request.urlopen(f"{server}/api/settings") as response:
            return response.read().decode()

    def test_get_shape_on_a_fresh_machine(self, server):
        data = _get(f"{server}/api/settings")
        assert data == {
            "backend": "auto",
            "api_key_set": False,
            "api_key_hint": "",
            "env_key_set": False,
            "cli_found": False,
            "backend_forced_by_env": False,
            "effective": "none",  # nothing to reach Claude with yet
            "resolve_python": "",  # no Resolve worker Python saved yet
            "resolve_python_env_set": False,
        }

    def test_post_key_saves_and_never_leaks_it(self, server):
        secret = "sk-ant-test-abcd1234wxyz"
        data = _post(f"{server}/api/settings", {"api_key": secret})
        assert data["api_key_set"] is True
        assert data["api_key_hint"] == "…wxyz"  # last 4 only
        assert data["effective"] == "api"  # auto now resolves to the key
        # The full key appears in NO response body, GET or POST.
        assert secret not in json.dumps(data)
        assert secret not in self._raw_get(server)
        # ...but it did land in the settings file (0600 on POSIX).
        from monteur.settings import api_key, settings_path

        assert api_key() == secret
        if os.name == "posix":
            assert (settings_path().stat().st_mode & 0o777) == 0o600

    def test_post_key_is_stripped(self, server):
        data = _post(f"{server}/api/settings", {"api_key": "  sk-ant-pad-1234  "})
        assert data["api_key_hint"] == "…1234"

    def test_post_empty_key_clears(self, server):
        _post(f"{server}/api/settings", {"api_key": "sk-ant-test-abcd"})
        data = _post(f"{server}/api/settings", {"api_key": ""})
        assert data["api_key_set"] is False
        assert data["api_key_hint"] == ""
        assert data["effective"] == "none"  # no key, no CLI -> nothing again

    def test_post_key_with_inner_whitespace_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/settings", {"api_key": "sk-ant oops"})
        assert exc_info.value.code == 400
        assert "API key" in json.loads(exc_info.value.read())["error"]

    def test_post_key_non_string_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/settings", {"api_key": 42})
        assert exc_info.value.code == 400

    def test_post_backend_saves_and_resolves(self, server):
        self.monkeypatch.setattr(self.ai, "_cli_path", lambda: "/fake/claude")
        data = _post(f"{server}/api/settings", {"backend": "claude-cli"})
        assert data["backend"] == "claude-cli"
        assert data["cli_found"] is True
        assert data["effective"] == "claude-cli"
        # The saved choice steers the SAME resolver every AI call goes
        # through — a later completion would use the CLI.
        assert self.ai._resolve_backend() == "claude-cli"

    def test_post_backend_invalid_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/settings", {"backend": "gemini"})
        assert exc_info.value.code == 400
        assert "backend" in json.loads(exc_info.value.read())["error"]

    def test_forced_cli_without_executable_is_effective_none(self, server):
        data = _post(f"{server}/api/settings", {"backend": "claude-cli"})
        assert data["backend"] == "claude-cli"
        assert data["cli_found"] is False
        assert data["effective"] == "none"  # resolution raises -> "none"

    def test_env_key_reported_and_wins(self, server):
        self.monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
        data = _get(f"{server}/api/settings")
        assert data["env_key_set"] is True
        assert data["effective"] == "api"

    def test_env_backend_reported_as_forced(self, server):
        self.monkeypatch.setenv("MONTEUR_AI_BACKEND", "api")
        data = _post(f"{server}/api/settings", {"backend": "claude-cli"})
        assert data["backend"] == "claude-cli"  # stored all the same
        assert data["backend_forced_by_env"] is True
        assert data["effective"] == "api"  # ...but the env var wins

    def test_ai_test_job_happy_path(self, server):
        self.monkeypatch.setattr(self.ai, "_cli_path", lambda: "/fake/claude")
        _post(f"{server}/api/settings", {"backend": "claude-cli"})
        calls = []

        def fake_complete(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return "  OK \n"

        self.monkeypatch.setattr(self.ai, "complete", fake_complete)
        data = _post(f"{server}/api/settings/test", {})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "ai-test"
        assert job["result"] == {"backend": "claude-cli", "reply": "OK"}
        assert {"stage": "ai-test", "name": "claude-cli"} in job["progress"]
        (call,) = calls
        assert "OK" in call[0]  # the sign-of-life prompt

    def test_ai_test_job_failure_carries_the_ai_message(self, server):
        self.monkeypatch.setattr(self.ai, "_cli_path", lambda: "/fake/claude")

        def failing_complete(prompt, **kwargs):
            raise self.ai.MonteurAIError("the 'claude' CLI exited with code 1")

        self.monkeypatch.setattr(self.ai, "complete", failing_complete)
        data = _post(f"{server}/api/settings/test", {})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert job["message"] == "the 'claude' CLI exited with code 1"

    def test_ai_test_job_without_any_backend_fails_helpfully(self, server):
        data = _post(f"{server}/api/settings/test", {})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        # the combined no-backend message, mentioning the Studio settings
        assert "Studio's settings" in job["message"]


class TestResolvePythonApi:
    """The DaVinci Resolve worker-Python settings + one-click detection.

    The product rule under test: the end user never sees a CLI or an
    environment variable — Studio finds (or accepts) a compatible Python
    and REMEMBERS it in the settings file. The autouse _isolated_settings
    fixture keeps that file in a scratch directory.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("MONTEUR_RESOLVE_PYTHON", raising=False)
        self.monkeypatch = monkeypatch

    # -- GET/POST /api/settings: the resolve_python field -------------------

    def test_get_reports_saved_path_and_env_flag(self, server, tmp_path):
        exe = tmp_path / "python311"
        exe.write_text("")
        from monteur.settings import save_settings

        save_settings({"resolve_python": str(exe)})
        data = _get(f"{server}/api/settings")
        assert data["resolve_python"] == str(exe)
        assert data["resolve_python_env_set"] is False

    def test_env_override_is_reported(self, server):
        self.monkeypatch.setenv("MONTEUR_RESOLVE_PYTHON", "/opt/py311/python")
        data = _get(f"{server}/api/settings")
        assert data["resolve_python_env_set"] is True

    def test_post_saves_an_existing_path(self, server, tmp_path):
        exe = tmp_path / "python311"
        exe.write_text("")
        data = _post(f"{server}/api/settings", {"resolve_python": f"  {exe}  "})
        assert data["resolve_python"] == str(exe)  # stripped + echoed back
        from monteur.settings import resolve_python

        assert resolve_python() == str(exe)

    def test_post_missing_file_is_400_and_saves_nothing(self, server, tmp_path):
        bogus = str(tmp_path / "not-there" / "python.exe")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/settings", {"resolve_python": bogus})
        assert exc_info.value.code == 400
        message = json.loads(exc_info.value.read())["error"]
        assert "no file at" in message
        assert "Find a compatible Python" in message  # points at the button
        from monteur.settings import resolve_python

        assert resolve_python() == ""

    def test_post_empty_clears(self, server, tmp_path):
        exe = tmp_path / "python311"
        exe.write_text("")
        _post(f"{server}/api/settings", {"resolve_python": str(exe)})
        data = _post(f"{server}/api/settings", {"resolve_python": ""})
        assert data["resolve_python"] == ""
        from monteur.settings import resolve_python

        assert resolve_python() == ""

    def test_post_non_string_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/settings", {"resolve_python": 311})
        assert exc_info.value.code == 400

    # -- GET /api/resolve/diagnose ------------------------------------------

    def test_diagnose_endpoint_returns_the_report(self, server, monkeypatch):
        import monteur.resolve as resolve

        report = {
            "worker_interpreter": "/py311",
            "interpreter_source": "settings",
            "status": {"connected": False, "error": "crashed", "reason": "crash"},
            # the crash-forensics fields travel verbatim to the settings UI
            "info": {
                "python_version": "3.11.9",
                "bits": 64,
                "env": {
                    "RESOLVE_SCRIPT_LIB": {
                        "value": '"C:\\x"', "quoted": True, "exists": False,
                    },
                },
                "resolve_install": {"library": None, "searched": ["C:\\x"]},
            },
            "load_test": {
                "stages": [{"stage": "locate", "ok": True, "path": "C:\\x"}],
                "crashed_at": "dll-load",
                "reason": "crash",
            },
            "verdict": "Your RESOLVE_SCRIPT_LIB has quotation marks around it.",
        }
        monkeypatch.setattr(resolve, "diagnose", lambda timeout=25.0: report)
        assert _get(f"{server}/api/resolve/diagnose") == report

    def test_diagnose_endpoint_runs_the_real_thing(self, server):
        # No fakes: the isolated child probes run for real (this container
        # has no Resolve, so the honest answer is "not connected").
        data = _get(f"{server}/api/resolve/diagnose")
        assert data["status"]["connected"] is False
        assert data["interpreter_source"] in ("env", "settings", "default")
        assert data["verdict"]
        # the crash-forensics payload is present: env flags for all four
        # variables, the fusionscript search result, and the load_test slot
        # (None here — a clean "not connected" never triggers the load test).
        env = data["info"]["env"]
        assert set(env) == {
            "RESOLVE_SCRIPT_API",
            "RESOLVE_SCRIPT_LIB",
            "PYTHONPATH",
            "MONTEUR_RESOLVE_PYTHON",
        }
        assert all(
            set(entry) >= {"value", "quoted", "exists"} for entry in env.values()
        )
        install = data["info"]["resolve_install"]
        assert install["library"] is None  # no Resolve in this container
        assert install["searched"]
        assert data["load_test"] is None
        # the Windows registry census travels too (empty off Windows, but
        # the keys must exist so the UI never branches on absence)
        assert data["info"]["registered_pythons"] == []
        assert data["info"]["registry_highest"] is None

    def test_app_renders_the_registry_census(self):
        # The diagnosis details block lists the registered Pythons and marks
        # the problematic highest one (the fusionscript registry mechanism).
        html = Path(_APP_HTML).read_text(encoding="utf-8")
        assert "Registered Pythons: " in html
        assert "registered_pythons" in html
        assert "registry_highest" in html

    # -- POST /api/resolve/detect -------------------------------------------

    def _probed(self):
        return [
            {"path": "/py313", "ok": False, "reason": "incompatible",
             "version": "3.13.0", "bits": 64},
            {"path": "/py311", "ok": True, "connected": False,
             "version": "3.11.9", "bits": 64},
        ]

    def test_detect_found_saves_to_settings_and_reports(self, server, monkeypatch):
        import monteur.resolve as resolve

        probed = self._probed()
        monkeypatch.setattr(
            resolve,
            "find_resolve_python",
            lambda timeout_per=10.0: {
                "found": "/py311", "connected": False, "probed": probed,
            },
        )
        diagnose_calls = []

        def fake_diagnose(timeout=25.0):
            # runs AFTER the save — the verdict reflects the new interpreter
            from monteur.settings import resolve_python

            diagnose_calls.append(resolve_python())
            return {"verdict": "Saved Python is ready.", "status": {}}

        monkeypatch.setattr(resolve, "diagnose", fake_diagnose)
        data = _post(f"{server}/api/resolve/detect", {})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "resolve-detect"
        assert {"stage": "detect", "name": "probing Python installations"} in job["progress"]
        result = job["result"]
        assert result["found"] == "/py311"
        assert result["connected"] is False
        assert result["version"] == "3.11.9"  # from the winning probe entry
        assert result["probed"] == probed
        assert result["verdict"] == "Saved Python is ready."
        # THE point of the endpoint: the find was saved automatically...
        from monteur.settings import resolve_python

        assert resolve_python() == "/py311"
        # ...the verdict was computed after that save...
        assert diagnose_calls == ["/py311"]
        # ...and the settings view reflects it immediately.
        assert _get(f"{server}/api/settings")["resolve_python"] == "/py311"

    def test_detect_none_found_is_a_successful_job(self, server, monkeypatch):
        import monteur.resolve as resolve

        probed = [
            {"path": "/py313", "ok": False, "reason": "incompatible",
             "version": "3.13.0", "bits": 64},
        ]
        monkeypatch.setattr(
            resolve,
            "find_resolve_python",
            lambda timeout_per=10.0: {
                "found": None, "connected": False, "probed": probed,
            },
        )
        monkeypatch.setattr(
            resolve,
            "diagnose",
            lambda timeout=25.0: {
                "verdict": "No compatible Python on this machine yet.",
                "status": {},
            },
        )
        data = _post(f"{server}/api/resolve/detect", {})
        job = _wait_for_job(server, data["job"])
        # Not-found is information for the guided-install UI, NOT an error.
        assert job["state"] == "done"
        result = job["result"]
        assert result["found"] is None
        assert result["connected"] is False
        assert result["version"] == ""
        assert result["probed"] == probed
        assert result["verdict"] == "No compatible Python on this machine yet."
        from monteur.settings import resolve_python

        assert resolve_python() == ""  # nothing saved

    def test_detect_for_real_probes_this_machine(self, server):
        # No fakes: candidates are real interpreters on this box. Whatever
        # the outcome, the job must SUCCEED and report every probe honestly,
        # and a find must land in the settings file.
        data = _post(f"{server}/api/resolve/detect", {})
        job = _wait_for_job(server, data["job"], timeout=120.0)
        assert job["state"] == "done"
        result = job["result"]
        assert isinstance(result["probed"], list) and result["probed"]
        assert result["verdict"]
        from monteur.settings import resolve_python

        if result["found"]:
            assert resolve_python() == result["found"]
            assert result["probed"][-1]["ok"] is True
        else:
            assert resolve_python() == ""


class TestSoundElements:
    """The "elements" payload: the user's sound library placed as real clips."""

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    @pytest.fixture()
    def library(self, tmp_path):
        np = pytest.importorskip("numpy")
        import wave

        folder = tmp_path / "sfx"
        folder.mkdir()
        rate = 22050

        def write(name, samples):
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
            with wave.open(str(folder / name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm.tobytes())

        rng = np.random.default_rng(3)
        t = np.linspace(0.0, 0.8, int(0.8 * rate), endpoint=False)
        write("hit.wav", rng.uniform(-1, 1, len(t)) * np.exp(-t * 8) * 0.9)
        write("swoosh.wav", rng.uniform(-1, 1, rate) * np.hanning(rate) * 0.9)
        ramp = np.linspace(0.0, 1.0, 3 * rate, endpoint=False)
        write("rise.wav", rng.uniform(-1, 1, 3 * rate) * ramp**2 * 0.9)
        return str(folder)

    def _build_payload(self, library, **extra):
        payload = {
            "folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
            "fps": 25, "format": "fcpxml", "elements": library,
        }
        payload.update(extra)
        return payload

    def test_build_with_elements_places_and_reports(self, server, library):
        data = _post(
            f"{server}/api/create/build", self._build_payload(library)
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        # the scan of the library is a visible progress stage
        stages = [p["stage"] for p in job["progress"]]
        assert "elements" in stages
        entry = next(p for p in job["progress"] if p["stage"] == "elements")
        assert entry["name"] == "sfx"
        result = job["result"]
        # elements imply the SFX layer, and the notes say what happened
        notes = result["plan"]["notes"]
        assert any(n.startswith("sfx layer:") for n in notes)
        assert any(n.startswith("sound elements:") for n in notes)
        # placed cues travel in the plan_json with their concrete files
        cues = result["plan_json"]["sfx"]
        assert cues, "the SFX layer must be planned"
        filed = [c for c in cues if c.get("file")]
        assert filed, "at least one library file must be placed"
        assert all(c["file"].startswith(library) for c in filed)
        # ...and the FCPXML carries them as real effects clips
        assert 'audioRole="effects"' in result["content"]

    def test_elements_with_explicit_sfx_false_is_an_error(self, server, library):
        data = _post(
            f"{server}/api/create/build",
            self._build_payload(library, sfx=False),
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "SFX layer" in job["message"]

    def test_elements_folder_missing_is_a_clean_error(self, server):
        data = _post(
            f"{server}/api/create/build",
            {
                "folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
                "elements": "/no/such/library",
            },
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "not a directory" in job["message"]

    def test_elements_persist_in_the_autosaved_draft(self, server, library):
        # The browser sends sfx=true alongside a filled elements folder
        # (creBuildBody) — both land in the autosave's settings for resume.
        data = _post(
            f"{server}/api/create/build", self._build_payload(library, sfx=True)
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        full = _get(f"{server}/api/drafts/autosave")
        assert full["settings"]["elements"] == library
        assert full["settings"]["sfx"] is True

    def test_revise_reassigns_from_the_library(self, server, library):
        build = _post(
            f"{server}/api/create/build", self._build_payload(library)
        )
        build_job = _wait_for_job(server, build["job"])
        assert build_job["state"] == "done"
        plan_json = build_job["result"]["plan_json"]
        assert any(c.get("file") for c in plan_json["sfx"])

        revise = _post(
            f"{server}/api/create/revise",
            {
                "plan_json": plan_json, "folder": self.DEMO,
                "brief": "ruhiger", "elements": library, "fps": 25,
            },
        )
        job = _wait_for_job(server, revise["job"])
        assert job["state"] == "done"
        # the library ran again on the revised plan (elements stage + files)
        assert any(p["stage"] == "elements" for p in job["progress"])
        revised_cues = job["result"]["plan_json"]["sfx"]
        assert any(c.get("file") for c in revised_cues)

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_elements_field(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-elements"',
            'id="cre-browse-elements"',
            "Sound elements folder (optional)",
            "places them as real clips on their own audio track",
            "riser into the drop, impact on the smash cuts",
            "body.elements = elements",
            'p.stage === "elements"',
            "Rating your sound elements…",
        ):
            assert needle in source, needle
        # filling the folder auto-enables the SFX layer...
        assert 'if (this.value.trim()) $("cre-sfx").checked = true;' in source
        # ...and the folder is remembered with the other draft settings
        assert '"ai_cut", "brief", "elements"' in source
        assert 'if (typeof s.elements === "string")' in source
        # the elements input lives in the Fine-tune block, next to the SFX
        # checkbox
        finetune = source.split('id="cre-finetune"', 1)[1].split("</details>", 1)[0]
        assert 'id="cre-sfx"' in finetune
        assert 'id="cre-elements"' in finetune


# --- "Sehen ohne Resolve": storyboard thumbnails + the preview player --------


def _get_raw(url, headers=None):
    """A raw GET for binary endpoints: (status, headers dict, body bytes).

    HTTPError responses (the thumb placeholder 404, the preview 416) are
    returned like any other — their status/headers/body ARE the contract.
    """
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


def _preview_plan_dict(music=True):
    """A tiny real plan over the demo footage, in the plan_json save format:
    three entries with one 0.4 s record gap (a black dip) — the same shape
    monteur.preview's own tests render."""
    from monteur.montage import MontageEntry, MontagePlan, plan_to_dict

    demo = _DEMO_FOOTAGE
    entries = [
        MontageEntry(
            clip_path=str(demo / "clip_A.mp4"),
            source_start=1.0, source_end=3.0,
            record_start=0.0, record_end=2.0, score=1.0,
        ),
        MontageEntry(
            clip_path=str(demo / "clip_C.mp4"),
            source_start=2.0, source_end=4.0,
            record_start=2.4, record_end=4.4, score=0.9,
        ),
        MontageEntry(
            clip_path=str(demo / "clip_D.mp4"),
            source_start=0.5, source_end=2.1,
            record_start=4.4, record_end=6.0, score=0.8,
        ),
    ]
    plan = MontagePlan(
        music_path=str(demo / "song.wav") if music else "",
        duration=6.0,
        music_start=8.0 if music else 0.0,
        entries=entries,
        dips=[(2.0, 0.4)],
    )
    return plan_to_dict(plan)


class TestThumbApi:
    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def _url(self, server, clip, t=1.0, w=160):
        return f"{server}/api/thumb?clip={_quote(clip)}&t={t}&w={w}"

    def test_thumb_serves_a_jpeg_with_long_cache_headers(self, server):
        status, headers, body = _get_raw(
            self._url(server, f"{self.DEMO}/clip_A.mp4")
        )
        assert status == 200
        assert headers["Content-Type"] == "image/jpeg"
        assert body[:3] == b"\xff\xd8\xff"  # real JPEG bytes, not a stub
        assert len(body) > 1000
        assert int(headers["Content-Length"]) == len(body)
        # long client-side cache: the URL's frame never changes for an
        # unchanged clip (the server key includes the file's mtime)
        assert "max-age=31536000" in headers["Cache-Control"]
        assert "immutable" in headers["Cache-Control"]

    def test_thumb_second_request_is_a_cache_hit(self, server):
        from monteur.web import server as web_server

        url = self._url(server, f"{self.DEMO}/clip_C.mp4", t=2.5, w=144)
        cache_dir = Path(web_server._thumb_dir())
        before = set(cache_dir.glob("*.jpg"))
        status, _, first = _get_raw(url)
        assert status == 200
        created = set(cache_dir.glob("*.jpg")) - before
        assert len(created) == 1  # exactly one new cache file for this key
        cached_file = created.pop()
        stamp = cached_file.stat().st_mtime_ns

        status, _, second = _get_raw(url)
        assert status == 200
        assert second == first  # byte-identical repeat
        # instant repeat: the frame was NOT re-extracted (same file, same
        # mtime, no new cache entries)
        assert cached_file.stat().st_mtime_ns == stamp
        assert set(cache_dir.glob("*.jpg")) - before == {cached_file}

    def test_thumb_width_is_part_of_the_cache_key(self, server):
        from monteur.web import server as web_server

        cache_dir = Path(web_server._thumb_dir())
        before = set(cache_dir.glob("*.jpg"))
        for w in (96, 128):
            status, _, _body = _get_raw(
                self._url(server, f"{self.DEMO}/clip_D.mp4", t=1.0, w=w)
            )
            assert status == 200
        assert len(set(cache_dir.glob("*.jpg")) - before) == 2

    def test_thumb_bad_path_is_404_with_a_placeholder_image(self, server):
        status, headers, body = _get_raw(
            self._url(server, f"{self.DEMO}/no_such_clip.mp4")
        )
        assert status == 404
        # the body is a tiny image the UI can render as a quiet gray tile
        assert headers["Content-Type"] == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_thumb_missing_clip_param_is_404_placeholder(self, server):
        status, headers, _body = _get_raw(f"{server}/api/thumb?t=1&w=160")
        assert status == 404
        assert headers["Content-Type"] == "image/png"

    def test_thumb_malformed_numbers_are_404_placeholder(self, server):
        status, _, _body = _get_raw(
            f"{server}/api/thumb?clip={_quote(self.DEMO + '/clip_A.mp4')}"
            f"&t=abc&w=xyz"
        )
        assert status == 404


class TestPreviewApi:
    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def _render(self, server, payload=None):
        body = {"plan_json": _preview_plan_dict()}
        body.update(payload or {})
        data = _post(f"{server}/api/create/preview", body)
        job = _wait_for_job(server, data["job"])
        return job

    def test_preview_job_happy_path_and_range_serving(self, server):
        job = self._render(server, {"audio": "music"})
        assert job["state"] == "done"
        assert job["kind"] == "preview"

        result = job["result"]
        import re as re_mod

        assert re_mod.fullmatch(
            r"/api/preview/[0-9a-f]{16}\.mp4", result["url"]
        )
        assert result["duration"] == pytest.approx(6.0, abs=0.3)
        assert result["width"] == 640

        # the engine's per-segment progress flowed into the job entries
        stages = [p["stage"] for p in job["progress"]]
        assert stages and set(stages) == {"preview"}
        last = job["progress"][-1]
        assert last["index"] == last["total"]  # counted all the way through

        # full GET: a real MP4, range-capable
        url = f"{server}{result['url']}"
        status, headers, body = _get_raw(url)
        assert status == 200
        assert headers["Content-Type"] == "video/mp4"
        assert headers["Accept-Ranges"] == "bytes"
        assert int(headers["Content-Length"]) == len(body)
        assert b"ftyp" in body[:16]  # MP4 container magic
        size = len(body)

        # Range GET: <video> seeking needs true 206 partial responses
        status, headers, part = _get_raw(url, headers={"Range": "bytes=0-99"})
        assert status == 206
        assert headers["Content-Range"] == f"bytes 0-99/{size}"
        assert len(part) == 100
        assert part == body[:100]

        status, headers, tail = _get_raw(url, headers={"Range": "bytes=100-"})
        assert status == 206
        assert headers["Content-Range"] == f"bytes 100-{size - 1}/{size}"
        assert tail == body[100:]

        # unsatisfiable range: 416 with the total size
        status, headers, _body = _get_raw(
            url, headers={"Range": f"bytes={size}-"}
        )
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{size}"

    def test_second_preview_replaces_the_first(self, server):
        first = self._render(server)
        assert first["state"] == "done"
        first_url = f"{server}{first['result']['url']}"
        status, _, _body = _get_raw(first_url)
        assert status == 200

        second = self._render(server)
        assert second["state"] == "done"
        assert second["result"]["url"] != first["result"]["url"]
        # the previews dir is capped at the latest: the old file is gone...
        status, _, _body = _get_raw(first_url)
        assert status == 404
        # ...and the new one serves
        status, _, _body = _get_raw(f"{server}{second['result']['url']}")
        assert status == 200

    def test_preview_defaults_to_original_audio_without_music(self, server):
        job = self._render(server, {"plan_json": _preview_plan_dict(music=False)})
        assert job["state"] == "done"

    def test_preview_music_mode_without_music_is_a_job_error(self, server):
        job = self._render(
            server,
            {"plan_json": _preview_plan_dict(music=False), "audio": "music"},
        )
        assert job["state"] == "error"
        assert "no music" in job["message"]

    def test_preview_missing_plan_json_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/create/preview", {"audio": "music"})
        assert exc_info.value.code == 400
        assert "plan_json" in json.loads(exc_info.value.read())["error"]

    def test_preview_bad_plan_is_a_job_error(self, server):
        data = _post(
            f"{server}/api/create/preview", {"plan_json": {"nonsense": True}}
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "not a Monteur plan" in job["message"]

    def test_preview_empty_plan_is_a_job_error(self, server):
        plan = _preview_plan_dict()
        plan["entries"] = []
        data = _post(f"{server}/api/create/preview", {"plan_json": plan})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "no entries" in job["message"]

    def test_unknown_preview_token_is_404(self, server):
        status, _, _body = _get_raw(f"{server}/api/preview/{'0' * 16}.mp4")
        assert status == 404
        status, _, _body = _get_raw(f"{server}/api/preview/evil.txt")
        assert status == 404


class TestStoryboardAndPreviewUi:
    """Static asserts on app.html: the storyboard + preview player markup."""

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_storyboard(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-storyboard"',
            'id="cre-sb-board"',
            'id="cre-sb-story"',
            "/api/thumb?clip=",       # storyboard thumbs come from the API
            'loading = "lazy"',       # thumbs lazy-load as cards scroll in
            "pin-btn sb-pin",         # THE pin toggle lives on the card
            "sb-dip",                 # dip markers render between cards
            "sbActTitles",            # composer act labels degrade gracefully
        ):
            assert needle in source, needle
        # the storyboard replaced the old revise pin list completely
        assert "cre-rev-entries" not in source
        assert "renderReviseEntries" not in source
        # pins still feed the same revise state (rev.pins) — one pin UI
        assert "rev.pins.indexOf(stamp)" in source

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_preview_player(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-preview-btn"',
            'id="cre-preview-video"',
            "/api/create/preview",
            'p.stage === "preview"',
            "Rendered by Monteur&rsquo;s own engine in seconds",
            "Dissolves show as hard cuts here; the Resolve build stays the reference.",
        ):
            assert needle in source, needle
        # the preview row sits at the TOP of the result card — before the
        # "Build in DaVinci Resolve" block
        assert source.index('id="cre-preview-btn"') < source.index('id="cre-resolve-btn"')
        # a new build/revise/apply/resume invalidates the player
        assert source.count("resetPreview()") >= 5


class TestExportVideoApi:
    """POST /api/create/export-video — the Direct Export job.

    ``render_export`` is replaced at its import site (the job body does
    ``from monteur.preview import render_export`` at CALL time), so
    ``monkeypatch.setattr`` on :mod:`monteur.preview` is a complete test
    hook — no ffmpeg render behind the endpoint tests.
    """

    def _patch_export(self, monkeypatch, result=None, ticks=()):
        """Fake render_export; feeds ``ticks`` through the progress
        callback before returning. Returns the recorded calls."""
        import monteur.preview as preview_module

        calls = []

        def fake_export(
            plan, out_path, *, canvas, fps, audio, quality, progress=None,
            size=None, grade=None, cancel=None,
        ):
            calls.append(
                {
                    "entries": len(plan.entries),
                    "out_path": out_path,
                    "canvas": canvas,
                    "fps": fps,
                    "audio": audio,
                    "quality": quality,
                    "size": size,
                    "grade": grade,
                }
            )
            if progress is not None:
                for done, total, label in ticks:
                    progress(done, total, label)
            return dict(
                result
                or {
                    "path": out_path, "duration": 6.0, "width": 3840,
                    "height": 2160, "seconds": 12.5, "notes": [],
                }
            )

        monkeypatch.setattr(preview_module, "render_export", fake_export)
        return calls

    def _export(self, server, **payload):
        payload.setdefault("plan_json", _preview_plan_dict())
        data = _post(f"{server}/api/create/export-video", payload)
        assert isinstance(data["job"], str) and data["job"]
        return _wait_for_job(server, data["job"])

    def test_export_happy_path_with_defaults(self, server, monkeypatch, tmp_path):
        target = str(tmp_path / "exports" / "nested")
        notes = ["dissolve into clip_D.mp4 at 4.4s: ... hard cut instead"]
        calls = self._patch_export(
            monkeypatch,
            result={
                "path": target + "/monteur_export.mp4", "duration": 6.0,
                "width": 3840, "height": 2160, "seconds": 12.5,
                "notes": notes,
            },
            ticks=[(1, 3, "clip_A.mp4"), (2, 3, "black"), (3, 3, "mux")],
        )
        job = self._export(server, target_dir=target)
        assert job["state"] == "done"
        assert job["kind"] == "export-video"
        # defaults: uhd canvas, high quality, 25 fps, the plan's music,
        # the default file name with .mp4 appended, NO size override
        assert calls == [
            {
                "entries": 3,
                "out_path": os.path.join(target, "monteur_export.mp4"),
                "canvas": "uhd",
                "fps": 25.0,
                "audio": "music",
                "quality": "high",
                "size": None,
                "grade": None,
            }
        ]
        # the target folder was created with parents before the render
        assert Path(target).is_dir()
        # the engine's staged progress flowed into the job entries
        stages = [p for p in job["progress"] if p["stage"] == "export"]
        assert [(p["index"], p["total"], p["name"]) for p in stages] == [
            (1, 3, "clip_A.mp4"), (2, 3, "black"), (3, 3, "mux"),
        ]
        # the result is exactly the documented shape, notes verbatim
        assert job["result"] == {
            "path": target + "/monteur_export.mp4",
            "duration": 6.0,
            "seconds": 12.5,
            "notes": notes,
        }

    def test_export_forwards_explicit_options(self, server, monkeypatch, tmp_path):
        calls = self._patch_export(monkeypatch)
        job = self._export(
            server, target_dir=str(tmp_path), name="holiday", canvas="cine",
            audio="mix", quality="medium", fps=30,
        )
        assert job["state"] == "done"
        assert calls == [
            {
                "entries": 3,
                "out_path": str(tmp_path / "holiday.mp4"),
                "canvas": "cine",
                "fps": 30.0,
                "audio": "mix",
                "quality": "medium",
                "size": None,
                "grade": None,
            }
        ]
        assert calls[0]["grade"] is None  # no grade sent -> neutral (None)

    def test_export_forwards_the_colour_grade(self, server, monkeypatch, tmp_path):
        from monteur.color import Grade

        calls = self._patch_export(monkeypatch)
        job = self._export(
            server, target_dir=str(tmp_path),
            grade={"brightness": 0.2, "contrast": -0.3, "warmth": 0.5, "look": "custom"},
        )
        assert job["state"] == "done"
        g = calls[0]["grade"]
        assert isinstance(g, Grade)
        assert (g.brightness, g.contrast, g.warmth) == (0.2, -0.3, 0.5)
        assert g.saturation == 0.0  # absent control defaults neutral

    def test_export_audio_defaults_to_original_without_music(
        self, server, monkeypatch, tmp_path
    ):
        calls = self._patch_export(monkeypatch)
        job = self._export(
            server, plan_json=_preview_plan_dict(music=False),
            target_dir=str(tmp_path),
        )
        assert job["state"] == "done"
        assert calls[0]["audio"] == "original"

    def test_export_music_mode_without_music_is_a_job_error(
        self, server, monkeypatch, tmp_path
    ):
        self._patch_export(monkeypatch)
        job = self._export(
            server, plan_json=_preview_plan_dict(music=False),
            target_dir=str(tmp_path), audio="music",
        )
        assert job["state"] == "error"
        assert "no music" in job["message"]

    def test_export_missing_plan_json_is_400(self, server, tmp_path):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/export-video",
                {"target_dir": str(tmp_path)},
            )
        assert exc_info.value.code == 400
        assert "plan_json" in json.loads(exc_info.value.read())["error"]

    def test_export_missing_target_dir_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/export-video",
                {"plan_json": _preview_plan_dict()},
            )
        assert exc_info.value.code == 400
        assert "target_dir" in json.loads(exc_info.value.read())["error"]

    def test_export_bad_quality_is_400(self, server, tmp_path):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/export-video",
                {
                    "plan_json": _preview_plan_dict(),
                    "target_dir": str(tmp_path),
                    "quality": "ultra",
                },
            )
        assert exc_info.value.code == 400
        assert "quality" in json.loads(exc_info.value.read())["error"]

    def test_export_bad_plan_is_a_job_error(self, server, tmp_path):
        job = self._export(
            server, plan_json={"nonsense": True}, target_dir=str(tmp_path)
        )
        assert job["state"] == "error"
        assert "not a Monteur plan" in job["message"]

    def test_export_empty_plan_is_a_job_error(self, server, tmp_path):
        plan = _preview_plan_dict()
        plan["entries"] = []
        job = self._export(server, plan_json=plan, target_dir=str(tmp_path))
        assert job["state"] == "error"
        assert "no entries" in job["message"]

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_export_video_block(self):
        # Static asserts (the established pattern): the Export-video block
        # with folder/name/quality controls, the staged progress handling,
        # the ready line with the notes list, and the calm own-engine
        # sentence — placed right AFTER the Build-in-Resolve block and
        # before the timeline download bar.
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-export-block"',
            'id="cre-export-dir"',
            'id="cre-browse-export"',
            'id="cre-export-name"',
            'id="cre-export-quality"',
            'id="cre-export-btn"',
            'id="cre-export-panel"',
            'id="cre-export-done"',
            'id="cre-export-notes"',
            '<option value="high" selected>',
            '<option value="medium">',
            '"/api/create/export-video"',
            'p.stage === "export"',
            "Your video is ready: ",
            "Resolve stays the path for grading and fine-tuning",
        ):
            assert needle in source, needle
        # the export block sits right after the Build-in-DaVinci-Resolve
        # block (which holds the Resolve render row) and before the
        # timeline download bar
        resolve_block = source.index('id="cre-resolve-btn"')
        render_row = source.index('id="cre-render-block"')
        export_block = source.index('id="cre-export-block"')
        download_bar = source.index('id="cre-fmt-label"')
        assert resolve_block < render_row < export_block < download_bar


# --- YouTube upload connection (monteur.youtube behind the server) -----------


def _story_plan_dict():
    """A hand-made plan for the prefill tests: three labelled entries whose
    clip changes 15 s apart (-> three chapters) plus a composed story note.
    Prefill never touches the files, so the paths don't need to exist."""
    from monteur.montage import MontageEntry, MontagePlan, plan_to_dict

    entries = [
        MontageEntry(
            clip_path="/footage/ride.mp4", source_start=0.0, source_end=10.0,
            record_start=0.0, record_end=15.0, score=1.0,
            label="Overtake in a left curve",
        ),
        MontageEntry(
            clip_path="/footage/summit.mp4", source_start=0.0, source_end=10.0,
            record_start=15.0, record_end=30.0, score=0.9,
            label="Summit sunrise",
        ),
        MontageEntry(
            clip_path="/footage/camp.mp4", source_start=0.0, source_end=10.0,
            record_start=30.0, record_end=45.0, score=0.8,
            label="Camp fire evening",
        ),
    ]
    plan = MontagePlan(
        music_path="", duration=45.0, entries=entries,
        notes=["story: Three friends ride over the Alps.", "act 1: departure"],
    )
    return plan_to_dict(plan)


class TestYouTubeApi:
    """The /api/youtube/* surface: status/credentials, the loopback OAuth
    handshake, the upload job and the offline prefill. All Google traffic
    is monkeypatched on monteur.youtube (the server resolves its functions
    at call time), so no test leaves the process."""

    def _connect_settings(self):
        from monteur.settings import save_settings

        save_settings(
            {
                "youtube_client_id": "cid",
                "youtube_client_secret": "cs",
                "youtube_refresh_token": "1//rt",
            }
        )

    # -- status + credentials ------------------------------------------------

    def test_status_starts_unconfigured(self, server):
        assert _get(f"{server}/api/youtube/status") == {
            "configured": False, "connected": False, "channel": "",
        }

    def test_credentials_save_and_clear(self, server):
        data = _post(
            f"{server}/api/youtube/credentials",
            {"client_id": " cid ", "client_secret": " cs "},
        )
        assert data == {"configured": True, "connected": False, "channel": ""}
        from monteur.settings import youtube_client_id, youtube_client_secret

        assert youtube_client_id() == "cid"  # stripped
        assert youtube_client_secret() == "cs"

        # Clearing the project also disconnects — the old token belongs
        # to the old Google project.
        from monteur.settings import save_settings, youtube_refresh_token

        save_settings({"youtube_refresh_token": "1//rt", "youtube_channel": "C"})
        data = _post(
            f"{server}/api/youtube/credentials",
            {"client_id": "", "client_secret": ""},
        )
        assert data == {"configured": False, "connected": False, "channel": ""}
        assert youtube_refresh_token() == ""

    def test_credentials_must_come_as_a_pair(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/youtube/credentials",
                {"client_id": "cid", "client_secret": ""},
            )
        assert exc_info.value.code == 400
        assert "pair" in json.loads(exc_info.value.read())["error"]

    def test_credentials_must_be_strings(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/youtube/credentials",
                {"client_id": 42, "client_secret": "cs"},
            )
        assert exc_info.value.code == 400

    # -- the loopback OAuth flow ----------------------------------------------

    def test_connect_needs_credentials_first(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/youtube/connect", {})
        assert exc_info.value.code == 400
        assert "client id" in json.loads(exc_info.value.read())["error"]

    def test_connect_points_the_redirect_at_this_server(self, server):
        _post(
            f"{server}/api/youtube/credentials",
            {"client_id": "cid", "client_secret": "cs"},
        )
        data = _post(f"{server}/api/youtube/connect", {})
        port = int(server.rsplit(":", 1)[1])
        assert data["redirect_uri"] == (
            f"http://127.0.0.1:{port}/api/youtube/callback"
        )
        query = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(data["auth_url"]).query)
        )
        assert query["client_id"] == "cid"
        assert query["redirect_uri"] == data["redirect_uri"]
        assert query["scope"].endswith("youtube.upload")
        assert query["access_type"] == "offline"
        assert query["prompt"] == "consent"
        assert query["state"]

    def _read_page(self, url):
        try:
            with urllib.request.urlopen(url) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read().decode()

    def test_callback_state_mismatch_renders_an_error_page(self, server):
        _post(
            f"{server}/api/youtube/credentials",
            {"client_id": "cid", "client_secret": "cs"},
        )
        _post(f"{server}/api/youtube/connect", {})
        status, headers, body = self._read_page(
            f"{server}/api/youtube/callback?code=abc&state=WRONG"
        )
        assert status == 400
        assert "text/html" in headers.get("Content-Type", "")
        assert "stale" in body
        assert _get(f"{server}/api/youtube/status")["connected"] is False

    def test_callback_happy_path_stores_the_refresh_token(self, server, monkeypatch):
        _post(
            f"{server}/api/youtube/credentials",
            {"client_id": "cid", "client_secret": "cs"},
        )
        data = _post(f"{server}/api/youtube/connect", {})
        query = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(data["auth_url"]).query)
        )
        seen = {}

        def fake_exchange(client_id, client_secret, code, redirect_uri, transport=None):
            seen.update(
                client_id=client_id, client_secret=client_secret,
                code=code, redirect_uri=redirect_uri,
            )
            return {"access_token": "at", "refresh_token": "1//fresh"}

        monkeypatch.setattr("monteur.youtube.exchange_code", fake_exchange)
        status, headers, body = self._read_page(
            f"{server}/api/youtube/callback?code=the-code&state={query['state']}"
        )
        assert status == 200
        assert "YouTube connected" in body
        assert "window.close" in body  # the tab closes itself
        assert seen == {
            "client_id": "cid", "client_secret": "cs", "code": "the-code",
            "redirect_uri": data["redirect_uri"],
        }
        from monteur.settings import youtube_refresh_token

        assert youtube_refresh_token() == "1//fresh"
        assert _get(f"{server}/api/youtube/status")["connected"] is True

        # The state is single-use: replaying the same callback must fail.
        status, _headers, body = self._read_page(
            f"{server}/api/youtube/callback?code=the-code&state={query['state']}"
        )
        assert status == 400 and "stale" in body

    def test_callback_exchange_error_renders_readably(self, server, monkeypatch):
        from monteur.youtube import MonteurYouTubeError

        _post(
            f"{server}/api/youtube/credentials",
            {"client_id": "cid", "client_secret": "cs"},
        )
        data = _post(f"{server}/api/youtube/connect", {})
        state = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(data["auth_url"]).query)
        )["state"]

        def boom(*args, **kwargs):
            raise MonteurYouTubeError("could not connect YouTube: invalid_grant")

        monkeypatch.setattr("monteur.youtube.exchange_code", boom)
        status, _headers, body = self._read_page(
            f"{server}/api/youtube/callback?code=x&state={state}"
        )
        assert status == 502
        assert "invalid_grant" in body

    def test_callback_without_refresh_token_explains(self, server, monkeypatch):
        _post(
            f"{server}/api/youtube/credentials",
            {"client_id": "cid", "client_secret": "cs"},
        )
        data = _post(f"{server}/api/youtube/connect", {})
        state = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(data["auth_url"]).query)
        )["state"]
        monkeypatch.setattr(
            "monteur.youtube.exchange_code",
            lambda *a, **k: {"access_token": "at"},  # no refresh_token
        )
        status, _headers, body = self._read_page(
            f"{server}/api/youtube/callback?code=x&state={state}"
        )
        assert status == 502
        assert "refresh token" in body

    def test_callback_google_error_param_renders(self, server):
        status, _headers, body = self._read_page(
            f"{server}/api/youtube/callback?error=access_denied"
        )
        assert status == 400
        assert "access_denied" in body

    def test_disconnect_clears_the_token_but_keeps_credentials(self, server):
        self._connect_settings()
        data = _post(f"{server}/api/youtube/disconnect", {})
        assert data == {"configured": True, "connected": False, "channel": ""}
        from monteur.settings import youtube_refresh_token

        assert youtube_refresh_token() == ""

    # -- the upload job --------------------------------------------------------

    def _upload_payload(self, tmp_path, **extra):
        video = tmp_path / "final.mp4"
        video.write_bytes(b"video-bytes" * 100)
        return {"path": str(video), "title": "My Cut", **extra}

    def test_upload_validates_up_front(self, server, tmp_path):
        self._connect_settings()
        for payload, needle in (
            ({"title": "t"}, "path"),
            ({"path": str(tmp_path / "f.mp4")}, "title"),
            ({"path": str(tmp_path / "missing.mp4"), "title": "t"}, "no video file"),
            (self._upload_payload(tmp_path, privacy="public"), "privacy"),
        ):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(f"{server}/api/youtube/upload", payload)
            assert exc_info.value.code == 400
            assert needle in json.loads(exc_info.value.read())["error"]

    def test_upload_rejects_non_list_tags(self, server, tmp_path):
        self._connect_settings()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/youtube/upload",
                self._upload_payload(tmp_path, tags={"not": "a list"}),
            )
        assert exc_info.value.code == 400
        assert "tags" in json.loads(exc_info.value.read())["error"]

    def test_upload_needs_a_connection(self, server, tmp_path):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/youtube/upload", self._upload_payload(tmp_path))
        assert exc_info.value.code == 400
        assert "not connected" in json.loads(exc_info.value.read())["error"]

    def test_upload_job_happy_path(self, server, tmp_path, monkeypatch):
        self._connect_settings()
        seen = {}

        def fake_refresh(client_id, client_secret, refresh_token, transport=None):
            seen["refresh"] = (client_id, client_secret, refresh_token)
            return "fresh-at"

        def fake_upload(token, path, *, title, description, tags, privacy, progress):
            seen["upload"] = {
                "token": token, "path": path, "title": title,
                "description": description, "tags": tags, "privacy": privacy,
            }
            progress(500, 1100)
            progress(1100, 1100)
            return {"video_id": "vid42", "channel": "My Channel"}

        monkeypatch.setattr("monteur.youtube.refresh_access_token", fake_refresh)
        monkeypatch.setattr("monteur.youtube.upload_video", fake_upload)
        payload = self._upload_payload(
            tmp_path, description="d", tags="travel, alps", privacy="private"
        )
        data = _post(f"{server}/api/youtube/upload", payload)
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["kind"] == "youtube-upload"
        assert seen["refresh"] == ("cid", "cs", "1//rt")
        assert seen["upload"]["token"] == "fresh-at"
        assert seen["upload"]["tags"] == ["travel", "alps"]  # comma string split
        assert seen["upload"]["privacy"] == "private"

        result = job["result"]
        assert result["video_id"] == "vid42"
        assert result["url"] == "https://studio.youtube.com/video/vid42/edit"
        assert result["watch_url"] == "https://www.youtube.com/watch?v=vid42"
        assert result["privacy"] == "private"
        assert result["channel"] == "My Channel"
        assert result["notes"] == []

        # Byte progress entries the UI turns into the bar.
        uploads = [p for p in job["progress"] if p["stage"] == "upload"]
        assert [(p["sent"], p["total"]) for p in uploads] == [
            (500, 1100), (1100, 1100),
        ]
        assert any(p["stage"] == "auth" for p in job["progress"])

        # The channel hint is remembered for the settings status line.
        from monteur.settings import youtube_channel

        assert youtube_channel() == "My Channel"
        assert _get(f"{server}/api/youtube/status")["channel"] == "My Channel"

    def test_upload_token_expired_refreshes_once_and_retries(
        self, server, tmp_path, monkeypatch
    ):
        from monteur.youtube import TokenExpired

        self._connect_settings()
        calls = {"refresh": 0, "upload": 0}

        def fake_refresh(*args, **kwargs):
            calls["refresh"] += 1
            return f"at-{calls['refresh']}"

        def fake_upload(token, path, **kwargs):
            calls["upload"] += 1
            if calls["upload"] == 1:
                raise TokenExpired("stale")
            assert token == "at-2"  # the RETRY runs on the re-refreshed token
            return {"video_id": "v2", "channel": ""}

        monkeypatch.setattr("monteur.youtube.refresh_access_token", fake_refresh)
        monkeypatch.setattr("monteur.youtube.upload_video", fake_upload)
        data = _post(
            f"{server}/api/youtube/upload", self._upload_payload(tmp_path)
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert calls == {"refresh": 2, "upload": 2}

    def test_upload_still_expired_after_retry_says_reconnect(
        self, server, tmp_path, monkeypatch
    ):
        from monteur.youtube import TokenExpired

        self._connect_settings()
        monkeypatch.setattr(
            "monteur.youtube.refresh_access_token", lambda *a, **k: "at"
        )

        def always_expired(*args, **kwargs):
            raise TokenExpired("stale")

        monkeypatch.setattr("monteur.youtube.upload_video", always_expired)
        data = _post(
            f"{server}/api/youtube/upload", self._upload_payload(tmp_path)
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "reconnect" in job["message"]

    def test_upload_quota_error_is_the_friendly_message(
        self, server, tmp_path, monkeypatch
    ):
        from monteur.youtube import QUOTA_MESSAGE, QuotaExceeded

        self._connect_settings()
        monkeypatch.setattr(
            "monteur.youtube.refresh_access_token", lambda *a, **k: "at"
        )

        def quota(*args, **kwargs):
            raise QuotaExceeded(QUOTA_MESSAGE)

        monkeypatch.setattr("monteur.youtube.upload_video", quota)
        data = _post(
            f"{server}/api/youtube/upload", self._upload_payload(tmp_path)
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert job["message"] == QUOTA_MESSAGE

    def test_upload_refresh_failure_is_a_job_error(
        self, server, tmp_path, monkeypatch
    ):
        from monteur.youtube import MonteurYouTubeError

        self._connect_settings()

        def bad_refresh(*args, **kwargs):
            raise MonteurYouTubeError(
                "your YouTube connection is no longer valid — reconnect in settings"
            )

        monkeypatch.setattr("monteur.youtube.refresh_access_token", bad_refresh)
        data = _post(
            f"{server}/api/youtube/upload", self._upload_payload(tmp_path)
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "reconnect in settings" in job["message"]

    def test_upload_thumbnail_note_lands_in_the_result(
        self, server, tmp_path, monkeypatch
    ):
        self._connect_settings()
        thumb = tmp_path / "thumb.jpg"
        thumb.write_bytes(b"jpg")
        monkeypatch.setattr(
            "monteur.youtube.refresh_access_token", lambda *a, **k: "at"
        )
        monkeypatch.setattr(
            "monteur.youtube.upload_video",
            lambda *a, **k: {"video_id": "v", "channel": ""},
        )
        monkeypatch.setattr(
            "monteur.youtube.set_thumbnail",
            lambda token, video_id, image_path, transport=None: (
                "thumbnail not set: needs phone verification"
            ),
        )
        data = _post(
            f"{server}/api/youtube/upload",
            self._upload_payload(tmp_path, thumbnail=str(thumb)),
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"  # a thumbnail note is never fatal
        assert job["result"]["notes"] == [
            "thumbnail not set: needs phone verification"
        ]

    # -- the offline prefill -----------------------------------------------------

    def test_prefill_needs_a_plan(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/youtube/prefill", {"name": "x"})
        assert exc_info.value.code == 400

    def test_prefill_bad_plan_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/youtube/prefill", {"plan_json": {"nope": 1}})
        assert exc_info.value.code == 400
        assert "not a Monteur plan" in json.loads(exc_info.value.read())["error"]

    def test_prefill_builds_deterministic_metadata(self, server):
        data = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": _story_plan_dict(), "name": "Alps Draft"},
        )
        assert data["title"] == "Alps Draft"  # the draft name wins
        lines = data["description"].split("\n")
        # The story first, a blank line, then the chapter lines from 0:00.
        assert lines[0] == "Three friends ride over the Alps."
        assert lines[1] == ""
        assert lines[2] == "0:00 Overtake in a left curve"
        assert lines[3] == "0:15 Summit sunrise"
        assert lines[4] == "0:30 Camp fire evening"
        assert len(lines) == 5  # nothing else invented
        # Tags mined from the vision labels, stopwords dropped.
        assert "overtake" in data["tags"]
        assert "summit" in data["tags"]
        assert "sunrise" in data["tags"]
        assert "the" not in data["tags"]

    def test_prefill_title_falls_back_to_the_story(self, server):
        data = _post(
            f"{server}/api/youtube/prefill", {"plan_json": _story_plan_dict()}
        )
        assert data["title"] == "Three friends ride over the Alps."

    def test_prefill_without_story_or_chapters_stays_honest(self, server):
        plan = _story_plan_dict()
        plan["notes"] = []
        plan["entries"] = plan["entries"][:1]
        data = _post(f"{server}/api/youtube/prefill", {"plan_json": plan})
        assert data["title"] == ""
        assert data["description"] == "0:00 Overtake in a left curve"

    # -- the static UI --------------------------------------------------------------

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_youtube_settings_section(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="set-yt-section"',
            "YouTube &mdash; upload connection",
            'id="set-yt-id"',
            'id="set-yt-secret"',
            'id="set-yt-connect"',
            'id="set-yt-disconnect"',
            # the honest setup + private-draft copy
            "console.cloud.google.com",
            "YouTube Data API v3",
            "Desktop app",
            "test user",
            "private drafts",
            "6 uploads a day",
            '"/api/youtube/status"',
            '"/api/youtube/credentials"',
            '"/api/youtube/connect"',
            '"/api/youtube/disconnect"',
        ):
            assert needle in source, needle
        # the third settings section: AI, then Resolve, then YouTube
        ai_section = source.index('id="settings-title"')
        resolve_section = source.index('id="set-resolve-section"')
        yt_section = source.index('id="set-yt-section"')
        assert ai_section < resolve_section < yt_section

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_upload_blocks_in_both_success_states(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="yt-x-block"', 'id="yt-r-block"',
            'id="yt-x-title"', 'id="yt-r-title"',
            'id="yt-x-desc"', 'id="yt-r-desc"',
            'id="yt-x-tags"', 'id="yt-r-tags"',
            'id="yt-x-privacy"', 'id="yt-r-privacy"',
            'id="yt-x-btn"', 'id="yt-r-btn"',
            'id="yt-x-bar"', 'id="yt-r-bar"',
            ">Upload to YouTube<",
            'value="private" selected',
            'value="unlisted"',
            "Connect in settings",
            '"/api/youtube/upload"',
            '"/api/youtube/prefill"',
            "Uploaded as a private draft — review and publish in YouTube Studio",
            'p.stage === "upload"',
        ):
            assert needle in source, needle
        # yt-r lives inside the Resolve-render success block, yt-x inside
        # the Direct-Export success block.
        render_result = source.index('id="cre-render-result"')
        yt_r = source.index('id="yt-r-block"')
        export_result = source.index('id="cre-export-result"')
        yt_x = source.index('id="yt-x-block"')
        assert render_result < yt_r < export_result < yt_x


# --- the shoot plan on the movie endpoints --------------------------------------


class TestMovieShootPlanApi:
    """Every movie payload carries monteur.movie.shoot_plan — deterministic,
    so load/assign stay instant endpoints."""

    def test_load_carries_the_shoot_plan(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj", folders=["/f1"])
        data = _post(f"{server}/api/movie/load", {"project_dir": project_dir})
        sp = data["shoot_plan"]
        assert sp["counts"]["scenes"] == 3
        assert sp["percent"] == 33
        assert [s["status"] for s in sp["scenes"]] == [
            "assigned", "unshot", "unshot",
        ]
        assert [u["scene"] for u in sp["unshot"]] == [2, 3]
        # the unshot cards carry the scene's shooting tips inline
        assert sp["unshot"][0]["tips"] == ["Kamera tief halten", "2 Takes"]
        assert sp["reshoot"] == [] and sp["thin"] == []

    def test_assign_refreshes_the_shoot_plan(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj")
        data = _post(
            f"{server}/api/movie/assign",
            {"project_dir": project_dir, "scene": 2, "folder": "/footage"},
        )
        sp = data["shoot_plan"]
        assert sp["percent"] == 33
        assert sp["scenes"][1]["status"] == "assigned"
        assert [u["scene"] for u in sp["unshot"]] == [1, 3]

    def test_thin_scene_shows_up(self, server, tmp_path):
        footage = tmp_path / "takes"
        footage.mkdir()
        (footage / "S01_T01.mp4").touch()
        project_dir = _write_movie_project(tmp_path / "proj")
        data = _post(
            f"{server}/api/movie/assign",
            {"project_dir": project_dir, "scene": 1, "folder": str(footage)},
        )
        sp = data["shoot_plan"]
        assert sp["scenes"][0]["takes"] == 1
        assert [t["scene"] for t in sp["thin"]] == [1]
        assert "S01_T##" in sp["thin"][0]["why"]

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_has_the_shoot_plan_panel(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="mov-shootplan"',
            ">Shoot plan<",
            'id="mov-sp-line"',
            'id="mov-sp-fill"',
            'id="mov-sp-cards"',
            'id="mov-sp-empty"',
            # the cards reuse the coverage MUST-card look and scroll to
            # their scene slot
            'movSpCard("Unshot", "must"',
            'movSpCard("Reshoot", "must"',
            'movSpCard("Thin", "nice"',
            '"mov-scene-" + item.scene',
            "Go to scene",
            # refreshed by load/assign responses and finished checks
            "movApplyShootPlan(data.shoot_plan)",
            "movApplyShootPlan(result.shoot_plan)",
            "movRefreshShootPlan()",
        ):
            assert needle in source, needle


class TestMovieCheckPersistsApi:
    """A finished check is remembered on its scene slot (movie.json)."""

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def test_check_persists_and_carries_the_shoot_plan(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj", folders=[self.DEMO])
        data = _post(
            f"{server}/api/movie/check", {"project_dir": project_dir, "scene": 1}
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        result = job["result"]
        assert "persist_error" not in result
        # the refreshed shoot plan rides along: demo footage sifts clean,
        # so the technical-only check reads as checked-ok
        sp = result["shoot_plan"]
        assert sp["scenes"][0]["status"] == "checked-ok"
        assert sp["counts"]["checked_ok"] == 1

        # ... and it survived on disk: a fresh load reads the same state
        loaded = _post(f"{server}/api/movie/load", {"project_dir": project_dir})
        stored = loaded["project"]["scenes"][0]["last_check"]
        assert stored["clips"] == 4
        assert stored["folder"] == self.DEMO
        assert loaded["shoot_plan"]["scenes"][0]["status"] == "checked-ok"

    def test_reassigning_makes_the_stored_check_stale(self, server, tmp_path):
        project_dir = _write_movie_project(tmp_path / "proj", folders=[self.DEMO])
        data = _post(
            f"{server}/api/movie/check", {"project_dir": project_dir, "scene": 1}
        )
        assert _wait_for_job(server, data["job"])["state"] == "done"
        moved = _post(
            f"{server}/api/movie/assign",
            {"project_dir": project_dir, "scene": 1, "folder": "/elsewhere"},
        )
        # the check no longer matches the assigned folder — back to assigned
        assert moved["shoot_plan"]["scenes"][0]["status"] == "assigned"


# --- movie result parity: the assembled film gets the full toolchain -----------


class TestMovieResultParityApi:
    """The movie-assemble result carries the film as plan_json, and that
    plan feeds the SAME plan-based endpoints the Create card uses."""

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")
        _clear_scan_cache()

    def _film(self, server, tmp_path, **extra):
        project_dir = _write_movie_project(
            tmp_path / "proj", n_scenes=2, folders=[self.DEMO, self.DEMO]
        )
        data = _post(
            f"{server}/api/movie/assemble", {"project_dir": project_dir, **extra}
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        return job["result"]

    def test_assemble_result_carries_the_film_plan(self, server, tmp_path):
        from monteur.montage import plan_from_dict

        result = self._film(server, tmp_path)
        plan = plan_from_dict(result["plan_json"])  # must load cleanly
        assert plan.entries
        assert plan.music_path == ""  # set sound — audio mode "original"
        assert plan.duration == pytest.approx(
            result["duration_seconds"], abs=0.05
        )
        assert result["fps"] == 25.0
        assert result["canvas"] == "uhd"
        assert result["title"] == "Nachtfahrt"
        scenes = result["scenes"]
        assert [s["name"] for s in scenes] == [
            "Scene 1: INT. AUTO - NIGHT", "Scene 2: EXT. WALDWEG - NIGHT",
        ]
        assert scenes[0]["start_seconds"] == 0
        assert 0 < scenes[1]["start_seconds"] < result["duration_seconds"]
        # every entry sits inside the film and points at real demo media
        for entry in plan.entries:
            assert Path(entry.clip_path).is_file()
            assert 0 <= entry.record_start < entry.record_end

    def test_movie_plan_previews_end_to_end(self, server, tmp_path):
        result = self._film(server, tmp_path)
        data = _post(
            f"{server}/api/create/preview",
            {"plan_json": result["plan_json"], "audio": "original", "width": 320},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        status, headers, body = _get_raw(f"{server}{job['result']['url']}")
        assert status == 200
        assert headers["Content-Type"] == "video/mp4"
        assert b"ftyp" in body[:16]

    def test_movie_plan_exports_with_original_audio(
        self, server, tmp_path, monkeypatch
    ):
        import monteur.preview as preview_module

        result = self._film(server, tmp_path)
        calls = []

        def fake_export(plan, out_path, *, canvas, fps, audio, quality,
                        progress=None, size=None, grade=None, cancel=None):
            calls.append(
                {"entries": len(plan.entries), "canvas": canvas,
                 "fps": fps, "audio": audio, "out_path": out_path}
            )
            return {"path": out_path, "duration": plan.duration,
                    "seconds": 1.0, "notes": []}

        monkeypatch.setattr(preview_module, "render_export", fake_export)
        target = str(tmp_path / "export")
        data = _post(
            f"{server}/api/create/export-video",
            {
                "plan_json": result["plan_json"],
                "target_dir": target,
                "name": "mein-film",
                "canvas": result["canvas"],
                "fps": result["fps"],
                "audio": "original",  # what the movie card always sends
            },
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert calls and calls[0]["audio"] == "original"
        assert calls[0]["canvas"] == "uhd"
        assert calls[0]["out_path"].endswith("mein-film.mp4")
        assert calls[0]["entries"] == len(result["plan_json"]["entries"])

    def test_youtube_prefill_accepts_the_movie_plan(self, server, tmp_path):
        result = self._film(server, tmp_path)
        meta = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": result["plan_json"], "name": result["title"]},
        )
        assert meta["title"] == "Nachtfahrt"

    def test_direct_reviews_the_film_from_its_scene_folders(
        self, server, tmp_path, monkeypatch
    ):
        import monteur.director as director_module

        result = self._film(server, tmp_path)
        seen = {}
        review = {"score": 71, "verdict": "solide", "praise": [],
                  "issues": [], "summary": "ok"}

        def fake_direct_cut(plan, reports, music=None, notes=""):
            seen["entries"] = len(plan.entries)
            seen["clips"] = sorted(Path(r.path).name for r in reports)
            seen["notes"] = notes
            return dict(review)

        monkeypatch.setattr(director_module, "direct_cut", fake_direct_cut)
        screenplay = "Screenplay: Nachtfahrt\nScene 1 (INT. AUTO - NIGHT): ..."
        data = _post(
            f"{server}/api/create/direct",
            {
                "plan_json": result["plan_json"],
                "folders": [self.DEMO, self.DEMO],  # de-duped server-side
                "notes": screenplay,
            },
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        assert job["result"]["review"] == review
        assert job["result"]["applied"] is False
        # the dossier saw every clip of the (single, de-duped) scene folder
        assert seen["clips"] == [
            "clip_A.mp4", "clip_B.mp4", "clip_C.mp4", "clip_D.mp4",
        ]
        # ... and judged against the screenplay the movie card sends
        assert seen["notes"] == screenplay
        assert seen["entries"] == len(result["plan_json"]["entries"])

    def test_direct_folders_must_be_a_nonempty_list(self, server):
        for bad in ([], [""], "not-a-list"):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(
                    f"{server}/api/create/direct",
                    {"plan_json": _preview_plan_dict(), "folders": bad},
                )
            assert exc_info.value.code == 400
            assert "folders" in json.loads(exc_info.value.read())["error"]

    def test_direct_without_folder_or_folders_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/direct",
                {"plan_json": _preview_plan_dict()},
            )
        assert exc_info.value.code == 400
        assert "folder" in json.loads(exc_info.value.read())["error"]

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_app_movie_result_card_has_the_full_toolchain(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            # preview player
            'id="mov-preview-btn"',
            'id="mov-preview-video"',
            # storyboard with scene chips
            'id="mov-storyboard"',
            'id="mov-sb-board"',
            "sbEntryCard(entry, i, true)",
            # Build in DaVinci Resolve
            'id="mov-resolve-btn"',
            # Export video + YouTube upload
            'id="mov-export-block"',
            'id="mov-export-dir"',
            'id="mov-export-name"',
            'id="mov-export-btn"',
            'id="yt-m-block"',
            'id="yt-m-btn"',
            '"yt-m"',
            # director's notes against the screenplay
            'id="mov-dir-btn"',
            'id="mov-dir-result"',
            "movScreenplayContext",
            'renderDirectorReview((result && result.review) || {}, "mov-dir")',
            # the film plan always plays its own sound
            'audio: "original"',
        ):
            assert needle in source, needle
        # the movie result card keeps the download button as its last act
        card = source.index('id="mov-asm-result"')
        download = source.index('id="mov-asm-download"')
        assert card < download


# --- the shot inspector's endpoints (clipinfo / alternatives / plan-adjust) ----


def _inspector_reports():
    """Fake sifted reports with vision fields — no media files needed."""
    from monteur.sift import ClipReport, Moment

    def clip(name, group=""):
        return ClipReport(
            path=f"/footage/{name}",
            duration=30.0,
            moments=[
                Moment(2.0, 5.0, 0.9, label=f"{name} opener", role="opener",
                       hero=0.7, group=group),
                Moment(10.0, 13.0, 0.6, label=f"{name} middle", group=group),
                Moment(20.0, 24.0, 0.8, label=f"{name} closer", role="closer"),
            ],
            usable_ratio=0.75,
        )

    return [clip("a.mp4", group="ridge"), clip("b.mp4", group="ridge"),
            clip("c.mp4")]


def _inspector_plan_dict(reports):
    from monteur.montage import plan_montage, plan_to_dict
    from monteur.music import MusicAnalysis, MusicSection

    music = MusicAnalysis(
        path="/music/song.wav", duration=12.0, tempo=120.0,
        beats=[i * 0.5 for i in range(24)],
        downbeats=[i * 2.0 for i in range(6)],
        sections=[MusicSection(0.0, 12.0, 0.6, "mid")],
    )
    # a short cut on purpose: most moments stay UNUSED, so the bench
    # (the alternatives material) is never empty in these tests
    return plan_to_dict(
        plan_montage(reports, music, cut_lead=0.0, max_duration=4.0)
    )


class TestInspectorApi:
    """clipinfo / alternatives / plan-adjust: instant, cache-only, no sift."""

    FOLDER = "/footage"

    @pytest.fixture()
    def cached(self, monkeypatch):
        reports = _inspector_reports()

        def fake_cached(folder):
            return reports if folder == self.FOLDER else None

        monkeypatch.setattr("monteur.web.server._cached_reports", fake_cached)
        return reports

    # -- clipinfo --------------------------------------------------------

    def test_clipinfo_missing_clip_is_400(self, server, cached):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/clipinfo?folder={self.FOLDER}")
        assert exc_info.value.code == 400
        assert "clip" in json.loads(exc_info.value.read())["error"]

    def test_clipinfo_missing_folder_is_400(self, server, cached):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/clipinfo?clip=a.mp4")
        assert exc_info.value.code == 400
        assert "folder" in json.loads(exc_info.value.read())["error"]

    def test_clipinfo_without_scan_cache_is_404(self, server, cached):
        query = urllib.parse.urlencode({"clip": "a.mp4", "folder": "/elsewhere"})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/clipinfo?{query}")
        assert exc_info.value.code == 404
        assert "scan" in json.loads(exc_info.value.read())["error"]

    def test_clipinfo_unknown_clip_is_404(self, server, cached):
        query = urllib.parse.urlencode(
            {"clip": "nope.mp4", "folder": self.FOLDER}
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server}/api/clipinfo?{query}")
        assert exc_info.value.code == 404

    def test_clipinfo_facts_and_overlapping_moment(self, server, cached, monkeypatch):
        monkeypatch.setattr(
            "monteur.web.server._probe_facts",
            lambda path: {"width": 3840, "height": 2160, "fps": 25.0,
                          "has_audio": True},
        )
        query = urllib.parse.urlencode(
            {"clip": "a.mp4", "folder": self.FOLDER, "t0": 2.5, "t1": 4.5}
        )
        data = _get(f"{server}/api/clipinfo?{query}")
        assert data["name"] == "a.mp4"
        assert data["clip"] == "/footage/a.mp4"  # matched by basename
        assert data["duration"] == 30.0
        assert data["usable_ratio"] == 0.75
        assert (data["width"], data["height"], data["fps"]) == (3840, 2160, 25.0)
        # the 2.5-4.5 window overlaps the opener moment most
        assert data["moment"]["label"] == "a.mp4 opener"
        assert data["moment"]["role"] == "opener"
        assert data["moment"]["hero"] == 0.7
        assert data["moment"]["group"] == "ridge"

    def test_clipinfo_probe_failure_degrades_to_zeros(self, server, cached):
        # /footage/a.mp4 does not exist — probe facts soften to zeros
        query = urllib.parse.urlencode({"clip": "a.mp4", "folder": self.FOLDER})
        data = _get(f"{server}/api/clipinfo?{query}")
        assert (data["width"], data["height"], data["fps"]) == (0, 0, 0.0)
        assert data["moment"] is None  # no window sent -> no moment guess

    # -- alternatives ----------------------------------------------------

    def _plan(self, cached):
        return _inspector_plan_dict(cached)

    def test_alternatives_missing_plan_is_400(self, server, cached):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/alternatives",
                  {"folder": self.FOLDER, "slot": 0})
        assert exc_info.value.code == 400

    def test_alternatives_bad_slot_is_400(self, server, cached):
        plan = self._plan(cached)
        for slot in ("x", None, 99, -1):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(f"{server}/api/alternatives",
                      {"plan_json": plan, "folder": self.FOLDER, "slot": slot})
            assert exc_info.value.code == 400

    def test_alternatives_without_scan_cache_is_404(self, server, cached):
        plan = self._plan(cached)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/alternatives",
                  {"plan_json": plan, "folder": "/elsewhere", "slot": 0})
        assert exc_info.value.code == 404

    def test_alternatives_reuse_the_directors_bench(self, server, cached):
        from monteur.director import review_context
        from monteur.montage import plan_from_dict

        plan = self._plan(cached)
        data = _post(f"{server}/api/alternatives",
                     {"plan_json": plan, "folder": self.FOLDER, "slot": 0})
        assert data["slot"] == 0
        alternatives = data["alternatives"]
        assert 1 <= len(alternatives) <= 6
        # every candidate comes from the director's bench (no new scoring)
        bench = review_context(plan_from_dict(plan), cached)["bench"]
        bench_keys = {(b["clip"], b["start"], b["end"]) for b in bench}
        for alt in alternatives:
            assert (alt["name"], alt["start"], alt["end"]) in bench_keys
            assert alt["clip"].startswith("/footage/")  # full path for thumbs
        # same-clip / same-scene-group candidates lead the list
        kinship = [alt["same_scene"] for alt in alternatives]
        assert kinship == sorted(kinship, reverse=True)

    # -- plan/adjust -----------------------------------------------------

    def test_adjust_returns_the_export_shape(self, server, cached):
        plan = self._plan(cached)
        data = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": plan, "slot": 1, "transition": "dissolve",
             "format": "edl", "audio": "original"},
        )
        assert data["filename"] == "monteur_montage.edl"
        assert data["content"].startswith("TITLE:")
        assert data["plan"]["tempo"] == 0  # nothing re-listened
        adjusted = data["plan_json"]["entries"]
        assert adjusted[1]["transition"] == pytest.approx(
            min(0.5, (adjusted[1]["record_end"] - adjusted[1]["record_start"]) / 2)
        )
        assert any("boundary: dissolve into slot 2" in n
                   for n in data["plan"]["notes"])
        # the request's own plan_json is not mutated server-side
        assert plan["entries"][1]["transition"] == 0.0

    def test_adjust_smash_then_cut_round_trips(self, server, cached):
        plan = self._plan(cached)
        smashed = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": plan, "slot": 2, "transition": "smash",
             "audio": "original"},
        )["plan_json"]
        assert len(smashed["dips"]) == len(plan["dips"]) + 1
        restored = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": smashed, "slot": 2, "transition": "cut",
             "audio": "original"},
        )["plan_json"]
        assert restored["dips"] == plan["dips"]
        assert restored["entries"] == plan["entries"]

    def test_adjust_validation_400s(self, server, cached):
        plan = self._plan(cached)
        cases = [
            ({"slot": 1, "transition": "wipe"}, "transition"),
            ({"slot": "x", "transition": "cut"}, "slot"),
            ({"slot": 0, "transition": "dissolve"}, "fade-in"),
            ({"slot": 99, "transition": "cut"}, "not in this plan"),
        ]
        for extra, needle in cases:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(f"{server}/api/plan/adjust",
                      {"plan_json": plan, **extra})
            assert exc_info.value.code == 400
            assert needle in json.loads(exc_info.value.read())["error"]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/plan/adjust",
                  {"slot": 1, "transition": "cut"})
        assert exc_info.value.code == 400

    # -- plan/adjust: the title mode (editable dip titles) -----------------

    def _plan_with_dip(self, server, cached):
        """A plan carrying one black dip (via the boundary mode's smash)."""
        plan = self._plan(cached)
        return _post(
            f"{server}/api/plan/adjust",
            {"plan_json": plan, "slot": 2, "transition": "smash",
             "audio": "original"},
        )["plan_json"]

    def test_adjust_title_is_pure_surgery_in_export_shape(self, server, cached):
        smashed = self._plan_with_dip(server, cached)
        data = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": smashed, "dip": 0, "title": "  THE RIDE  ",
             "format": "edl", "audio": "original"},
        )
        # the standard export shape — tempo honestly 0, nothing re-listened
        assert data["filename"] == "monteur_montage.edl"
        assert data["content"].startswith("TITLE:")
        assert data["plan"]["tempo"] == 0
        adjusted = data["plan_json"]
        # the title landed (trimmed), aligned with the dips
        assert adjusted["title_texts"][0] == "THE RIDE"
        assert len(adjusted["title_texts"]) == len(adjusted["dips"])
        # pure surgery: entries and dips stay bit-identical
        assert adjusted["entries"] == smashed["entries"]
        assert adjusted["dips"] == smashed["dips"]
        assert any("title: dip 1 reads 'THE RIDE'" in n
                   for n in data["plan"]["notes"])
        # the request's own plan_json is not mutated server-side
        assert smashed.get("title_texts", []) in ([], [""])

    def test_adjust_title_empty_clears(self, server, cached):
        smashed = self._plan_with_dip(server, cached)
        titled = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": smashed, "dip": 0, "title": "ACT ONE",
             "audio": "original"},
        )["plan_json"]
        data = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": titled, "dip": 0, "title": "", "audio": "original"},
        )
        assert data["plan_json"]["title_texts"][0] == ""
        assert any("title: dip 1 cleared" in n for n in data["plan"]["notes"])

    def test_adjust_title_validation_400s(self, server, cached):
        plan = self._plan(cached)  # carries no dips
        smashed = self._plan_with_dip(server, cached)
        cases = [
            # a non-string title
            ({"plan_json": smashed, "dip": 0, "title": 5}, "'title'"),
            # a plan without dips has no title slots
            ({"plan_json": plan, "dip": 0, "title": "x"}, "no black dips"),
            # malformed / out-of-range dip indices
            ({"plan_json": smashed, "dip": "x", "title": "x"}, "'dip'"),
            ({"plan_json": smashed, "dip": None, "title": "x"}, "'dip'"),
            ({"plan_json": smashed, "dip": 99, "title": "x"}, "not in this plan"),
            ({"plan_json": smashed, "dip": -1, "title": "x"}, "not in this plan"),
        ]
        for payload, needle in cases:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(f"{server}/api/plan/adjust", payload)
            assert exc_info.value.code == 400
            assert needle in json.loads(exc_info.value.read())["error"]
        # the shared plan_json gate covers the title mode too
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/plan/adjust", {"dip": 0, "title": "x"})
        assert exc_info.value.code == 400


class TestPlanAdjustOnDemo:
    """Behavioral: a real build's plan takes a dissolve and gives it back."""

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def test_adjust_round_trip_on_built_plan(self, server):
        build = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "style": "travel", "format": "edl"},
        )
        job = _wait_for_job(server, build["job"], timeout=300.0)
        assert job["state"] == "done", job["message"]
        plan = job["result"]["plan_json"]
        # the built plan already carries the strip metadata
        assert plan.get("phases")
        assert plan.get("music_energy")
        slot = 1
        before = plan["entries"][slot]["transition"]
        target = "cut" if before > 0 else "dissolve"
        adjusted = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": plan, "slot": slot, "transition": target},
        )
        entry = adjusted["plan_json"]["entries"][slot]
        if target == "dissolve":
            assert entry["transition"] > 0
        else:
            assert entry["transition"] == 0
        # give the boundary back — the grid is bit-identical again
        reverse = "dissolve" if target == "cut" else "cut"
        restored = _post(
            f"{server}/api/plan/adjust",
            {"plan_json": adjusted["plan_json"], "slot": slot,
             "transition": reverse},
        )
        expected = plan["entries"]
        if before == 0 and reverse == "dissolve":
            expected = adjusted["plan_json"]["entries"]
        got = restored["plan_json"]["entries"]
        untouched = [e for i, e in enumerate(got) if i != slot]
        assert untouched == [e for i, e in enumerate(plan["entries"]) if i != slot]


class TestProUiStatic:
    """Static asserts on app.html: the strip, the inspector and the badges."""

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_timeline_strip_markup_and_wiring(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            # strip markup above the storyboard
            'id="cre-strip"',
            'id="cre-strip-blocks"',
            'id="cre-strip-lane"',
            'id="cre-strip-legend"',
            # proportional blocks, phase colors, dips, drop + downbeats
            "function renderTimelineStrip",
            "function drawStripLane",
            "--phase-opening",
            "--phase-climax",
            "plan.music_energy",
            "beat_marks",
            "drop_marks",
            "tl-dip",
            # theme-aware redraw: CSS variables + data-theme observer
            "function cssVar",
            'attributeFilter: ["data-theme"]',
            # multi-lane (Resolve idiom): track-header gutter + ruler + the
            # Title and A2 SFX lanes, each fed from the plan
            'id="cre-tl-frame"',
            'class="tl-gutter"',
            'id="cre-strip-ruler"',
            'id="cre-strip-titles"',
            'id="cre-strip-sfx"',
            "function renderStripRuler",
            "function renderTitleLane",
            "function renderSfxLane",
            "plan.title_texts",
            "plan.sfx",
            "plan.music_gaps",
            "tl-titlemark",
            "tl-sfxmark",
            # the Grade adjustment layer over V1
            'id="cre-strip-grade"',
            "class=\"tlg tlg-grade\"",
            "function renderGradeLane",
            "tl-grademark",
            # clip/SFX labels + the silence gap label
            "tl-block-lbl",
            "function renderStripRuler",
            # the NLE surface: timeline toolbar (timecode/Snap/BPM readout)
            'id="cre-tl-bar"',
            'id="tl-tc"',
            'id="tl-snap"',
            'id="tl-bpm"',
            "plan.tempo",
            "function snapScrubTime",
            # program monitor: in-frame timecode + shot label + full transport
            'id="po-tc"',
            'id="po-shot"',
            'id="po-first"',
            'id="po-last"',
            "function poSetShotLabel",
            # the clean project meta line
            'id="cre-proj-meta"',
        ):
            assert needle in source, needle
        # phase colors exist in BOTH themes
        assert source.count("--phase-climax:") == 2
        # the lane colour tokens are defined in BOTH themes
        for token in ("--music:", "--sfx:", "--title:", "--clip:", "--grade:"):
            assert source.count(token) == 2, token

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_look_and_colour_markup_and_wiring(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            # the Look & colour block: preview + look chips + four sliders
            'id="cre-color"',
            'id="cre-color-frame"',
            'id="cre-color-warm"',
            'id="cre-look-chips"',
            'id="cg-brightness"',
            'id="cg-contrast"',
            'id="cg-saturation"',
            'id="cg-warmth"',
            'id="cg-reset"',
            # the JS: presets, the export payload dict, the live CSS preview
            "COLOR_LOOKS",
            "function gradeToDict",
            "function gradeCss",
            "function applyGradePreview",
            "function setGradeLook",
            "function colorPreviewFrame",
            # the grade rides on the export payload and persists in the project
            "body.grade = gradeDict",
            "options.grade = gradeToDict",
            "restoreGrade",
        ):
            assert needle in source, needle
        # all six looks are wired
        for look in ("neutral", "filmic", "muted", "warm", "cool", "faded"):
            assert look in source, look

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_series_in_create_markup_and_wiring(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            # the "How many videos?" control in Options + the Short picker
            'id="cre-series-row"',
            'id="series-1"',
            'id="series-2"',
            'id="cre-series-strip"',
            "function setSeriesCount",
            "function startSeriesBuild",
            "function loadSeriesShort",
            "function renderSeriesStrip",
            # a series posts to its own endpoint; the single build branches
            "/api/create/series",
            "startSeriesBuild(andThen); return;",
            "body.series = cre.seriesCount",
            # the whole set persists in the project options
            "options.series_shorts = cre.series.shorts",
            "series_active",
        ):
            assert needle in source, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_inspector_markup_and_wiring(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-inspector"',
            'id="cre-insp-title"',
            'id="cre-insp-facts"',
            'id="cre-insp-pin"',
            'id="cre-insp-trans"',
            'data-trans="cut"',
            'data-trans="dissolve"',
            'data-trans="smash"',
            'id="cre-insp-alts-btn"',
            'id="cre-insp-alts"',
            # the endpoints the inspector rides on
            "/api/clipinfo",
            "/api/alternatives",
            "/api/plan/adjust",
            # the swap goes through the EXISTING apply machinery
            "startDirectApply(body, function () { selectSlot(i); });",
            # shared selection + keyboard
            "function selectSlot",
            '"ArrowRight"',
            '"Escape"',
            "revPinStamp",
        ):
            assert needle in source, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_finding_badges_hooks(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            "function findingsBySlot",
            "function applyFindingBadges",
            "sb-flag",
            "tl-flag",
            # issues[].slots from the review, plus parseable engine notes
            "(issue.slots || []).forEach",
            "arrangement: scenes (",
            # badges refresh when the review changes — in both directions
            "applyFindingBadges(); // a cleared review takes its warning dots with it",
        ):
            assert needle in source, needle


# --- proxies + /api/media + the virtual playout (the "watch without rendering"
# --- surface: monteur.proxies, GET /api/media, the step-3 playout engine) ----


class TestMediaApi:
    """GET /api/media?path=… — Range-capable serving of proxy-or-original."""

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    @pytest.fixture()
    def _empty_proxy_cache(self, tmp_path, monkeypatch):
        """Point the cache at an empty dir so the ORIGINAL is served."""
        monkeypatch.setenv("MONTEUR_PROXIES_PATH", str(tmp_path / "no-proxies"))

    def _url(self, server, path):
        return f"{server}/api/media?path={_quote(path)}"

    def test_serves_the_original_with_full_range_behavior(
        self, server, _empty_proxy_cache
    ):
        clip = Path(self.DEMO) / "clip_A.mp4"
        data = clip.read_bytes()
        url = self._url(server, clip)

        status, headers, body = _get_raw(url)
        assert status == 200
        assert headers["Content-Type"] == "video/mp4"
        assert headers["Accept-Ranges"] == "bytes"
        assert int(headers["Content-Length"]) == len(data)
        assert body == data  # no proxy -> the original file, byte-true

        # Range GET: <video> seeking needs true 206 partial responses
        status, headers, part = _get_raw(url, headers={"Range": "bytes=0-99"})
        assert status == 206
        assert headers["Content-Range"] == f"bytes 0-99/{len(data)}"
        assert part == data[:100]

        status, headers, tail = _get_raw(url, headers={"Range": "bytes=100-"})
        assert status == 206
        assert headers["Content-Range"] == f"bytes 100-{len(data) - 1}/{len(data)}"
        assert tail == data[100:]

        status, headers, suffix = _get_raw(url, headers={"Range": "bytes=-50"})
        assert status == 206
        assert suffix == data[-50:]

        # unsatisfiable range: 416 with the total size
        status, headers, _body = _get_raw(
            url, headers={"Range": f"bytes={len(data)}-"}
        )
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{len(data)}"

        # a malformed Range header falls back to the full 200 (RFC 7233)
        status, _, whole = _get_raw(url, headers={"Range": "bytes=abc"})
        assert status == 200
        assert whole == data

    def test_serves_the_fresh_proxy_when_one_exists(self, server):
        from monteur import proxies

        clip = Path(self.DEMO) / "clip_C.mp4"
        proxy = proxies.ensure_proxy(clip)  # session cache — usually fresh
        proxy_data = proxy.read_bytes()
        assert proxy_data != clip.read_bytes()

        status, headers, body = _get_raw(self._url(server, clip))
        assert status == 200
        assert headers["Content-Type"] == "video/mp4"
        assert body == proxy_data  # the PROXY, not the original

        status, headers, part = _get_raw(
            self._url(server, clip), headers={"Range": "bytes=0-15"}
        )
        assert status == 206
        assert part == proxy_data[:16]
        assert headers["Content-Range"] == f"bytes 0-15/{len(proxy_data)}"

    def test_stale_proxy_falls_back_to_the_original(self, server, tmp_path):
        """An edited clip (new mtime) must NOT serve the old proxy."""
        from monteur import proxies

        copy = tmp_path / "edited.mp4"
        import shutil

        shutil.copyfile(Path(self.DEMO) / "clip_D.mp4", copy)
        proxies.ensure_proxy(copy)
        os.utime(copy, (1_000_000, 1_000_000))  # "the clip was replaced"
        status, _headers, body = _get_raw(self._url(server, copy))
        assert status == 200
        assert body == copy.read_bytes()  # the original — never a stale proxy

    def test_content_type_follows_the_original_suffix(
        self, server, _empty_proxy_cache
    ):
        song = Path(self.DEMO) / "song.wav"
        status, headers, body = _get_raw(self._url(server, song))
        assert status == 200
        assert headers["Content-Type"] == "audio/wav"
        assert body[:4] == b"RIFF"

    def test_missing_path_param_is_400(self, server):
        status, _, body = _get_raw(f"{server}/api/media")
        assert status == 400
        assert "path" in json.loads(body)["error"]

    def test_nonexistent_file_is_404(self, server):
        status, _, body = _get_raw(
            self._url(server, f"{self.DEMO}/no_such_clip.mp4")
        )
        assert status == 404
        assert "no such media file" in json.loads(body)["error"]


class TestProxiesJobs:
    """The background "proxies" job: kicked by every scan, startable by hand.

    ``monteur.proxies`` is resolved at CALL time in the job body, so
    ``monkeypatch.setattr("monteur.proxies.ensure_proxies", …)`` is a
    complete hook — no real transcode behind these tests.
    """

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def _patch_proxies(self, monkeypatch, errors=None, ticks=()):
        calls = {"batches": [], "pruned": 0}

        def fake_ensure(paths, progress=None, *, cancel=None):
            calls["batches"].append([str(p) for p in paths])
            for done, total, name in ticks:
                if progress is not None:
                    progress(done, total, name)
            made = {str(p): str(p) + ".proxy" for p in paths}
            for path in (errors or {}):
                made.pop(path, None)
            return made, dict(errors or {})

        def fake_prune(max_gb=5.0):
            calls["pruned"] += 1
            return []

        monkeypatch.setattr("monteur.proxies.ensure_proxies", fake_ensure)
        monkeypatch.setattr("monteur.proxies.prune_proxies", fake_prune)
        return calls

    def test_scan_kicks_a_proxies_job_automatically(self, server, monkeypatch):
        calls = self._patch_proxies(
            monkeypatch, ticks=[(1, 4, "clip_A.mp4"), (4, 4, "clip_D.mp4")]
        )
        _clear_scan_cache()
        data = _post(f"{server}/api/create/scan", {"folder": self.DEMO})
        scan = _wait_for_job(server, data["job"])
        assert scan["state"] == "done"
        # the scan result names the proxies job it kicked
        proxies_job_id = scan["result"]["proxies_job"]
        assert isinstance(proxies_job_id, str) and proxies_job_id
        job = _wait_for_job(server, proxies_job_id)
        assert job["kind"] == "proxies"
        assert job["state"] == "done"
        # every scanned clip was handed to ensure_proxies, in one batch
        assert len(calls["batches"]) == 1
        assert sorted(Path(p).name for p in calls["batches"][0]) == [
            "clip_A.mp4", "clip_B.mp4", "clip_C.mp4", "clip_D.mp4",
        ]
        assert calls["pruned"] == 1  # best-effort prune after the batch
        # per-file progress arrives as {"stage": "proxy"} entries
        stages = {p["stage"] for p in job["progress"]}
        assert stages == {"proxy"}
        assert job["result"] == {"ready": 4, "total": 4, "errors": []}

    def test_manual_proxies_endpoint(self, server, monkeypatch):
        calls = self._patch_proxies(monkeypatch)
        data = _post(f"{server}/api/proxies", {"folder": self.DEMO})
        job = _wait_for_job(server, data["job"])
        assert job["kind"] == "proxies"
        assert job["state"] == "done"
        assert len(calls["batches"]) == 1

    def test_per_file_failures_are_soft_and_named(self, server, monkeypatch):
        broken = str(Path(self.DEMO) / "clip_B.mp4")
        self._patch_proxies(monkeypatch, errors={broken: "ffmpeg said no"})
        data = _post(f"{server}/api/proxies", {"folder": self.DEMO})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"  # soft — the job still succeeds
        assert job["result"]["ready"] == 3
        assert job["result"]["errors"] == ["clip_B.mp4: ffmpeg said no"]

    def test_proxies_missing_folder_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{server}/api/proxies", {})
        assert exc_info.value.code == 400

    def test_proxies_bad_folder_is_an_error_job(self, server):
        data = _post(f"{server}/api/proxies", {"folder": "/no/such/folder"})
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "error"
        assert "not a directory" in job["message"]


class TestPlayoutUi:
    """Static asserts on app.html: the virtual playout, the moment player
    and the demoted (but present) exact-preview render."""

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_playout_container_and_transport(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        step3 = _step3_html(source)
        for needle in (
            # the double-buffered stage with the dip-title overlay
            'id="cre-playout"',
            'id="po-video-a"',
            'id="po-video-b"',
            'id="po-title"',
            'id="po-music"',
            # transport: play/pause + readout
            'id="po-play"',
            'id="po-time"',
        ):
            assert needle in step3, needle
        for needle in (
            # the rAF-driven virtual clock is the master
            "performance.now()",
            "requestAnimationFrame(poTick)",
            # drift correction: every 500 ms, reseek anything > 80 ms off
            "now - po.lastDrift > 500",
            "> 0.08",
            # audio modes: mix ducks the clips' own sound to 0.6
            'po.audioMode === "mix" ? 0.6 : 1',
            # everything streams from the proxy-or-original endpoint
            "/api/media?path=",
            # rebuilds keep t where possible
            "poSeek(Math.min(keepT",
        ):
            assert needle in source, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_strip_is_the_scrubber(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-strip-playhead"',
            "tl-playhead",
            'strip.addEventListener("pointerdown"',
            'document.addEventListener("pointermove"',  # drag continues past strip
            "function poJumpCut",       # ← → jump between cuts
            "function poSeek",
            'e.code === "Space"',       # Space toggles (step 3, not typing)
        ):
            assert needle in source, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_mini_player_markup_and_guards(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="mini-player"',
            'id="mini-video"',
            'id="mini-title"',
            'id="mini-loop"',
            'id="mini-close"',
            # the timeupdate guard pauses (or loops) at source_end
            'addEventListener("timeupdate"',
            "mini.loop",
            # board cards get the hover ▶ affordance
            "attachMomentPlayer",
            "sb-play",
            # Esc + click-outside close
            "closeMiniPlayer",
        ):
            assert needle in source, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_exact_preview_is_demoted_but_present(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        step3 = _step3_html(source)
        # the playout stage comes FIRST; the render button moved below it
        assert step3.index('id="cre-playout"') < step3.index('id="cre-preview-btn"')
        assert "Render exact preview" in step3
        # its help line says what only the rendered file has
        assert "final loudness" in step3.lower() or "final music mix" in step3.lower()
        # the render pipeline itself is untouched
        assert "/api/create/preview" in source
        assert 'id="cre-preview-video"' in source

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_proxy_status_line_in_step_1(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        assert 'id="cre-proxy-status"' in source
        assert "Preparing smooth playback" in source
        assert "Playback ready" in source
        # the scan result's proxies_job feeds the watcher
        assert "result.proxies_job" in source


class TestDeliberateSilenceUi:
    """Static asserts on app.html: the music-flow Fine-tune select and the
    playout's volume gating over plan.music_gaps (blueprint 1.2)."""

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_fine_tune_select_and_copy(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            'id="cre-musicflow"',
            '<option value="deliberate" selected>Deliberate silence (recommended)</option>',
            '<option value="continuous">Continuous</option>',
            # the owner-approved copy, verbatim
            "the song breaks under title cards and for one beat before the "
            "drop &mdash; silence is a weapon.",
            "Continuous: the song plays through everything.",
        ):
            assert needle in source, needle
        # only the non-default is sent, so old drafts stay clean
        assert 'if ($("cre-musicflow").value === "continuous") '\
            'body.music_flow = "continuous";' in source
        # ...and the draft restore reads it back (absent = deliberate)
        assert 's.music_flow === "continuous"' in source

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_playout_gets_the_gap_array_and_volume_gating(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            # the gap array is handed to the playout engine...
            "musicGaps: []",
            "plan.music_gaps || []",
            # ...a plan change in the gaps rebuilds the playout
            'parts.push(JSON.stringify(plan.music_gaps || []));',
            # the gain is a pure function of the clock with a 50 ms ramp
            "var PO_GAP_FADE = 0.05;",
            "function poMusicGain(t)",
            "function poApplyMusicGain()",
            # VOLUME gates, never a pause (the 05032d0 free-running contract)
            "deliberate silences gate the VOLUME, never pause",
        ):
            assert needle in source, needle
        # the free-running drift sync is untouched: the music element is
        # still never paused at segment boundaries
        assert "poSyncMusic(false)" in source

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_revise_carries_the_build_music_flow(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        assert "if (built.music_flow) body.music_flow = built.music_flow;" in source


def _launch_chromium(playwright):
    """A Chromium for the acceptance tests: the standard install when
    present, else any pinned browser on the machine. None = skip."""
    try:
        return playwright.chromium.launch()
    except Exception:  # noqa: BLE001 — fall through to pinned browsers
        pass
    import glob

    patterns = (
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
    )
    for pattern in patterns:
        for candidate in sorted(glob.glob(pattern), reverse=True):
            try:
                return playwright.chromium.launch(executable_path=candidate)
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
    return None


def _pool_scan(page, folder, timeout=120_000):
    """Drive the media-pool Footage step (Increment B): reference a folder,
    select every resolved clip, then Analyze selected — the staged replacement
    for the old "fill #cre-folder + click Scan" preamble. Downstream (build,
    storyboard, playout, export) is unchanged, so the acceptance tests only
    swap this entry step."""
    page.fill("#cre-pool-path", folder)
    page.click("#cre-pool-add-path")
    page.wait_for_selector(
        "#cre-pool-grid .pool-card", state="visible", timeout=timeout
    )
    page.click("#cre-pool-selall")  # tick every resolved clip
    page.wait_for_selector("#cre-scan-btn:not([disabled])", timeout=10_000)
    page.click("#cre-scan-btn")     # Analyze selected


class TestPlayoutAcceptance:
    """Playwright: scan → storyboard → play/scrub the draft → moment player.

    NOTE on codecs: the sandbox/test Chromium ships WITHOUT the proprietary
    H.264 decoder (``canPlayType('video/mp4; codecs="avc1…"') === ''``), so
    the proxies (and the H.264 demo originals) cannot VISUALLY decode here.
    The engine is deliberately built so that does not matter: the virtual
    clock is rAF-driven and independent of media readiness, so play/scrub/
    dips/titles are asserted via element state, the clock and the network
    (206 Range responses on /api/media), with a codec-conditional branch
    for ``currentTime`` where decode would be needed. Real browsers
    (Chrome/Edge/Safari/Firefox with H.264) decode these exact files fine.
    """

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def test_watch_without_rendering_end_to_end(self, server, tmp_path):
        playwright_api = pytest.importorskip(
            "playwright.sync_api", reason="playwright is not installed"
        )
        shots = Path(
            os.environ.get("MONTEUR_PLAYWRIGHT_SHOTS") or str(tmp_path / "shots")
        )
        shots.mkdir(parents=True, exist_ok=True)
        media_responses = []

        with playwright_api.sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            if browser is None:
                pytest.skip("no Chromium browser available for Playwright")
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.on(
                "response",
                lambda response: media_responses.append(
                    (response.url, response.status)
                ) if "/api/media" in response.url else None,
            )
            po = "window.monteurPlayout.state"

            # ---- step 1: scan, then the quiet proxy status line -----------
            page.goto(server)
            page.click("#pm-new")  # enter the create workflow from the Home hub
            _pool_scan(page, self.DEMO)
            page.wait_for_selector("#cre-next-1", state="visible", timeout=120_000)
            page.wait_for_function(
                "document.getElementById('cre-proxy-status')"
                ".textContent.indexOf('Playback ready') === 0",
                timeout=180_000,
            )
            page.screenshot(path=str(shots / "playout-step1-proxies.png"))

            # ---- step 2 -> 3: build the draft ------------------------------
            page.click("#cre-next-1")
            page.click("#audio-original")  # no song: fast + deterministic
            page.fill("#cre-maxlen", "12")
            page.click("#cre-next-2")  # entering the storyboard builds
            page.wait_for_selector(
                "#cre-sb-board .sb-card", state="visible", timeout=180_000
            )
            page.wait_for_selector("#cre-playout", state="visible")
            duration = page.evaluate(po + ".duration")
            assert duration > 3
            assert page.evaluate(po + ".segs.length") >= 2

            # ---- play: the virtual clock advances, the playhead moves -----
            page.click("#po-play")
            page.wait_for_function(po + ".t > 0.8", timeout=10_000)
            assert page.evaluate(po + ".playing") is True
            left_1 = page.eval_on_selector(
                "#cre-strip-playhead", "el => parseFloat(el.style.left)"
            )
            assert left_1 > 0
            page.wait_for_function(
                po + f".t > {page.evaluate(po + '.t')} + 0.4", timeout=5_000
            )
            left_2 = page.eval_on_selector(
                "#cre-strip-playhead", "el => parseFloat(el.style.left)"
            )
            assert left_2 > left_1  # the strip playhead tracks the clock
            assert not page.text_content("#po-time").startswith("0:00 /")
            # the active video element is wired to /api/media and got real
            # bytes (206 partial responses — the Range machinery end-to-end)
            assert media_responses, "no /api/media traffic during playback"
            assert any(status == 206 for _url, status in media_responses)
            active_src = page.evaluate(po + ".active.currentSrc")
            assert "/api/media?path=" in active_src
            page.screenshot(path=str(shots / "playout-step3-playing.png"))
            page.click("#po-play")  # pause
            assert page.evaluate(po + ".playing") is False

            # ---- scrub: click mid-strip -> the readout jumps ---------------
            # the multi-lane strip sits low on the page; bring it fully into
            # view so the coordinate click lands on the track, not off-screen
            page.locator("#cre-strip").scroll_into_view_if_needed()
            box = page.locator("#cre-strip").bounding_box()
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 8)
            t_mid = page.evaluate(po + ".t")
            assert abs(t_mid - duration / 2) < max(0.6, duration * 0.15)
            readout = page.text_content("#po-time")
            assert not readout.startswith("0:00 /")  # the readout jumped too

            # ---- moment player: click a board card's thumbnail -------------
            entry = page.evaluate("window.monteurPlayout.entries()[0]")
            page.click("#cre-sb-board .sb-card[data-slot='0'] .sb-thumb-wrap")
            page.wait_for_selector("#mini-player", state="visible")
            title = page.text_content("#mini-title")
            assert Path(str(entry["clip_path"])).name in title
            assert "–" in title  # "clip.mp4 · 0:01–0:03"
            src = page.eval_on_selector(
                "#mini-video", "el => el.currentSrc || el.src"
            )
            assert "/api/media?path=" in src
            decodable = page.evaluate(
                "document.createElement('video')"
                ".canPlayType('video/mp4; codecs=\"avc1.42E01E\"') !== ''"
            )
            if decodable:
                # a real browser: the player seeks into the segment and plays
                page.wait_for_function(
                    "(() => { const v = document.getElementById('mini-video');"
                    f" return v.readyState >= 1 && v.currentTime >= "
                    f"{entry['source_start']} - 0.05; }})()",
                    timeout=10_000,
                )
                current = page.evaluate(
                    "document.getElementById('mini-video').currentTime"
                )
                assert (
                    entry["source_start"] - 0.05
                    <= current
                    <= entry["source_end"] + 0.25
                )
            else:
                # sandbox Chromium: no H.264 decode — assert the honest
                # fallback instead: the segment's bytes were requested over
                # /api/media and the player shows its can't-decode note.
                page.wait_for_function(
                    "(() => { const v = document.getElementById('mini-video');"
                    " return v.error !== null ||"
                    " !document.getElementById('mini-note').hidden; })()",
                    timeout=10_000,
                )
                assert any(
                    urllib.parse.quote(str(entry["clip_path"]), safe="") in url
                    for url, _status in media_responses
                )
            page.screenshot(path=str(shots / "playout-miniplayer.png"))
            page.keyboard.press("Escape")
            page.wait_for_selector("#mini-player", state="hidden")

            # ---- dip: black stage + title overlay at the right time --------
            # Take the plan's first dip, or carve one through the inspector's
            # real smash control when the build produced none.
            def sorted_dips():
                return page.evaluate(
                    "((window.monteurPlayout.plan().dips) || [])"
                    ".slice().sort((a, b) => a[0] - b[0])"
                )

            if not sorted_dips():
                slot = page.evaluate(
                    """(() => {
                      const entries = window.monteurPlayout.entries();
                      let best = -1, bestLen = 0;
                      for (let i = 1; i < entries.length; i++) {
                        const prev = entries[i - 1];
                        const len = prev.record_end - prev.record_start;
                        if ((entries[i].transition || 0) > 0) continue;
                        if (len > bestLen) { best = i; bestLen = len; }
                      }
                      return best;
                    })()"""
                )
                assert slot >= 1
                page.click(f"#cre-strip-blocks .tl-block[data-slot='{slot}']")
                page.wait_for_selector("#cre-inspector", state="visible")
                page.click('#cre-insp-trans button[data-trans="smash"]')
                page.wait_for_function(
                    "((window.monteurPlayout.plan().dips) || []).length > 0",
                    timeout=30_000,
                )
            # give every dip a visible act title, then rebuild the playout
            page.evaluate(
                """(() => {
                  const plan = window.monteurPlayout.plan();
                  plan.title_texts = (plan.dips || []).map(() => 'KAPITEL ZWEI');
                  window.monteurPlayout.rebuild();
                })()"""
            )
            dip_start, dip_length = sorted_dips()[0]
            page.evaluate(
                f"window.monteurPlayout.seek({max(0.0, dip_start - 0.15)})"
            )
            page.evaluate("window.monteurPlayout.play()")
            # inside the dip: both buffers hidden (black) + the title as DOM
            page.wait_for_function(
                """(() => {
                  const title = document.getElementById('po-title');
                  const a = document.getElementById('po-video-a');
                  const b = document.getElementById('po-video-b');
                  return !title.hidden &&
                    title.textContent === 'KAPITEL ZWEI' &&
                    !a.classList.contains('on') && !b.classList.contains('on');
                })()""",
                timeout=int((0.15 + dip_length) * 1000) + 8_000,
            )
            page.evaluate("window.monteurPlayout.pause()")
            t_now = page.evaluate(po + ".t")
            assert dip_start - 0.05 <= t_now <= dip_start + dip_length + 0.3
            page.screenshot(path=str(shots / "playout-dip-title.png"))

            browser.close()

    def test_deliberate_silence_gates_volume_never_pauses(self, server, tmp_path):
        """Blueprint 1.2: play across a music gap — the free-running audio
        element is NOT paused, its VOLUME is 0 inside the gap and 1 past
        it; scrubbing into a gap lands muted, past it unmuted."""
        playwright_api = pytest.importorskip(
            "playwright.sync_api", reason="playwright is not installed"
        )
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            if browser is None:
                pytest.skip("no Chromium browser available for Playwright")
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            po = "window.monteurPlayout.state"
            music_el = 'document.getElementById("po-music")'

            # scan -> step 2 with the demo song (audio mode stays "music")
            page.goto(server)
            page.click("#pm-new")  # enter the create workflow from the Home hub
            _pool_scan(page, self.DEMO)
            page.wait_for_selector("#cre-next-1", state="visible", timeout=120_000)
            page.click("#cre-next-1")
            page.fill("#cre-music", f"{self.DEMO}/song.wav")
            page.fill("#cre-maxlen", "12")
            page.click("#cre-next-2")
            page.wait_for_selector(
                "#cre-sb-board .sb-card", state="visible", timeout=180_000
            )
            page.wait_for_selector("#cre-playout", state="visible")
            assert page.evaluate(po + '.audioMode') == "music"

            # inject a known 1s gap and rebuild the playout from the plan
            page.evaluate(
                """(() => {
                  const plan = window.monteurPlayout.plan();
                  plan.music_gaps = [[1.0, 2.0]];
                  window.monteurPlayout.rebuild();
                })()"""
            )
            assert page.evaluate(po + ".musicGaps") == [[1.0, 2.0]]

            # scrubbing while paused: into the gap = muted, past it = unmuted
            page.evaluate("window.monteurPlayout.seek(1.5)")
            assert page.evaluate(music_el + ".volume") == 0
            page.evaluate("window.monteurPlayout.seek(2.5)")
            assert page.evaluate(music_el + ".volume") == 1

            # play ACROSS the gap: the element keeps running, only the
            # volume gates (the 05032d0 never-pause contract)
            page.evaluate("window.monteurPlayout.seek(0.4)")
            page.click("#po-play")  # a real user gesture, so play() sticks
            page.wait_for_function(
                music_el + ".paused === false", timeout=10_000
            )
            assert page.evaluate(music_el + ".volume") == 1
            page.wait_for_function(
                po + ".t > 1.1 && " + po + ".t < 1.95", timeout=10_000
            )
            inside = page.evaluate(
                "({paused: " + music_el + ".paused, volume: "
                + music_el + ".volume, t: " + po + ".t})"
            )
            assert inside["paused"] is False, "the gap must never pause the song"
            assert inside["volume"] == 0, inside
            page.wait_for_function(po + ".t > 2.1", timeout=10_000)
            after = page.evaluate(
                "({paused: " + music_el + ".paused, volume: "
                + music_el + ".volume})"
            )
            assert after["paused"] is False
            assert after["volume"] == 1, after
            page.evaluate("window.monteurPlayout.pause()")

            browser.close()

class TestNoRebuildOnCleanReturn:
    """Storyboard -> options -> storyboard with UNCHANGED options must not
    rebuild. The build runs once on the first visit; afterwards only a
    changed build option (or step 3's manual "Rebuild draft") builds anew.
    The gate is a fingerprint of the build payload (everything except the
    download-only "format" key) stored on build success and on draft
    resume — plan mutations (revise/apply/adjust) deliberately leave it
    alone, so clean navigation can never rebuild those edits away."""

    DEMO = str(_DEMO_FOOTAGE)

    # ---- static wiring ---------------------------------------------------

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_fingerprint_helpers_exist(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            "function stableJson",
            "function buildFingerprintOf",
            "function buildFingerprint()",
            "function buildIsDirty",
            "function syncContinueHint",
            # the download format never dirties the draft
            'if (key !== "format") copy[key] = body[key];',
        ):
            assert needle in html, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_continue_handler_skips_a_clean_rebuild(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        # clean draft -> pure navigation, no job
        assert "if (cre.result && rev.planJson && !buildIsDirty())" in html
        # ...and the dirty/no-draft path still builds exactly as before
        assert 'creShowStep(creStepIndex("storyboard"), true);\n  startBuild(null);' in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_fingerprint_is_stored_on_build_success_and_resume(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert "cre.buildFp = buildFingerprintOf(body);" in html  # build done
        assert "cre.buildFp = buildFingerprint();" in html        # draft resume
        # six assignments: the single build's start (null) + success, the
        # series build's start (null) + success, resume, and the
        # footage-changed reset — revise/apply/adjust/swap never touch it, so
        # a revision can't be rebuilt away by navigation
        assert html.count("cre.buildFp =") == 6
        assert html.count("cre.buildFp = null") == 3
        # both build paths store the SAME fingerprint shape on success
        assert html.count("cre.buildFp = buildFingerprintOf(body);") == 2

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_rebuild_button_is_still_the_manual_override(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert (
            'id="cre-build-btn" type="button">Rebuild draft'
            '<span class="spin" aria-hidden="true"></span></button>' in html
        )
        assert (
            '$("cre-build-btn").addEventListener("click", '
            "function () { startBuild(null); });" in html
        )

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_step2_continue_carries_the_quiet_hint(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert 'id="cre-next-2-hint"' in html
        assert "(rebuild needed)" in html
        # the hint lives INSIDE the continue button and starts hidden
        button = html.split('id="cre-next-2"', 1)[1].split("</button>", 1)[0]
        assert 'id="cre-next-2-hint" hidden' in button
        # ...and is refreshed on step entry and on step-2 edits
        assert 'if (step.key === "options") syncContinueHint();' in html
        assert '$("cre-step-2").addEventListener(kind' in html

    # ---- the bug's exact reproduction, in a real browser -------------------

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def test_clean_return_never_rebuilds(self, server, tmp_path):
        playwright_api = pytest.importorskip(
            "playwright.sync_api", reason="playwright is not installed"
        )
        shots = Path(
            os.environ.get("MONTEUR_PLAYWRIGHT_SHOTS") or str(tmp_path / "shots")
        )
        shots.mkdir(parents=True, exist_ok=True)
        build_posts = []  # every POST /api/create/build the page ever fires

        with playwright_api.sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            if browser is None:
                pytest.skip("no Chromium browser available for Playwright")
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.on(
                "request",
                lambda request: build_posts.append(request.url)
                if request.method == "POST" and "/api/create/build" in request.url
                else None,
            )
            plan_js = "JSON.stringify(window.monteurPlayout.plan())"
            board_js = "JSON.stringify(window.monteurPlayout.entries())"

            # ---- step 1: scan ------------------------------------------------
            page.goto(server)
            page.click("#pm-new")  # enter the create workflow from the Home hub
            _pool_scan(page, self.DEMO)
            page.wait_for_selector("#cre-next-1", state="visible", timeout=120_000)

            # ---- step 2 -> 3: the ONE initial build --------------------------
            # WITH music: the revise autosave keeps the music path, so the
            # reload+resume leg below restores a resumable, valid wizard
            page.click("#cre-next-1")
            page.fill("#cre-music", f"{self.DEMO}/song.wav")
            page.fill("#cre-maxlen", "12")
            page.click("#cre-next-2")
            page.wait_for_selector(
                "#cre-sb-board .sb-card", state="visible", timeout=180_000
            )
            assert len(build_posts) == 1
            board_1 = page.evaluate(board_js)
            page.screenshot(path=str(shots / "norebuild-01-first-build.png"))

            # ---- back to options, change NOTHING, continue: NO build job ----
            page.click("#cre-back-3")
            page.wait_for_selector("#cre-step-2", state="visible")
            assert page.is_hidden("#cre-next-2-hint")  # clean: no rebuild note
            page.locator("#cre-next-2").scroll_into_view_if_needed()
            page.screenshot(path=str(shots / "norebuild-02-options-clean.png"))
            page.click("#cre-next-2")
            page.wait_for_selector("#cre-sb-board .sb-card", state="visible")
            page.wait_for_timeout(800)  # a rebuild would POST immediately
            assert len(build_posts) == 1, "clean navigation must never rebuild"
            assert page.evaluate(board_js) == board_1  # the board is identical
            page.screenshot(path=str(shots / "norebuild-03-clean-return.png"))

            # ---- change pace: the hint appears, continue rebuilds ------------
            # (pace lives in the Fine-tune block now — open it first)
            page.click("#cre-back-3")
            page.evaluate("document.getElementById('cre-finetune').open = true")
            page.click("#pace-2")
            page.wait_for_selector("#cre-next-2-hint", state="visible")
            page.locator("#cre-next-2").scroll_into_view_if_needed()
            page.screenshot(path=str(shots / "norebuild-04-dirty-hint.png"))
            with page.expect_request(
                lambda r: r.method == "POST" and "/api/create/build" in r.url,
                timeout=10_000,
            ):
                page.click("#cre-next-2")
            page.wait_for_selector(
                "#cre-sb-board .sb-card", state="visible", timeout=180_000
            )
            assert len(build_posts) == 2

            # ---- revise in step 3, then options -> storyboard: the REVISED
            # plan survives the round trip, still without a build job ----------
            plan_before_revise = page.evaluate(plan_js)
            page.fill("#cre-rev-brief", "ruhiger")
            page.click("#cre-rev-btn")
            page.wait_for_function(  # the field clears only on revise success
                "document.getElementById('cre-rev-brief').value === ''",
                timeout=180_000,
            )
            revised = page.evaluate(plan_js)
            assert revised != plan_before_revise
            assert "revision:" in revised  # the engine marks revised plans
            page.click("#cre-back-3")
            page.wait_for_selector("#cre-step-2", state="visible")
            assert page.is_hidden("#cre-next-2-hint")  # a revise is NOT dirty
            page.click("#cre-next-2")
            page.wait_for_selector("#cre-sb-board .sb-card", state="visible")
            page.wait_for_timeout(800)
            assert len(build_posts) == 2, "a revise must not re-arm the build"
            assert page.evaluate(plan_js) == revised  # the revision is intact
            page.screenshot(path=str(shots / "norebuild-05-revise-kept.png"))

            # ---- reload + resume: forward/back still never rebuilds ----------
            page.reload()
            # persistence flipped to projects and the step-1 draft-resume panel
            # is gone (the Media workspace replaced it): the reload lands on the
            # Project-Manager Home, and the cut is reopened from its card there.
            page.wait_for_selector(
                "#pm-recents .pm-card", state="visible", timeout=30_000
            )
            page.click("#pm-recents .pm-card")
            page.wait_for_selector(
                "#cre-sb-board .sb-card", state="visible", timeout=120_000
            )
            resumed = page.evaluate(plan_js)
            assert "revision:" in resumed  # the autosave carried the revised cut
            page.click("#cre-back-3")
            page.wait_for_selector("#cre-step-2", state="visible")
            assert page.is_hidden("#cre-next-2-hint")  # resume seeds a clean fp
            page.click("#cre-next-2")
            page.wait_for_selector("#cre-sb-board .sb-card", state="visible")
            page.wait_for_timeout(800)
            assert len(build_posts) == 2, (
                "resume -> options -> storyboard must not rebuild"
            )
            assert page.evaluate(plan_js) == resumed
            page.screenshot(path=str(shots / "norebuild-06-resumed.png"))

            browser.close()


# --- Auto-first options (step 2) + the free-running playout music ------------


def _step2_html(html):
    """The Options step's markup (between its section tag and step 3's)."""
    return html.split('id="cre-step-2"', 1)[1].split('id="cre-step-3"', 1)[0]


class TestMovieRecents:
    """A Movie project indexes itself as a lightweight recents pointer."""

    def test_register_indexes_a_movie_pointer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MONTEUR_PROJECTS_PATH", str(tmp_path / "projects"))
        from monteur import projects
        from monteur.web import server

        class _Stub:
            title = "Nebel"

        mdir = str(tmp_path / "films" / "nebel")
        server._register_movie_recent(mdir, _Stub())
        listed = projects.list_projects()
        assert len(listed) == 1
        entry = listed[0]
        assert entry["type"] == "movie"
        assert entry["name"] == "Nebel"
        assert entry["movie_path"] == os.path.abspath(mdir)
        assert entry["id"].startswith("mv")
        # reopening the SAME folder updates the one entry, never duplicates
        server._register_movie_recent(mdir, _Stub())
        assert len(projects.list_projects()) == 1
        # a different film is a separate pointer
        server._register_movie_recent(str(tmp_path / "films" / "other"), _Stub())
        assert len(projects.list_projects()) == 2

    def test_register_never_raises(self, monkeypatch):
        # best-effort: a bad projects root must not break the movie endpoint
        from monteur.web import server

        class _Stub:
            title = "X"

        monkeypatch.setattr(server.os.path, "abspath", lambda p: (_ for _ in ()).throw(OSError("boom")))
        server._register_movie_recent("/whatever", _Stub())  # must not raise


class TestNativeShell:
    """`serve_app`: Monteur Studio in a native window (pywebview), with a
    browser fallback when pywebview isn't installed. The GUI never actually
    opens here — webview and the server are both stubbed."""

    def test_serve_app_opens_a_native_window(self, monkeypatch):
        import types

        from monteur.web import server

        calls = {}
        fake = types.ModuleType("webview")
        fake.create_window = lambda title, url, **kw: calls.update(window=(title, url, kw))
        fake.start = lambda: calls.update(started=True)
        monkeypatch.setitem(sys.modules, "webview", fake)

        class _Srv:
            server_address = ("127.0.0.1", 8801)

        def fake_serve(**kw):
            # a real server would bind, fire on_bind + ready, then loop; the
            # stub just hands back a URL so the window can open
            kw["on_bind"](_Srv())
            kw["ready"].set()

        monkeypatch.setattr(server, "serve", fake_serve)
        server.serve_app(port=8801, title="Monteur Studio")

        assert calls.get("started") is True
        title, url, kw = calls["window"]
        assert title == "Monteur Studio"
        assert url == "http://127.0.0.1:8801/"
        assert kw.get("width") and kw.get("height")  # a real window size
        # frameless: app.html draws its own Fluent title bar + drives the
        # window through the js_api bridge
        assert kw.get("frameless") is True
        assert isinstance(kw.get("js_api"), server._WindowControls)

    def test_window_controls_bridge_drives_the_window(self):
        import types

        from monteur.web import server

        recorder = []

        class _Win:
            def minimize(self):
                recorder.append("minimize")

            def maximize(self):
                recorder.append("maximize")

            def restore(self):
                recorder.append("restore")

            def destroy(self):
                recorder.append("destroy")

        wv = types.ModuleType("webview")
        wv.windows = [_Win()]
        controls = server._WindowControls(wv)
        controls.minimize()
        controls.toggle_maximize()  # -> maximize
        controls.toggle_maximize()  # -> restore
        controls.close()
        assert recorder == ["minimize", "maximize", "restore", "destroy"]

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_one_bar_with_menu_and_caption(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        # the separate title bar is gone — everything lives in the ONE top bar
        assert 'class="titlebar"' not in source
        assert 'id="titlebar"' not in source
        for needle in (
            'id="tb-menu"',        # the File/View/Help menu bar
            'id="tb-caption"',     # the caption buttons, in the top bar
            'id="cap-min"', 'id="cap-max"', 'id="cap-close"',
            "var APP_MENUS",
            "function renderAppMenus",
            "function nativeWindowCall",
            "native-shell",        # the class the shell adds on pywebviewready
            "-webkit-app-region: drag",   # the drag region
            "app-region: no-drag",        # interactive bits don't drag
        ):
            assert needle in source, needle
        # the menu carries real actions (File has New Cut + Close window)
        assert '"New Cut"' in source and '"Close window"' in source

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_menu_has_accelerators_and_shortcut_sheet(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            # a single source of truth for the accelerators
            "var SHORTCUTS",
            "function comboMatches",
            "function openShortcuts",
            "Keyboard shortcuts",
            # real accelerators, shown in the menu and wired globally
            '"Ctrl+N"', '"Ctrl+Shift+N"', '"Ctrl+,"', '"Ctrl+1"',
            # the richer menu framework
            "function buildMenuItem",
            "function openSubmenu",
            "Open Recent",           # dynamic submenu of recent projects
            "recentSubmenu",
            "nativeOnly",            # Close window only in the native shell
            "mi-key", "mi-check", "mi-arrow",
        ):
            assert needle in source, needle
        # View is page-navigation with a checkmark on the current view
        assert '"Home"' in source and '"Create"' in source and '"Movie"' in source

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_fluent_materials_present(self):
        source = _APP_HTML.read_text(encoding="utf-8")
        for needle in (
            "--acrylic:", "--acrylic-strong:", "--mica:", "--shadow-flyout:",
            # acrylic flyouts + dialogs, mica title bar
            "backdrop-filter: blur(30px)",
            "background-image: var(--mica)",
        ):
            assert needle in source, needle

    def test_window_controls_never_raise(self):
        import types

        from monteur.web import server

        # no windows yet, and a window whose methods explode: still no crash
        wv = types.ModuleType("webview")
        wv.windows = []
        server._WindowControls(wv).minimize()  # no window -> no-op

        class _Bad:
            def minimize(self):
                raise RuntimeError("boom")

            def destroy(self):
                raise RuntimeError("boom")

        wv.windows = [_Bad()]
        controls = server._WindowControls(wv)
        controls.minimize()  # swallowed
        controls.close()  # swallowed

    def test_serve_app_falls_back_to_browser_without_pywebview(self, monkeypatch):
        from monteur.web import server

        # `import webview` -> ImportError (the [app] extra is not installed)
        monkeypatch.setitem(sys.modules, "webview", None)
        calls = {}
        monkeypatch.setattr(server, "serve", lambda **kw: calls.update(kw))
        server.serve_app(port=8802, project_root="/tmp/x")
        # the browser path, not a window
        assert calls.get("open_browser") is True
        assert calls.get("port") == 8802
        assert calls.get("project_root") == "/tmp/x"

    def test_ui_window_flag_routes_to_serve_app(self, monkeypatch):
        import argparse

        from monteur import cli

        seen = {}
        monkeypatch.setattr("monteur.web.serve_app", lambda **kw: seen.update(kw, mode="window"))
        monkeypatch.setattr("monteur.web.serve", lambda **kw: seen.update(mode="browser"))
        cli.cmd_ui(argparse.Namespace(window=True, port=9000, project="."))
        assert seen.get("mode") == "window"
        assert seen.get("port") == 9000

    def test_ui_without_window_flag_uses_the_browser(self, monkeypatch):
        import argparse

        from monteur import cli

        seen = {}
        monkeypatch.setattr("monteur.web.serve_app", lambda **kw: seen.update(mode="window"))
        monkeypatch.setattr("monteur.web.serve", lambda **kw: seen.update(kw, mode="browser"))
        cli.cmd_ui(argparse.Namespace(window=False, port=8765, project=".", no_browser=False))
        assert seen.get("mode") == "browser"
        assert seen.get("open_browser") is True


class TestAutoFirstOptionsUi:
    """Static asserts: pace + transitions moved INTO the Fine-tune block,
    both defaulting to Auto — the main Options view keeps only the big
    decisions (platform, music, audio mode, shape, length, style, fps,
    brief, ai_cut)."""

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_pace_and_transitions_live_in_the_fine_tune_block(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        step2 = _step2_html(html)
        finetune = step2.split('id="cre-finetune"', 1)[1].split(
            "</details>", 1
        )[0]
        for needle in (
            'id="pace-auto"', 'id="pace-1"', 'id="pace-2"', 'id="pace-4"',
            'id="cre-pace"',
            'id="trans-auto"', 'id="trans-cuts"', 'id="trans-dissolves"',
            'id="trans-smash"',
        ):
            assert needle in finetune, needle
        # ...and nowhere in the slim main view above the fine-tune block
        main_view = step2.split('id="cre-finetune"', 1)[0]
        for absent in ('id="pace-auto"', 'id="cre-pace"', 'id="trans-auto"'):
            assert absent not in main_view, absent

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_both_default_to_auto_with_the_auto_copy(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        step2 = _step2_html(html)
        assert 'id="pace-auto" aria-pressed="true">Auto<' in step2
        assert 'id="trans-auto" aria-pressed="true">Auto<' in step2
        # the copy sells the automation instead of asking for numbers
        assert "Auto (recommended): Monteur cuts to the music" in step2
        assert "lengths vary like a human edit" in step2
        assert "Set seconds only to force a feel" in step2
        assert "Auto (recommended): decided per cut" in step2
        assert "hard on the beat in action" in step2
        assert "smash-to-black at act breaks" in step2

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_main_view_keeps_only_the_big_decisions(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        main_view = _step2_html(html).split('id="cre-finetune"', 1)[0]
        for needle in (
            'id="cre-platform-row"',   # what are you making?
            'id="cre-music"',          # the song
            'id="audio-music"',        # audio mode
            'id="len-target"',         # length
            'id="cre-style-cards"',    # style
            'id="cre-canvas-cards"',   # shape stays where it is
            'id="cre-fps"',            # frame rate
            'id="cre-brief"',          # the brief
            'id="cre-ai-cut"',         # Claude composes the cut
        ):
            assert needle in main_view, needle

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_auto_keeps_the_payload_and_fingerprint_unchanged(self):
        """Auto = the exact same payload as before the move: pace stays null
        (key omitted from the build body), transitions stays the literal
        "auto" — settings keys unchanged, so stored drafts round-trip and
        the no-rebuild fingerprint keeps matching."""
        html = _APP_HTML.read_text(encoding="utf-8")
        assert '["pace-auto", null]' in html
        assert 'transitions: "auto"' in html
        assert "if (cre.pace) body.pace = cre.pace;" in html
        assert "transitions: cre.transitions," in html
        # draft restore still reads the same settings keys
        assert "if (s.transitions && TRANSITION_IDS[s.transitions])" in html
        assert "var pace = parseFloat(s.pace);" in html
        # the fingerprint reads VALUES (the payload), never layout
        assert (
            "function buildFingerprint() "
            "{ return buildFingerprintOf(creBuildBody()); }" in html
        )


class TestPlayoutMusicFreeRun:
    """The song must play straight through the black title dips (dips are
    VIDEO black, not audio silence) and must never be restarted per
    segment: static asserts + a real-browser acceptance run."""

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_music_glides_instead_of_reseeking(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        # gentle drift correction: a playbackRate nudge; a hard reseek only
        # past a REAL desync (0.35 s) or an explicit user seek (force)
        for needle in (
            "var drift = (music.currentTime || 0) - want;",
            "Math.abs(drift) > 0.35",
            "drift > 0 ? 0.96 : 1.04",
        ):
            assert needle in html, needle
        # the old behavior — reseek the music at the video's 80 ms
        # threshold, which stalled the element (seek + rebuffer) every
        # drift check — is gone; 0.08 remains for the VIDEO buffers only
        assert "Math.abs(music.currentTime - want) > 0.08" not in html
        assert "Math.abs(po.active.currentTime - want) > 0.08" in html

    # ---- the acceptance run: audio never pauses through a dip --------------

    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def test_autofirst_options_and_music_through_dips(self, server, tmp_path):
        playwright_api = pytest.importorskip(
            "playwright.sync_api", reason="playwright is not installed"
        )
        shots = Path(
            os.environ.get("MONTEUR_PLAYWRIGHT_SHOTS") or str(tmp_path / "shots")
        )
        shots.mkdir(parents=True, exist_ok=True)
        build_payloads = []

        with playwright_api.sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            if browser is None:
                pytest.skip("no Chromium browser available for Playwright")
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.on(
                "request",
                lambda request: build_payloads.append(request.post_data)
                if request.method == "POST"
                and "/api/create/build" in request.url
                else None,
            )

            # ---- step 1: scan ------------------------------------------------
            page.goto(server)
            page.click("#pm-new")  # enter the create workflow from the Home hub
            _pool_scan(page, self.DEMO)
            page.wait_for_selector("#cre-next-1", state="visible", timeout=120_000)

            # ---- step 2: slim options; pace + transitions in Fine-tune -------
            page.click("#cre-next-1")
            assert page.is_hidden("#pace-auto")   # tucked away until opened
            assert page.is_hidden("#trans-auto")
            page.locator("#cre-finetune").scroll_into_view_if_needed()
            page.screenshot(path=str(shots / "autofirst-options-slim.png"))
            page.click("#cre-finetune summary")
            page.wait_for_selector("#pace-auto", state="visible")
            # both default to Auto
            assert page.get_attribute("#pace-auto", "aria-pressed") == "true"
            assert page.get_attribute("#trans-auto", "aria-pressed") == "true"
            assert page.text_content("#pace-auto").strip() == "Auto"
            assert page.text_content("#trans-auto").strip() == "Auto"
            page.screenshot(path=str(shots / "autofirst-finetune-auto.png"))

            # ---- build WITH music, untouched Auto controls --------------------
            page.fill("#cre-music", f"{self.DEMO}/song.wav")
            page.fill("#cre-maxlen", "12")
            page.click("#cre-next-2")
            page.wait_for_selector(
                "#cre-sb-board .sb-card", state="visible", timeout=180_000
            )
            page.wait_for_selector("#cre-playout", state="visible")
            # Auto sends the exact default payload: no pace key, transitions
            # stays the literal "auto" — the engine decides per cut
            assert len(build_payloads) == 1
            body = json.loads(build_payloads[0])
            assert "pace" not in body
            assert body["transitions"] == "auto"

            # ---- a dip to cross: take the plan's, or carve one ----------------
            def sorted_dips():
                return page.evaluate(
                    "((window.monteurPlayout.plan().dips) || [])"
                    ".slice().sort((a, b) => a[0] - b[0])"
                )

            if not sorted_dips():
                slot = page.evaluate(
                    """(() => {
                      const entries = window.monteurPlayout.entries();
                      let best = -1, bestLen = 0;
                      for (let i = 1; i < entries.length; i++) {
                        const prev = entries[i - 1];
                        const len = prev.record_end - prev.record_start;
                        if ((entries[i].transition || 0) > 0) continue;
                        if (len > bestLen) { best = i; bestLen = len; }
                      }
                      return best;
                    })()"""
                )
                assert slot >= 1
                page.click(f"#cre-strip-blocks .tl-block[data-slot='{slot}']")
                page.wait_for_selector("#cre-inspector", state="visible")
                page.click('#cre-insp-trans button[data-trans="smash"]')
                page.wait_for_function(
                    "((window.monteurPlayout.plan().dips) || []).length > 0",
                    timeout=30_000,
                )
            page.evaluate(
                """(() => {
                  const plan = window.monteurPlayout.plan();
                  plan.title_texts = (plan.dips || []).map(() => 'ACT BREAK');
                  window.monteurPlayout.rebuild();
                })()"""
            )
            dip_start, dip_length = sorted_dips()[0]
            dip_end = dip_start + dip_length

            # ---- play across the dip; the song must NEVER pause ---------------
            page.evaluate(
                f"window.monteurPlayout.seek({max(0.0, dip_start - 0.6)})"
            )
            page.evaluate("window.monteurPlayout.play()")
            page.wait_for_function(
                "!document.getElementById('po-music').paused", timeout=10_000
            )
            # count pause/seek events and sample the element from HERE on:
            # free-running means zero of either while crossing the dip
            page.evaluate(
                """(() => {
                  const m = document.getElementById('po-music');
                  window.__mus = {pauses: 0, seeks: 0, samples: []};
                  m.addEventListener('pause', () => { window.__mus.pauses++; });
                  m.addEventListener('seeking', () => { window.__mus.seeks++; });
                  window.__musTimer = setInterval(() => {
                    window.__mus.samples.push({
                      t: window.monteurPlayout.state.t,
                      paused: m.paused,
                      ct: m.currentTime
                    });
                  }, 40);
                })()"""
            )
            page.wait_for_function(
                f"window.monteurPlayout.state.t > {dip_end + 0.5}",
                timeout=int((1.1 + dip_length) * 1000) + 15_000,
            )
            page.evaluate("clearInterval(window.__musTimer)")
            mus = page.evaluate("window.__mus")
            page.evaluate("window.monteurPlayout.pause()")

            assert mus["pauses"] == 0, "the song paused while crossing the dip"
            assert mus["seeks"] == 0, "the song was reseeked mid-play"
            samples = mus["samples"]
            assert len(samples) >= 10
            in_dip = [
                s for s in samples if dip_start + 0.05 <= s["t"] <= dip_end - 0.05
            ]
            assert in_dip, "no samples landed inside the dip window"
            assert all(s["paused"] is False for s in samples)
            # currentTime advances monotonically — and really advances
            cts = [s["ct"] for s in samples]
            assert all(b >= a for a, b in zip(cts, cts[1:]))
            span = samples[-1]["t"] - samples[0]["t"]
            assert cts[-1] - cts[0] >= 0.5 * span

            # the dip itself: black stage, title as DOM text, song running
            page.evaluate(
                f"window.monteurPlayout.seek({dip_start + dip_length / 2})"
            )
            page.wait_for_function(
                "!document.getElementById('po-title').hidden", timeout=5_000
            )
            assert page.text_content("#po-title") == "ACT BREAK"
            page.screenshot(path=str(shots / "autofirst-dip-title.png"))

            browser.close()
