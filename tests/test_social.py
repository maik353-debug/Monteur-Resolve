"""Social-platform wave 1: the "short" style, platform presets, #Shorts wiring.

Engine (monteur.montage): the "short" style's anti-canon grid (NO opening
hold, hook capped at ~2 s absolute), the hook casting on slot 0 (pattern
interrupt over the opener role), the loop-friendly last slot, the
PLATFORMS presets + resolve_platform precedence, and byte-parity of every
existing style. Composer (monteur.compose): the "short" craft brief and
its prompt lines. Server (monteur.web.server): platform forwarding,
drafts persistence, the #Shorts prefill routing, static UI asserts.
CLI: --platform parsing and resolution.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from monteur.montage import (
    BEST_FIRST,
    PLATFORMS,
    STYLES,
    plan_montage,
    plan_to_dict,
    resolve_platform,
)
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import ClipReport, Moment

from _demo import DEMO as _DEMO_FOOTAGE

_APP_HTML = Path(__file__).parent.parent / "monteur" / "web" / "app.html"


def make_music(duration: float = 12.0) -> MusicAnalysis:
    """Beats at 0.5 s (120 BPM), one flat section — a neutral grid."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, 0.6, "mid")],
    )


def slot_length(entry) -> float:
    return entry.record_end - entry.record_start


# --- the "short" style table ---------------------------------------------------


class TestShortStyle:
    def test_registered_with_hook_punch_loop_arc(self):
        style = STYLES["short"]
        assert style.name == "Social Short"
        assert [label for _, label in style.arc] == ["hook", "punch", "loop"]
        shares = [share for share, _ in style.arc]
        assert shares == pytest.approx([0.08, 0.72, 0.2])
        assert style.beats_per_cut == {"hook": 1, "punch": 1, "loop": 2}
        assert style.prefer_highlights_in == "punch"
        assert style.no_opening_hold is True
        # every other style keeps the establishing-hold canon
        assert all(
            not s.no_opening_hold for key, s in STYLES.items() if key != "short"
        )

    def test_first_cut_has_no_opening_hold(self):
        """Anti-canon: the short's first gap is the hook BASE (1 beat), while
        an arc style with the same tempo opens on a 2x establishing hold."""
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=40.0,
                moments=[Moment(i * 5.0, i * 5.0 + 4.0, 0.8) for i in range(8)],
            )
        ]
        short = plan_montage(reports, make_music(24.0), style="short", cut_lead=0.0)
        assert any("no opening hold" in n for n in short.notes)
        assert slot_length(short.entries[0]) == pytest.approx(0.5)  # 1 beat

        video = plan_montage(
            reports, make_music(24.0), style="music_video", cut_lead=0.0
        )
        # same tempo, fast style — but the canon holds its opener ~2x base
        assert slot_length(video.entries[0]) > slot_length(short.entries[0])
        assert any("opening hold" in n for n in video.notes)

    def test_first_cut_capped_at_two_seconds_on_slow_pace(self):
        """pace=4 inflates the hook base to ~8 beats (4 s); the absolute
        hook cap (~2 s) must undercut the base — a late hook is no hook."""
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=120.0,
                moments=[Moment(i * 10.0, i * 10.0 + 8.0, 0.8) for i in range(10)],
            )
        ]
        plan = plan_montage(
            reports, make_music(60.0), style="short", cut_lead=0.0, pace=4.0
        )
        assert slot_length(plan.entries[0]) <= 2.0 + 1e-6
        # deliberately faster than the paced base of ~4 s
        assert slot_length(plan.entries[0]) < 4.0

    def test_short_plans_no_fades_and_hard_cuts(self):
        """A loopable short must not fade to black or dissolve — it replays."""
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=40.0,
                moments=[Moment(i * 5.0, i * 5.0 + 4.0, 0.8) for i in range(8)],
            )
        ]
        plan = plan_montage(reports, make_music(), style="short")
        assert plan.fade_in == 0.0
        assert plan.fade_out == 0.0
        assert all(e.transition == 0.0 for e in plan.entries)
        assert plan.phases and plan.phases[0][2] == "hook"


# --- hook casting (slot 0 = the pattern interrupt) -------------------------------


def _hook_reports() -> list[ClipReport]:
    """Pool where the opener-role moment and the hook-score moment differ.

    Chronological pool order: a.mp4 0-5 (the calm opener, role but no
    motion/hero) comes FIRST; a.mp4 10-15 is the pattern interrupt (fast,
    hero) and would lose slot 0 to the opener under the role preference.
    """
    return [
        ClipReport(
            path="/footage/a.mp4",
            duration=40.0,
            moments=[
                Moment(0.0, 5.0, 0.6, role="opener", label="calm wide"),
                Moment(
                    10.0, 15.0, 0.5,
                    entry_motion=(4.0, 0.0), exit_motion=(4.0, 0.0),
                    hero=0.9, label="jump over the gap",
                ),
                Moment(20.0, 25.0, 0.55),
                Moment(30.0, 35.0, 0.5),
            ],
        )
    ]


class TestHookCasting:
    def test_short_slot0_takes_pattern_interrupt_not_opener(self):
        plan = plan_montage(_hook_reports(), make_music(), style="short")
        assert plan.entries[0].source_start == pytest.approx(10.0)
        assert any(
            "hook: opening on the boldest moment" in n for n in plan.notes
        )

    def test_other_styles_still_prefer_the_opener_role(self):
        plan = plan_montage(_hook_reports(), make_music(), style="music_video")
        assert plan.entries[0].source_start == pytest.approx(0.0)
        assert not any(n.startswith("hook:") for n in plan.notes)

    def test_hook_score_blends_motion_hero_and_score(self):
        """A pure-score moment loses the hook to a motion+hero moment, even
        when the score moment sits earlier in the pool."""
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=40.0,
                moments=[
                    Moment(0.0, 5.0, 1.0),  # best score, static, no hero
                    Moment(
                        10.0, 15.0, 0.4,
                        entry_motion=(3.0, 0.0), exit_motion=(3.0, 0.0),
                        hero=0.8,
                    ),
                    Moment(20.0, 25.0, 0.5),
                ],
            )
        ]
        plan = plan_montage(reports, make_music(), style="short")
        # 0.5*1.0 + 0.3*0.8 + 0.2*0.4 = 0.82 beats 0.5*0 + 0.3*0 + 0.2*1.0
        assert plan.entries[0].source_start == pytest.approx(10.0)


# --- loop ending (the last slot cuts back into the hook) --------------------------


class TestLoopEnding:
    def test_last_slot_prefers_the_hooks_scene_group(self):
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=60.0,
                moments=[
                    Moment(
                        0.0, 6.0, 0.7,
                        entry_motion=(3.0, 0.0), exit_motion=(3.0, 0.0),
                        hero=0.9, group="cliff",
                    ),
                    Moment(10.0, 16.0, 0.6, group="forest"),
                    Moment(20.0, 26.0, 0.65, group="cliff", label="cliff again"),
                    Moment(30.0, 36.0, 0.6, group="river"),
                    Moment(40.0, 46.0, 0.6, group="camp"),
                ],
            )
        ]
        plan = plan_montage(reports, make_music(), style="short")
        assert plan.entries[0].source_start == pytest.approx(0.0)  # the hook
        assert plan.entries[-1].source_start == pytest.approx(20.0)  # same scene
        assert any("loop: last shot matches the hook's scene" in n for n in plan.notes)

    def test_falls_back_to_matching_motion_energy(self):
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=60.0,
                moments=[
                    Moment(
                        0.0, 6.0, 0.7,
                        entry_motion=(4.0, 0.0), exit_motion=(4.0, 0.0), hero=0.9,
                    ),
                    Moment(10.0, 16.0, 0.6),  # static
                    Moment(
                        20.0, 26.0, 0.6,
                        entry_motion=(3.8, 0.0), exit_motion=(3.8, 0.0),
                    ),  # closest motion to the hook
                    Moment(
                        30.0, 36.0, 0.6,
                        entry_motion=(1.0, 0.0), exit_motion=(1.0, 0.0),
                    ),
                ],
            )
        ]
        plan = plan_montage(reports, make_music(), style="short")
        assert plan.entries[-1].source_start == pytest.approx(20.0)
        assert any(
            "loop: last shot matches the hook's motion energy" in n
            for n in plan.notes
        )

    def test_graceful_without_groups_or_motion(self):
        """All-static, group-less footage: no loop reservation, no fake note
        — the normal fill decides the last slot and the plan still builds."""
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=60.0,
                moments=[Moment(i * 8.0, i * 8.0 + 6.0, 0.6) for i in range(5)],
            )
        ]
        plan = plan_montage(reports, make_music(), style="short")
        assert plan.entries
        assert not any(n.startswith("loop:") for n in plan.notes)


# --- platform presets --------------------------------------------------------------


class TestPlatformPresets:
    def test_the_table(self):
        assert PLATFORMS["youtube"] == {
            "canvas": "uhd", "style": None, "max_seconds": None,
        }
        assert PLATFORMS["short"] == {
            "canvas": "vertical-uhd", "style": "short", "max_seconds": 60.0,
        }
        assert PLATFORMS["reel"] == {
            "canvas": "vertical-uhd", "style": "short", "max_seconds": 90.0,
        }
        assert PLATFORMS["tiktok"] == {
            "canvas": "vertical-uhd", "style": "short", "max_seconds": 60.0,
        }

    def test_vertical_platform_forces_style_canvas_and_cap(self):
        resolved = resolve_platform("tiktok")
        assert resolved["style"] == "short"
        assert resolved["canvas"] == "vertical-uhd"
        assert resolved["max_duration"] == 60.0
        assert any("length capped at 60s" in n for n in resolved["notes"])

    def test_default_auto_style_is_not_explicit(self):
        assert resolve_platform("short", style="auto")["style"] == "short"
        assert resolve_platform("short", style="")["style"] == "short"
        assert resolve_platform("short", style="short")["style"] == "short"

    def test_explicit_style_wins_platform_keeps_canvas_and_cap(self):
        resolved = resolve_platform(
            "tiktok", style="trailer", canvas="hd", max_duration=120.0
        )
        assert resolved["style"] == "trailer"
        assert resolved["canvas"] == "vertical-uhd"  # the frame IS the platform
        assert resolved["max_duration"] == 60.0
        assert any('keeping your "trailer" style' in n for n in resolved["notes"])
        assert any("length capped at 60s" in n for n in resolved["notes"])

    def test_cap_never_extends_a_shorter_request(self):
        resolved = resolve_platform("reel", max_duration=30.0)
        assert resolved["max_duration"] == 30.0
        assert not any("capped" in n for n in resolved["notes"])

    def test_youtube_keeps_style_and_length(self):
        resolved = resolve_platform("youtube", style="travel", max_duration=300.0)
        assert resolved["style"] == "travel"
        assert resolved["canvas"] == "uhd"
        assert resolved["max_duration"] == 300.0
        assert resolved["notes"] == []

    def test_unknown_platform_raises_with_the_valid_list(self):
        with pytest.raises(ValueError) as exc_info:
            resolve_platform("vine")
        assert "valid platforms" in str(exc_info.value)
        assert "tiktok" in str(exc_info.value)


# --- parity: the existing styles stay byte-identical --------------------------------


def _parity_music() -> MusicAnalysis:
    return MusicAnalysis(
        path="/music/parity.wav",
        duration=60.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(120)],
        downbeats=[i * 2.0 for i in range(30)],
        phrases=[0.0, 8.0, 16.0, 24.0, 32.0, 40.0, 48.0, 56.0],
        drops=[32.0],
        sections=[
            MusicSection(0.0, 20.0, 0.3, "low"),
            MusicSection(20.0, 40.0, 0.6, "mid"),
            MusicSection(40.0, 60.0, 0.9, "high"),
        ],
    )


def _parity_reports() -> list[ClipReport]:
    a = ClipReport(
        path="/footage/a.mp4",
        duration=40.0,
        moments=[
            Moment(0.0, 6.0, 0.9, entry_motion=(2.0, 0.0), exit_motion=(1.5, 0.5),
                   highlight=0.2, label="wide valley", role="opener", group="valley"),
            Moment(10.0, 15.0, 0.7, entry_motion=(0.0, 0.0), exit_motion=(0.0, 0.0)),
            Moment(20.0, 26.0, 0.8, entry_motion=(3.0, 1.0), exit_motion=(3.0, 1.0),
                   highlight=0.9, label="overtake", role="climax", hero=0.8, group="road"),
            Moment(30.0, 34.0, 0.6),
        ],
        usable_ratio=0.8,
    )
    b = ClipReport(
        path="/footage/b.mp4",
        duration=35.0,
        moments=[
            Moment(2.0, 7.0, 0.95, entry_motion=(1.0, 2.0), exit_motion=(0.5, 1.0),
                   highlight=0.5, label="summit", role="closer", group="summit"),
            Moment(9.0, 12.0, 0.5),
            Moment(15.0, 21.0, 0.85, entry_motion=(2.5, 0.0), exit_motion=(2.0, 0.0),
                   highlight=0.7, role="build", group="road"),
            Moment(25.0, 30.0, 0.65),
        ],
        usable_ratio=0.7,
    )
    return [a, b]


# sha256 of the sorted-key JSON of plan_to_dict, captured on the engine
# BEFORE the "short" style existed. Any drift means an existing style's
# plan changed — the one thing this feature must never do.
_PARITY_GOLDEN = {
    "auto": "0920c6f954669f1f5aaa64b24ac2139ea292321d94044a5eb18b41fb99121950",
    "travel": "aea7a53eccfc21cb9c8ec70381c4c79c9c01600635c5236b03d23040feb320f2",
    "wedding": "da7ffef60f96594fc2eb5253aebc79ead40d124bde252ce9f18139abcdcd7b27",
    "music_video": "b761ea04c4c2b254169331152e44539b6a853a0e6dc3ec823b51ac58d1194875",
    "trailer": "c6125f1f4894940a52142c53495a7c18a64ab572a372b69f5fc695faf48801a9",
    "trailer-best-paced": "bcaf8191ac707dd4529a78e9a7bcd422746f3d2565952d3e08257bf571f3d9f9",
    "travel-no-music": "fd8624e966b4d5b18940d425f4f691516e35e75b37b5a9351415156aa73a1c6b",
}


def _digest(plan) -> str:
    return hashlib.sha256(
        json.dumps(plan_to_dict(plan), sort_keys=True).encode()
    ).hexdigest()


class TestExistingStylesParity:
    @pytest.mark.parametrize(
        "style", ["auto", "travel", "wedding", "music_video", "trailer"]
    )
    def test_style_plans_byte_identical(self, style):
        plan = plan_montage(_parity_reports(), _parity_music(), style=style, sfx=True)
        assert _digest(plan) == _PARITY_GOLDEN[style]

    def test_paced_best_first_trailer_byte_identical(self):
        plan = plan_montage(
            _parity_reports(), _parity_music(), style="trailer", order=BEST_FIRST,
            max_duration=30.0, pace=1.0, transitions="smash",
        )
        assert _digest(plan) == _PARITY_GOLDEN["trailer-best-paced"]

    def test_no_music_travel_byte_identical(self):
        plan = plan_montage(_parity_reports(), None, style="travel", max_duration=24.0)
        assert _digest(plan) == _PARITY_GOLDEN["travel-no-music"]


# --- composer: the "short" craft brief ----------------------------------------------


class TestShortCraftBrief:
    def test_brief_exists_and_teaches_hook_and_loop(self):
        from monteur.compose import CRAFT_BRIEFS

        brief = CRAFT_BRIEFS["short"]
        assert "Hook in the FIRST second" in brief
        assert "never establish" in brief
        assert "loops back into the opening" in brief
        assert "boldest" in brief  # the boldest image, not the prettiest

    def test_prompt_carries_the_short_brief(self):
        from monteur.compose import CRAFT_BRIEFS, _build_prompt, compose_context

        reports = _hook_reports()
        plan = plan_montage(reports, make_music(), style="short")
        context = compose_context(plan, reports, make_music(), style="short")
        assert context["style"] == "short"
        assert any(slot.get("phase") == "hook" for slot in context["slots"])
        prompt = _build_prompt(context, "short", "a 30s ride short")
        assert CRAFT_BRIEFS["short"] in prompt
        assert "a 30s ride short" in prompt


# --- server: platform forwarding, drafts, the #Shorts prefill ------------------------


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Scratch settings + drafts files, exactly like tests/test_web.py —
    these tests must never touch the developer's real ~/.monteur files."""
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MONTEUR_DRAFTS_PATH", str(tmp_path / "drafts.json"))


@pytest.fixture()
def server(tmp_path):
    from monteur.project import Project
    from monteur.web.server import MonteurHandler, MonteurServer

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


def _wait_for_job(server, job_id, timeout=60.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _get(f"{server}/api/jobs/{job_id}")
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} still {job['state']!r} after {timeout}s")


def _shorts_plan_dict(duration=45.0):
    """A hand-made plan for the prefill tests (paths never touched)."""
    from monteur.montage import MontageEntry, MontagePlan

    plan = MontagePlan(
        music_path="",
        duration=duration,
        entries=[
            MontageEntry(
                clip_path="/footage/ride.mp4", source_start=0.0, source_end=10.0,
                record_start=0.0, record_end=duration, score=1.0,
                label="Overtake in a left curve",
            ),
        ],
        notes=["story: One ride, one jump."],
    )
    return plan_to_dict(plan)


class TestPlatformApi:
    DEMO = str(_DEMO_FOOTAGE)

    @pytest.fixture(autouse=True)
    def _needs_demo_media(self):
        if not Path(self.DEMO).is_dir():
            pytest.skip("demo footage not generated in this environment")

    def test_build_platform_short_forces_style_canvas_and_cap(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "platform": "short", "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        notes = job["result"]["plan"]["notes"]
        assert any('style "short"' in n for n in notes)
        assert any('platform "short": length capped at 60s' in n for n in notes)
        assert any("hook:" in n for n in notes)
        # the platform's canvas reached the timeline: 9:16 in 4K
        assert 'width="2160"' in job["result"]["content"]
        assert 'height="3840"' in job["result"]["content"]

    def test_build_platform_explicit_style_wins_with_a_note(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "platform": "tiktok", "style": "trailer", "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        notes = job["result"]["plan"]["notes"]
        assert any('style "trailer"' in n for n in notes)
        assert any('keeping your "trailer" style' in n for n in notes)
        # the platform still sets the canvas and the cap
        assert 'width="2160"' in job["result"]["content"]
        assert any("length capped at 60s" in n for n in notes)

    def test_build_unknown_platform_is_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"{server}/api/create/build",
                {"folder": self.DEMO, "platform": "vine"},
            )
        assert exc_info.value.code == 400
        assert "valid platforms" in json.loads(exc_info.value.read())["error"]

    def test_platform_round_trips_through_the_autosaved_draft(self, server):
        data = _post(
            f"{server}/api/create/build",
            {"folder": self.DEMO, "music": f"{self.DEMO}/song.wav",
             "platform": "reel", "format": "fcpxml"},
        )
        job = _wait_for_job(server, data["job"])
        assert job["state"] == "done"
        full = _get(f"{server}/api/drafts/autosave")
        settings = full["settings"]
        assert settings["platform"] == "reel"
        # the RESOLVED style/canvas are persisted, so a resumed draft
        # restores exactly the controls the build ran with
        assert settings["style"] == "short"
        assert settings["canvas"] == "vertical-uhd"


class TestPlatformDraftsRoundTrip:
    def test_hand_saved_draft_keeps_the_platform(self, server):
        record = {
            "name": "tiktok wip",
            "folder": "/footage/trip",
            "settings": {"style": "short", "canvas": "vertical-uhd",
                         "platform": "tiktok", "max_duration": 60},
            "plan_json": _shorts_plan_dict(),
        }
        stored = _post(f"{server}/api/drafts", record)
        full = _get(f"{server}/api/drafts/{stored['id']}")
        assert full["settings"]["platform"] == "tiktok"
        assert full["settings"]["style"] == "short"


class TestShortsPrefill:
    def test_vertical_short_gets_the_shorts_routing(self, server):
        data = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": _shorts_plan_dict(45.0), "name": "Alpine jump",
             "canvas": "vertical-uhd"},
        )
        assert data["title"] == "Alpine jump #Shorts"
        assert data["description"].splitlines()[0] == "#Shorts"
        # the story still follows
        assert "One ride, one jump." in data["description"]

    def test_vertical_hd_canvas_also_routes(self, server):
        data = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": _shorts_plan_dict(59.0), "canvas": "vertical"},
        )
        assert data["title"].endswith("#Shorts")
        assert data["description"].startswith("#Shorts")

    def test_wide_canvas_never_gets_the_tag(self, server):
        data = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": _shorts_plan_dict(45.0), "name": "Alpine jump",
             "canvas": "uhd"},
        )
        assert "#Shorts" not in data["title"]
        assert "#Shorts" not in data["description"]

    def test_long_vertical_video_never_gets_the_tag(self, server):
        data = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": _shorts_plan_dict(90.0), "name": "Alpine jump",
             "canvas": "vertical-uhd"},
        )
        assert "#Shorts" not in data["title"]
        assert "#Shorts" not in data["description"]

    def test_no_canvas_stays_untouched(self, server):
        """Callers that don't say what frame the video has (the Movie flow)
        never get Shorts routing — the old payload keeps the old reply."""
        data = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": _shorts_plan_dict(45.0), "name": "Alpine jump"},
        )
        assert data["title"] == "Alpine jump"
        assert "#Shorts" not in data["description"]

    def test_title_keeps_the_100_char_limit_with_the_tag(self, server):
        long_name = "R" * 140
        data = _post(
            f"{server}/api/youtube/prefill",
            {"plan_json": _shorts_plan_dict(30.0), "name": long_name,
             "canvas": "vertical-uhd"},
        )
        assert len(data["title"]) <= 100
        assert data["title"].endswith(" #Shorts")


# --- UI: the "What are you making?" chip row (static asserts) ------------------------


@pytest.mark.skipif(not _APP_HTML.exists(), reason="app.html not built yet")
class TestPlatformUi:
    def test_chip_row_sits_at_the_top_of_step_2(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert 'id="cre-platform-row"' in html
        assert "What are you making?" in html
        for chip in ("plat-youtube", "plat-short", "plat-reel", "plat-tiktok",
                     "plat-custom"):
            assert f'id="{chip}"' in html
        # the row comes BEFORE the shape/length/style controls of step 2
        assert html.index('id="cre-platform-row"') < html.index('id="cre-canvas-cards"')
        assert html.index('id="cre-step-2"') < html.index('id="cre-platform-row"')
        # Custom is the default chip
        assert 'id="plat-custom" aria-pressed="true"' in html

    def test_help_copy_per_chip(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert "9:16, max 60s, hook-first cutting, loop-friendly ending." in html
        assert "9:16, max 90s, hook-first cutting, loop-friendly ending." in html
        assert "16:9 in 4K — length and style stay yours." in html
        assert 'id="cre-platform-info"' in html

    def test_payload_and_drafts_carry_the_platform(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert 'body.platform = cre.platform' in html
        assert '"platform", "arrangement"].forEach' in html
        assert "s.platform" in html  # draft restore

    def test_short_style_card_exists(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert "Social Short" in html
        assert "Hook, punch, loop." in html

    def test_prefill_sends_the_built_canvas(self):
        html = _APP_HTML.read_text(encoding="utf-8")
        assert "body.canvas = builtCanvas" in html


# --- CLI: --platform ------------------------------------------------------------------


class TestCliPlatform:
    def test_parser_accepts_the_platforms(self):
        from monteur.cli import build_parser

        args = build_parser().parse_args(
            ["create", "clips", "song.mp3", "-o", "cut.fcpxml",
             "--platform", "tiktok"]
        )
        assert args.platform == "tiktok"
        args = build_parser().parse_args(
            ["create", "clips", "song.mp3", "-o", "cut.fcpxml"]
        )
        assert args.platform is None

    def test_parser_rejects_unknown_platform(self):
        from monteur.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["create", "clips", "song.mp3", "-o", "c.fcpxml",
                 "--platform", "vine"]
            )

    def test_create_resolves_the_platform(self, tmp_path, capsys):
        from monteur.cli import main

        demo = Path(str(_DEMO_FOOTAGE))
        if not demo.is_dir():
            pytest.skip("demo footage not generated in this environment")
        out = tmp_path / "cut.fcpxml"
        plan_path = tmp_path / "plan.json"
        main([
            "create", str(demo), str(demo / "song.wav"), "-o", str(out),
            "--platform", "tiktok", "--save-plan", str(plan_path),
        ])
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert any('style "short"' in n for n in data["notes"])
        assert any('platform "tiktok": length capped at 60s' in n
                   for n in data["notes"])
        content = out.read_text(encoding="utf-8")
        assert 'width="2160"' in content and 'height="3840"' in content
        printed = capsys.readouterr().out
        assert "Platform tiktok: canvas vertical-uhd, style short" in printed

    def test_create_explicit_style_beats_the_preset(self, tmp_path):
        from monteur.cli import main

        demo = Path(str(_DEMO_FOOTAGE))
        if not demo.is_dir():
            pytest.skip("demo footage not generated in this environment")
        out = tmp_path / "cut.fcpxml"
        plan_path = tmp_path / "plan.json"
        main([
            "create", str(demo), str(demo / "song.wav"), "-o", str(out),
            "--platform", "reel", "--style", "trailer",
            "--save-plan", str(plan_path),
        ])
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert any('style "trailer"' in n for n in data["notes"])
        assert any('keeping your "trailer" style' in n for n in data["notes"])
