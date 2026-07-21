"""Tests for the arrangement — the editor's own scene order (montage/compose/CLI).

The Arrange step hands the CASTING ORDER to the editor while the engine
keeps the craft: arranged scenes claim slots 0..k-1 in the given order,
the grid/rhythm/finishing stay exactly the engine's, remaining slots
auto-fill (heuristic or composer — arranged slots are LOCKED for both),
"after"/"sfx" requests shape the boundaries, and a deterministic
consistency report lands in the notes under an "arrangement:" prefix.
"""

from __future__ import annotations

import json

import pytest

from monteur import ai
from monteur.compose import compose_montage
from monteur.montage import (
    montage_to_timeline,
    plan_montage,
    plan_to_dict,
)
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import ClipReport, Moment


def make_music(drops: list[float] | None = None) -> MusicAnalysis:
    """24 beats at 0.5s spacing (120 bpm) over 12s; low/mid/high sections."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=12.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(24)],
        sections=[
            MusicSection(0.0, 4.0, 0.2, "low"),
            MusicSection(4.0, 8.0, 0.5, "mid"),
            MusicSection(8.0, 12.0, 0.9, "high"),
        ],
        drops=drops or [],
    )


def make_reports() -> list[ClipReport]:
    a = ClipReport(
        path="/footage/a.mp4",
        duration=30.0,
        moments=[Moment(1.0, 6.0, 0.9), Moment(10.0, 12.0, 0.5), Moment(20.0, 23.0, 0.7)],
        usable_ratio=0.8,
    )
    b = ClipReport(
        path="/footage/b.mp4",
        duration=25.0,
        moments=[Moment(2.0, 5.0, 0.95), Moment(8.0, 10.0, 0.6), Moment(15.0, 19.0, 0.8)],
        usable_ratio=0.7,
    )
    return [a, b]


def make_big_reports() -> list[ClipReport]:
    """Plenty of distinct 5s moments, so no reuse muddies order checks."""
    reports = []
    for name in ("a", "b", "c"):
        moments = [Moment(i * 10.0, i * 10.0 + 5.0, 0.8 - i * 0.01) for i in range(8)]
        reports.append(
            ClipReport(path=f"/footage/{name}.mp4", duration=90.0, moments=moments)
        )
    return reports


def arrangement_notes(plan) -> list[str]:
    return [n for n in plan.notes if n.startswith("arrangement:")]


# --- parity (hard requirement) --------------------------------------------------


def test_arrangement_none_is_byte_identical():
    # The arrangement is an INPUT, not plan state: passing None must yield
    # a byte-identical serialized plan — for every style and both orders.
    for style in ("auto", "trailer"):
        for order in ("chronological", "best_first"):
            base = plan_montage(
                make_reports(), make_music(drops=[6.0]), order=order,
                style=style, sfx=True,
            )
            same = plan_montage(
                make_reports(), make_music(drops=[6.0]), order=order,
                style=style, sfx=True, arrangement=None,
            )
            assert json.dumps(plan_to_dict(base), sort_keys=True) == json.dumps(
                plan_to_dict(same), sort_keys=True
            )
            assert not arrangement_notes(base)


# --- order & casting -------------------------------------------------------------


def test_arranged_scenes_claim_slots_in_user_order():
    arrangement = [
        {"clip": "c.mp4", "start": 20.0},
        {"clip": "/footage/a.mp4", "start": 40.0},
        {"clip": "b.mp4", "start": 0.0},
    ]
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0, arrangement=arrangement
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    assert entries[0].clip_path == "/footage/c.mp4"
    assert entries[1].clip_path == "/footage/a.mp4"
    assert entries[2].clip_path == "/footage/b.mp4"
    # slot 0 starts at 0 and each arranged entry sits on the grid
    assert entries[0].record_start == 0.0
    # the sources come from the requested moments (snapped inside them)
    assert 20.0 <= entries[0].source_start < 25.0
    assert 40.0 <= entries[1].source_start < 45.0
    assert 0.0 <= entries[2].source_start < 5.0
    note = arrangement_notes(plan)[0]
    assert note.startswith("arrangement: 3 of")
    assert "follow your order" in note


def test_moment_trimmed_to_slot_with_note():
    # a.mp4's first moment is 5s; every slot in this song is <= 2s, so the
    # scene is trimmed onto the grid and the note says so honestly.
    plan = plan_montage(
        make_reports(), make_music(), cut_lead=0.0,
        arrangement=[{"clip": "a.mp4", "start": 1.0}],
    )
    first = sorted(plan.entries, key=lambda e: e.record_start)[0]
    assert first.clip_path == "/footage/a.mp4"
    assert first.source_end - first.source_start == pytest.approx(
        first.record_end - first.record_start
    )
    assert any(
        n.startswith("arrangement: scene 1 trimmed 5.0s ->") and "beat grid" in n
        for n in plan.notes
    )


def test_short_moment_pads_toward_clip_end_and_keeps_gap_note():
    # One tiny moment (0.3s) near its clip's end: padding runs out of file
    # and the existing gap-note behavior reports the short slot.
    tiny = ClipReport(
        path="/footage/tiny.mp4",
        duration=10.35,
        moments=[Moment(10.0, 10.3, 0.9)],
    )
    plan = plan_montage(
        [tiny] + make_big_reports(), make_music(), cut_lead=0.0,
        arrangement=[{"clip": "tiny.mp4", "start": 10.0}],
    )
    first = sorted(plan.entries, key=lambda e: e.record_start)[0]
    assert first.clip_path == "/footage/tiny.mp4"
    assert first.source_end == pytest.approx(10.35)  # padded to the file end
    assert any(n.startswith("gap at 0.00s") for n in plan.notes)


def test_fewer_scenes_than_slots_autofills_the_rest():
    plan_plain = plan_montage(make_big_reports(), make_music(), cut_lead=0.0)
    n_slots = len(plan_plain.entries)
    assert n_slots > 2
    arrangement = [
        {"clip": "b.mp4", "start": 30.0},
        {"clip": "a.mp4", "start": 70.0},
    ]
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0, arrangement=arrangement
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    assert len(entries) == n_slots  # the grid is the engine's, unchanged
    assert entries[0].clip_path == "/footage/b.mp4"
    assert entries[1].clip_path == "/footage/a.mp4"
    # the auto-fill never replays the arranged pieces first
    placed = {(e.clip_path, round(e.source_start, 2)) for e in entries[:2]}
    rest = {(e.clip_path, round(e.source_start, 2)) for e in entries[2:]}
    assert not (placed & rest)
    assert f"arrangement: 2 of {n_slots} slots follow your order" in "\n".join(
        plan.notes
    )


def test_more_scenes_than_slots_drops_excess_from_the_end_with_note():
    plan_plain = plan_montage(make_big_reports(), make_music(), cut_lead=0.0)
    n_slots = len(plan_plain.entries)
    arrangement = [
        {"clip": ("a.mp4", "b.mp4", "c.mp4")[i % 3], "start": (i // 3) * 10.0}
        for i in range(n_slots + 4)
    ]
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0, arrangement=arrangement
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    # the user's order survives up to the last slot; the excess is dropped
    for i in range(n_slots):
        assert entries[i].clip_path.endswith(arrangement[i]["clip"])
    assert any(
        "4 scenes did not fit" in n and "raise the length or drop scenes" in n
        for n in arrangement_notes(plan)
    )


# --- "after" boundary requests ----------------------------------------------------


def test_after_dissolve_sets_the_boundary_transition():
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0,
        arrangement=[
            {"clip": "a.mp4", "start": 0.0, "after": {"transition": "dissolve"}},
            {"clip": "b.mp4", "start": 0.0},
        ],
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    # dissolve INTO the next slot, the existing length rule
    expected = min(0.5, (entries[1].record_end - entries[1].record_start) / 2.0)
    assert entries[1].transition == pytest.approx(expected)
    assert any("boundaries" in n and "1 dissolve" in n for n in arrangement_notes(plan))


def test_after_cut_forces_a_hard_cut_even_in_dissolve_mode():
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0, transitions="dissolves",
        arrangement=[
            {"clip": "a.mp4", "start": 0.0, "after": {"transition": "cut"}},
            {"clip": "b.mp4", "start": 0.0},
        ],
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    assert entries[1].transition == 0.0  # forced hard even in dissolve-happy mode
    # everyone else keeps the dissolve mode's habit
    assert any(e.transition > 0 for e in entries[2:])


def test_after_smash_inserts_a_dip_with_a_title_slot_marker():
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0,
        arrangement=[
            {"clip": "a.mp4", "start": 0.0, "after": {"transition": "smash"}},
            {"clip": "b.mp4", "start": 0.0},
        ],
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    boundary = entries[1].record_start
    # the outgoing scene gave up 0.4s to a black gap ending on the boundary
    assert plan.dips == [(pytest.approx(boundary - 0.4), pytest.approx(0.4))]
    assert entries[0].record_end == pytest.approx(boundary - 0.4)
    timeline = montage_to_timeline(plan, fps=25.0)
    assert any(m.name == "Title slot" for m in timeline.markers)
    assert any("1 smash to black" in n for n in arrangement_notes(plan))


def test_after_accepts_bare_string_and_smash_skips_existing_dip():
    # "after" may be the bare string (CLI ergonomics), and a smash on a
    # boundary the style already dipped is not doubled.
    music = MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
    )
    base = plan_montage(make_big_reports(), music, style="trailer", cut_lead=0.0)
    assert base.dips  # the trailer smashes to black at act changes
    entries = sorted(base.entries, key=lambda e: e.record_start)
    # find the slot whose end the style dipped, and re-ask for the smash
    dip_start = base.dips[0][0]
    idx = next(
        i for i, e in enumerate(entries)
        if abs((e.record_end + 0.4) - (dip_start + 0.4)) < 1e-6
    )
    arrangement = [
        {"clip": "a.mp4", "start": (i % 8) * 10.0, "after": "smash" if i == idx else "cut"}
        for i in range(idx + 2)
    ]
    plan = plan_montage(
        make_big_reports(), music, style="trailer", cut_lead=0.0,
        arrangement=arrangement,
    )
    starts = [round(s, 3) for s, _ in plan.dips]
    assert len(starts) == len(set(starts))  # no doubled dip on one boundary


# --- "sfx" boundary cues -----------------------------------------------------------


def test_sfx_request_creates_a_cue_and_marker_without_the_sfx_layer():
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0,
        arrangement=[
            {"clip": "a.mp4", "start": 0.0, "sfx": "impact"},
            {"clip": "b.mp4", "start": 0.0},
        ],
    )
    impacts = [c for c in plan.sfx if c.kind == "impact"]
    assert len(impacts) == 1
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    assert impacts[0].time == pytest.approx(entries[1].record_start)
    assert impacts[0].query == "cinematic impact hit"
    assert "after scene 1" in impacts[0].note
    timeline = montage_to_timeline(plan, fps=25.0)
    assert any(m.name == "SFX: impact" for m in timeline.markers)
    assert any("1 sound cue at your boundaries" in n for n in arrangement_notes(plan))


def test_sfx_riser_ends_on_the_boundary_and_whoosh_centers_on_it():
    plan = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0,
        arrangement=[
            {"clip": "a.mp4", "start": 0.0, "sfx": "riser"},
            {"clip": "b.mp4", "start": 0.0, "sfx": "whoosh"},
            {"clip": "c.mp4", "start": 0.0},
        ],
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    riser = next(c for c in plan.sfx if c.kind == "riser")
    assert riser.time + riser.duration == pytest.approx(entries[1].record_start)
    whoosh = next(c for c in plan.sfx if c.kind == "whoosh")
    assert whoosh.time + whoosh.duration / 2.0 == pytest.approx(
        entries[2].record_start
    )


def test_sfx_request_is_not_doubled_when_the_layer_already_covers_it():
    # A drop-forced cut gets an impact from the SFX layer; an arranged
    # impact on the same boundary must not double it.
    plan_plain = plan_montage(
        make_big_reports(), make_music(drops=[6.0]), cut_lead=0.0, sfx=True
    )
    entries = sorted(plan_plain.entries, key=lambda e: e.record_start)
    drop_idx = next(
        i for i, e in enumerate(entries) if abs(e.record_start - 6.0) < 1e-6
    )
    arrangement = [
        {"clip": ("a.mp4", "b.mp4", "c.mp4")[i % 3], "start": (i // 3) * 10.0}
        for i in range(drop_idx)
    ]
    arrangement[-1]["sfx"] = "impact"  # the boundary INTO the drop slot
    plan = plan_montage(
        make_big_reports(), make_music(drops=[6.0]), cut_lead=0.0, sfx=True,
        arrangement=arrangement,
    )
    on_drop = [c for c in plan.sfx if c.kind == "impact" and abs(c.time - 6.0) < 0.5]
    assert len(on_drop) == 1


# --- pacing flags -------------------------------------------------------------------


def test_calm_scene_on_the_drop_is_flagged():
    calm = ClipReport(
        path="/footage/calm.mp4",
        duration=60.0,
        moments=[Moment(i * 10.0, i * 10.0 + 5.0, 0.8) for i in range(6)],
    )
    lively = ClipReport(
        path="/footage/lively.mp4",
        duration=60.0,
        moments=[
            Moment(
                i * 10.0, i * 10.0 + 5.0, 0.7,
                entry_motion=(4.0, 0.0), exit_motion=(4.0, 0.0), highlight=0.8,
            )
            for i in range(6)
        ],
    )
    plan_plain = plan_montage([calm, lively], make_music(drops=[6.0]), cut_lead=0.0)
    entries = sorted(plan_plain.entries, key=lambda e: e.record_start)
    drop_idx = next(
        i for i, e in enumerate(entries) if abs(e.record_start - 6.0) < 1e-6
    )
    arrangement = [
        {"clip": "lively.mp4", "start": (i % 6) * 10.0} for i in range(drop_idx + 1)
    ]
    arrangement[drop_idx] = {"clip": "calm.mp4", "start": 0.0}
    plan = plan_montage(
        [calm, lively], make_music(drops=[6.0]), cut_lead=0.0,
        arrangement=arrangement,
    )
    assert any(
        f"scene {drop_idx + 1} is a calm moment on the drop" in n
        for n in arrangement_notes(plan)
    )
    # ...and no flag when the calm scene sits elsewhere
    arrangement[drop_idx] = {"clip": "lively.mp4", "start": 0.0}
    arrangement[0] = {"clip": "calm.mp4", "start": 0.0}
    plan2 = plan_montage(
        [calm, lively], make_music(drops=[6.0]), cut_lead=0.0,
        arrangement=arrangement,
    )
    assert not any("calm moment on the drop" in n for n in arrangement_notes(plan2))


def test_two_takes_of_the_same_scene_back_to_back_are_flagged():
    reports = make_big_reports()
    reports[0].moments[0].group = "summit"
    reports[1].moments[0].group = "summit"
    plan = plan_montage(
        reports, make_music(), cut_lead=0.0,
        arrangement=[
            {"clip": "a.mp4", "start": 0.0},
            {"clip": "b.mp4", "start": 0.0},
            {"clip": "c.mp4", "start": 0.0},
        ],
    )
    assert any(
        "scenes 1 and 2 are takes of the same scene back to back" in n
        for n in arrangement_notes(plan)
    )


# --- validation ---------------------------------------------------------------------


def test_unknown_clip_raises_naming_it():
    with pytest.raises(ValueError, match="no clip named 'ghost.mp4'"):
        plan_montage(
            make_reports(), make_music(),
            arrangement=[{"clip": "ghost.mp4", "start": 0.0}],
        )


def test_all_unknown_clips_are_named_at_once():
    with pytest.raises(ValueError) as err:
        plan_montage(
            make_reports(), make_music(),
            arrangement=[
                {"clip": "ghost.mp4", "start": 0.0},
                {"clip": "phantom.mp4", "start": 0.0},
            ],
        )
    assert "'ghost.mp4'" in str(err.value) and "'phantom.mp4'" in str(err.value)


def test_malformed_items_raise_clear_errors():
    with pytest.raises(ValueError, match="must be a list"):
        plan_montage(make_reports(), make_music(), arrangement={"clip": "a.mp4"})
    with pytest.raises(ValueError, match="scene 1 must be an object"):
        plan_montage(make_reports(), make_music(), arrangement=["a.mp4"])
    with pytest.raises(ValueError, match="scene 1 is missing 'clip'"):
        plan_montage(make_reports(), make_music(), arrangement=[{"start": 1.0}])
    with pytest.raises(ValueError, match="unknown transition 'wipe'"):
        plan_montage(
            make_reports(), make_music(),
            arrangement=[{"clip": "a.mp4", "start": 0.0, "after": "wipe"}],
        )
    with pytest.raises(ValueError, match="unknown sfx 'boom'"):
        plan_montage(
            make_reports(), make_music(),
            arrangement=[{"clip": "a.mp4", "start": 0.0, "sfx": "boom"}],
        )


# --- the composer respects the lock -------------------------------------------------


def test_composer_leaves_arranged_slots_locked(monkeypatch):
    # Claude tries to recast EVERY slot with b.mp4 material — the arranged
    # slots 0..1 must survive verbatim, the rest may be recast.
    arrangement = [
        {"clip": "c.mp4", "start": 20.0},
        {"clip": "a.mp4", "start": 40.0},
    ]
    plain = plan_montage(
        make_big_reports(), make_music(), cut_lead=0.0, arrangement=arrangement,
        allow_repeats=True,  # every cast reuses b.mp4@10 — deliberate here
    )
    n = len(plain.entries)
    reply = {
        "story": "one story",
        "cast": [{"slot": i, "clip": "b.mp4", "start": 10.0} for i in range(n)],
        "titles": [],
        "why": [],
    }
    calls = []

    def fake(prompt, *, system="", json_schema=None, **kwargs):
        calls.append(prompt)
        return json.dumps(reply)

    monkeypatch.setattr(ai, "complete", fake)
    plan = compose_montage(
        make_big_reports(), make_music(), cut_lead=0.0, arrangement=arrangement,
        allow_repeats=True,
    )
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    assert entries[0].clip_path == "/footage/c.mp4"  # locked
    assert entries[1].clip_path == "/footage/a.mp4"  # locked
    assert all(e.clip_path == "/footage/b.mp4" for e in entries[2:])  # cast
    assert any("2 slots locked by your arrangement" in note for note in plan.notes)
    # the dossier flags the locked slots and the prompt explains the lock
    assert '"locked": true' in calls[0]
    assert "locked" in calls[0] and "arrangement" in calls[0]


def test_composer_dossier_unchanged_without_arrangement(monkeypatch):
    calls = []

    def fake(prompt, *, system="", json_schema=None, **kwargs):
        calls.append(prompt)
        return json.dumps({"story": "", "cast": [], "titles": [], "why": []})

    monkeypatch.setattr(ai, "complete", fake)
    compose_montage(make_big_reports(), make_music(), cut_lead=0.0)
    assert '"locked"' not in calls[0]


# --- CLI ----------------------------------------------------------------------------


def test_cli_create_parser_accepts_arrangement():
    from monteur.cli import build_parser

    args = build_parser().parse_args(
        ["create", "/footage", "/music/song.wav", "-o", "out.fcpxml",
         "--arrangement", "story.json"]
    )
    assert args.arrangement == "story.json"
    # ...and the default stays off
    args = build_parser().parse_args(
        ["create", "/footage", "/music/song.wav", "-o", "out.fcpxml"]
    )
    assert args.arrangement == ""


# --- arrangement + autofill under the zero-repeat promise ------------------------


def test_arrangement_autofill_never_repeats_when_repeats_off():
    # Two arranged scenes up front; the autofill serves the rest. With
    # repeats off the whole cut — arranged AND autofilled — must show zero
    # duplicate (clip, source_start) pairs and no shared source material.
    from monteur.montage import _shares_material

    arrangement = [
        {"clip": "b.mp4", "start": 15.0},
        {"clip": "a.mp4", "start": 1.0},
    ]
    plan = plan_montage(
        make_reports(), make_music(), cut_lead=0.0, arrangement=arrangement
    )
    assert plan.entries
    pairs = [(e.clip_path, round(e.source_start, 3)) for e in plan.entries]
    assert len(pairs) == len(set(pairs))
    for i, a in enumerate(plan.entries):
        for b in plan.entries[i + 1:]:
            assert not _shares_material(a, b)
    assert not any("footage repeats" in n for n in plan.notes)
