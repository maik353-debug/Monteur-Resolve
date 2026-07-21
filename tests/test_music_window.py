"""Tests for the adaptive music window — the tool decides when music enters.

Covers monteur.music.intro_profile / detect_low_band, the scoring in
monteur.montage.decide_music_window (styles are WEIGHTS, never rules),
plan_montage's default decision + the music_window override, serialization
tolerance, the composer override, and every export surface honoring the
window (timeline, FCPXML, Resolve append, preview/export audio graphs).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from monteur.montage import (
    MontageEntry,
    MontagePlan,
    decide_music_window,
    montage_to_timeline,
    music_window_bounds,
    music_window_candidates,
    plan_from_dict,
    plan_montage,
    plan_to_dict,
)
from monteur.music import MusicAnalysis, MusicSection, intro_profile
from monteur.sift import ClipReport, Moment

STANDARD_PHASES = [
    (0.0, 8.0, "opening"),
    (8.0, 16.0, "build"),
    (16.0, 32.0, "climax"),
    (32.0, 40.0, "outro"),
]


def hard_music(low: bool = True) -> MusicAnalysis:
    """40s four-on-the-floor: full energy + kick evidence from bar one."""
    return MusicAnalysis(
        path="/music/hard.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.9, "high")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
        low_energy=[0.5] * 80 if low else [],
    )


def ambient_music() -> MusicAnalysis:
    """40s track whose first 12s are a quiet beatless pad."""
    return MusicAnalysis(
        path="/music/ambient.wav",
        duration=40.0,
        tempo=120.0,
        beats=[12.0 + i * 0.5 for i in range(56)],
        sections=[
            MusicSection(0.0, 12.0, 0.1, "low"),
            MusicSection(12.0, 40.0, 0.9, "high"),
        ],
        downbeats=[12.0 + i * 2.0 for i in range(14)],
        phrases=[12.0 + i * 8.0 for i in range(4)],
        low_energy=[0.02] * 24 + [0.5] * 56,
    )


def make_long_reports() -> list[ClipReport]:
    return [
        ClipReport(
            path="/footage/long.mp4",
            duration=120.0,
            moments=[Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(20)],
        )
    ]


# --------------------------------------------------------------- intro_profile


class TestIntroProfile:
    def test_hard_intro(self):
        profile = intro_profile(hard_music())
        assert profile["label"] == "hard"
        assert profile["rel_energy"] == pytest.approx(1.0)
        assert profile["onset_density"] == pytest.approx(2.0)
        assert profile["low_presence"] == pytest.approx(0.5)
        assert profile["hardness"] >= 0.65

    def test_ambient_intro(self):
        profile = intro_profile(ambient_music())
        assert profile["label"] == "ambient"
        assert profile["rel_energy"] < 0.2
        assert profile["onset_density"] == 0.0
        assert profile["hardness"] <= 0.35

    def test_without_low_band_evidence_never_hard(self):
        # Conservative by design: no spectral data (hand-built analyses,
        # saves from before the field) -> at most "moderate", so existing
        # plans keep music_in = 0 byte-identically.
        profile = intro_profile(hard_music(low=False))
        assert profile["label"] == "moderate"
        assert profile["low_presence"] == 0.0

    def test_start_measures_the_chosen_source_window(self):
        # Measured from 12s the "ambient" track opens hard-ish: the intro
        # is the CUT's window (plan.music_start), not the file's first bar.
        music = ambient_music()
        at_zero = intro_profile(music)
        at_body = intro_profile(music, start=12.0)
        assert at_zero["label"] == "ambient"
        assert at_body["label"] == "hard"
        assert at_body["start"] == pytest.approx(12.0)

    def test_body_less_window_is_neutral(self):
        music = hard_music()
        profile = intro_profile(music, window_s=100.0)
        assert profile["rel_energy"] == pytest.approx(1.0)
        assert profile["window_s"] == pytest.approx(40.0)


def test_detect_low_band_on_synthetic_audio():
    np = pytest.importorskip("numpy")
    from monteur.music import detect_low_band

    rate = 22050
    t = np.linspace(0.0, 2.0, 2 * rate, endpoint=False)
    low = np.sin(2 * np.pi * 60.0 * t)  # kick register
    high = np.sin(2 * np.pi * 1000.0 * t)  # nowhere near it
    curve_low = detect_low_band(low, rate)
    curve_high = detect_low_band(high, rate)
    assert len(curve_low) == 4  # 0.5s windows
    assert all(v > 0.9 for v in curve_low)
    assert all(v < 0.1 for v in curve_high)
    assert detect_low_band(np.zeros(rate), rate) == [0.0, 0.0]


# --------------------------------------------------------- decide_music_window


class TestDecideMusicWindow:
    def test_scoring_matrix_not_rigid(self):
        # (intro, style) -> delayed or not: styles WEIGH the intro, they
        # never decide alone.
        cases = {
            ("hard", "trailer"): True,
            ("hard", "music_video"): True,
            ("hard", "travel"): True,  # mismatch penalty: hard music, calm cut
            ("hard", "wedding"): True,
            ("ambient", "trailer"): False,  # ambient starts at 0 EVERYWHERE
            ("ambient", "travel"): False,
            ("ambient", "short"): False,
        }
        for (intro, style), delayed in cases.items():
            music = hard_music() if intro == "hard" else ambient_music()
            music_in, note = decide_music_window(music, style, STANDARD_PHASES)
            if delayed:
                assert music_in > 0, (intro, style)
                assert "music enters at" in note
                assert f"the song opens {intro}" in note
            else:
                assert (music_in, note) == (0.0, ""), (intro, style)

    def test_short_style_always_starts_at_zero(self):
        # 60 seconds has no room for a dry open — the table's one absolute.
        phases = [(0.0, 4.8, "hook"), (4.8, 48.0, "punch"), (48.0, 60.0, "loop")]
        music_in, note = decide_music_window(hard_music(), "short", phases)
        assert (music_in, note) == (0.0, "")

    def test_moderate_intro_stays_at_zero_everywhere(self):
        music = hard_music(low=False)  # moderate: no kick evidence
        for style in ("trailer", "music_video", "travel", "wedding", "auto"):
            assert decide_music_window(music, style, STANDARD_PHASES)[0] == 0.0

    def test_no_phases_means_no_candidates(self):
        assert decide_music_window(hard_music(), "auto", []) == (0.0, "")

    def test_entry_snaps_to_the_nearest_downbeat(self):
        phases = [
            (0.0, 8.7, "opening"),
            (8.7, 16.0, "build"),
            (16.0, 32.0, "climax"),
            (32.0, 40.0, "outro"),
        ]
        music_in, note = decide_music_window(hard_music(), "trailer", phases)
        assert music_in == pytest.approx(8.0)  # 8.7 -> downbeat at 8.0
        assert "snapped to downbeat" in note

    def test_candidates_respect_music_start(self):
        # Downbeats shift by music_start into record time before snapping.
        music = hard_music()
        cands = music_window_candidates(music, STANDARD_PHASES, music_start=1.0)
        times = [c["time"] for c in cands]
        assert times[0] == 0.0
        # downbeats at 2k in song time = 2k - 1 in record time -> 7.0
        assert any(t == pytest.approx(7.0) for t in times[1:])

    def test_candidate_never_past_half_the_cut(self):
        phases = [(0.0, 30.0, "opening"), (30.0, 40.0, "climax")]
        cands = music_window_candidates(hard_music(), phases)
        assert [c["time"] for c in cands] == [0.0]


# ------------------------------------------------------ plan_montage integration


class TestPlanMontageWindow:
    def test_hard_trailer_delays_the_music_by_default(self):
        plan = plan_montage(
            make_long_reports(), hard_music(), style="trailer", cut_lead=0.0
        )
        assert plan.music_in == pytest.approx(8.0)  # the build start
        assert plan.music_out == 0.0
        assert any("music enters at 8.0s" in n for n in plan.notes)
        data = plan_to_dict(plan)
        assert data["music_in"] == pytest.approx(8.0)
        assert "music_out" not in data

    def test_moderate_intro_keeps_full_parity(self):
        # No low-band evidence -> moderate -> music_in stays 0 and the plan
        # serializes byte-identically to one from before the window existed.
        plan = plan_montage(
            make_long_reports(), hard_music(low=False), style="trailer", cut_lead=0.0
        )
        assert plan.music_in == 0.0 and plan.music_out == 0.0
        data = plan_to_dict(plan)
        assert "music_in" not in data and "music_out" not in data
        assert not any("music enters" in n for n in plan.notes)

    def test_override_wins_and_snaps(self):
        plan = plan_montage(
            make_long_reports(),
            hard_music(low=False),
            style="travel",
            cut_lead=0.0,
            music_window=(4.4, 30.0),
        )
        assert plan.music_in == pytest.approx(4.0)  # snapped to the downbeat
        assert plan.music_out == pytest.approx(30.0)
        assert any(
            "music window" in n and "your setting" in n for n in plan.notes
        )

    def test_override_validation_errors(self):
        reports = make_long_reports()
        with pytest.raises(ValueError, match="needs music"):
            plan_montage(reports, None, max_duration=20.0, music_window=(2.0, 0.0))
        with pytest.raises(ValueError, match="must be"):
            plan_montage(reports, hard_music(), music_window="late")
        with pytest.raises(ValueError, match="not be negative"):
            plan_montage(reports, hard_music(), music_window=(-1.0, 0.0))
        with pytest.raises(ValueError, match="after music_in"):
            plan_montage(reports, hard_music(), music_window=(10.0, 5.0))
        with pytest.raises(ValueError, match="montage end"):
            plan_montage(reports, hard_music(), music_window=(500.0, 0.0))

    def test_window_round_trips_and_tolerates_absence(self):
        plan = plan_montage(
            make_long_reports(), hard_music(), style="trailer", cut_lead=0.0
        )
        data = json.loads(json.dumps(plan_to_dict(plan)))
        restored = plan_from_dict(data)
        assert restored.music_in == pytest.approx(plan.music_in)
        assert restored.music_out == plan.music_out
        # tolerance: plans saved before the fields existed load as 0/0
        del data["music_in"]
        old = plan_from_dict(data)
        assert old.music_in == 0.0 and old.music_out == 0.0

    def test_sfx_layer_anchors_a_riser_on_the_music_entry(self):
        plan = plan_montage(
            make_long_reports(), hard_music(), style="trailer", cut_lead=0.0,
            sfx=True,
        )
        assert plan.music_in > 0
        risers = [c for c in plan.sfx if c.kind == "riser"]
        ends = [round(c.time + c.duration, 3) for c in risers]
        assert pytest.approx(plan.music_in) in ends  # THE trailer moment
        anchored = next(
            c for c in risers
            if abs(c.time + c.duration - plan.music_in) < 1e-6
        )
        assert "music entry" in anchored.note


# ------------------------------------------------------------- export surfaces


def window_plan(music_in: float = 2.0, music_out: float = 5.0) -> MontagePlan:
    return MontagePlan(
        music_path="/music/song.wav",
        duration=6.0,
        music_start=8.0,
        song_duration=60.0,
        music_in=music_in,
        music_out=music_out,
        entries=[
            MontageEntry(
                clip_path="/media/a.mov", source_start=1.0, source_end=4.0,
                record_start=0.0, record_end=3.0, score=1.0,
            ),
            MontageEntry(
                clip_path="/media/b.mov", source_start=0.0, source_end=3.0,
                record_start=3.0, record_end=6.0, score=0.9,
            ),
        ],
    )


def test_music_window_bounds_clamps_defensively():
    assert music_window_bounds(window_plan(0.0, 0.0)) == (0.0, 6.0)
    assert music_window_bounds(window_plan(2.0, 5.0)) == (2.0, 5.0)
    assert music_window_bounds(window_plan(2.0, 99.0)) == (2.0, 6.0)
    assert music_window_bounds(window_plan(7.0, 0.0)) == (0.0, 6.0)  # broken -> full


def test_timeline_places_the_music_inside_the_window():
    timeline = montage_to_timeline(window_plan(), fps=25.0)
    music = next(c for c in timeline.clips if c.track == "A1" and c.kind == "audio")
    assert (music.record_in, music.record_out) == (50, 125)  # 2.0..5.0s
    # record<->song mapping unchanged: source starts at music_start + music_in
    assert music.source_in == 250  # (8.0 + 2.0) * 25
    assert music.source_out == 325
    marker = next(m for m in timeline.markers if m.name.startswith("Cut to"))
    assert marker.frame == 50


def test_timeline_without_window_is_unchanged():
    timeline = montage_to_timeline(window_plan(0.0, 0.0), fps=25.0)
    music = next(c for c in timeline.clips if c.track == "A1" and c.kind == "audio")
    assert (music.record_in, music.record_out, music.source_in) == (0, 150, 200)
    assert next(m for m in timeline.markers if m.name.startswith("Cut to")).frame == 0


def test_fcpxml_offsets_honor_the_window():
    from monteur.io.fcpxml import read_fcpxml, write_fcpxml

    timeline = montage_to_timeline(window_plan(), fps=25.0)
    back = read_fcpxml(write_fcpxml(timeline))
    music = next(c for c in back.clips if c.kind == "audio" and c.track == "A1")
    assert (music.record_in, music.record_out) == (50, 125)
    assert music.source_in == 250


def test_resolve_append_honors_the_window():
    from test_resolve import build_append, make_bridge, standard_timeline

    plan = window_plan(music_in=1.0, music_out=3.0)
    plan.music_start = 42.0
    bridge, project = make_bridge([standard_timeline()])
    build_append(bridge, plan, fps=24.0)
    music = project.media_pool.appended[-1]
    assert music["mediaType"] == 2
    assert music["startFrame"] == round((42.0 + 1.0) * 24)  # source: start + in
    assert music["endFrame"] == music["startFrame"] + round(2.0 * 24) - 1
    assert music["recordFrame"] == round(1.0 * 24)  # record: the entry point


def test_resolve_gapless_fallback_warns_about_the_entry():
    from test_resolve import build_append, make_bridge, standard_timeline

    plan = window_plan(music_in=1.0, music_out=0.0)
    bridge, project = make_bridge([standard_timeline()])
    project.media_pool.reject_record_placement = True
    warnings: list[str] = []
    build_append(bridge, plan, fps=24.0, warnings=warnings)
    assert any("music entry at 1.0s" in w for w in warnings)


def test_export_audio_graph_delays_and_fades_the_bed():
    from monteur import preview

    graph = preview._export_audio_graph(
        "music", "1:a", None, [], 0.0, 0.0, 6.0, music_in=2.0, music_len=3.0
    )
    head = graph.split(";")[0]
    assert head == (
        "[1:a]atrim=0:3.000,aresample=48000,adelay=2000|2000,"
        "afade=t=in:st=2.000:d=0.500[xmw]"
    )
    assert "[xmw]" in graph.split(";")[1]  # the windowed bed feeds the chain
    assert "loudnorm=I=-14:TP=-1:LRA=11" in graph  # the finish is untouched


def test_export_audio_graph_without_window_is_byte_identical():
    from monteur import preview

    plain = preview._export_audio_graph("music", "1:a", None, [], 0.5, 1.0, 6.0)
    defaulted = preview._export_audio_graph(
        "music", "1:a", None, [], 0.5, 1.0, 6.0, music_in=0.0, music_len=0.0
    )
    assert plain == defaulted
    assert "adelay" not in plain


def test_render_preview_command_delays_the_music(monkeypatch, tmp_path):
    from monteur import preview

    cmds: list[list[str]] = []
    monkeypatch.setattr(
        preview, "_run_ffmpeg", lambda args, label: cmds.append(list(args))
    )
    monkeypatch.setattr(
        preview,
        "probe",
        lambda path: SimpleNamespace(
            width=1920, height=1080, duration=6.0, has_audio=True
        ),
    )
    plan = window_plan()
    out = tmp_path / "p.mp4"
    preview.render_preview(plan, str(out), audio="music")
    final = cmds[-1]
    ss = final[final.index("-ss") + 1]
    assert ss == "10.000"  # music_start 8 + music_in 2
    af = final[final.index("-af") + 1]
    assert af.startswith("atrim=0:3.000,adelay=2000|2000,afade=t=in:st=2.000:d=0.500")


def test_render_preview_mix_command_windows_the_music_chain(monkeypatch, tmp_path):
    from monteur import preview

    cmds: list[list[str]] = []
    monkeypatch.setattr(
        preview, "_run_ffmpeg", lambda args, label: cmds.append(list(args))
    )
    monkeypatch.setattr(
        preview,
        "probe",
        lambda path: SimpleNamespace(
            width=1920, height=1080, duration=6.0, has_audio=True
        ),
    )
    preview.render_preview(window_plan(), str(tmp_path / "m.mp4"), audio="mix")
    final = cmds[-1]
    chain = final[final.index("-filter_complex") + 1]
    assert chain.startswith(
        "[1:a]atrim=0:3.000,adelay=2000|2000,afade=t=in:st=2.000:d=0.500,volume=1[m]"
    )


# ------------------------------------------------------------ composer override


class TestComposerOverride:
    def _compose(self, monkeypatch, reply_extra: dict, music=None):
        from monteur import ai
        from monteur.compose import compose_montage

        reply = {"story": "", "cast": [], "titles": [], "why": [], **reply_extra}

        def fake_complete(prompt, *, system="", json_schema=None, **kwargs):
            return json.dumps(reply)

        monkeypatch.setattr(ai, "complete", fake_complete)
        return compose_montage(
            make_long_reports(),
            music if music is not None else hard_music(),
            style="trailer",
            cut_lead=0.0,
        )

    def test_schema_offers_optional_music_in(self):
        from monteur.compose import COMPOSE_SCHEMA

        assert "music_in" in COMPOSE_SCHEMA["properties"]
        assert "music_in" not in COMPOSE_SCHEMA["required"]

    def test_valid_candidate_snaps_and_notes(self, monkeypatch):
        plan = self._compose(monkeypatch, {"music_in": 8.3})
        assert plan.music_in == pytest.approx(8.0)  # snapped to the candidate
        assert any("composer: music enters at 8.0s" in n for n in plan.notes)

    def test_composer_may_pull_the_music_back_to_zero(self, monkeypatch):
        plan = self._compose(monkeypatch, {"music_in": 0.0})
        assert plan.music_in == 0.0
        assert any(
            "composer: music enters with the first frame" in n for n in plan.notes
        )

    def test_invalid_value_keeps_the_engine_choice(self, monkeypatch):
        plan = self._compose(monkeypatch, {"music_in": 4.7})
        assert plan.music_in == pytest.approx(8.0)  # the engine's own decision
        assert any("not one of the candidates" in n for n in plan.notes)

    def test_dossier_carries_profile_and_candidates(self, monkeypatch):
        from monteur.compose import compose_context

        plan = plan_montage(
            make_long_reports(), hard_music(), style="trailer", cut_lead=0.0
        )
        context = compose_context(
            plan, make_long_reports(), hard_music(), style="trailer"
        )
        assert context["music_opening"]["label"] == "hard"
        assert set(context["music_opening"]) == {
            "label", "rel_energy", "onset_density", "low_presence",
        }
        assert context["music_in"] == pytest.approx(8.0)
        assert 0.0 in context["music_in_candidates"]
        assert pytest.approx(8.0) in context["music_in_candidates"]
