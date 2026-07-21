"""Tests for magie-blueprint wave 1, items 1.4 / 1.5 / 1.7.

1.4 — bed ducking envelopes on the music bed (impact/braam dips, riser
shelves, mix-mode prominent original sound) composed onto the linear
gate chain, plus TRUE two-pass loudnorm in render_export.

1.5 — drop intelligence: BEST drop selection (musical weight), the
arc-squeeze floor, the "short" style's drop pin co-designed with
best_energy_window's 15% lead, and the loop seam (phrase-boundary window
end + exit→hook-entry motion handback). The low-band drop refinement
fixtures live in tests/test_music.py next to the drop detector.

1.7 — frame hygiene: sliver-slot elimination at every producing site,
typed fps-aware cut leads (cut_lead_for + dissolve lead 0), and
beat-quantized dips/dissolves/title fades through the ONE shared
quantize_finish helper (plan_pulse as the common tempo witness).
"""

from __future__ import annotations

import math
import re
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from _demo import DEMO
from monteur import preview
from monteur.montage import (
    MontageEntry,
    MontagePlan,
    SfxCue,
    _absorb_slivers,
    adjust_entry_boundary,
    cut_lead_for,
    plan_montage,
    plan_pulse,
    quantize_finish,
)
from monteur.music import MusicAnalysis, MusicSection, best_drop, drop_weight
from monteur.preview import (
    _bed_envelope_filters,
    _export_audio_graph,
    _parse_loudnorm_stats,
    _parse_mean_volume,
    _title_filter,
    ducking_windows,
    render_export,
)
from monteur.sift import ClipReport, Moment

try:
    import imageio_ffmpeg  # noqa: F401

    HAVE_FFMPEG = True
except ImportError:
    import shutil as _shutil

    HAVE_FFMPEG = bool(_shutil.which("ffmpeg"))

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
needs_demo = pytest.mark.skipif(
    not (DEMO / "clip_A.mp4").exists(), reason="demo footage not present"
)


# ------------------------------------------------------------------ helpers


def cue(kind: str, time: float, duration: float, file: str = "/sfx/x.wav") -> SfxCue:
    return SfxCue(
        time=time, duration=duration, kind=kind, query="q", note="n", file=file
    )


def _silent_wav(path: Path, seconds: float = 1.0, rate: int = 48000) -> str:
    """A digitally silent wav — a 'placed' accent that ducks the bed
    without adding its own energy, so the duck depth is measurable."""
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return str(path)


def _tone_wav(
    path: Path, seconds: float = 10.0, amp: float = 0.05, freq: float = 220.0,
    rate: int = 48000,
) -> str:
    """A steady sine 'song': constant level (the demo song's own beat
    dynamics swing ±16 dB per 0.1s and would drown any measurement), with
    generous headroom so the two-pass linear gain can reach −14 LUFS
    without hitting the −1 dBTP ceiling."""
    frames = int(seconds * rate)
    pcm = [
        int(32767 * amp * math.sin(2 * math.pi * freq * i / rate))
        for i in range(frames)
    ]
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(struct.pack("<" + "h" * frames, *pcm))
    return str(path)


def _rms_db(path: str, start: float, end: float) -> float:
    """Mean RMS level (dB) of the audio between start and end."""
    import subprocess

    from monteur.media import find_ffmpeg

    out = subprocess.run(
        [
            find_ffmpeg(), "-hide_banner", "-i", str(path),
            "-af", f"atrim={start}:{end},astats=metadata=1:reset=0",
            "-f", "null", "-",
        ],
        capture_output=True,
    ).stderr.decode("utf-8", "replace")
    match = re.search(r"RMS level dB:\s*(-?[\d.]+|-inf)", out)
    assert match, f"no astats RMS in ffmpeg output for {path}"
    return float("-inf") if match.group(1) == "-inf" else float(match.group(1))


def demo_plan(music: bool = True) -> MontagePlan:
    entries = [
        MontageEntry(
            clip_path=str(DEMO / "clip_A.mp4"),
            source_start=1.0, source_end=3.0,
            record_start=0.0, record_end=2.0, score=1.0,
        ),
        MontageEntry(
            clip_path=str(DEMO / "clip_C.mp4"),
            source_start=2.0, source_end=4.0,
            record_start=2.0, record_end=4.0, score=0.9,
        ),
        MontageEntry(
            clip_path=str(DEMO / "clip_D.mp4"),
            source_start=0.5, source_end=2.5,
            record_start=4.0, record_end=6.0, score=0.8,
        ),
    ]
    return MontagePlan(
        music_path=str(DEMO / "song.wav") if music else "",
        duration=6.0,
        music_start=8.0 if music else 0.0,
        song_duration=60.0 if music else 0.0,
        entries=entries,
    )


# ================================================================== 1.4 —
# ducking windows


class TestDuckingWindows:
    def test_impact_and_subdrop_get_the_minus_six_dip(self):
        windows = ducking_windows(
            [cue("impact", 2.0, 1.0), cue("sub-drop", 4.0, 0.4)], 10.0
        )
        assert len(windows) == 2
        (lo1, hi1, gain1, fade1), (lo2, hi2, gain2, fade2) = windows
        assert (lo1, hi1) == (2.0, 3.0)
        assert (lo2, hi2) == (4.0, pytest.approx(4.4))
        for gain in (gain1, gain2):
            assert 20 * math.log10(gain) == pytest.approx(-6.0, abs=0.01)
        assert fade1 == fade2 == pytest.approx(0.05)  # gap-gate micro-fade

    def test_accent_duck_is_capped_not_the_whole_ringout(self):
        # a 4s impact tail rings out, but the bed dips only for the hit
        (lo, hi, _gain, _fade), = ducking_windows([cue("impact", 1.0, 4.0)], 10.0)
        assert (lo, hi) == (1.0, pytest.approx(2.5))  # _DUCK_ACCENT_MAX_S

    def test_riser_gets_the_gentler_shelf_over_its_full_window(self):
        (lo, hi, gain, fade), = ducking_windows([cue("riser", 3.0, 2.0)], 10.0)
        assert (lo, hi) == (3.0, 5.0)  # the full build, ending on its hit
        assert 20 * math.log10(gain) == pytest.approx(-4.0, abs=0.01)
        assert fade == pytest.approx(0.25)  # a shelf eases, it does not dip

    def test_marker_cues_and_level_kinds_never_duck(self):
        cues = [
            cue("impact", 2.0, 1.0, file=""),  # marker only: makes no sound
            cue("whoosh", 3.0, 0.6),  # sits level with the bed
            cue("ambience", 0.0, 4.0),
        ]
        assert ducking_windows(cues, 10.0) == []

    def test_prominent_oton_windows_take_the_shelf(self):
        (lo, hi, gain, fade), = ducking_windows(
            [], 10.0, prominent=[(2.0, 5.0)]
        )
        assert (lo, hi) == (2.0, 5.0)
        assert 20 * math.log10(gain) == pytest.approx(-4.0, abs=0.01)
        assert fade == pytest.approx(0.25)

    def test_windows_clamped_and_sorted(self):
        windows = ducking_windows(
            [cue("impact", 9.5, 2.0), cue("riser", -1.0, 2.0)], 10.0
        )
        assert [w[:2] for w in windows] == [(0.0, 1.0), (9.5, 10.0)]


class TestBedEnvelopeComposition:
    def test_ducks_chain_after_the_gap_gates(self):
        filters = _bed_envelope_filters(
            [(4.0, 4.8)], [(2.0, 3.0, 0.501187, 0.05)]
        )
        assert filters[0] == "asetnsamples=n=256"
        assert "1-min(1,max(0," in filters[1]  # the gate: a full mute
        assert "1-0.498813*" in filters[2]  # the duck: a floor, same trapezoid
        assert all(f.endswith(":eval=frame") for f in filters[1:])

    def test_ducks_alone_still_get_asetnsamples(self):
        filters = _bed_envelope_filters([], [(2.0, 3.0, 0.5, 0.05)])
        assert filters[0] == "asetnsamples=n=256"
        assert len(filters) == 2

    def test_no_ducks_is_byte_identical_to_the_gates(self):
        from monteur.preview import _gap_gate_filters

        assert _bed_envelope_filters([(4.0, 4.8)], []) == _gap_gate_filters(
            [(4.0, 4.8)]
        )
        assert _bed_envelope_filters([], []) == []

    def test_export_graph_carries_duck_windows_on_the_song_only(self):
        graph = _export_audio_graph(
            "mix", "1:a", "2:a", [], 0.0, 0.0, 10.0,
            duck_windows=[(2.0, 3.0, 0.501187, 0.05)],
        )
        head = graph.split(";")[0]
        assert head.startswith("[1:a]asetnsamples=n=256,volume=volume=")
        assert "1-0.498813*" in head  # the −6 dB floor
        # the original bed's chain is untouched
        assert "asetnsamples" not in graph.split("[xo]")[0].split(";")[-1]

    def test_export_graph_without_ducks_is_byte_identical(self):
        plain = _export_audio_graph("music", "1:a", None, [], 0.5, 1.0, 6.0)
        defaulted = _export_audio_graph(
            "music", "1:a", None, [], 0.5, 1.0, 6.0, duck_windows=[]
        )
        assert plain == defaulted


# ------------------------------------------------------- two-pass loudnorm


LOUDNORM_STDERR = """
[Parsed_loudnorm_0 @ 0x55d]
{
\t"input_i" : "-23.61",
\t"input_tp" : "-11.83",
\t"input_lra" : "5.20",
\t"input_thresh" : "-33.95",
\t"output_i" : "-14.47",
\t"output_tp" : "-1.62",
\t"output_lra" : "4.90",
\t"output_thresh" : "-24.67",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.47"
}
"""


class TestTwoPassLoudnorm:
    def test_parse_loudnorm_stats(self):
        stats = _parse_loudnorm_stats(LOUDNORM_STDERR)
        assert stats == {
            "input_i": -23.61,
            "input_tp": -11.83,
            "input_lra": 5.2,
            "input_thresh": -33.95,
            "target_offset": 0.47,
        }

    def test_parse_rejects_silence_and_garbage(self):
        assert _parse_loudnorm_stats("no json here") is None
        assert (
            _parse_loudnorm_stats(LOUDNORM_STDERR.replace("-23.61", "-inf"))
            is None
        )
        assert (
            _parse_loudnorm_stats(LOUDNORM_STDERR.replace("-23.61", "7.0"))
            is None  # a positive integrated loudness is out of range
        )

    def test_parse_mean_volume(self):
        assert _parse_mean_volume("... mean_volume: -23.4 dB\n") == -23.4
        assert _parse_mean_volume("nothing") is None

    def test_graph_default_keeps_the_single_pass_string(self):
        graph = _export_audio_graph("music", "1:a", None, [], 0.0, 0.0, 6.0)
        assert "loudnorm=I=-14:TP=-1:LRA=11" in graph

    def test_graph_loudnorm_override_lands_at_the_tail(self):
        two = "loudnorm=I=-14:TP=-1:LRA=11:measured_I=-23.61:linear=true"
        graph = _export_audio_graph(
            "music", "1:a", None, [], 0.0, 0.0, 6.0, loudnorm=two
        )
        assert two in graph
        assert graph.index(two) < graph.index("aresample=48000")

    def _mock_export(self, monkeypatch, tmp_path, capture_behavior):
        """render_export with every ffmpeg call mocked; returns the final
        filter_complex string and the result dict."""
        cmds: list[list[str]] = []

        def fake_run(args, label):
            cmds.append(list(args))

        monkeypatch.setattr(preview, "_run_ffmpeg", fake_run)
        monkeypatch.setattr(preview, "_run_ffmpeg_capture", capture_behavior)
        monkeypatch.setattr(
            preview,
            "probe",
            lambda path: SimpleNamespace(
                width=640, height=360, duration=6.0, has_audio=True
            ),
        )
        plan = MontagePlan(
            music_path="/music/song.wav",
            duration=6.0,
            song_duration=60.0,
            entries=[
                MontageEntry("/a.mp4", 0.0, 3.0, 0.0, 3.0, 1.0),
                MontageEntry("/b.mp4", 0.0, 3.0, 3.0, 6.0, 1.0),
            ],
        )
        result = render_export(plan, str(tmp_path / "o.mp4"), audio="music")
        final = cmds[-1]
        return final[final.index("-filter_complex") + 1], result

    def test_measured_values_are_injected_into_pass_two(self, monkeypatch, tmp_path):
        measure_cmds: list[list[str]] = []

        def fake_capture(args, label):
            measure_cmds.append(list(args))
            return LOUDNORM_STDERR

        graph, result = self._mock_export(monkeypatch, tmp_path, fake_capture)
        # pass 1 ran audio-only into the null muxer with print_format=json
        assert len(measure_cmds) == 1
        m = measure_cmds[0]
        assert m[-2:] == ["-f", "null"] or m[-3:] == ["-f", "null", "-"]
        assert "print_format=json" in m[m.index("-filter_complex") + 1]
        # pass 2 carries the measured values, linear=true
        assert (
            "loudnorm=I=-14:TP=-1:LRA=11:measured_I=-23.61:measured_TP=-11.83"
            ":measured_LRA=5.20:measured_thresh=-33.95:offset=0.47:linear=true"
        ) in graph
        assert not any("loudness" in n for n in result["notes"])

    def test_no_true_peak_headroom_is_honestly_noted(self, monkeypatch, tmp_path):
        # crest-limited material: the needed +9.6 dB gain would push the
        # -2.0 dBTP peaks past -1 dBTP — loudnorm reverts to dynamic by
        # its own rule, and the export says so instead of hiding it.
        peaky = LOUDNORM_STDERR.replace('"input_tp" : "-11.83"', '"input_tp" : "-2.0"')
        graph, result = self._mock_export(
            monkeypatch, tmp_path, lambda args, label: peaky
        )
        assert "linear=true" in graph  # the two-pass values still ride along
        assert any(
            "no headroom" in n and "-14 LUFS" in n for n in result["notes"]
        )

    def test_failed_measurement_degrades_to_single_pass_with_note(
        self, monkeypatch, tmp_path
    ):
        from monteur.media import MonteurMediaError

        def broken_capture(args, label):
            raise MonteurMediaError("boom")

        graph, result = self._mock_export(monkeypatch, tmp_path, broken_capture)
        assert "loudnorm=I=-14:TP=-1:LRA=11," in graph  # the plain single pass
        assert "linear=true" not in graph
        assert any("single-pass" in n for n in result["notes"])


# --------------------------------------------------------- real renders (1.4)


def _tone_plan(tmp_path: Path) -> MontagePlan:
    plan = demo_plan()
    plan.music_path = _tone_wav(tmp_path / "tone.wav")
    plan.music_start = 0.0
    plan.song_duration = 10.0
    return plan


@needs_ffmpeg
@needs_demo
def test_export_ducks_the_bed_under_a_placed_impact(tmp_path):
    # A digitally SILENT placed impact over a steady-tone song: the accent
    # window ducks the music −6 dB while adding no energy of its own, so
    # the dip is measurable on the rendered file (two-pass loudnorm is a
    # single linear gain and preserves the relative depth).
    plan = _tone_plan(tmp_path)
    plan.sfx = [
        SfxCue(
            time=3.0, duration=1.0, kind="impact", query="impact", note="test",
            file=_silent_wav(tmp_path / "silent.wav", 1.2),
        )
    ]
    out = tmp_path / "duck.mp4"
    result = render_export(plan, str(out), size=(320, 180), audio="music")
    assert not any("single-pass" in n for n in result["notes"])
    inside = _rms_db(out, 3.15, 3.85)  # clear of the 50 ms edge fades
    outside = _rms_db(out, 1.5, 2.9)
    depth = outside - inside
    assert 4.5 <= depth <= 7.5, f"duck depth {depth:.1f} dB (want ~6)"


@needs_ffmpeg
@needs_demo
def test_export_two_pass_lands_within_one_lu_of_minus_fourteen(tmp_path):
    import subprocess

    from monteur.media import find_ffmpeg

    out = tmp_path / "loud.mp4"
    result = render_export(
        _tone_plan(tmp_path), str(out), size=(320, 180), audio="music"
    )
    assert not any("single-pass" in n for n in result["notes"])
    stderr = subprocess.run(
        [
            find_ffmpeg(), "-hide_banner", "-nostats", "-i", str(out),
            "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True,
    ).stderr.decode("utf-8", "replace")
    stats = _parse_loudnorm_stats(stderr)
    assert stats is not None
    assert abs(stats["input_i"] - (-14.0)) <= 1.0, stats["input_i"]


# ================================================================== 1.5 —
# best-drop selection


def weighted_music(
    drops: list[float],
    *,
    duration: float = 40.0,
    hot: tuple[float, float] | None = None,
) -> MusicAnalysis:
    """Beats 0.5s / downbeats 2s / phrases 8s; optionally one HOT stretch
    so the drop inside it outweighs the others."""
    sections = [MusicSection(0.0, duration, 0.4, "mid")]
    if hot is not None:
        lo, hi = hot
        sections = [
            MusicSection(0.0, lo, 0.3, "low"),
            MusicSection(lo, hi, 0.95, "high"),
            MusicSection(hi, duration, 0.4, "mid"),
        ]
    return MusicAnalysis(
        path="/music/track.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=sections,
        downbeats=[i * 2.0 for i in range(int(duration / 2))],
        phrases=[i * 8.0 for i in range(int(duration / 8) + 1)],
        drops=list(drops),
    )


def long_reports() -> list[ClipReport]:
    return [
        ClipReport(
            path="/footage/long.mp4",
            duration=200.0,
            moments=[Moment(i * 5.0, i * 5.0 + 3.0, 0.8) for i in range(40)],
        )
    ]


class TestDropWeight:
    def test_jump_plus_payoff(self):
        music = weighted_music([10.0, 24.0], hot=(24.0, 32.0))
        # drop at 24: jump 0.95-0.3=0.65 + payoff 0.95; drop at 10: flat 0.3
        assert drop_weight(music, 24.0) > drop_weight(music, 10.0)

    def test_sectionless_weighs_everything_zero_ties_earliest(self):
        music = MusicAnalysis(path="/m.wav", duration=40.0, tempo=120.0, drops=[10.0, 24.0])
        assert drop_weight(music, 10.0) == drop_weight(music, 24.0) == 0.0
        assert best_drop(music, [24.0, 10.0]) == 10.0  # the pre-1.5 choice


class TestBestDropSelection:
    def test_climax_pins_to_the_strongest_not_the_first(self):
        # Blueprint 1.5: two in-range drops; the SECOND sits on the hot
        # stretch and wins the climax pin — the old engine took the first.
        music = weighted_music([10.0, 24.0], hot=(24.0, 32.0))
        plan = plan_montage(long_reports(), music, style="travel", cut_lead=0.0)
        assert any(
            "climax aligned to drop at 24.0s (the strongest of 2)" in n
            for n in plan.notes
        )
        assert any(s == pytest.approx(24.0) and lab == "climax"
                   for s, _e, lab in plan.phases)

    def test_single_drop_note_is_unchanged(self):
        music = weighted_music([20.0])
        plan = plan_montage(long_reports(), music, style="travel", cut_lead=0.0)
        assert any("climax aligned to drop at 20.0s" in n for n in plan.notes)
        assert not any("strongest of" in n for n in plan.notes)

    def test_out_of_range_first_drop_no_longer_blocks_an_in_range_one(self):
        # The old engine looked only at drops[0]; an in-range second drop
        # now pins the climax (sanctioned 1.5 change).
        music = weighted_music([1.0, 24.0])
        plan = plan_montage(long_reports(), music, style="travel", cut_lead=0.0)
        assert any("climax aligned to drop at 24.0s" in n for n in plan.notes)

    def test_best_energy_window_prefers_the_heavier_drop(self):
        from monteur.music import best_energy_window

        music = weighted_music([10.0, 24.0], hot=(24.0, 32.0))
        start = best_energy_window(music, 10.0)
        assert start == pytest.approx(24.0 - 0.15 * 10.0)  # around the heavy one


class TestArcSqueezeFloor:
    def _beats_only(self, drops):
        # beats only (no downbeats/phrases): boundary snapping at 0.5s
        # granularity, fine enough that the floor's effect stays visible
        return MusicAnalysis(
            path="/music/track.wav",
            duration=40.0,
            tempo=120.0,
            beats=[i * 0.5 for i in range(80)],
            sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
            drops=list(drops),
        )

    def test_squeezed_side_keeps_minimum_shares(self):
        # travel arc: climax nominally at 20.0; a drop at 5.0 squeezes the
        # pre-side to 25% — proportional scaling would leave the opening
        # 1.5s (3.75%). The floor raises it to 5% of the montage (2.0s).
        plan = plan_montage(
            long_reports(), self._beats_only([5.0]), style="travel", cut_lead=0.0
        )
        assert any("squeezed phases keep at least 5%" in n for n in plan.notes)
        opening = next(p for p in plan.phases if p[2] == "opening")
        assert opening[1] - opening[0] >= 0.05 * 40.0 - 1e-6
        climax = next(p for p in plan.phases if p[2] == "climax")
        assert climax[0] == pytest.approx(5.0)  # the pin itself never moves

    def test_gentle_pin_never_triggers_the_floor(self):
        plan = plan_montage(
            long_reports(), self._beats_only([18.0]), style="travel", cut_lead=0.0
        )
        assert not any("squeezed phases" in n for n in plan.notes)


# ------------------------------------------------------ short pin + loop seam


def short_music() -> MusicAnalysis:
    """60s song; drop at 32s. A 30s request snaps to the 32s phrase
    (end_on_phrase), best_energy_window leads the drop by 15% (27.2) and
    the loop seam then seats the window END on the phrase at 56s —
    window [24, 56], drop at record 8.0."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=60.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(120)],
        sections=[MusicSection(0.0, 60.0, 0.6, "mid")],
        downbeats=[i * 2.0 for i in range(30)],
        phrases=[i * 8.0 for i in range(8)],
        drops=[32.0],
    )


def short_reports() -> list[ClipReport]:
    moments = [Moment(i * 6.0, i * 6.0 + 4.0, 0.6) for i in range(30)]
    moments[10] = Moment(60.0, 64.0, 0.5, highlight=0.95)  # the audible peak
    return [ClipReport(path="/footage/a.mp4", duration=200.0, moments=moments)]


class TestShortDropPin:
    def test_pin_lands_on_the_windows_drop_with_strongest_moment(self):
        plan = plan_montage(
            short_reports(), short_music(), style="short",
            max_duration=30.0, cut_lead=0.0,
        )
        # loop seam first: window 24.0..56.0, so the drop sits at record 8.0
        assert plan.duration == pytest.approx(32.0)
        assert plan.music_start == pytest.approx(24.0)
        assert any(
            "loop seam: the song window ends on the phrase boundary at 56.0s" in n
            for n in plan.notes
        )
        assert any(
            "short: cut pinned on the drop at 8.0s" in n for n in plan.notes
        )
        drop_entry = next(
            e for e in plan.entries if e.record_start == pytest.approx(8.0)
        )
        # the drop slot took the highest-highlight moment
        assert drop_entry.source_start >= 60.0 - 1e-6
        assert drop_entry.source_start < 64.0
        # the hit HOLDS: no cut inside ~2 beats after the pin
        assert drop_entry.record_end >= 8.0 + 2 * 0.5 - 1e-6

    def test_no_drop_no_pin_no_seam_note(self):
        music = short_music()
        music.drops = []
        plan = plan_montage(
            short_reports(), music, style="short", max_duration=30.0, cut_lead=0.0
        )
        assert not any("cut pinned on the drop" in n for n in plan.notes)

    def test_seam_never_costs_the_drop_pin(self):
        # A drop near the window edge: seating the end on a phrase would
        # push the drop out of the pinnable 5-95% range — the seam yields.
        music = short_music()
        music.drops = [30.0]
        music.phrases = [0.0, 31.0]  # the only candidate would strand the drop
        plan = plan_montage(
            short_reports(), music, style="short", max_duration=30.0, cut_lead=0.0
        )
        # falls back to the downbeat seam (31.0-length in range?) or none;
        # whatever it chose, the drop stayed pinnable and pinned:
        assert any("cut pinned on the drop" in n for n in plan.notes)


class TestLoopMotionHandback:
    def test_exit_to_hook_entry_bonus_flips_the_loop_pick(self):
        # Hook: fastest moment, enters panning right (4,0). Loop candidates
        # (no groups): A's motion energy is closest to the hook's but exits
        # AGAINST the hook's entry; B is slower but hands motion back.
        reports = [
            ClipReport(
                path="/footage/a.mp4",
                duration=60.0,
                moments=[
                    Moment(0.0, 6.0, 0.7,
                           entry_motion=(4.0, 0.0), exit_motion=(4.0, 0.0)),
                    Moment(10.0, 16.0, 0.6),  # static filler
                    Moment(20.0, 26.0, 0.6,  # A: close energy, opposite exit
                           entry_motion=(3.8, 0.0), exit_motion=(-3.8, 0.0)),
                    Moment(30.0, 36.0, 0.6,  # B: slower, exits INTO the hook
                           entry_motion=(2.0, 0.0), exit_motion=(2.0, 0.0)),
                ],
            )
        ]
        music = MusicAnalysis(
            path="/music/song.wav", duration=12.0, tempo=120.0,
            beats=[i * 0.5 for i in range(24)],
            sections=[MusicSection(0.0, 12.0, 0.6, "mid")],
        )
        plan = plan_montage(reports, music, style="short", cut_lead=0.0)
        assert plan.entries[-1].source_start == pytest.approx(30.0)
        assert any(
            "loop: last shot matches the hook's motion energy — and hands "
            "its motion back to the hook" in n
            for n in plan.notes
        )


# ================================================================== 1.7 —
# sliver elimination


class TestSliverElimination:
    def test_absorb_into_the_preceding_slot(self):
        # the sliver's LEFT boundary is removed: [0, 2.0] grows to [0, 2.1]
        assert _absorb_slivers([0.0, 2.0, 2.1, 4.0]) == [0.0, 2.1, 4.0]

    def test_protected_left_edge_pushes_into_the_following_slot(self):
        assert _absorb_slivers([0.0, 2.0, 2.1, 4.0], {2.0}) == [0.0, 2.0, 4.0]
        # the surviving boundary is the protected one
        assert 2.0 in _absorb_slivers([0.0, 2.0, 2.1, 4.0], {2.0})

    def test_both_edges_protected_keeps_the_sliver(self):
        cuts = _absorb_slivers([0.0, 3.8, 4.0], {3.8})  # 4.0 = montage end
        assert cuts == [0.0, 3.8, 4.0]

    def test_grid_remainder_sliver_is_absorbed(self):
        # last beat 0.1s before the end: the remainder slot would be 0.1s
        assert _absorb_slivers([0.0, 2.0, 3.9, 4.0]) == [0.0, 2.0, 4.0]

    def test_plan_montage_never_emits_a_sub_floor_slot(self):
        # A beat grid whose last beat sits 0.1s under the montage end.
        music = MusicAnalysis(
            path="/m.wav", duration=12.1, tempo=120.0,
            beats=[i * 0.5 for i in range(25)],  # ...11.5, 12.0; end 12.1
            sections=[MusicSection(0.0, 12.1, 0.9, "high")],
        )
        plan = plan_montage(long_reports(), music, cut_lead=0.0)
        for e in plan.entries:
            assert e.record_end - e.record_start >= 0.3 - 1e-6

    def test_auto_drop_cut_absorbs_its_sliver_neighbour(self):
        # A drop 0.2s after a grid beat: the beat cut yields, the drop stays.
        music = MusicAnalysis(
            path="/m.wav", duration=40.0, tempo=120.0,
            beats=[i * 0.5 for i in range(80)],
            sections=[MusicSection(0.0, 40.0, 0.9, "high")],
            drops=[20.2],
        )
        plan = plan_montage(
            long_reports(), music, style="auto", allow_repeats=True, cut_lead=0.0
        )
        assert any(e.record_start == pytest.approx(20.2) for e in plan.entries)
        for e in plan.entries:
            assert e.record_end - e.record_start >= 0.3 - 1e-6

    def test_no_music_pseudo_grid_has_no_slivers(self):
        # Regression: the 24s travel pseudo grid used to carry 0.15s
        # phase-boundary slivers (see the regenerated parity comment).
        reports = [
            ClipReport(
                path=f"/footage/{n}.mp4",
                duration=40.0,
                moments=[Moment(i * 8.0, i * 8.0 + 5.0, 0.8) for i in range(5)],
            )
            for n in ("a", "b")
        ]
        plan = plan_montage(reports, None, style="travel", max_duration=24.0)
        for e in plan.entries:
            assert e.record_end - e.record_start >= 0.3 - 1e-6


# --------------------------------------------------------- fps-aware leads


class TestFpsAwareLeads:
    def test_cut_lead_for_the_one_decision(self):
        assert cut_lead_for(None) == pytest.approx(0.04)
        assert cut_lead_for(25.0) == pytest.approx(0.04)  # 1 frame — unchanged
        assert cut_lead_for(50.0) == pytest.approx(0.02)
        assert cut_lead_for(30.0) == pytest.approx(1.0 / 30.0)
        # explicit request: whole frames, never below one (0 stays 0)
        assert cut_lead_for(25.0, 0.05) == pytest.approx(0.04)
        assert cut_lead_for(25.0, 0.01) == pytest.approx(0.04)
        assert cut_lead_for(25.0, 0.0) == 0.0
        assert cut_lead_for(None, 0.1) == pytest.approx(0.1)
        with pytest.raises(ValueError, match="fps must be positive"):
            cut_lead_for(0.0)

    def _music(self):
        return MusicAnalysis(
            path="/m.wav", duration=12.0, tempo=120.0,
            beats=[i * 0.5 for i in range(24)],
            sections=[MusicSection(0.0, 12.0, 0.9, "high")],
        )

    def test_fps_25_is_byte_identical_to_the_default(self):
        import json

        from monteur.montage import plan_to_dict

        base = plan_montage(long_reports(), self._music())
        at25 = plan_montage(long_reports(), self._music(), fps=25.0)
        assert json.dumps(plan_to_dict(base), sort_keys=True) == json.dumps(
            plan_to_dict(at25), sort_keys=True
        )

    def test_fps_50_leads_by_exactly_one_frame(self):
        plan = plan_montage(long_reports(), self._music(), fps=50.0)
        interior = [
            e.record_start for e in plan.entries[1:] if e.transition == 0.0
        ]
        assert interior  # hard cuts exist
        for start in interior:
            # each hard boundary sits exactly 0.02s before a beat
            assert (start + 0.02) % 0.5 == pytest.approx(0.0, abs=1e-6) or (
                (start + 0.02) % 0.5 == pytest.approx(0.5, abs=1e-6)
            )


class TestDissolveLeadZero:
    def test_dissolving_boundary_returns_to_the_grid(self):
        # travel opening dissolves; with the default lead the dissolve
        # boundaries sit ON the grid while hard cuts keep the lead.
        music = MusicAnalysis(
            path="/m.wav", duration=40.0, tempo=120.0,
            beats=[i * 0.5 for i in range(80)],
            sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
            downbeats=[i * 2.0 for i in range(20)],
            phrases=[i * 8.0 for i in range(5)],
        )
        reports = [
            ClipReport(
                path=f"/footage/v{i:02d}.mp4",
                duration=30.0,
                moments=[Moment(4.0, 6.0, 0.8)],
            )
            for i in range(20)
        ]
        plan = plan_montage(reports, music, style="travel")
        dissolving = [e for e in plan.entries if e.transition > 0]
        assert dissolving
        for e in dissolving:
            assert e.record_start % 0.5 == pytest.approx(0.0, abs=1e-6) or (
                e.record_start % 0.5 == pytest.approx(0.5, abs=1e-6)
            )
        hard = [
            e for e in plan.entries[1:]
            if e.transition == 0.0 and e.record_start < 32.0
        ]
        assert any(
            abs((e.record_start + 0.04) % 0.5) < 1e-6
            or abs((e.record_start + 0.04) % 0.5 - 0.5) < 1e-6
            for e in hard
        )


# ------------------------------------------------- beat-quantized finishes


class TestQuantizeFinish:
    def test_no_pulse_returns_the_classic_value(self):
        assert quantize_finish(0.4, 0.0) == 0.4
        assert quantize_finish(0.5, -1.0) == 0.5

    def test_nearest_half_beat(self):
        assert quantize_finish(0.4, 0.5) == pytest.approx(0.5)  # 2 x 0.25
        assert quantize_finish(0.5, 0.8) == pytest.approx(0.4)  # 1 x 0.4
        assert quantize_finish(0.3, 1.0) == pytest.approx(0.5)

    def test_ceiling_floors_to_the_grid_or_keeps_the_target(self):
        assert quantize_finish(0.5, 0.8, max_s=0.5) == pytest.approx(0.4)
        # not even half a beat fits under the cap: the raw target survives
        assert quantize_finish(0.2, 2.0, max_s=0.3) == pytest.approx(0.2)

    def test_plan_pulse_reads_the_downbeat_marks(self):
        plan = MontagePlan(music_path="/m.wav", duration=10.0)
        assert plan_pulse(plan) == 0.0
        plan.beat_marks = [0.0, 2.0, 4.0, 6.0]
        assert plan_pulse(plan) == pytest.approx(0.5)


def surgery_plan(beat_marks: list[float] | None = None) -> MontagePlan:
    plan = MontagePlan(
        music_path="/music/song.wav",
        duration=12.0,
        entries=[
            MontageEntry(f"/footage/{c}.mp4", 0.0, 4.0, i * 4.0, i * 4.0 + 4.0,
                         0.8, clip_duration=25.0)
            for i, c in enumerate("abc")
        ],
    )
    if beat_marks:
        plan.beat_marks = list(beat_marks)
    return plan


class TestQuantizedSurgeryContract:
    def test_beatless_surgery_keeps_the_classic_values(self):
        plan = surgery_plan()
        smashed = adjust_entry_boundary(plan, 1, "smash")
        assert smashed.dips == [(pytest.approx(3.6), pytest.approx(0.4))]
        dissolved = adjust_entry_boundary(plan, 1, "dissolve")
        assert dissolved.entries[1].transition == pytest.approx(0.5)

    def test_downbeat_marks_quantize_the_smash_and_dissolve(self):
        # pulse 0.5 (downbeats every 2s): the dip becomes exactly one beat
        plan = surgery_plan(beat_marks=[i * 2.0 for i in range(6)])
        smashed = adjust_entry_boundary(plan, 1, "smash")
        assert smashed.dips == [(pytest.approx(3.5), pytest.approx(0.5))]
        assert "0.5s title gap" in " ".join(smashed.notes)
        # pulse 0.8 (downbeats every 3.2s): the dissolve floors to 0.4
        plan = surgery_plan(beat_marks=[i * 3.2 for i in range(6)])
        dissolved = adjust_entry_boundary(plan, 1, "dissolve")
        assert dissolved.entries[1].transition == pytest.approx(0.4)

    def test_surgery_matches_the_planners_own_carving(self):
        # The adjust_entry_boundary CONTRACT: same plan pulse, same dip.
        music = weighted_music([])
        plan = plan_montage(
            long_reports(), music, style="trailer", cut_lead=0.0,
        )
        assert plan.dips  # the trailer dipped its act changes
        planned_len = plan.dips[0][1]
        # remove the dip, then re-smash it via surgery: identical length
        idx = next(
            i for i, e in enumerate(plan.entries)
            if abs(e.record_start - (plan.dips[0][0] + plan.dips[0][1])) < 1e-3
        )
        reverted = adjust_entry_boundary(plan, idx, "cut")
        resmashed = adjust_entry_boundary(reverted, idx, "smash")
        restored = next(
            (s, l) for s, l in resmashed.dips
            if abs(s - plan.dips[0][0]) < 1e-6
        )
        assert restored[1] == pytest.approx(planned_len)
        assert planned_len == pytest.approx(0.5)  # one beat at 120 bpm

    def test_planned_dips_quantize_only_with_downbeats(self):
        # dual fixture: the same trailer over beat-only music keeps 0.4s
        music = weighted_music([])
        music.downbeats = []
        music.phrases = []
        plan = plan_montage(long_reports(), music, style="trailer", cut_lead=0.0)
        assert plan.dips
        assert all(length == pytest.approx(0.4) for _s, length in plan.dips)


class TestQuantizedTitleFade:
    def test_title_fade_quantizes_through_the_shared_helper(self):
        # pulse 1.0 (60 bpm): the 0.3s target becomes half a beat (0.5s)
        assert quantize_finish(0.3, 1.0, max_s=0.5) == pytest.approx(0.5)

    def test_title_filter_uses_the_passed_fade(self):
        classic = _title_filter("/t.txt", "/f.ttf", 360, 2.0)
        quantized = _title_filter("/t.txt", "/f.ttf", 360, 2.0, 0.5)
        assert "t/0.300" in classic.replace("lt(t,", "t/")  # 0.3 ramp
        assert "0.500" in quantized
        # a dip too short for the bigger fade shows the text full-length
        gated = _title_filter("/t.txt", "/f.ttf", 360, 1.1, 0.5)
        assert "lt(t,1.100)" in gated
