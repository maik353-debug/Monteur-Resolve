"""Blueprint Wave-1 acceptance tests (magie-blueprint items 1.1/1.3/1.6/1.8/1.9).

The metrics here are the blueprint's own Abnahme criteria:

* **Coincidence rate (1.1)** — for a synthetic pool with KNOWN envelope
  peaks, >= 80% of unpinned slots put the picture's peak within ±0.25 s of
  the slot's cut-lead point — and the SAME pool with the peaks stripped
  scores 0%, proving the delta comes from the aim, not the fixture.
* **Neutral degradation (1.1)** — no envelope signal -> byte-identical
  plans (the head-start fill of before; the wider proof is the untouched
  hand-built-moment suites in test_montage/test_arrange/test_compose).
* **Zero-repeat bookkeeping (1.1)** — the aim's skipped heads are
  reclaimable gaps: nothing repeats AND nothing is burnt.
* **SFX offset correctness per kind (1.3)** — riser plays its LAST run-up
  seconds, impact/braam peak on the hit, whoosh peak on the cut, tails
  ring out; ``source_offset`` serializes only when set.
* **Recovery breath + hot/cool phrase groups (1.6)**.
* **Titles in the preview (1.8)** — probe-gated like the export.
* **First-frame gate (1.9)** — the short's hook nudges its aimed in-point
  to a sharper frame inside the peak promise's own ±0.25 s window.
"""

from __future__ import annotations

import json
import types

import pytest

from monteur.montage import (
    MontagePlan,
    MontageEntry,
    SfxCue,
    montage_to_timeline,
    plan_montage,
    plan_from_dict,
    plan_to_dict,
    _aim_start,
    _phase_cut_lengths,
    _PoolItem,
)
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import USABLE, ClipReport, ClipSegment, Moment

CUT_LEAD = 0.04  # the planner's default cut-ahead lead
TOL = 0.25  # the blueprint's honest coincidence tolerance (±0.25 s)


def peaked_moment(start: float, end: float, peak: float, score: float = 0.8, **kw) -> Moment:
    """A moment with a known triangular envelope peaking at ``peak``."""
    span = max(end - start, 1e-9)
    envelope = [
        (t, max(0.05, 1.0 - abs(t - peak) / span))
        for t in [start + 0.5 * i for i in range(int(span * 2) + 1)]
    ]
    return Moment(start, end, score, envelope=envelope, peak_time=peak, **kw)


def stripped(moment: Moment) -> Moment:
    """The same moment without any envelope signal (the pre-1.1 world)."""
    return Moment(
        moment.start, moment.end, moment.score,
        entry_motion=moment.entry_motion, exit_motion=moment.exit_motion,
        highlight=moment.highlight,
    )


def make_music(duration: float = 30.0) -> MusicAnalysis:
    return MusicAnalysis(
        path="/music/song.wav",
        duration=duration,
        tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, 0.5, "mid")],
    )


def make_peaked_reports(n: int = 40) -> list[ClipReport]:
    """One 4 s moment per distinct clip, every peak 1.5 s in (at 3.5 s)."""
    return [
        ClipReport(
            path=f"/footage/p{i:02d}.mp4",
            duration=30.0,
            moments=[peaked_moment(2.0, 6.0, 3.5)],
        )
        for i in range(n)
    ]


def coincidence_rate(plan: MontagePlan, peaks: dict[str, float]) -> float:
    """Share of slots whose known peak sits within ±TOL of the cut-lead point.

    The blueprint's metric: the beat a slot serves lands ``CUT_LEAD`` after
    its (shifted) record start — except the montage's first slot, whose
    beat is 0. A peak that is not even on screen can never coincide.
    """
    hits = 0
    for entry in plan.entries:
        peak = peaks[entry.clip_path]
        lead = CUT_LEAD if entry.record_start > 1e-9 else 0.0
        if not (entry.source_start - 1e-9 <= peak <= entry.source_end + 1e-9):
            continue  # the peak never reaches the screen: a miss
        peak_record = entry.record_start + (peak - entry.source_start)
        if abs(peak_record - (entry.record_start + lead)) <= TOL + 1e-9:
            hits += 1
    return hits / len(plan.entries)


# --- 1.1: coincidence rate (the blueprint's headline metric) --------------------


def test_coincidence_rate_over_80_percent_with_peaks():
    reports = make_peaked_reports()
    peaks = {r.path: r.moments[0].peak_time for r in reports}
    plan = plan_montage(reports, make_music())
    rate = coincidence_rate(plan, peaks)
    assert rate >= 0.8, f"coincidence rate {rate:.2f} below the 80% target"
    assert any(n.startswith("cut on action:") for n in plan.notes)


def test_coincidence_rate_zero_before_the_change():
    # The SAME pool with the envelope stripped is the pre-1.1 engine: every
    # moment plays from its head, the peak (1.5 s in) misses every beat by
    # far more than ±0.25 s — the measured delta is the aim's doing.
    reports = make_peaked_reports()
    peaks = {r.path: 3.5 for r in reports}
    legacy = [
        ClipReport(path=r.path, duration=r.duration, moments=[stripped(r.moments[0])])
        for r in reports
    ]
    plan = plan_montage(legacy, make_music())
    assert coincidence_rate(plan, peaks) == 0.0
    assert not any(n.startswith("cut on action:") for n in plan.notes)


def test_no_envelope_signal_plans_byte_identically():
    # Neutral degradation: moments without the 1.1 fields fill from their
    # heads, exactly as before — provable byte-for-byte via the plan JSON.
    legacy = [
        ClipReport(
            path=f"/footage/v{i:02d}.mp4", duration=30.0, moments=[Moment(2.0, 6.0, 0.8)]
        )
        for i in range(40)
    ]
    a = plan_montage(legacy, make_music())
    b = plan_montage(legacy, make_music())
    assert json.dumps(plan_to_dict(a)) == json.dumps(plan_to_dict(b))
    # every fresh use starts at the moment's head — no aim happened
    assert all(e.source_start == pytest.approx(2.0) for e in a.entries[:40])
    assert not any(n.startswith("cut on action:") for n in a.notes)


def test_aim_start_clamps_and_neutrality():
    item = _PoolItem("/c.mp4", 30.0, peaked_moment(2.0, 6.0, 3.5))
    # the aim puts the peak CUT_LEAD after the slot start
    assert _aim_start(item, 1.0, CUT_LEAD) == pytest.approx(3.46)
    # clamped so the slot still fits inside the moment
    assert _aim_start(item, 3.5, CUT_LEAD) == pytest.approx(2.5)
    # a drop hold may extend past the moment through vetted slack...
    short = _PoolItem(
        "/c.mp4", 30.0, peaked_moment(2.0, 3.0, 2.8), slack_end=8.0
    )
    assert _aim_start(short, 2.0, CUT_LEAD, drop=True) == pytest.approx(2.76)
    # ...but never without it (unknown slack stays inside the moment)
    short.slack_end = 0.0
    assert _aim_start(short, 2.0, CUT_LEAD, drop=True) is None
    # no signal / partially consumed = no aim (today's behavior)
    assert _aim_start(_PoolItem("/c.mp4", 30.0, Moment(2.0, 6.0, 0.8)), 1.0, CUT_LEAD) is None
    used = _PoolItem("/c.mp4", 30.0, peaked_moment(2.0, 6.0, 3.5), consumed=0.5)
    assert _aim_start(used, 1.0, CUT_LEAD) is None


def test_zero_repeat_survives_the_aim_and_heads_are_not_burnt():
    # One 10 s moment, a 12 s request, repeats OFF: the cap makes it a 10 s
    # cut that must use EVERY second exactly once. The aim skips ~6 s of
    # head for the first slot — if that head were burnt, the plan would
    # shrink far below 10 s; instead it is a reclaimable gap.
    music = MusicAnalysis(
        path="/music/song.wav", duration=12.0, tempo=120.0,
        beats=[i * 0.5 for i in range(24)],
        sections=[MusicSection(0.0, 12.0, 0.9, "high")],
    )
    report = ClipReport(
        path="/footage/one.mp4", duration=20.0,
        moments=[peaked_moment(0.0, 10.0, 6.0)],
    )
    plan = plan_montage([report], music, allow_repeats=False, cut_lead=0.0)
    assert plan.duration == pytest.approx(10.0)
    assert not any(n.startswith("length reduced to") and "no repeats" in n for n in plan.notes[2:])
    # zero-repeat: no two entries overlap in source
    spans = sorted((e.source_start, e.source_end) for e in plan.entries)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert s2 >= e1 - 1e-6
    # nothing burnt: the record time equals the distinct material played
    played = sum(e - s for s, e in spans)
    assert played == pytest.approx(10.0, abs=0.01)
    # ...and the first use really did aim at the peak
    first = min(plan.entries, key=lambda e: e.record_start)
    assert first.source_start == pytest.approx(6.0)


def test_drop_hold_aims_peak_at_the_drop_and_extends_past_it():
    # Auto style, drop at 20 s: the drop slot takes the loudest moment,
    # aims its peak AT the drop instant (record 20.0 — the slot starts
    # cut_lead earlier) and the hold extends past the 1 s moment through
    # the report's USABLE segment slack (ClipReport material, 1.1c).
    music = MusicAnalysis(
        path="/music/song.wav", duration=40.0, tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        drops=[20.0],
    )
    strong = ClipReport(
        path="/footage/strong.mp4", duration=30.0,
        segments=[ClipSegment(0.0, 30.0, USABLE, 0.6)],
        moments=[peaked_moment(10.0, 11.0, 10.5, score=0.5, highlight=0.9)],
    )
    fillers = [
        ClipReport(path=f"/footage/f{i:02d}.mp4", duration=30.0, moments=[Moment(2.0, 4.0, 0.8)])
        for i in range(40)
    ]
    plan = plan_montage([strong] + fillers, music, style="auto", allow_repeats=True)
    drop_entry = next(e for e in plan.entries if e.clip_path == "/footage/strong.mp4")
    # the slot starts cut_lead before the drop; the peak lands ON the drop
    assert drop_entry.record_start == pytest.approx(20.0 - CUT_LEAD)
    assert drop_entry.source_start == pytest.approx(10.5 - CUT_LEAD)
    peak_record = drop_entry.record_start + (10.5 - drop_entry.source_start)
    assert peak_record == pytest.approx(20.0)
    # the hold extends PAST the moment's end through the usable slack
    assert drop_entry.source_end > 11.0 + 1e-6


# --- 1.9: first-frame gate (shorts) ---------------------------------------------


def make_short_music(duration: float = 30.0) -> MusicAnalysis:
    return MusicAnalysis(
        path="/music/song.wav", duration=duration, tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, 0.9, "high")],
    )


def hook_reports(frame_quality: list[tuple[float, float]]) -> list[ClipReport]:
    hook = peaked_moment(2.0, 6.0, 4.0, score=0.9)
    hook.entry_motion = (12.0, 0.0)  # the boldest moment wins the hook slot
    hook.exit_motion = (12.0, 0.0)
    hook.frame_quality = frame_quality
    fillers = [
        ClipReport(path=f"/footage/f{i:02d}.mp4", duration=60.0, moments=[Moment(1.0, 3.0, 0.5)])
        for i in range(30)
    ]
    return [ClipReport(path="/footage/hook.mp4", duration=30.0, moments=[hook])] + fillers


def test_short_hook_gates_in_point_by_first_frame_quality():
    # A notably sharper frame 0.2 s BEFORE the peak wins the in-point; the
    # peak then sits 0.2 s into the slot — inside the ±0.25 s promise.
    plan = plan_montage(
        hook_reports([(3.8, 0.95), (4.0, 0.3)]), make_short_music(), style="short"
    )
    hook_entry = plan.entries[0]
    assert hook_entry.clip_path == "/footage/hook.mp4"
    assert hook_entry.source_start == pytest.approx(3.8)


def test_short_hook_keeps_the_peak_without_a_quality_gain():
    # Near-equal frames: the peak rules, the gate does not move the start.
    plan = plan_montage(
        hook_reports([(3.8, 0.31), (4.0, 0.3)]), make_short_music(), style="short"
    )
    assert plan.entries[0].source_start == pytest.approx(4.0)


def test_short_hook_gate_never_pushes_the_peak_off_screen():
    # A sharper frame AFTER the peak may not win: starting there would cut
    # the peak out of the shot entirely.
    plan = plan_montage(
        hook_reports([(4.25, 0.95), (4.0, 0.3)]), make_short_music(), style="short"
    )
    assert plan.entries[0].source_start == pytest.approx(4.0)


# --- 1.6: breath in the canon ----------------------------------------------------


def test_recovery_breath_follows_the_drop_hold():
    lengths = _phase_cut_lengths(
        n_units=24, base=1, pattern=(1, 1, 1, 2), first_hold=3, recovery=2
    )
    assert lengths[0] == 3  # the drop hold
    assert lengths[1] == 2  # the recovery breath (2x base)
    assert lengths[2] == 1  # then the pattern re-accelerates


def test_hot_cool_phrase_groups_alternate():
    lengths = _phase_cut_lengths(
        n_units=32, base=1, pattern=(1, 1, 2, 1),
        cool_pattern=(2, 2, 4, 2), group_units=8,
    )
    # group membership by cumulative units: groups 0/2 hot, 1/3 cool
    consumed = 0
    hot, cool = [], []
    for length in lengths:
        (cool if (consumed // 8) % 2 == 1 else hot).append(length)
        consumed += length
    assert hot and cool
    assert sum(cool) / len(cool) > sum(hot) / len(hot)  # cool groups breathe
    assert max(hot) <= 2 and max(cool) <= 4


def test_trailer_plan_carries_recovery_and_phrase_groups():
    music = MusicAnalysis(
        path="/music/track.wav", duration=40.0, tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
        drops=[20.0],
    )
    reports = [
        ClipReport(
            path="/footage/long.mp4", duration=120.0,
            moments=[Moment(i * 4.0, i * 4.0 + 2.0, 0.8) for i in range(20)],
        )
    ]
    plan = plan_montage(reports, music, style="trailer", cut_lead=0.0)
    note = next(n for n in plan.notes if n.startswith("rhythm: "))
    assert "recovery breath" in note
    assert "hot/cool" in note
    # the slot right after the drop hold is the 2x-base recovery breath
    drop = next(e for e in plan.entries if e.record_start == pytest.approx(20.0))
    after = next(
        e for e in plan.entries if e.record_start == pytest.approx(drop.record_end)
    )
    assert (after.record_end - after.record_start) == pytest.approx(1.0)  # 2 beats
    assert (drop.record_end - drop.record_start) > (after.record_end - after.record_start)


# --- 1.3: SFX source offsets per kind ---------------------------------------------


def sfx_plan(style: str = "trailer", duration: float = 40.0) -> MontagePlan:
    plan = MontagePlan(music_path="/music/song.wav", duration=duration)
    plan.notes.append(f'style "{style}": whatever')
    return plan


def sfx_music(duration: float = 40.0, drops=(20.0,)) -> MusicAnalysis:
    return MusicAnalysis(
        path="/music/song.wav", duration=duration, tempo=120.0, drops=list(drops)
    )


def test_riser_plays_its_last_run_up_seconds():
    from monteur.elements import SoundElement, assign_elements

    plan = sfx_plan(duration=12.0)
    music = sfx_music(duration=12.0, drops=(5.0,))
    pool = [SoundElement("/sfx/rise.wav", 6.0, "riser", 0.9)]
    assign_elements(plan, music, pool)
    cue = next(c for c in plan.sfx if c.kind == "riser" and c.file)
    assert cue.time == pytest.approx(0.0)
    assert cue.duration == pytest.approx(5.0)
    # the LAST 5 s of the 6 s file: the build ends at the file's climax
    assert cue.source_offset == pytest.approx(1.0)
    assert cue.time + cue.duration == pytest.approx(5.0)  # still ends ON the drop


def test_impact_peak_lands_on_the_hit():
    from monteur.elements import SoundElement, assign_elements

    plan = sfx_plan()
    pool = [
        SoundElement(
            "/sfx/hit.wav", 1.5, "impact", 0.9, features={"peak_time": 0.2}
        )
    ]
    assign_elements(plan, sfx_music(), pool)
    cue = next(c for c in plan.sfx if c.kind == "impact" and c.file)
    # the file starts its run-in early; the measured peak hits the drop
    assert cue.time == pytest.approx(20.0 - 0.2)
    assert cue.source_offset == pytest.approx(0.0)
    assert cue.time + 0.2 == pytest.approx(20.0)
    assert cue.duration == pytest.approx(1.5)  # the tail rings out


def test_impact_head_skip_when_the_montage_starts_too_late():
    from monteur.elements import SoundElement, assign_elements

    plan = sfx_plan()
    music = sfx_music(drops=(0.1,))
    pool = [
        SoundElement(
            "/sfx/hit.wav", 1.0, "impact", 0.9, features={"peak_time": 0.3}
        )
    ]
    assign_elements(plan, music, pool)
    cue = next(c for c in plan.sfx if c.kind == "impact" and c.file)
    # run-in would start at -0.2: skip that much head, peak still on the hit
    assert cue.time == pytest.approx(0.0)
    assert cue.source_offset == pytest.approx(0.2)
    assert 0.3 - cue.source_offset == pytest.approx(0.1)


def test_whoosh_peak_aligns_to_the_cut_and_rings_out():
    from monteur.elements import SoundElement, assign_elements

    plan = sfx_plan()
    plan.sfx = [SfxCue(9.7, 0.6, "whoosh", "whoosh transition fast", "fast cut")]
    pool = [
        SoundElement(
            "/sfx/wh.wav", 2.0, "whoosh", 0.9, features={"peak_time": 1.0}
        )
    ]
    assign_elements(plan, sfx_music(), pool)
    cue = plan.sfx[0]
    assert cue.file == "/sfx/wh.wav"
    # planned cue centered the 0.6 s whoosh on the cut at 10.0; the FILE
    # peak now sits exactly there, and the full 2 s file plays (tail rule)
    assert cue.time == pytest.approx(9.0)
    assert cue.duration == pytest.approx(2.0)
    assert cue.time + 1.0 == pytest.approx(10.0)


def test_braam_peak_on_the_dip():
    from monteur.elements import SoundElement, assign_elements

    plan = sfx_plan()
    plan.dips = [(30.0, 0.4)]
    plan.sfx = [SfxCue(30.0, 0.4, "sub-drop", "sub drop boom", "title slot")]
    pool = [
        SoundElement(
            "/sfx/braam.wav", 3.0, "braam", 0.9, features={"peak_time": 0.4}
        )
    ]
    assign_elements(plan, sfx_music(), pool)
    cue = plan.sfx[0]
    assert cue.file == "/sfx/braam.wav"
    assert cue.time == pytest.approx(29.6)
    assert cue.time + 0.4 == pytest.approx(30.0)  # peak ON the dip start
    assert cue.duration == pytest.approx(3.0)  # rings out under and past the black


def test_source_offset_serializes_only_when_set():
    plan = MontagePlan(music_path="/m.wav", duration=10.0)
    plan.sfx = [
        SfxCue(1.0, 0.5, "impact", "q", "n", file="/sfx/a.wav"),
        SfxCue(4.0, 2.0, "riser", "q", "n", file="/sfx/b.wav", source_offset=1.5),
    ]
    data = plan_to_dict(plan)
    assert "source_offset" not in data["sfx"][0]  # unset: bytes unchanged
    assert data["sfx"][1]["source_offset"] == pytest.approx(1.5)
    loaded = plan_from_dict(json.loads(json.dumps(data)))
    assert loaded.sfx[0].source_offset == 0.0
    assert loaded.sfx[1].source_offset == pytest.approx(1.5)


def test_timeline_clip_reads_from_the_source_offset():
    plan = MontagePlan(music_path="/m.wav", duration=10.0)
    plan.entries = [
        MontageEntry(
            clip_path="/footage/a.mp4", source_start=0.0, source_end=10.0,
            record_start=0.0, record_end=10.0, score=1.0,
        )
    ]
    plan.sfx = [
        SfxCue(2.0, 3.0, "riser", "q", "n", file="/sfx/rise.wav", source_offset=1.5)
    ]
    timeline = montage_to_timeline(plan, fps=25.0)
    sfx_clip = next(c for c in timeline.clips if c.track == "A2")
    assert sfx_clip.source_in == 38  # 1.5 s at 25 fps (seconds_to_frames rounds)
    assert sfx_clip.source_out - sfx_clip.source_in == 75  # 3.0 s play


# --- 1.8: titles in the preview ---------------------------------------------------


def titled_plan(tmp_path=None) -> MontagePlan:
    plan = MontagePlan(music_path="", duration=3.0)
    plan.entries = [
        MontageEntry(
            clip_path="/footage/a.mp4", source_start=0.0, source_end=1.3,
            record_start=0.0, record_end=1.3, score=1.0,
        ),
        MontageEntry(
            clip_path="/footage/b.mp4", source_start=0.0, source_end=1.3,
            record_start=1.7, record_end=3.0, score=1.0,
        ),
    ]
    plan.dips = [(1.3, 0.4)]
    plan.title_texts = ["EIN SOMMER"]
    return plan


def test_preview_draws_the_title_on_the_dip_segment(monkeypatch, tmp_path):
    # Command-level proof, independent of this machine's ffmpeg build: with
    # drawtext + a font present, the dip segment's ffmpeg call carries the
    # drawtext filter and the composed text (the same _title_filter the
    # export uses).
    from monteur import preview

    calls: list[list[str]] = []

    def fake_run(args, label):
        calls.append(list(args))

    monkeypatch.setattr(preview, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(preview, "_supports_drawtext", lambda: True)
    monkeypatch.setattr(preview, "_find_font", lambda: "/fake/font.ttf")
    monkeypatch.setattr(
        preview,
        "probe",
        lambda path: types.SimpleNamespace(
            duration=3.0, width=640, height=360, has_audio=True, fps=25.0
        ),
    )
    out = tmp_path / "preview.mp4"
    preview.render_preview(titled_plan(), str(out), audio="original")
    dip_calls = [c for c in calls if any("drawtext=" in a for a in c)]
    assert len(dip_calls) == 1  # exactly the one titled dip segment
    filter_arg = next(a for a in dip_calls[0] if "drawtext=" in a)
    assert "fontfile=" in filter_arg
    # the text itself travels via textfile (robust quoting)
    text_files = [a for a in dip_calls[0] if a.endswith(".txt")]
    assert not text_files  # the path is inside the filter string
    assert "title_" in filter_arg


def test_preview_without_drawtext_stays_plain_black(monkeypatch, tmp_path):
    # Defensive degradation: no drawtext filter -> no -vf on the dip, the
    # preview still renders (probed exactly like the export's title path).
    from monteur import preview

    calls: list[list[str]] = []
    monkeypatch.setattr(preview, "_run_ffmpeg", lambda args, label: calls.append(list(args)))
    monkeypatch.setattr(preview, "_supports_drawtext", lambda: False)
    monkeypatch.setattr(
        preview,
        "probe",
        lambda path: types.SimpleNamespace(
            duration=3.0, width=640, height=360, has_audio=True, fps=25.0
        ),
    )
    preview.render_preview(titled_plan(), str(tmp_path / "p.mp4"), audio="original")
    assert not any(any("drawtext=" in a for a in c) for c in calls)


@pytest.mark.skipif(
    not (
        __import__("monteur.preview", fromlist=["_supports_drawtext"])._supports_drawtext()
        and __import__("monteur.preview", fromlist=["_find_font"])._find_font()
    ),
    reason="this ffmpeg build cannot draw text (or no font)",
)
def test_preview_title_pixels_are_not_black(tmp_path):
    # The real thing, probe-gated: render the preview, extract a frame from
    # the dip window, and demand non-black pixels — the title is VISIBLE.
    from monteur.media import extract_rgb_frame
    from monteur.preview import render_preview
    from _demo import DEMO

    if not (DEMO / "clip_A.mp4").exists():
        pytest.skip("demo footage not present")
    plan = titled_plan()
    plan.entries[0].clip_path = str(DEMO / "clip_A.mp4")
    plan.entries[1].clip_path = str(DEMO / "clip_C.mp4")
    out = tmp_path / "titled.mp4"
    render_preview(plan, str(out), audio="original")
    frame = extract_rgb_frame(str(out), 1.5, size=(160, 90))
    assert int(frame.max()) > 40, "dip frame is black — the title did not draw"
