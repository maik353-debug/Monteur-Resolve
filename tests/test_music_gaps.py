"""Tests for deliberate silence (magie-blueprint 1.2, "Bewusste Stille").

The owner's law: "Bewusste Stille ist super, versehentlich nie" —
deliberate silence yes, accidental silence never. Covers the gap
computation in monteur.montage._plan_music_gaps (dip gaps extended to
downbeats, the pre-drop beat, the short-style exemption, the dry-open
no-double-silence rule, the carrier guard, the continuous parity),
serialization tolerance, the beat-grid invariant on every surface
(timeline / FCPXML / Resolve append / preview / export), the surgery
pruning (adjust_entry_boundary, pin_entry, revise), and the outro
sibling monteur.music.outro_profile + decide_music_out.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from monteur.montage import (
    MontageEntry,
    MontagePlan,
    adjust_entry_boundary,
    decide_music_out,
    montage_to_timeline,
    music_bed_gaps,
    music_bed_segments,
    pin_entry,
    plan_from_dict,
    plan_montage,
    plan_to_dict,
)
from monteur.music import MusicAnalysis, MusicSection, outro_profile
from monteur.sift import ClipReport, Moment


def make_music(
    drops: list[float] | None = None,
    duration: float = 40.0,
    low: bool = True,
) -> MusicAnalysis:
    """A four-on-the-floor grid: beats every 0.5s, downbeats every 2s."""
    n = int(duration * 2)
    return MusicAnalysis(
        path="/music/song.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(n)],
        sections=[MusicSection(0.0, duration, 0.9, "high")],
        downbeats=[i * 2.0 for i in range(int(duration / 2))],
        phrases=[i * 8.0 for i in range(int(duration / 8) + 1)],
        drops=list(drops or []),
        low_energy=[0.5] * n if low else [],
    )


def make_reports() -> list[ClipReport]:
    return [
        ClipReport(
            path="/footage/long.mp4",
            duration=200.0,
            moments=[Moment(i * 5.0, i * 5.0 + 3.0, 0.8) for i in range(36)],
        )
    ]


def trailer_plan(sfx: bool = True, **kwargs) -> MontagePlan:
    """A trailer (smash-to-black dips) over hard music with a drop at 24s."""
    return plan_montage(
        make_reports(),
        make_music(drops=[24.0], low=False),  # moderate intro: music_in = 0
        style="trailer",
        cut_lead=0.0,
        sfx=sfx,
        **kwargs,
    )


# ------------------------------------------------------------- gap computation


class TestGapComputation:
    def test_dip_gaps_extend_to_the_following_downbeat(self):
        plan = trailer_plan()
        assert plan.music_gaps, "a trailer with the SFX layer plans gaps"
        dip_gaps = [
            (lo, hi)
            for lo, hi in plan.music_gaps
            if any(abs(d_start - lo) < 0.26 for d_start, _l in plan.dips)
        ]
        assert dip_gaps
        beat = 0.5
        for lo, hi in dip_gaps:
            dip_start, dip_len = next(
                (d, length) for d, length in plan.dips if abs(d - lo) < 0.26
            )
            dip_end = dip_start + dip_len
            # the gap covers the dip and ends at/after the black...
            assert lo == pytest.approx(dip_start)
            assert hi >= dip_end - 1e-6
            # ...capped at ~1 beat past the dip end
            assert hi <= dip_end + beat + 1e-6
            # and when it extends, it lands exactly ON a downbeat
            if hi > dip_end + 1e-6:
                assert any(abs(hi - d) < 1e-6 for d in [i * 2.0 for i in range(40)])
        assert any("under the act title" in n for n in plan.notes)

    def test_dip_gap_without_a_near_downbeat_ends_at_the_cut(self):
        # Downbeats every 8s: no downbeat within 1 beat of any dip end, so
        # every dip gap re-enters right at the cut out of the black.
        music = make_music(low=False)
        music.downbeats = [i * 8.0 for i in range(6)]
        plan = plan_montage(
            make_reports(), music, style="trailer", cut_lead=0.0, sfx=True
        )
        dip_ends = {round(d + length, 3) for d, length in plan.dips}
        for lo, hi in plan.music_gaps:
            if any(abs(d - lo) < 0.26 for d, _l in plan.dips):
                assert round(hi, 3) in dip_ends
        assert any("re-enters at the cut" in n for n in plan.notes)

    def test_pre_drop_gap_is_exactly_one_beat_ending_on_the_drop(self):
        plan = trailer_plan()
        beat = 0.5
        # Blueprint 1.7: the beat-quantized dips are now exactly one beat
        # long on this 120bpm grid too, so gap LENGTH alone no longer
        # identifies the pre-drop breath — exclude the dip gaps by their
        # dip start.
        pre_drop = [
            (lo, hi)
            for lo, hi in plan.music_gaps
            if abs(hi - lo - beat) < 1e-6
            and not any(abs(d_start - lo) < 0.26 for d_start, _l in plan.dips)
        ]
        assert len(pre_drop) == 1
        lo, hi = pre_drop[0]
        # the drop landed on a plan drop mark; re-entry is exactly ON it
        assert any(abs(hi - d) < 0.05 for d in plan.drop_marks)
        assert lo == pytest.approx(hi - beat)
        assert any("1 beat before the drop" in n for n in plan.notes)
        assert any("re-entry on the hit" in n for n in plan.notes)

    def test_short_style_never_plans_the_pre_drop_beat(self):
        plan = plan_montage(
            make_reports(),
            make_music(drops=[24.0], duration=50.0, low=False),
            style="short",
            max_duration=50.0,
            cut_lead=0.0,
            sfx=True,
        )
        assert plan.music_gaps == []

    def test_drop_inside_the_dry_open_gets_no_double_silence(self):
        # hard intro -> trailer delays the music (music_in > 0); a drop at
        # (or one beat after) the entry must NOT stack silence on silence.
        music = make_music(drops=[8.2], low=True)
        plan = plan_montage(
            make_reports(), music, style="trailer", cut_lead=0.0, sfx=False
        )
        assert plan.music_in > 0
        beat = 0.5
        for lo, hi in plan.music_gaps:
            assert lo >= plan.music_in + 1e-6
        # the 8.2s drop's pre-beat would start at 7.7 < music_in=8.0 -> none
        assert not any(abs(hi - 8.2) < 0.26 for _lo, hi in plan.music_gaps)

    def test_carrier_guard_no_cue_no_dip_gap(self):
        # The same trailer WITHOUT the SFX layer: dips exist, but nothing
        # carries their silence — the song plays through the black.
        plan = trailer_plan(sfx=False)
        assert plan.dips
        dip_gaps = [
            (lo, hi)
            for lo, hi in plan.music_gaps
            if any(abs(d - lo) < 0.26 for d, _l in plan.dips)
        ]
        assert dip_gaps == []
        # ...while the pre-drop beat still fires (the cut carries it)
        assert len(plan.music_gaps) == 1
        assert any("1 beat before the drop" in n for n in plan.notes)

    def test_a_marker_cue_counts_as_a_carrier(self):
        # sfx=True files nothing — the sub-drop cues are pure markers, and
        # markers COUNT: they record the intent, monteur.elements files them
        # later, and assign_elements only ever adds cues (never removes),
        # so a plan-time carrier stays a carrier.
        plan = trailer_plan(sfx=True)
        assert all(not cue.file for cue in plan.sfx)
        assert any(
            any(abs(d - lo) < 0.26 for d, _l in plan.dips)
            for lo, hi in plan.music_gaps
        )

    def test_continuous_flow_plans_zero_gaps_and_is_purely_subtractive(self):
        deliberate = trailer_plan()
        continuous = trailer_plan(music_flow="continuous")
        assert continuous.music_gaps == []
        # continuous = the deliberate plan minus the gaps and their notes
        d = plan_to_dict(deliberate)
        c = plan_to_dict(continuous)
        d.pop("music_gaps")
        d["notes"] = [n for n in d["notes"] if not n.startswith("silence:")]
        assert json.dumps(d, sort_keys=True) == json.dumps(c, sort_keys=True)

    def test_no_gap_scenario_is_byte_identical_across_flows(self):
        # No dips, no drops: deliberate and continuous plan the same bytes.
        kwargs = dict(style="travel", cut_lead=0.0)
        a = plan_montage(make_reports(), make_music(low=False), **kwargs)
        b = plan_montage(
            make_reports(), make_music(low=False),
            music_flow="continuous", **kwargs,
        )
        assert json.dumps(plan_to_dict(a), sort_keys=True) == json.dumps(
            plan_to_dict(b), sort_keys=True
        )

    def test_unknown_music_flow_raises(self):
        with pytest.raises(ValueError, match="unknown music_flow"):
            plan_montage(make_reports(), make_music(), music_flow="sometimes")

    def test_serialized_only_when_set_and_tolerant_on_load(self):
        plan = trailer_plan()
        data = json.loads(json.dumps(plan_to_dict(plan)))
        assert data["music_gaps"]
        restored = plan_from_dict(data)
        assert restored.music_gaps == pytest.approx(plan.music_gaps)
        # plans saved before the field existed load with no gaps
        del data["music_gaps"]
        old = plan_from_dict(data)
        assert old.music_gaps == []
        # gap-free plans never write the key
        continuous = trailer_plan(music_flow="continuous")
        assert "music_gaps" not in plan_to_dict(continuous)


# ------------------------------------------------------------ bed helpers


def gap_plan(
    gaps: list[tuple[float, float]] | None = None,
    music_in: float = 0.0,
    music_out: float = 0.0,
) -> MontagePlan:
    return MontagePlan(
        music_path="/music/song.wav",
        duration=10.0,
        music_start=20.0,
        song_duration=120.0,
        music_in=music_in,
        music_out=music_out,
        music_gaps=list(gaps if gaps is not None else [(4.0, 4.8)]),
        entries=[
            MontageEntry(
                clip_path="/media/a.mov", source_start=0.0, source_end=10.0,
                record_start=0.0, record_end=10.0, score=1.0,
            ),
        ],
    )


class TestBedHelpers:
    def test_gaps_clamped_merged_and_sorted(self):
        plan = gap_plan(
            gaps=[(6.0, 7.0), (6.5, 8.0), (-1.0, 0.5), (9.5, 99.0)],
            music_in=1.0,
        )
        assert music_bed_gaps(plan) == [(6.0, 8.0), (9.5, 10.0)]

    def test_segments_are_the_window_minus_the_gaps(self):
        plan = gap_plan(gaps=[(4.0, 4.8)], music_in=1.0, music_out=9.0)
        assert music_bed_segments(plan) == [(1.0, 4.0), (4.8, 9.0)]

    def test_no_gaps_yield_the_single_full_window(self):
        plan = gap_plan(gaps=[])
        assert music_bed_segments(plan) == [(0.0, 10.0)]

    def test_gap_covering_everything_falls_back_to_the_full_window(self):
        # Defensive: a hand-edited plan must never yield a silent-only bed.
        plan = gap_plan(gaps=[(0.0, 10.0)])
        assert music_bed_segments(plan) == [(0.0, 10.0)]


# ------------------------------------------------------- timeline (beat proof)


class TestTimelineSplit:
    def test_bed_splits_and_the_record_song_mapping_holds(self):
        plan = gap_plan(gaps=[(4.0, 4.8)])
        timeline = montage_to_timeline(plan, fps=25.0)
        beds = [c for c in timeline.clips if c.track == "A1" and c.kind == "audio"]
        assert [(c.record_in, c.record_out) for c in beds] == [(0, 100), (120, 250)]
        # THE invariant: record t plays song time music_start + t on every
        # piece — the post-gap clip's source SKIPS the gap's span too, so a
        # beat after the gap maps to the same song beat as without gaps.
        offset = 20.0 * 25  # music_start in frames
        for bed in beds:
            assert bed.source_in == offset + bed.record_in
            assert bed.source_out - bed.source_in == bed.record_out - bed.record_in
        # concretely: the re-entry at record 4.8s reads song 24.8s
        assert beds[1].source_in == 620

    def test_beat_after_the_gap_lands_on_the_same_song_frame(self):
        # A downbeat at record 6.0s must read song frame (20+6)*25 whether
        # or not a gap precedes it.
        gapped = montage_to_timeline(gap_plan(gaps=[(4.0, 4.8)]), fps=25.0)
        plain = montage_to_timeline(gap_plan(gaps=[]), fps=25.0)

        def song_frame_at(timeline, record_frame):
            for c in timeline.clips:
                if c.track != "A1" or c.kind != "audio":
                    continue
                if c.record_in <= record_frame < c.record_out:
                    return c.source_in + (record_frame - c.record_in)
            return None

        assert song_frame_at(gapped, 150) == song_frame_at(plain, 150) == 650

    def test_one_cut_to_marker_only(self):
        timeline = montage_to_timeline(gap_plan(gaps=[(4.0, 4.8)]), fps=25.0)
        markers = [m for m in timeline.markers if m.name.startswith("Cut to")]
        assert len(markers) == 1 and markers[0].frame == 0

    def test_window_and_gaps_compose(self):
        plan = gap_plan(gaps=[(4.0, 4.8)], music_in=2.0, music_out=9.0)
        timeline = montage_to_timeline(plan, fps=25.0)
        beds = [c for c in timeline.clips if c.track == "A1" and c.kind == "audio"]
        assert [(c.record_in, c.record_out) for c in beds] == [(50, 100), (120, 225)]
        for bed in beds:
            assert bed.source_in == 500 + bed.record_in


# ---------------------------------------------------------------------- fcpxml


class TestFcpxmlSplit:
    def test_split_bed_round_trips_on_lane_minus_one(self):
        from monteur.io.fcpxml import read_fcpxml, write_fcpxml

        timeline = montage_to_timeline(gap_plan(gaps=[(4.0, 4.8)]), fps=25.0)
        xml = write_fcpxml(timeline)
        # both bed pieces are connected clips on the music lane
        assert xml.count('audioRole="music"') == 2
        assert xml.count('lane="-1"') == 2
        back = read_fcpxml(xml)
        beds = sorted(
            (c for c in back.audio_clips() if c.track == "A1"),
            key=lambda c: c.record_in,
        )
        assert [(c.record_in, c.record_out) for c in beds] == [(0, 100), (120, 250)]
        assert [c.source_in for c in beds] == [500, 620]

    def test_gapless_plan_still_writes_one_continuous_bed(self):
        # The existing contract (one A1 clip spans the V1 dips) holds for
        # every plan without music_gaps.
        from monteur.io.fcpxml import write_fcpxml

        timeline = montage_to_timeline(gap_plan(gaps=[]), fps=25.0)
        xml = write_fcpxml(timeline)
        assert xml.count('audioRole="music"') == 1


# --------------------------------------------------------------- resolve append


class TestResolveAppend:
    def test_positioned_append_places_one_clip_per_audible_span(self):
        from test_resolve import build_append, make_bridge, standard_timeline

        plan = gap_plan(gaps=[(4.0, 4.8)])
        bridge, project = make_bridge([standard_timeline()])
        build_append(bridge, plan, fps=25.0)
        music = [
            info for info in project.media_pool.appended if info.get("mediaType") == 2
        ]
        assert len(music) == 2
        first, second = music
        assert first["recordFrame"] == 0
        assert first["startFrame"] == round(20.0 * 25)
        assert first["endFrame"] == first["startFrame"] + round(4.0 * 25) - 1
        # the post-gap span continues at music_start + 4.8s — beats aligned
        assert second["recordFrame"] == round(4.8 * 25)
        assert second["startFrame"] == round(24.8 * 25)
        assert second["endFrame"] == second["startFrame"] + round(5.2 * 25) - 1

    def test_gapless_fallback_appends_one_bed_and_warns(self):
        from test_resolve import build_append, make_bridge, standard_timeline

        plan = gap_plan(gaps=[(4.0, 4.8)])
        bridge, project = make_bridge([standard_timeline()])
        project.media_pool.reject_record_placement = True
        warnings: list[str] = []
        build_append(bridge, plan, fps=25.0, warnings=warnings)
        music = [
            info for info in project.media_pool.appended if info.get("mediaType") == 2
        ]
        # butting two spans together would shift every beat after the gap —
        # the fallback appends ONE continuous bed and says so honestly
        assert len(music) == 1
        assert music[0]["endFrame"] - music[0]["startFrame"] + 1 == round(10.0 * 25)
        assert any("deliberate music silence" in w for w in warnings)


# ------------------------------------------------------ preview / export graphs


class TestRenderGraphs:
    def test_gap_gate_filters_shape(self):
        from monteur.preview import _gap_gate_filters

        assert _gap_gate_filters([]) == []
        filters = _gap_gate_filters([(4.0, 4.8)])
        assert filters[0] == "asetnsamples=n=256"
        assert filters[1] == (
            "volume=volume='1-min(1,max(0,min((t-3.950)/0.050,"
            "(4.850-t)/0.050)))':eval=frame"
        )
        # micro-fades sit OUTSIDE the gap: full mute over [4.0, 4.8] exactly
        assert len(_gap_gate_filters([(1.0, 2.0), (3.0, 4.0)])) == 3

    def test_export_audio_graph_gates_the_song_only(self):
        from monteur import preview

        graph = preview._export_audio_graph(
            "music", "1:a", None, [], 0.0, 0.0, 10.0,
            music_gaps=[(4.0, 4.8)],
        )
        head = graph.split(";")[0]
        assert head.startswith("[1:a]asetnsamples=n=256,volume=volume=")
        assert head.endswith("[xmw]")
        assert "loudnorm=I=-14:TP=-1:LRA=11" in graph

    def test_export_audio_graph_without_gaps_is_byte_identical(self):
        from monteur import preview

        plain = preview._export_audio_graph("music", "1:a", None, [], 0.5, 1.0, 6.0)
        defaulted = preview._export_audio_graph(
            "music", "1:a", None, [], 0.5, 1.0, 6.0, music_gaps=[]
        )
        assert plain == defaulted
        assert "asetnsamples" not in plain

    def test_export_graph_gates_after_the_window_delay(self):
        from monteur import preview

        graph = preview._export_audio_graph(
            "music", "1:a", None, [], 0.0, 0.0, 10.0,
            music_in=2.0, music_len=7.0, music_gaps=[(4.0, 4.8)],
        )
        head = graph.split(";")[0]
        # order matters: trim/delay first (t becomes record time), THEN gate
        assert head.index("adelay") < head.index("asetnsamples")
        assert "afade=t=in:st=2.000" in head  # the entry fade is untouched

    def test_render_preview_music_command_carries_the_gate(self, monkeypatch, tmp_path):
        from monteur import preview

        cmds: list[list[str]] = []
        monkeypatch.setattr(
            preview, "_run_ffmpeg", lambda args, label: cmds.append(list(args))
        )
        monkeypatch.setattr(
            preview,
            "probe",
            lambda path: SimpleNamespace(
                width=1920, height=1080, duration=10.0, has_audio=True
            ),
        )
        preview.render_preview(gap_plan(), str(tmp_path / "p.mp4"), audio="music")
        final = cmds[-1]
        af = final[final.index("-af") + 1]
        assert "asetnsamples=n=256" in af
        assert "volume=volume='1-min(1,max(0,min((t-3.950)/0.050," in af

    def test_render_preview_mix_chain_gates_only_the_music(self, monkeypatch, tmp_path):
        from monteur import preview

        cmds: list[list[str]] = []
        monkeypatch.setattr(
            preview, "_run_ffmpeg", lambda args, label: cmds.append(list(args))
        )
        monkeypatch.setattr(
            preview,
            "probe",
            lambda path: SimpleNamespace(
                width=1920, height=1080, duration=10.0, has_audio=True
            ),
        )
        preview.render_preview(gap_plan(), str(tmp_path / "m.mp4"), audio="mix")
        final = cmds[-1]
        chain = final[final.index("-filter_complex") + 1]
        music_part, original_part = chain.split("[m];", 1)
        assert "asetnsamples" in music_part  # the song is gated...
        assert "asetnsamples" not in original_part  # ...the camera sound is not

    def test_render_preview_without_gaps_is_the_old_command(self, monkeypatch, tmp_path):
        from monteur import preview

        cmds: list[list[str]] = []
        monkeypatch.setattr(
            preview, "_run_ffmpeg", lambda args, label: cmds.append(list(args))
        )
        monkeypatch.setattr(
            preview,
            "probe",
            lambda path: SimpleNamespace(
                width=1920, height=1080, duration=10.0, has_audio=True
            ),
        )
        preview.render_preview(gap_plan(gaps=[]), str(tmp_path / "p.mp4"), audio="music")
        final = cmds[-1]
        assert "-af" not in final or "asetnsamples" not in final[final.index("-af") + 1]


# ----------------------------------------------------------- surgery pruning


class TestSurgeryPruning:
    def _plan_with_dip_gap(self) -> MontagePlan:
        plan = trailer_plan()
        assert any(
            any(abs(d - lo) < 0.26 for d, _l in plan.dips)
            for lo, hi in plan.music_gaps
        )
        return plan

    def test_removing_a_dip_removes_its_silence(self):
        plan = self._plan_with_dip_gap()
        dip_start, dip_len = plan.dips[0]
        gap_count = len(plan.music_gaps)
        slot = next(
            i
            for i, e in enumerate(plan.entries)
            if abs(e.record_start - (dip_start + dip_len)) < 0.05
        )
        # give the outgoing clip room to grow back (the removal precondition)
        plan.entries[slot - 1].clip_duration = 0.0
        adjusted = adjust_entry_boundary(plan, slot, "cut")
        assert len(adjusted.dips) == len(plan.dips) - 1
        assert len(adjusted.music_gaps) == gap_count - 1
        assert not any(abs(lo - dip_start) < 0.26 for lo, hi in adjusted.music_gaps)
        # the original plan is untouched
        assert len(plan.music_gaps) == gap_count

    def test_an_inserted_dip_gets_no_gap(self):
        # Surgery plans no carrier cue, so the song plays through the new
        # black until a full re-plan — silence never appears by accident.
        plan = self._plan_with_dip_gap()
        slot = next(
            i
            for i in range(1, len(plan.entries))
            if not any(
                abs(d + length - plan.entries[i].record_start) < 0.06
                for d, length in plan.dips
            )
            and plan.entries[i - 1].record_end - plan.entries[i - 1].record_start > 0.7
        )
        adjusted = adjust_entry_boundary(plan, slot, "smash")
        assert len(adjusted.dips) == len(plan.dips) + 1
        assert adjusted.music_gaps == plan.music_gaps

    def test_pin_entry_drops_gaps_it_overlaps(self):
        plan = self._plan_with_dip_gap()
        lo, hi = plan.music_gaps[0]
        pinned = MontageEntry(
            clip_path="/footage/long.mp4",
            source_start=150.0, source_end=150.0 + (hi + 0.5 - (lo - 0.5)),
            record_start=lo - 0.5, record_end=hi + 0.5, score=1.0,
        )
        pin_entry(plan, pinned)
        assert not any(
            g_lo < hi and g_hi > lo for g_lo, g_hi in plan.music_gaps
        )


# ----------------------------------------------------------------- revise loop


class TestReviseLoop:
    def test_replan_keeps_gaps_only_where_dips_survive(self):
        from monteur.revise import parse_revision, revise_plan

        plan = trailer_plan()
        revised = revise_plan(
            plan, make_reports(), make_music(drops=[24.0], low=False),
            parse_revision("ruhiger"),
            style="trailer", sfx=True, allow_repeats=True,
            max_duration=plan.duration,
        )
        # every surviving gap is still justified: a dip at its start or a
        # drop at its end — never an orphaned silence
        for lo, hi in revised.music_gaps:
            justified = any(
                abs(d - lo) <= 0.26 for d, _l in revised.dips
            ) or any(abs(d - hi) <= 0.26 for d in revised.drop_marks)
            assert justified, (lo, hi)

    def test_music_flow_passes_through_the_revision(self):
        from monteur.revise import parse_revision, revise_plan

        plan = trailer_plan(music_flow="continuous")
        revised = revise_plan(
            plan, make_reports(), make_music(drops=[24.0], low=False),
            parse_revision("ruhiger"),
            style="trailer", sfx=True, allow_repeats=True,
            max_duration=plan.duration, music_flow="continuous",
        )
        assert revised.music_gaps == []


# ------------------------------------------------- outro profile / music_out


def tail_music(tail_energy: float, tail_beats: bool, low: bool = True) -> MusicAnalysis:
    """40s song: a hot 30s body, then a 10s tail with the given character."""
    beats = [i * 0.5 for i in range(60)]
    if tail_beats:
        beats += [30.0 + i * 0.5 for i in range(20)]
    n = 80
    return MusicAnalysis(
        path="/music/tail.wav",
        duration=40.0,
        tempo=120.0,
        beats=beats,
        sections=[
            MusicSection(0.0, 30.0, 0.9, "high"),
            MusicSection(30.0, 40.0, tail_energy, "low" if tail_energy < 0.35 else "high"),
        ],
        downbeats=[i * 2.0 for i in range(20 if tail_beats else 15)],
        phrases=[i * 8.0 for i in range(5)],
        low_energy=([0.5] * 60 + [0.02] * 20) if low else [],
    )


class TestOutroProfile:
    def test_ambient_tail(self):
        profile = outro_profile(tail_music(0.05, tail_beats=False))
        assert profile["label"] == "ambient"
        assert profile["rel_energy"] < 0.3
        # the 12s window reaches 4 body beats at its head; the tail itself
        # is beatless — density stays far below the pulse ramp
        assert profile["onset_density"] < 0.5
        assert profile["hardness"] <= 0.35
        assert profile["end"] == pytest.approx(40.0)

    def test_hard_tail(self):
        music = make_music(low=True)
        profile = outro_profile(music)
        assert profile["label"] == "hard"
        assert profile["onset_density"] == pytest.approx(2.0)

    def test_without_low_band_evidence_never_hard(self):
        profile = outro_profile(make_music(low=False))
        assert profile["label"] == "moderate"
        assert profile["low_presence"] == 0.0

    def test_end_measures_the_cut_window(self):
        # measured at 30s the tail_music song still ends hot — the limp
        # tail only exists past the cut's own window
        music = tail_music(0.05, tail_beats=False)
        at_body = outro_profile(music, end=30.0)
        assert at_body["label"] in ("hard", "moderate")
        assert at_body["end"] == pytest.approx(30.0)


class TestDecideMusicOut:
    PHASES = [
        (0.0, 8.0, "opening"),
        (8.0, 16.0, "build"),
        (16.0, 30.0, "climax"),
        (30.0, 40.0, "outro"),
    ]

    def test_limp_ambient_tail_ends_the_music_early(self):
        out, note = decide_music_out(
            tail_music(0.05, tail_beats=False), "travel", self.PHASES, 40.0
        )
        assert out == pytest.approx(30.0)  # the limp run's start, on a downbeat
        assert "long ambient fade" in note

    def test_conservative_never_for_auto_or_short(self):
        music = tail_music(0.05, tail_beats=False)
        assert decide_music_out(music, "auto", [], 40.0) == (0.0, "")
        phases = [(0.0, 3.2, "hook"), (3.2, 32.0, "punch"), (32.0, 40.0, "loop")]
        assert decide_music_out(music, "short", phases, 40.0) == (0.0, "")

    def test_never_when_the_tail_still_pulses(self):
        # beats through the tail -> "moderate", not limp -> music plays on
        out, _ = decide_music_out(
            tail_music(0.2, tail_beats=True), "travel", self.PHASES, 40.0
        )
        assert out == 0.0

    def test_never_when_the_limp_run_starts_before_the_outro(self):
        music = tail_music(0.05, tail_beats=False)
        phases = [
            (0.0, 8.0, "opening"),
            (8.0, 16.0, "build"),
            (16.0, 34.0, "climax"),
            (34.0, 40.0, "outro"),
        ]
        # the low run starts at 30.0, inside the climax -> hands off
        assert decide_music_out(music, "travel", phases, 40.0)[0] == 0.0

    def test_never_cuts_more_than_thirty_percent(self):
        music = tail_music(0.05, tail_beats=False)
        phases = [
            (0.0, 4.0, "opening"),
            (4.0, 8.0, "build"),
            (8.0, 28.0, "climax"),
            (28.0, 44.0, "outro"),
        ]
        music.sections = [
            MusicSection(0.0, 29.0, 0.9, "high"),
            MusicSection(29.0, 44.0, 0.05, "low"),
        ]
        music.duration = 44.0
        # 29/44 = 66% < the 70% floor -> the music plays to the end
        assert decide_music_out(music, "travel", phases, 44.0)[0] == 0.0

    def test_plan_integration_notes_the_decision(self):
        music = tail_music(0.05, tail_beats=False)
        plan = plan_montage(
            make_reports(), music, style="travel", cut_lead=0.0
        )
        if plan.music_out > 0:  # the grid may shift phase edges slightly
            assert any("music ends at" in n for n in plan.notes)
            assert plan.music_out >= 0.7 * plan.duration - 1e-6


# ----------------------------------------------------- real render (RMS proof)

from _demo import DEMO  # noqa: E402

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


def _steady_tone(path: Path, seconds: float = 12.0) -> str:
    """A constant-level 330 Hz tone: silence in the render = a real gap."""
    rate = 22050
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(
            b"".join(
                struct.pack(
                    "<h", int(16000 * math.sin(2 * math.pi * 330 * i / rate))
                )
                for i in range(int(seconds * rate))
            )
        )
    return str(path)


def _render_gap_plan(tmp_path: Path, gaps: list[tuple[float, float]]) -> MontagePlan:
    return MontagePlan(
        music_path=_steady_tone(tmp_path / "tone.wav"),
        duration=6.0,
        music_start=0.0,
        song_duration=12.0,
        fade_in=0.0,
        fade_out=0.0,
        music_gaps=list(gaps),
        entries=[
            MontageEntry(
                clip_path=str(DEMO / "clip_A.mp4"),
                source_start=0.5, source_end=3.5,
                record_start=0.0, record_end=3.0, score=1.0,
            ),
            MontageEntry(
                clip_path=str(DEMO / "clip_C.mp4"),
                source_start=1.0, source_end=4.0,
                record_start=3.0, record_end=6.0, score=0.9,
            ),
        ],
    )


@needs_ffmpeg
@needs_demo
def test_export_renders_the_gap_silent_and_the_reentry_on_time(tmp_path):
    from test_preview import _rms_db

    from monteur.preview import render_export

    plan = _render_gap_plan(tmp_path, gaps=[(2.0, 2.5)])
    out = tmp_path / "gap.mp4"
    render_export(
        plan, str(out), size=(192, 108), audio="music", quality="medium"
    )
    # music before the gap and well after the re-entry: clearly audible
    assert _rms_db(str(out), 1.0, 1.9) > -40.0
    assert _rms_db(str(out), 2.7, 3.5) > -40.0
    # inside the gap (micro-fades live OUTSIDE it): essentially digital zero
    assert _rms_db(str(out), 2.05, 2.45) < -70.0
    # re-entry exactly at the gap end ±1 frame (40 ms at 25 fps): the ramp
    # is already audible right after 2.5s...
    assert _rms_db(str(out), 2.5, 2.56) > -55.0
    # ...and had NOT begun one frame before the gap end
    assert _rms_db(str(out), 2.42, 2.49) < -70.0


@needs_ffmpeg
@needs_demo
def test_preview_renders_the_gap_silent_too(tmp_path):
    from test_preview import _rms_db

    from monteur.preview import render_preview

    plan = _render_gap_plan(tmp_path, gaps=[(2.0, 2.5)])
    out = tmp_path / "gap_preview.mp4"
    render_preview(plan, str(out), width=192, audio="music")
    assert _rms_db(str(out), 1.0, 1.9) > -40.0
    assert _rms_db(str(out), 2.05, 2.45) < -70.0
    assert _rms_db(str(out), 2.7, 3.5) > -40.0
