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


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Every web test gets scratch settings AND drafts files — the server
    reads monteur.settings per request and autosaves drafts after builds,
    and tests must never touch (or depend on) the developer's real
    ~/.monteur/settings.json or ~/.monteur/drafts.json."""
    monkeypatch.setenv(
        "MONTEUR_SETTINGS_PATH", str(tmp_path / "web-settings.json")
    )
    monkeypatch.setenv(
        "MONTEUR_DRAFTS_PATH", str(tmp_path / "web-drafts.json")
    )


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


class TestArrangeUi:
    """Static asserts on app.html: the Arrange step's markup and wiring."""

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_step_strip_has_the_conditional_arrange_step(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        # the opt-in checkbox in step 2 and the conditional wbar chip
        assert 'id="cre-arrange"' in html
        assert "Arrange the story myself" in html
        assert '<span id="wbar-2b" hidden>' in html
        # the step section itself, between steps 2 and 3
        assert 'id="cre-step-2b"' in html
        assert html.index('id="cre-step-2"') < html.index('id="cre-step-2b"') \
            < html.index('id="cre-step-3"')
        # the wizard walks 1 -> 2 -> 2b -> 3 only when opted in
        assert 'creShowStep(cre.arrange.on ? "2b" : 3, true)' in html
        assert "setArrangeOn" in html

    @pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
    def test_palette_sequence_and_boundary_markup(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert 'id="cre-arr-palette"' in html
        assert 'id="cre-arr-seq"' in html
        assert 'id="cre-arr-filter"' in html   # find-search as the palette filter
        assert 'id="cre-arr-count"' in html    # the live counter
        assert "arr-bound" in html             # boundary control between cards
        assert 'ARR_TRANSITIONS = ["cut", "dissolve", "smash"]' in html
        assert '["", "impact", "whoosh", "riser"]' in html
        assert 'id="cre-arr-sfx-hint"' in html  # hidden-with-hint without elements
        assert "/api/thumb?clip=" in html

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
        # step 1: the "Continue where you left off" panel
        assert 'id="cre-drafts"' in html
        assert 'id="cre-drafts-list"' in html
        assert "Continue where you left off" in html
        # step 3: the Save-draft controls next to the download bar
        assert 'id="cre-save-draft"' in html
        assert 'id="cre-draft-name"' in html
        assert "Save draft" in html
        # the client speaks both new endpoints
        assert "/api/drafts" in html
        assert "/api/create/export" in html


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
            size=None,
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
            }
        ]

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
                        progress=None, size=None):
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
