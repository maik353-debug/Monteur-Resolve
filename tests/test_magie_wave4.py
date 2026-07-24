"""Blueprint Wave-4 acceptance tests (magie-blueprint 4.1/4.2/4.3).

The closed loop: Monteur watches its own cut and makes it better, and
learns from the user's corrections. The Abnahme (Welle 4) drives these:

* **Self-critique (4.1)** — the scorecard matches an INDEPENDENT
  measurement of the same plan (coincidence rate, silence honesty,
  slivers, loudness), computed a different way here.
* **Refine (4.2)** — a deliberately weak first plan is measurably improved
  by the loop (a HARD metric rises), deterministically reproducible, and
  the one-shot default stays byte-identical.
* **Learned preferences (4.3)** — a simulated correction signal shifts a
  later casting decision in the expected direction; an empty store is
  byte-identical; zero-repeat / sync / drop hold throughout.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from monteur import preview
from monteur.critique import (
    _COINCIDENCE_MIN_RATE,
    _LOUDNESS_TOL,
    critique,
)
from monteur.montage import (
    CastingBias,
    MontageEntry,
    MontagePlan,
    SfxCue,
    _fill,
    _PoolItem,
    plan_montage,
    plan_pulse,
    plan_to_dict,
)
from monteur.music import MusicAnalysis, MusicSection
from monteur.preview import _LOUDNORM_I, render_export
from monteur.refine import refine_plan
from monteur.sift import USABLE, ClipReport, ClipSegment, Moment

# Reuse the Wave-1 peak fixtures and the independent coincidence measure.
from test_peak import (
    CUT_LEAD, TOL, coincidence_rate, make_music, make_peaked_reports, peaked_moment,
)


# --------------------------------------------------------------------------- #
# 4.1 Self-critique: the scorecard matches an independent measurement          #
# --------------------------------------------------------------------------- #


def _independent_coincidence(plan: MontagePlan) -> tuple[int, int]:
    """Coincidence hits/total recomputed the critique's way, but standalone.

    Mirrors :func:`monteur.critique._coincidence` from the entries alone —
    the interior slots whose cast moment carries a peak, hit when the peak
    lands within ±0.25 s of the cut. An independent second implementation,
    so agreement proves critique reads the plan, not a shared bug.
    """
    hits = total = 0
    for e in plan.entries:
        if e.record_start <= 1e-6:
            continue
        peak = e.peak_source
        if peak is None or peak < 0:
            continue
        total += 1
        on_screen = e.source_start - 1e-6 <= peak <= e.source_end + 1e-6
        peak_record = e.record_start + (peak - e.source_start)
        if on_screen and abs(peak_record - e.record_start) <= TOL + 1e-6:
            hits += 1
    return hits, total


def test_critique_coincidence_matches_independent_measurement():
    reports = make_peaked_reports(60)
    plan = plan_montage(reports, make_music(40.0))
    card = critique(plan)
    metric = card.metrics["coincidence"]

    hits, total = _independent_coincidence(plan)
    assert total > 0
    assert metric.sample == total
    assert metric.value == pytest.approx(hits / total)
    # The blueprint's own headline check, computed yet another way (from the
    # reports' peaks, test_peak's measure) — the three agree.
    peaks = {r.path: r.moments[0].peak_time for r in reports}
    assert coincidence_rate(plan, peaks) >= _COINCIDENCE_MIN_RATE
    assert metric.passed


def test_critique_silence_honesty_names_the_uncarried_gap():
    # A carried gap (an impact under it) is honest; an uncarried one is an
    # accident the scorecard must flag by index.
    plan = MontagePlan(music_path="/m.wav", duration=10.0)
    plan.entries = [MontageEntry("/a.mp4", 0.0, 10.0, 0.0, 10.0, 1.0)]
    plan.music_gaps = [(2.0, 3.0), (6.0, 7.0)]
    plan.sfx = [SfxCue(time=2.5, duration=0.5, kind="impact", query="", note="")]
    card = critique(plan)
    silence = card.metrics["silence"]
    assert silence.sample == 2
    assert silence.value == pytest.approx(0.5)
    assert not silence.passed
    assert silence.culprits == [1]  # the second gap has no carrier

    # Give the second gap a drop to resolve on: now every silence is honest.
    plan.drop_marks = [7.0]
    good = critique(plan).metrics["silence"]
    assert good.passed and good.value == pytest.approx(1.0)


def test_critique_flags_slivers():
    plan = MontagePlan(music_path="/m.wav", duration=5.0)
    plan.entries = [
        MontageEntry("/a.mp4", 0.0, 2.0, 0.0, 2.0, 1.0),
        MontageEntry("/b.mp4", 0.0, 0.2, 2.0, 2.2, 1.0),  # a 0.2 s sliver
        MontageEntry("/c.mp4", 0.0, 2.8, 2.2, 5.0, 1.0),
    ]
    sliver = critique(plan).metrics["slivers"]
    assert sliver.value == 1.0
    assert sliver.culprits == [1]
    assert not sliver.passed


def test_critique_loudness_only_when_measured():
    plan = plan_montage(make_peaked_reports(20), make_music())
    assert "loudness" not in critique(plan).metrics  # no measurement, no metric

    good = critique(plan, measured_lufs=_LOUDNORM_I - 0.3).metrics["loudness"]
    assert good.passed and good.value == pytest.approx(_LOUDNORM_I - 0.3)
    bad = critique(plan, measured_lufs=_LOUDNORM_I - 4.0).metrics["loudness"]
    assert not bad.passed
    assert abs(bad.value - _LOUDNORM_I) > _LOUDNESS_TOL


# --------------------------------------------------------------------------- #
# 4.1 render_export exposes its measured integrated loudness                    #
# --------------------------------------------------------------------------- #

_LOUDNORM_STDERR = """
[Parsed_loudnorm_0 @ 0x0]
{
\t"input_i" : "-23.61",
\t"input_tp" : "-11.83",
\t"input_lra" : "5.20",
\t"input_thresh" : "-33.95",
\t"target_offset" : "0.47"
}
"""


def _mock_export(monkeypatch, tmp_path, capture):
    monkeypatch.setattr(preview, "_run_ffmpeg", lambda args, label, cancel=None: None)
    monkeypatch.setattr(preview, "_run_ffmpeg_capture", capture)
    monkeypatch.setattr(
        preview,
        "probe",
        lambda path: SimpleNamespace(width=640, height=360, duration=6.0, has_audio=True),
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
    return render_export(plan, str(tmp_path / "o.mp4"), audio="music")


def test_render_export_returns_measured_lufs(monkeypatch, tmp_path):
    result = _mock_export(monkeypatch, tmp_path, lambda args, label, cancel=None: _LOUDNORM_STDERR)
    assert result["measured_lufs"] == pytest.approx(-23.61)
    # The loop can now critique the real export's loudness without a re-pass.
    metric = critique(
        MontagePlan(music_path="/m.wav", duration=6.0),
        measured_lufs=result["measured_lufs"],
    ).metrics["loudness"]
    assert metric.value == pytest.approx(-23.61)


def test_render_export_omits_lufs_when_measurement_failed(monkeypatch, tmp_path):
    # A measurement pass that returns nothing parseable degrades to single
    # pass — and honestly OMITS the field (never a guessed number).
    result = _mock_export(monkeypatch, tmp_path, lambda args, label, cancel=None: "no json here")
    assert "measured_lufs" not in result
    assert any("single-pass" in n for n in result["notes"])


# --------------------------------------------------------------------------- #
# 4.2 Refine: a weak plan is measurably improved, deterministically             #
# --------------------------------------------------------------------------- #


def _weak_inputs():
    """4 s peaked moments whose slack is CAPPED at the moment end (a usable
    segment spanning exactly the moment), plus a slow starting pace: a long
    slot cannot aim the peak — not even into slack — so the first plan's
    coincidence is poor, and refine's DENSER pace (short slots that fit inside
    the moment) is what fixes it."""
    reports = [
        ClipReport(
            path=f"/footage/p{i:02d}.mp4",
            duration=30.0,
            moments=[peaked_moment(2.0, 6.0, 3.5)],
            segments=[ClipSegment(2.0, 6.0, USABLE, 0.9)],
        )
        for i in range(80)
    ]
    return reports, make_music(40.0)


def test_refine_improves_a_weak_plan():
    reports, music = _weak_inputs()
    base = plan_montage(reports, music, pace=5.0)
    base_rate = critique(base).metrics["coincidence"].value

    best, history = refine_plan(reports, music, pace=5.0, budget=4)
    best_rate = critique(best).metrics["coincidence"].value

    assert best_rate > base_rate  # the loop measurably improved coincidence
    assert critique(best).aggregate() >= critique(base).aggregate()
    assert best_rate >= _COINCIDENCE_MIN_RATE  # it actually cleared the bar
    assert len(history) >= 2
    # The loop is logged honestly, never silent.
    assert any(n.startswith("refine:") for n in best.notes)


def test_refine_is_deterministic():
    reports, music = _weak_inputs()
    a_plan, a_hist = refine_plan(reports, music, pace=5.0, budget=4)
    b_plan, b_hist = refine_plan(reports, music, pace=5.0, budget=4)
    # Same inputs -> same loop -> byte-identical winner and history.
    assert plan_to_dict(a_plan) == plan_to_dict(b_plan)
    assert [h["config"] for h in a_hist] == [h["config"] for h in b_hist]
    assert [h["aggregate"] for h in a_hist] == [h["aggregate"] for h in b_hist]


def test_refine_keeps_zero_repeat_and_sync():
    reports, music = _weak_inputs()
    best, _ = refine_plan(reports, music, pace=5.0, budget=4)
    # Zero-repeat: no two entries share source frames of the same clip.
    seen: dict[str, list[tuple[float, float]]] = {}
    for e in best.entries:
        for lo, hi in seen.get(e.clip_path, []):
            assert e.source_end <= lo + 1e-6 or e.source_start >= hi - 1e-6
        seen.setdefault(e.clip_path, []).append((e.source_start, e.source_end))
    # Sync held: the winning plan meets the coincidence acceptance bar.
    assert critique(best).metrics["coincidence"].passed


def test_refine_budget_zero_is_a_single_plan():
    reports, music = _weak_inputs()
    best, history = refine_plan(reports, music, pace=5.0, budget=0)
    assert len(history) == 1
    # Still byte-identical to the one-shot plan of the same inputs (modulo
    # the honest refine note the loop appends).
    one = plan_montage(reports, music, pace=5.0)
    best_d, one_d = plan_to_dict(best), plan_to_dict(one)
    best_d["notes"] = [n for n in best_d["notes"] if not n.startswith("refine:")]
    assert best_d == one_d


# --------------------------------------------------------------------------- #
# 4.3 Learned preferences: a correction shifts a later casting decision         #
# --------------------------------------------------------------------------- #


def _size_item(clip, size, entry=(0.0, 0.0), exit=(0.0, 0.0)):
    m = Moment(0.0, 6.0, 0.8, entry_motion=entry, exit_motion=exit, shot_size=size)
    return _PoolItem(clip, 40.0, m)


def _climax_cast(bias):
    """Cast a 3-slot arc whose middle (climax) slot is a near-tie between a
    'medium' and a 'close' candidate; return {record_start: shot_size}.

    The tie is engineered so the shot-grammar and order terms leave the
    'medium' candidate a hair ahead — only the learned close-at-climax
    preference (a 0.08 tie-breaker) tips the 'close' one over."""
    a = _size_item("/A.mp4", "medium")
    c = _size_item("/C.mp4", "close", entry=(1.0, math.sqrt(3)))  # cos=0.5 vs (1,0)
    d = _size_item("/D.mp4", "wide")
    prev = _size_item("/P.mp4", "close", exit=(1.0, 0.0))
    pool = [a, c, d, prev]
    for it in pool:
        it.consumed = 0.0
        it.uses = 0
        it.gaps = []
    slots = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    phases = [(0.0, 2.0, "build"), (2.0, 4.0, "climax"), (4.0, 6.0, "outro")]
    entries, _notes, _short = _fill(
        slots, [1, 2], pool, phases, None, frozenset(),
        semantic=False, slot_energies=None,
        pre_used={3}, preset={0: prev}, allow_repeats=False, casting_bias=bias,
    )
    return {round(e.record_start): e.shot_size for e in entries}


def test_preference_shifts_a_climax_casting_decision():
    # No preference: the climax slot casts the 'medium' shot.
    assert _climax_cast(None)[2] == "medium"
    # A learned "close-ups at the climax" preference tips the same slot to
    # the 'close' shot — the direction the user's corrections asked for.
    bias = CastingBias(shot_size=(("climax", "close", 0.08),))
    shifted = _climax_cast(bias)
    assert shifted[2] == "close"


def test_preference_never_repeats_material():
    # The shifted cast still draws every slot from a distinct clip.
    bias = CastingBias(shot_size=(("climax", "close", 0.08),))
    cast = _climax_cast(bias)
    assert len(cast) == 2  # both non-preset slots filled, no repeat


def test_neutral_casting_bias_is_byte_identical():
    reports = make_peaked_reports(40)
    music = make_music()
    base = plan_to_dict(plan_montage(reports, music))
    neutral = plan_to_dict(plan_montage(reports, music, casting_bias=CastingBias()))
    assert base == neutral
    none = plan_to_dict(plan_montage(reports, music, casting_bias=None))
    assert base == none


# --------------------------------------------------------------------------- #
# 4.3 The preferences store: conservative, isolated, inspectable, resettable    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def prefs(tmp_path, monkeypatch):
    """A fresh, isolated preferences store (never the real ~/.monteur)."""
    monkeypatch.setenv("MONTEUR_PREFERENCES_PATH", str(tmp_path / "prefs.json"))
    from monteur import preferences

    return preferences


def test_empty_store_folds_in_nothing(prefs):
    assert prefs.casting_bias() is None  # fresh user == today's behaviour


def test_one_signal_tips_nothing_but_a_repeat_activates(prefs):
    prefs.record_signal("shot_size", "climax", "close")
    assert prefs.casting_bias() is None  # a single correction is not a pattern
    prefs.record_signal("shot_size", "climax", "close")
    bias = prefs.casting_bias()
    assert bias is not None
    assert any(size == "close" and ctx == "climax" for ctx, size, _w in bias.shot_size)


def test_transition_signal_becomes_fewer_dissolves(prefs):
    prefs.record_signal("transition", "*", "cut")
    prefs.record_signal("transition", "*", "cut")
    bias = prefs.casting_bias()
    assert bias is not None and bias.fewer_dissolves


def test_inspect_and_reset(prefs):
    prefs.record_signal("shot_size", "climax", "close")
    prefs.record_signal("shot_size", "climax", "close")
    view = prefs.inspect()
    assert view["active"] == 1
    row = view["signals"][0]
    assert row["family"] == "shot_size" and row["direction"] == "close"
    assert row["count"] == 2 and row["active"] is True

    assert prefs.reset() is True
    assert prefs.inspect()["signals"] == []
    assert prefs.casting_bias() is None
    assert prefs.reset() is False  # nothing left to forget


def test_store_degrades_on_a_mangled_file(prefs, tmp_path):
    (tmp_path / "prefs.json").write_text("{ not json", encoding="utf-8")
    assert prefs.casting_bias() is None  # a broken store never takes Monteur down
    assert prefs.inspect()["signals"] == []


def test_fewer_dissolves_bias_reduces_dissolves():
    # A gentle-passage montage dissolves by default; the "fewer dissolves"
    # preference turns the weakest (plain) dissolves into hard cuts.
    reports = [
        ClipReport(path=f"/f/c{i:02d}.mp4", duration=30.0,
                   moments=[Moment(2.0, 6.0, 0.8)])
        for i in range(40)
    ]
    music = MusicAnalysis(
        path="/m.wav", duration=30.0, tempo=120.0,
        beats=[i * 0.5 for i in range(60)],
        sections=[MusicSection(0.0, 30.0, 0.2, "low")],  # a calm, dissolve-prone song
    )
    base = plan_montage(reports, music)
    fewer = plan_montage(reports, music, casting_bias=CastingBias(fewer_dissolves=True))
    base_diss = sum(1 for e in base.entries if e.transition > 1e-6)
    fewer_diss = sum(1 for e in fewer.entries if e.transition > 1e-6)
    assert base_diss > 0  # the default cut does dissolve in the calm song
    assert fewer_diss < base_diss  # the preference thinned them out


def test_lift_signal_becomes_a_negative_casting_bias(prefs):
    # Lifting a shot is the honest inverse of casting one: "not this size,
    # here". The weight is the same magnitude, the other way round.
    prefs.record_signal("avoid_shot_size", "climax", "wide")
    prefs.record_signal("avoid_shot_size", "climax", "wide")
    bias = prefs.casting_bias()
    assert bias is not None
    assert bias.size_bonus("climax", "wide") < 0
    assert bias.size_bonus("opening", "wide") == 0  # the lesson stays in context


def test_trim_signals_become_a_pace_notch(prefs):
    prefs.record_signal("shot_length", "*", "shorter")
    prefs.record_signal("shot_length", "*", "shorter")
    bias = prefs.casting_bias()
    assert bias is not None and bias.pace_notches == -1

    # Trimming both ways is not a preference — the notches cancel out.
    prefs.record_signal("shot_length", "*", "longer")
    prefs.record_signal("shot_length", "*", "longer")
    assert prefs.casting_bias() is None


def test_a_learned_pace_notch_actually_moves_the_cut():
    reports = make_peaked_reports(60)
    music = make_music()
    base = plan_montage(reports, music, style="trailer")
    slower = plan_montage(
        reports, music, style="trailer", casting_bias=CastingBias(pace_notches=1)
    )
    faster = plan_montage(
        reports, music, style="trailer", casting_bias=CastingBias(pace_notches=-1)
    )
    assert len(slower.entries) < len(base.entries) < len(faster.entries)
    assert any(n.startswith("learned pace:") for n in slower.notes)
    # An explicit pace is the editor speaking NOW; the learned notch must
    # not argue with it.
    pinned = plan_montage(
        reports, music, style="trailer", pace=1.5,
        casting_bias=CastingBias(pace_notches=1),
    )
    assert len(pinned.entries) == len(
        plan_montage(reports, music, style="trailer", pace=1.5).entries
    )
