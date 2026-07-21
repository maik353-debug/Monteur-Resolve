"""Tests for the revision loop (monteur.revise).

MusicAnalysis / ClipReport objects are constructed directly, as in
tests/test_montage.py. The region mechanics tests pin the EXACT behavior
documented in monteur/revise.py's module docstring: calmer = merge on the
original grid, snappier = re-cut the region from a faster global re-plan,
everything outside a region bit-identical to the original plan.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from monteur.montage import CHRONOLOGICAL, plan_montage
from monteur.music import MusicAnalysis, MusicSection
from monteur.revise import (
    CALM_SCALE,
    SNAPPY_SCALE,
    Revision,
    parse_revision,
    revise_plan,
    style_from_plan,
)
from monteur.sift import ClipReport, Moment


def make_music() -> MusicAnalysis:
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
    )


def make_high_music() -> MusicAnalysis:
    """Same beat grid, one all-out section: a uniform slot length per pace."""
    return MusicAnalysis(
        path="/music/song.wav",
        duration=12.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(24)],
        sections=[MusicSection(0.0, 12.0, 0.9, "high")],
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


def make_arc_music() -> MusicAnalysis:
    """40s track: beats every 0.5s, downbeats every 2s, phrases every 8s."""
    return MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
    )


def make_long_reports() -> list[ClipReport]:
    return [
        ClipReport(
            path="/footage/long.mp4",
            duration=120.0,
            moments=[Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(20)],
        )
    ]


def entry_dicts(entries) -> list[dict]:
    return [asdict(e) for e in entries]


def slot_length(entry) -> float:
    return entry.record_end - entry.record_start


# --- parse_revision: regions ----------------------------------------------------


def test_parse_german_second_half_too_hectic():
    rev = parse_revision("die zweite Hälfte ist zu hektisch")
    assert rev.region == (0.5, 1.0)
    assert rev.region_seconds is None
    assert rev.pace_scale == CALM_SCALE
    assert rev.transitions is None and rev.style is None
    assert rev.rationale.startswith("recognized: ")
    assert "zweite hälfte" in rev.rationale and "zu hektisch" in rev.rationale


def test_parse_english_second_half_calmer():
    rev = parse_revision("make the second half calmer")
    assert rev.region == (0.5, 1.0)
    assert rev.pace_scale == CALM_SCALE


def test_parse_named_regions_table():
    for text, span in (
        ("erste hälfte schneller", (0.0, 0.5)),
        ("first half faster", (0.0, 0.5)),
        ("der anfang ist zu lahm", (0.0, 0.25)),
        ("slower intro please", (0.0, 0.25)),
        ("the opening drags", (0.0, 0.25)),
        ("am ende ruhiger", (0.75, 1.0)),
        ("calmer outro", (0.75, 1.0)),
    ):
        assert parse_revision(text).region == span, text


def test_parse_blenden_is_not_a_region():
    # "blenden"/"schwarzblenden" contain "ende" but mean transitions,
    # not the outro — the region needs a word boundary.
    rev = parse_revision("überall blenden bitte")
    assert rev.region is None
    assert rev.transitions == "dissolves"


def test_parse_absolute_region_mmss():
    rev = parse_revision("ab 1:30 ruhiger")
    assert rev.region is None
    assert rev.region_seconds == (90.0, None)
    assert rev.pace_scale == CALM_SCALE
    assert "'ab 1:30'" in rev.rationale


def test_parse_absolute_region_seconds():
    rev = parse_revision("from 90s faster")
    assert rev.region_seconds == (90.0, None)
    assert rev.pace_scale == SNAPPY_SCALE
    assert parse_revision("ab 90 sekunden schneller").region_seconds == (90.0, None)


# --- parse_revision: pace / transitions / style ----------------------------------


def test_parse_pace_words_table():
    for text, scale in (
        ("ruhiger", CALM_SCALE),
        ("langsamer bitte", CALM_SCALE),
        ("zu hektisch", CALM_SCALE),
        ("calmer", CALM_SCALE),
        ("a bit slower", CALM_SCALE),
        ("schneller", SNAPPY_SCALE),
        ("faster", SNAPPY_SCALE),
        ("mehr energie", SNAPPY_SCALE),
        ("das ist zu lahm", SNAPPY_SCALE),
        ("alles zu langsam", SNAPPY_SCALE),  # "too slow" asks for FASTER cuts
    ):
        assert parse_revision(text).pace_scale == scale, text


def test_parse_transitions_table():
    for text, mode in (
        ("harte schnitte", "cuts"),
        ("hard cuts please", "cuts"),
        ("mehr blenden", "dissolves"),
        ("dissolves everywhere", "dissolves"),
        ("schwarzblenden dazwischen", "smash"),
        ("smash to black", "smash"),
    ):
        rev = parse_revision(text)
        assert rev.transitions == mode, text
    # "schwarzblenden" contains "blenden": smash must win, not dissolves
    assert parse_revision("schwarzblenden").transitions == "smash"


def test_parse_style_keywords():
    assert parse_revision("mehr wie ein trailer").style == "trailer"
    assert parse_revision("wie ein reisefilm schneiden").style == "travel"
    assert parse_revision("hochzeitsfilm").style == "wedding"


def test_parse_neutral_fallback_never_guesses():
    rev = parse_revision("mach es einfach schöner")
    assert rev.region is None and rev.region_seconds is None
    assert rev.pace_scale is None
    assert rev.transitions is None and rev.style is None
    assert rev.rationale.startswith("no actionable instruction found: ")
    assert "schöner" in rev.rationale


def test_parse_combined_cues():
    rev = parse_revision("zweite Hälfte ruhiger, harte Schnitte")
    assert rev.region == (0.5, 1.0)
    assert rev.pace_scale == CALM_SCALE
    assert rev.transitions == "cuts"


# --- revise_plan: calmer region (merge on the original grid) ---------------------


CALM_KWARGS = dict(order=CHRONOLOGICAL, cut_lead=0.0)


def test_calmer_second_half_merges_and_keeps_first_half_bit_identical():
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    rev = Revision(region=(0.5, 1.0), pace_scale=CALM_SCALE)
    revised = revise_plan(
        original, make_reports(), make_music(), rev, **CALM_KWARGS
    )
    # first half: every entry BIT-IDENTICAL to the original plan's
    first_orig = [e for e in original.entries if e.record_end <= 6.0 + 1e-9]
    first_new = [e for e in revised.entries if e.record_end <= 6.0 + 1e-9]
    assert len(first_orig) == 3
    assert entry_dicts(first_new) == entry_dicts(first_orig)
    # region: 8 slots merged pairwise into 4 (x1.6 rounds to whole slots)
    region_orig = [e for e in original.entries if e.record_start >= 6.0 - 1e-9]
    region_new = [e for e in revised.entries if e.record_start >= 6.0 - 1e-9]
    assert len(region_orig) == 8
    assert len(region_new) == 4
    assert [slot_length(e) for e in region_new] == pytest.approx([2.0, 1.5, 1.0, 1.5])
    # cuts in the region are a SUBSET of the original grid positions
    orig_starts = {round(e.record_start, 6) for e in original.entries}
    assert {round(e.record_start, 6) for e in region_new} <= orig_starts
    # the grid still tiles the cut contiguously and ends on the length
    for prev, nxt in zip(revised.entries, revised.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert revised.entries[-1].record_end == pytest.approx(12.0)
    assert any(
        "revision: calmer 6.0-12.0s (pace x1.6): 8 slots -> 4" in n
        for n in revised.notes
    )


def test_calmer_merge_keeps_the_earlier_entrys_material():
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    rev = Revision(region=(0.5, 1.0), pace_scale=CALM_SCALE)
    revised = revise_plan(
        original, make_reports(), make_music(), rev, **CALM_KWARGS
    )
    first_merged = next(e for e in revised.entries if e.record_start == pytest.approx(6.0))
    donor = next(e for e in original.entries if e.record_start == pytest.approx(6.0))
    assert first_merged.clip_path == donor.clip_path
    assert first_merged.source_start == pytest.approx(donor.source_start)
    # the source is padded toward the clip's end to fill the doubled slot
    assert first_merged.record_end == pytest.approx(8.0)
    assert first_merged.source_end - first_merged.source_start == pytest.approx(2.0)


def test_pinned_shot_survives_a_calmed_region_untouched():
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    donor = next(e for e in original.entries if e.record_start == pytest.approx(8.0))
    rev = Revision(region=(0.5, 1.0), pace_scale=CALM_SCALE)
    revised = revise_plan(
        original, make_reports(), make_music(), rev, pinned=[8.2], **CALM_KWARGS
    )
    # the pinned entry is verbatim: exact material AND record window
    kept = [e for e in revised.entries if asdict(e) == asdict(donor)]
    assert len(kept) == 1
    # the merge worked AROUND it: 8 region slots became 5, not 4
    region_new = [e for e in revised.entries if e.record_start >= 6.0 - 1e-9]
    assert len(region_new) == 5
    assert any(
        "revision: calmer 6.0-12.0s (pace x1.6): 8 slots -> 5; 1 pinned shot kept" in n
        for n in revised.notes
    )


def test_pin_that_hits_no_shot_is_noted_and_ignored():
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    rev = Revision(region=(0.5, 1.0), pace_scale=CALM_SCALE)
    revised = revise_plan(
        original, make_reports(), make_music(), rev, pinned=[99.0], **CALM_KWARGS
    )
    assert any("pin at 99s hits no shot; ignored" in n for n in revised.notes)
    assert not any("pinned shot" in n for n in revised.notes)


def test_calmer_region_never_absorbs_a_dip():
    kwargs = dict(order=CHRONOLOGICAL, cut_lead=0.0, allow_repeats=True, style="trailer")
    original = plan_montage(make_reports(), make_music(), **kwargs)
    assert original.dips
    rev = Revision(region=(0.5, 1.0), pace_scale=CALM_SCALE)
    revised = revise_plan(original, make_reports(), make_music(), rev, **kwargs)
    assert revised.dips == original.dips
    for dip_start, dip_len in revised.dips:
        assert not any(
            e.record_start < dip_start + dip_len - 1e-6
            and e.record_end > dip_start + 1e-6
            for e in revised.entries
        )


# --- revise_plan: snappier region (re-cut from a faster re-plan) -----------------


def test_snappier_second_half_recuts_region_and_restores_the_rest():
    kwargs = dict(order=CHRONOLOGICAL, cut_lead=0.0, pace=2.0)
    original = plan_montage(make_reports(), make_high_music(), **kwargs)
    # pace 2s -> 4-beat base cuts, opened by the rhythm hold (2x, capped)
    assert [slot_length(e) for e in original.entries] == pytest.approx(
        [4.0, 2.0, 2.0, 2.0, 2.0]
    )
    rev = Revision(region=(0.5, 1.0), pace_scale=SNAPPY_SCALE)
    revised = revise_plan(original, make_reports(), make_high_music(), rev, **kwargs)
    # outside the region: restored VERBATIM from the original plan
    first_new = [e for e in revised.entries if e.record_end <= 6.0 + 1e-9]
    assert entry_dicts(first_new) == entry_dicts(original.entries[:2])
    # inside: re-cut at 2.0 x 0.6 = 1.2s -> 2 beats -> 1s base slots (the
    # re-plan's own rhythm keeps a longer final breath), still on beats
    region_new = [e for e in revised.entries if e.record_start >= 6.0 - 1e-9]
    assert [slot_length(e) for e in region_new] == pytest.approx(
        [1.0, 1.0, 1.0, 1.0, 2.0]
    )
    assert all((e.record_start * 2) == pytest.approx(round(e.record_start * 2)) for e in region_new)
    for prev, nxt in zip(revised.entries, revised.entries[1:]):
        assert nxt.record_start == pytest.approx(prev.record_end)
    assert revised.entries[-1].record_end == pytest.approx(12.0)
    assert any(
        "revision: snappier 6.0-12.0s (pace x0.6)" in n and "restored from the original plan" in n
        for n in revised.notes
    )


def test_snappier_region_trims_new_entries_to_the_original_boundary():
    # Slots [0,5.5) [5.5,9.5) [9.5,12): only [9.5,12) lies fully inside the
    # second half, so the fill window is [9.5,12) and the faster grid (pace
    # 2.5 x 0.6 = 1.5s -> 3-beat cuts) enters it mid-slot: its [7.5,10.5)
    # entry is trimmed to [9.5,10.5), the source moving 1:1 with the record.
    kwargs = dict(order=CHRONOLOGICAL, cut_lead=0.0, pace=4.0)
    original = plan_montage(make_reports(), make_high_music(), **kwargs)
    assert [slot_length(e) for e in original.entries] == pytest.approx([5.5, 4.0, 2.5])
    rev = Revision(region=(0.5, 1.0), pace_scale=SNAPPY_SCALE)
    revised = revise_plan(original, make_reports(), make_high_music(), rev, **kwargs)
    windows = [(e.record_start, e.record_end) for e in revised.entries]
    assert windows == [
        (pytest.approx(0.0), pytest.approx(5.5)),
        (pytest.approx(5.5), pytest.approx(9.5)),
        (pytest.approx(9.5), pytest.approx(10.5)),
        (pytest.approx(10.5), pytest.approx(12.0)),
    ]
    assert entry_dicts(revised.entries[:2]) == entry_dicts(original.entries[:2])
    trimmed = revised.entries[2]
    assert trimmed.source_end - trimmed.source_start == pytest.approx(1.0)


def test_pinned_shot_survives_a_snappier_region():
    kwargs = dict(order=CHRONOLOGICAL, cut_lead=0.0, pace=2.0)
    original = plan_montage(make_reports(), make_high_music(), **kwargs)
    donor = next(e for e in original.entries if e.record_start == pytest.approx(8.0))
    rev = Revision(region=(0.5, 1.0), pace_scale=SNAPPY_SCALE)
    revised = revise_plan(
        original, make_reports(), make_high_music(), rev, pinned=[8.5], **kwargs
    )
    kept = [e for e in revised.entries if asdict(e) == asdict(donor)]
    assert len(kept) == 1  # [8,10) exactly as it was
    others = [
        e for e in revised.entries
        if e.record_start >= 6.0 - 1e-9 and asdict(e) != asdict(donor)
    ]
    assert [slot_length(e) for e in others] == pytest.approx([1.0, 1.0, 2.0])
    assert any("1 pinned shot kept" in n for n in revised.notes)


# --- revise_plan: whole-cut revisions ---------------------------------------------


def test_global_calmer_replans_with_fewer_entries():
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    rev = parse_revision("ruhiger")
    assert rev.region is None and rev.pace_scale == CALM_SCALE
    revised = revise_plan(original, make_reports(), make_music(), rev, **CALM_KWARGS)
    assert len(revised.entries) < len(original.entries)
    assert any("revision: calmer overall (pace x1.6)" in n for n in revised.notes)


def test_transitions_override_applies_to_the_replan():
    kwargs = dict(cut_lead=0.0, allow_repeats=True, style="trailer")
    original = plan_montage(make_reports(), make_music(), **kwargs)
    assert original.dips  # the trailer smashes to black by default
    rev = parse_revision("nur harte schnitte")
    revised = revise_plan(original, make_reports(), make_music(), rev, **kwargs)
    assert revised.dips == []
    assert all(e.transition == 0.0 for e in revised.entries)
    assert any("transitions: hard cuts only" in n for n in revised.notes)
    assert any("revision: transitions -> cuts" in n for n in revised.notes)


def test_style_override_applies_to_the_replan():
    original = plan_montage(make_long_reports(), make_arc_music(), cut_lead=0.0)
    rev = Revision(style="travel")
    revised = revise_plan(
        original, make_long_reports(), make_arc_music(), rev, cut_lead=0.0
    )
    assert any('style "travel"' in n for n in revised.notes)
    assert any("revision: style -> travel" in n for n in revised.notes)


def test_neutral_revision_reproduces_the_plan():
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    rev = parse_revision("hm, weiß nicht")
    revised = revise_plan(original, make_reports(), make_music(), rev, **CALM_KWARGS)
    assert entry_dicts(revised.entries) == entry_dicts(original.entries)
    assert any("revision: no changes requested" in n for n in revised.notes)


def test_absolute_region_beyond_the_cut_is_skipped_honestly():
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    rev = parse_revision("ab 1:30 ruhiger")  # 90s into a 12s cut
    revised = revise_plan(original, make_reports(), make_music(), rev, **CALM_KWARGS)
    assert entry_dicts(revised.entries) == entry_dicts(original.entries)
    assert any("pace change skipped" in n for n in revised.notes)


def test_absolute_region_resolves_against_the_cut_length():
    # "ab 6s" on a 12s cut = the second half, same as the fraction path.
    original = plan_montage(make_reports(), make_music(), **CALM_KWARGS)
    rev = parse_revision("ab 6s ruhiger")
    assert rev.region_seconds == (6.0, None)
    revised = revise_plan(original, make_reports(), make_music(), rev, **CALM_KWARGS)
    region_new = [e for e in revised.entries if e.record_start >= 6.0 - 1e-9]
    assert len(region_new) == 4
    assert any("calmer 6.0-12.0s" in n for n in revised.notes)


def test_stale_whooshes_are_pruned_after_a_calm_merge():
    kwargs = dict(cut_lead=0.0, style="travel", sfx=True)
    original = plan_montage(make_long_reports(), make_arc_music(), **kwargs)
    assert any(c.kind == "whoosh" for c in original.sfx)
    rev = Revision(region=(0.5, 1.0), pace_scale=CALM_SCALE)
    revised = revise_plan(original, make_long_reports(), make_arc_music(), rev, **kwargs)
    # every surviving whoosh still centers on a real cut
    starts = [e.record_start for e in revised.entries]
    for cue in revised.sfx:
        if cue.kind == "whoosh":
            center = cue.time + cue.duration / 2.0
            assert any(abs(center - s) <= 1e-3 for s in starts)
    # time-based cues (ambience/risers/impacts) ride along untouched
    assert any(c.kind == "ambience" for c in revised.sfx)


# --- style recovery from the plan's own notes -------------------------------------


def test_style_from_plan_reads_the_style_note():
    plan = plan_montage(make_long_reports(), make_arc_music(), style="travel")
    assert style_from_plan(plan) == "travel"
    plain = plan_montage(make_reports(), make_music())
    assert style_from_plan(plain) == "auto"
    plain.notes = []
    assert style_from_plan(plain) == "auto"


# --- CLI surface (argument parsing only, like tests/test_sfx_cli.py) --------------


def test_revise_cli_parses_all_flags():
    from monteur.cli import build_parser, cmd_revise

    args = build_parser().parse_args(
        [
            "revise", "plan.json", "clips", "-o", "out.fcpxml",
            "--brief", "zweite Hälfte ruhiger",
            "--pin", "0:04", "--pin", "12",
            "--save-plan", "next.json",
        ]
    )
    assert args.func is cmd_revise
    assert args.plan == "plan.json"
    assert args.folder == "clips"
    assert args.output == "out.fcpxml"
    assert args.brief == "zweite Hälfte ruhiger"
    assert args.pin == ["0:04", "12"]
    assert args.save_plan == "next.json"
    assert args.fps == 25.0
    assert args.audio is None  # resolved from the plan at run time
    assert args.canvas == "uhd"


def test_revise_cli_defaults():
    from monteur.cli import build_parser

    args = build_parser().parse_args(["revise", "plan.json", "clips", "-o", "o.fcpxml"])
    assert args.brief == ""
    assert args.pin == []
    assert args.save_plan == ""


def test_create_cli_parses_save_plan():
    from monteur.cli import build_parser

    args = build_parser().parse_args(
        ["create", "clips", "song.mp3", "-o", "out.fcpxml", "--save-plan", "plan.json"]
    )
    assert args.save_plan == "plan.json"
    args = build_parser().parse_args(["create", "clips", "song.mp3", "-o", "out.fcpxml"])
    assert args.save_plan == ""


def test_cli_pin_accepts_mmss_and_seconds():
    from monteur.cli import _parse_pin

    assert _parse_pin("0:04") == 4.0
    assert _parse_pin("1:30") == 90.0
    assert _parse_pin("12") == 12.0
    assert _parse_pin("7.5") == 7.5
    with pytest.raises(SystemExit):
        _parse_pin("vier")
    with pytest.raises(SystemExit):
        _parse_pin("-3")


# --- zero-repeat promise across revisions (repeats off) --------------------------


def _shared_material_pairs(plan):
    from monteur.montage import _shares_material

    pairs = []
    for i, a in enumerate(plan.entries):
        for b in plan.entries[i + 1:]:
            if _shares_material(a, b):
                pairs.append((a.record_start, b.record_start))
    return pairs


def test_snappier_region_replan_stays_repeat_free_when_repeats_off():
    kwargs = dict(order=CHRONOLOGICAL, cut_lead=0.0, pace=2.0)
    original = plan_montage(make_reports(), make_high_music(), **kwargs)
    assert _shared_material_pairs(original) == []
    rev = Revision(region=(0.5, 1.0), pace_scale=SNAPPY_SCALE)
    revised = revise_plan(
        original, make_reports(), make_high_music(), rev, **kwargs
    )
    # the splice restores originals next to an independent re-plan — with
    # repeats off the result must still show zero repeated material
    assert _shared_material_pairs(revised) == []
    pairs = [(e.clip_path, round(e.source_start, 3)) for e in revised.entries]
    assert len(pairs) == len(set(pairs))


def test_pinned_shot_never_doubles_material_when_repeats_off():
    kwargs = dict(order=CHRONOLOGICAL, cut_lead=0.0)
    original = plan_montage(make_reports(), make_music(), **kwargs)
    rev = Revision(region=(0.5, 1.0), pace_scale=SNAPPY_SCALE)
    revised = revise_plan(
        original, make_reports(), make_music(), rev, pinned=[0.5], **kwargs
    )
    assert _shared_material_pairs(revised) == []


def test_allow_repeats_true_skips_the_revision_dedupe():
    # With repeats allowed the revision keeps whatever the splice produced
    # — no "no repeats:" note, no re-sourcing.
    kwargs = dict(order=CHRONOLOGICAL, cut_lead=0.0, allow_repeats=True, pace=2.0)
    original = plan_montage(make_reports(), make_high_music(), **kwargs)
    rev = Revision(region=(0.5, 1.0), pace_scale=SNAPPY_SCALE)
    revised = revise_plan(
        original, make_reports(), make_high_music(), rev, **kwargs
    )
    assert not any(n.startswith("no repeats:") for n in revised.notes)


def test_dedupe_repeats_resources_or_drops_duplicates():
    from monteur.montage import MontageEntry, MontagePlan
    from monteur.revise import _dedupe_repeats

    def dup_plan():
        return MontagePlan(
            music_path="/music/song.wav",
            duration=4.0,
            entries=[
                MontageEntry("/f/a.mp4", 1.0, 3.0, 0.0, 2.0, 0.9),
                MontageEntry("/f/a.mp4", 1.0, 3.0, 2.0, 4.0, 0.9),
            ],
        )

    # spare material exists: the later duplicate is re-sourced onto it
    plan = dup_plan()
    reports = [
        ClipReport(
            path="/f/a.mp4",
            duration=30.0,
            moments=[Moment(1.0, 3.0, 0.9), Moment(10.0, 14.0, 0.6)],
        )
    ]
    resourced, dropped = _dedupe_repeats(plan, reports, [])
    assert (resourced, dropped) == (1, 0)
    assert _shared_material_pairs(plan) == []
    assert plan.entries[1].source_start == pytest.approx(10.0)
    assert plan.entries[1].source_end == pytest.approx(12.0)
    assert plan.entries[1].record_start == pytest.approx(2.0)  # slot kept

    # no spare material anywhere: the duplicate is dropped, never kept
    plan = dup_plan()
    reports = [
        ClipReport(
            path="/f/a.mp4", duration=3.0, moments=[Moment(1.0, 3.0, 0.9)]
        )
    ]
    resourced, dropped = _dedupe_repeats(plan, reports, [])
    assert (resourced, dropped) == (0, 1)
    assert len(plan.entries) == 1
    assert _shared_material_pairs(plan) == []
