"""Tests for magie-blueprint wave 2, items 2.1 / 2.2 / 2.3.

2.1 — secondary-drop forced cuts in ARC styles (trailer/paced/wedding/…):
the strongest secondary drops (by musical weight) each force a cut on the
drop, gated by a weight fraction of the climax and a min beat spacing,
with phase-hold clearing. "auto"/"short" keep their own every-drop path.

2.2 — O-Ton pops: the mirror of the 1.4 duck. Over a measured prominent
original-sound window the music ducks AND the original lifts
(oton_lift_windows, +3.5 dB), clamped honestly under −1 dBTP.

2.3 — J/L cuts: the original-sound edit decoupled from the picture cut at
chosen quiet transitions (jl_audio_edits) — a small fps-quantized
lead/lag, applied to the audio clip's record/source range in
montage_to_timeline (both exporters inherit it). Never on a drop, a
music-gap edge, a placed-SFX cut, or a climax boundary; opt-in, so
default timelines and hand-built plans stay byte-identical.
"""

from __future__ import annotations

import json

import pytest

from monteur.model import AUDIO, VIDEO
from monteur.montage import (
    montage_to_timeline,
    jl_audio_edits,
    plan_from_dict,
    plan_montage,
    plan_to_dict,
)
from monteur.music import MusicAnalysis, MusicSection
from monteur.preview import _LIFT_OTON_DB, oton_lift_windows
from monteur.sift import ClipReport, Moment


# --------------------------------------------------------------- helpers


def two_hot_music(drops: list[float], *, duration: float = 48.0) -> MusicAnalysis:
    """Two hot stretches (10–18 s and 26–36 s) so a drop inside EACH weighs
    heavily — the setup where a secondary drop can earn a forced cut."""
    sections = [
        MusicSection(0.0, 10.0, 0.30, "low"),
        MusicSection(10.0, 18.0, 0.95, "high"),
        MusicSection(18.0, 26.0, 0.35, "mid"),
        MusicSection(26.0, 36.0, 0.97, "high"),
        MusicSection(36.0, duration, 0.40, "mid"),
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


def flat_music(drops: list[float], *, duration: float = 40.0) -> MusicAnalysis:
    """One flat section — every drop weighs the same (0), so no secondary
    can out-weigh the climax pin."""
    return MusicAnalysis(
        path="/music/track.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(int(duration / 2))],
        phrases=[i * 8.0 for i in range(int(duration / 8) + 1)],
        drops=list(drops),
    )


def multi_reports(n: int = 30) -> list[ClipReport]:
    """DISTINCT clips (one 5 s moment each) — every slot boundary is a hard
    cut between different clips, the setup a J/L cut needs."""
    return [
        ClipReport(
            path=f"/footage/c{i:02d}.mp4",
            duration=30.0,
            moments=[Moment(2.0, 7.0, 0.8)],
        )
        for i in range(n)
    ]


def _video_audio_pairs(timeline):
    """Pair each V1 clip with the A-track clip emitted right after it
    (montage_to_timeline appends the entry's own-audio clip immediately
    after its video clip)."""
    pairs = []
    pending = None
    for clip in timeline.clips:
        if clip.kind == VIDEO:
            pending = clip
        elif clip.kind == AUDIO and pending is not None:
            pairs.append((pending, clip))
            pending = None
    return pairs


# ======================================================= 2.1 secondary drops


class TestSecondaryDrops:
    def test_strong_secondary_forces_a_cut_in_arc_style(self):
        # trailer arc, two heavy drops far apart: the climax pins to one, the
        # OTHER earns a secondary forced cut (blueprint 2.1).
        plan = plan_montage(
            multi_reports(), two_hot_music([12.0, 30.0]),
            style="trailer", cut_lead=0.0,
        )
        climax = [n for n in plan.notes if "climax aligned to drop" in n]
        secondary = [n for n in plan.notes if "secondary drop at" in n and "forces a cut" in n]
        assert climax, plan.notes
        assert secondary, plan.notes
        # both drops are registered, and a cut lands within 1 frame of each
        assert 12.0 in plan.drop_marks and 30.0 in plan.drop_marks
        cuts = [e.record_start for e in plan.entries]
        for drop in (12.0, 30.0):
            assert any(abs(c - drop) <= 0.04 + 1e-6 for c in cuts), (drop, cuts)

    def test_weak_secondary_is_ignored(self):
        # climax on the HOT drop (12 s), the second drop parked in a flat 0.35
        # section (22 s): it weighs far under half the climax's weight, so the
        # weight gate keeps it OUT (no forced secondary cut). Far enough apart
        # (20 beats) that spacing is not what excludes it.
        plan = plan_montage(
            multi_reports(), two_hot_music([12.0, 22.0]),
            style="trailer", cut_lead=0.0,
        )
        assert any("climax aligned to drop at 12.0s" in n for n in plan.notes), plan.notes
        assert not any("secondary drop at" in n for n in plan.notes), plan.notes

    def test_secondary_too_close_to_climax_is_skipped(self):
        # two heavy drops only ~6 beats (3 s) apart: the spacing gate
        # (_SECONDARY_DROP_MIN_BEATS) refuses the second.
        plan = plan_montage(
            multi_reports(), two_hot_music([12.0, 15.0]),
            style="trailer", cut_lead=0.0,
        )
        assert not any("secondary drop at" in n for n in plan.notes), plan.notes

    def test_auto_style_keeps_its_own_every_drop_path(self):
        # "auto" already forces every in-range drop via its own mechanism;
        # the arc-style secondary note must NOT appear (byte-identity of the
        # auto path is protected).
        plan = plan_montage(
            multi_reports(), two_hot_music([12.0, 30.0]),
            style="auto", allow_repeats=True, cut_lead=0.0,
        )
        assert not any("secondary drop at" in n for n in plan.notes), plan.notes


# ============================================================ 2.2 O-Ton pops


class TestOtonPops:
    def test_lift_boosts_a_quiet_prominent_moment_above_unity(self):
        # a quiet standout (−16 dB peak) has ample headroom → the full lift.
        lifts, notes = oton_lift_windows([(2.0, 6.0, -16.0)], 40.0)
        assert len(lifts) == 1
        lo, hi, gain, fade = lifts[0]
        assert (lo, hi) == (2.0, 6.0)
        assert gain == pytest.approx(10 ** (_LIFT_OTON_DB / 20.0))  # > 1, a boost
        assert gain > 1.0
        assert not any("clamped" in n for n in notes)

    def test_lift_is_clamped_under_the_true_peak_ceiling(self):
        # a HOT standout (0 dB peak) can't take the full +3.5 dB without
        # breaking −1 dBTP → the boost is clamped and a note says so.
        full = 10 ** (_LIFT_OTON_DB / 20.0)
        lifts, notes = oton_lift_windows([(2.0, 6.0, 0.0)], 40.0)
        assert len(lifts) == 1
        gain = lifts[0][2]
        assert 1.0 <= gain < full  # reduced, still not a duck
        assert any("clamped" in n for n in notes), notes

    def test_no_prominent_windows_means_no_lift(self):
        assert oton_lift_windows([], 40.0) == ([], [])


# ============================================================== 2.3 J/L cuts


class TestJLCuts:
    def _plan(self):
        return plan_montage(
            multi_reports(), two_hot_music([12.0, 30.0]),
            style="trailer", cut_lead=0.0,
        )

    def test_multiclip_transition_earns_a_jl_edit(self):
        edits, notes = jl_audio_edits(self._plan(), 25.0)
        assert edits, "expected at least one J/L edit across distinct-clip cuts"
        assert any("J/L cuts:" in n for n in notes), notes
        # every edit is a small sub-second lead/lag (not a wholesale shift)
        for lead, lag in edits.values():
            assert 0.0 <= lead <= 0.5 and 0.0 <= lag <= 0.5

    def test_jl_offsets_only_the_edited_audio_clip_in_the_timeline(self):
        plan = self._plan()
        edits, _ = jl_audio_edits(plan, 25.0)
        assert edits
        tl = montage_to_timeline(plan, 25.0, audio="original", jl_cuts=True)
        pairs = _video_audio_pairs(tl)
        offset = [
            i for i, (v, a) in enumerate(pairs)
            if (a.record_in, a.record_out) != (v.record_in, v.record_out)
        ]
        # exactly the edited entries have a shifted audio clip
        assert set(offset) == set(edits), (offset, sorted(edits))

    def test_default_timeline_has_no_jl_and_stays_byte_identical(self):
        plan = self._plan()
        tl = montage_to_timeline(plan, 25.0, audio="original")  # jl_cuts=False
        for v, a in _video_audio_pairs(tl):
            assert (a.record_in, a.record_out) == (v.record_in, v.record_out)
            assert (a.source_in, a.source_out) == (v.source_in, v.source_out)
        # the plan carries no J/L metadata → only-when-set serialization omits it
        blob = json.dumps(plan_to_dict(plan))
        assert "audio_lead" not in blob and "audio_lag" not in blob

    def test_jl_never_lands_on_a_drop_or_a_gap_edge(self):
        plan = self._plan()
        edits, _ = jl_audio_edits(plan, 25.0)
        gap_edges = [g for pair in plan.music_gaps for g in pair]
        for idx, (lead, lag) in edits.items():
            cut = plan.entries[idx].record_start
            out = plan.entries[idx].record_end
            for mark in plan.drop_marks:
                assert abs(cut - mark) > 0.05 and abs(out - mark) > 0.05
            for edge in gap_edges:
                assert abs(cut - edge) > 0.05 and abs(out - edge) > 0.05

    def test_hand_authored_offsets_are_respected_verbatim(self):
        plan = self._plan()
        # a human set an L-cut on some interior entry: the auto pass must not
        # recompute or double it.
        k = len(plan.entries) // 2
        plan.entries[k].audio_lag = 0.3
        edits, _ = jl_audio_edits(plan, 25.0)
        assert edits[k] == (0.0, 0.3)
        # it round-trips through the plan JSON (only-when-set, but SET here)
        blob = json.dumps(plan_to_dict(plan))
        assert "audio_lag" in blob
        back = plan_from_dict(json.loads(blob))
        assert back.entries[k].audio_lag == pytest.approx(0.3)
