"""Tests for the Claude composer (monteur.compose).

The AI seam (monteur.ai.complete) is always monkeypatched — these tests
exercise the engine side of the contract: grid parity with plan_montage,
the dossier/prompt content, cast validation with per-slot fallback, the
graceful/strict failure semantics and the title round-trip.
"""

from __future__ import annotations

import json

import pytest

from monteur import ai
from monteur import compose
from monteur.ai import MonteurAIError
from monteur.compose import COMPOSE_SCHEMA, CRAFT_BRIEFS, compose_montage
from monteur.montage import (
    montage_to_timeline,
    plan_from_dict,
    plan_montage,
    plan_to_dict,
)
from monteur.music import MusicAnalysis, MusicSection
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


def make_arc_music(drops: list[float] | None = None) -> MusicAnalysis:
    """40s track: beats every 0.5s, downbeats every 2s, phrases every 8s."""
    return MusicAnalysis(
        path="/music/track.wav",
        duration=40.0,
        tempo=120.0,
        beats=[i * 0.5 for i in range(80)],
        sections=[MusicSection(0.0, 40.0, 0.5, "mid")],
        downbeats=[i * 2.0 for i in range(20)],
        phrases=[i * 8.0 for i in range(5)],
        drops=drops or [],
    )


def make_reports(vision: bool = False) -> list[ClipReport]:
    """Two clips with plenty of 5s moments, so every slot can be cast."""
    reports = []
    for name, base_score in (("a", 0.8), ("b", 0.7)):
        moments = [
            Moment(i * 10.0, i * 10.0 + 5.0, base_score - i * 0.01)
            for i in range(8)
        ]
        reports.append(
            ClipReport(path=f"/footage/{name}.mp4", duration=90.0, moments=moments)
        )
    if vision:
        first = reports[0].moments[0]
        first.label = "sunrise over the pass"
        first.tags = ["sunrise", "mountains"]
        first.role = "opener"
        reports[1].moments[1].hero = 0.9
        reports[1].moments[1].group = "summit"
    return reports


def fake_complete(reply, calls=None):
    """A monteur.ai.complete stand-in returning ``reply`` (dict or str)."""

    def _complete(prompt, *, system="", json_schema=None, **kwargs):
        if calls is not None:
            calls.append(
                {"prompt": prompt, "system": system, "json_schema": json_schema}
            )
        return reply if isinstance(reply, str) else json.dumps(reply)

    return _complete


def empty_reply() -> dict:
    return {"story": "", "cast": [], "titles": [], "why": []}


def entry_tuple(entry):
    return (
        entry.clip_path,
        entry.source_start,
        entry.source_end,
        entry.record_start,
        entry.record_end,
    )


# --- grid parity & failure semantics ------------------------------------------


def test_ai_error_falls_back_to_plan_montage_byte_identical(monkeypatch):
    def boom(*args, **kwargs):
        raise MonteurAIError("no way to reach Claude")

    monkeypatch.setattr(ai, "complete", boom)
    baseline = plan_to_dict(
        plan_montage(make_reports(), make_music(), style="auto", cut_lead=0.0)
    )
    composed = plan_to_dict(
        compose_montage(make_reports(), make_music(), style="auto", cut_lead=0.0)
    )
    note = composed["notes"][-1]
    assert note == "composer unavailable: no way to reach Claude; heuristic cut"
    composed["notes"] = composed["notes"][:-1]
    assert composed == baseline


def test_unparseable_reply_falls_back_with_note(monkeypatch):
    monkeypatch.setattr(ai, "complete", fake_complete("this is not JSON"))
    plan = compose_montage(make_reports(), make_music(), cut_lead=0.0)
    assert plan.entries
    assert any(n.startswith("composer unavailable:") for n in plan.notes)
    assert any("heuristic cut" in n for n in plan.notes)


def test_strict_raises_on_ai_error(monkeypatch):
    def boom(*args, **kwargs):
        raise MonteurAIError("backend down")

    monkeypatch.setattr(ai, "complete", boom)
    with pytest.raises(MonteurAIError, match="backend down"):
        compose_montage(make_reports(), make_music(), strict=True)


def test_strict_raises_on_unparseable_reply(monkeypatch):
    monkeypatch.setattr(ai, "complete", fake_complete("not json"))
    with pytest.raises(MonteurAIError, match="unparseable JSON"):
        compose_montage(make_reports(), make_music(), strict=True)


def test_grid_is_identical_to_plan_montage_when_cast_applies(monkeypatch):
    reply = empty_reply()
    reply["cast"] = [{"slot": 0, "clip": "b.mp4", "start": 10.0}]
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    baseline = plan_montage(
        make_reports(), make_arc_music(drops=[20.0]), style="trailer", cut_lead=0.0
    )
    composed = compose_montage(
        make_reports(), make_arc_music(drops=[20.0]), style="trailer", cut_lead=0.0
    )
    # The grid — record windows, dips, fades, dissolves — is the engine's.
    assert [
        (e.record_start, e.record_end, e.transition) for e in composed.entries
    ] == [(e.record_start, e.record_end, e.transition) for e in baseline.entries]
    assert composed.dips == baseline.dips
    assert (composed.fade_in, composed.fade_out) == (
        baseline.fade_in,
        baseline.fade_out,
    )
    assert composed.duration == baseline.duration


def test_empty_plan_skips_the_ai_call(monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("ai.complete must not be called for empty plans")

    monkeypatch.setattr(ai, "complete", boom)
    plan = compose_montage([], make_music())
    assert not plan.entries


# --- the prompt dossier ---------------------------------------------------------


def test_prompt_carries_grammar_brief_slots_dips_and_inventory(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    plan = compose_montage(
        make_reports(),
        make_arc_music(drops=[20.0]),
        style="trailer",
        brief="Motorradtour über die Alpen, episch",
        cut_lead=0.0,
    )
    assert plan.dips  # the trailer smashes to black — the dossier needs dips
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert calls[0]["json_schema"] == COMPOSE_SCHEMA
    # the style's craft grammar, verbatim
    assert CRAFT_BRIEFS["trailer"] in prompt
    # the editor's brief
    assert "Motorradtour über die Alpen, episch" in prompt
    # the dossier: slots with phases/durations, dips, the full inventory
    context = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])
    assert context["style"] == "trailer"
    assert len(context["slots"]) == len(plan.entries)
    assert all("seconds" in s for s in context["slots"])
    assert {s.get("phase") for s in context["slots"]} >= {"opening", "climax"}
    assert len(context["dips"]) == len(plan.dips)
    assert len(context["inventory"]) == 16
    assert {i["clip"] for i in context["inventory"]} == {"a.mp4", "b.mp4"}
    # a drop-aligned climax marks its slot
    assert any(s.get("drop") for s in context["slots"])
    # dip-following slots hit out of black
    assert any(s.get("after_dip") for s in context["slots"])


def test_compose_streams_the_answer_and_reasoning(monkeypatch):
    # the storyboard build passes on_text + on_thinking so it can show the cut
    # being reasoned through and written live — compose must forward BOTH to
    # ai.complete (on_delta / on_thinking), verbatim
    captured: dict = {}

    def _complete(prompt, *, system="", json_schema=None,
                  on_delta=None, on_thinking=None, **kwargs):
        captured["on_delta"] = on_delta
        captured["on_thinking"] = on_thinking
        if on_thinking is not None:
            on_thinking("weighing the ")
            on_thinking("hero beat…")
        if on_delta is not None:
            on_delta("story so f")
            on_delta("ar…")
        return json.dumps(empty_reply())

    monkeypatch.setattr(ai, "complete", _complete)
    text: list[str] = []
    think: list[str] = []
    compose_montage(make_reports(), make_music(),
                    on_text=text.append, on_thinking=think.append, cut_lead=0.0)
    assert captured["on_delta"] is not None and captured["on_thinking"] is not None
    assert text == ["story so f", "ar…"]  # the answer streamed through
    assert think == ["weighing the ", "hero beat…"]  # ...and the reasoning


def test_prompt_says_when_vision_is_missing_and_notes_recommend_it(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    plan = compose_montage(make_reports(), make_music(), cut_lead=0.0)
    assert "No vision labels are available" in calls[0]["prompt"]
    assert any("Let Claude watch your clips" in n for n in plan.notes)


def test_prompt_carries_vision_annotations_when_present(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    plan = compose_montage(make_reports(vision=True), make_music(), cut_lead=0.0)
    prompt = calls[0]["prompt"]
    assert "No vision labels are available" not in prompt
    assert "sunrise over the pass" in prompt
    assert '"role": "opener"' in prompt
    assert '"hero": 0.9' in prompt
    assert '"group": "summit"' in prompt
    assert not any("Let Claude watch your clips" in n for n in plan.notes)


# --- casting -----------------------------------------------------------------------


def test_happy_compose_applies_the_cast_and_the_story(monkeypatch):
    reply = {
        "story": "from first light to the summit",
        "cast": [
            {"slot": 0, "clip": "b.mp4", "start": 10.0},
            {"slot": 1, "clip": "a.mp4", "start": 20.5},
        ],
        "titles": [],
        "why": ["open wide to establish", "then tighten the rhythm"],
    }
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    baseline = plan_montage(make_reports(), make_music(), cut_lead=0.0)
    plan = compose_montage(make_reports(), make_music(), cut_lead=0.0)

    first, second = plan.entries[0], plan.entries[1]
    assert first.clip_path == "/footage/b.mp4"
    slot_len = first.record_end - first.record_start
    assert first.source_start == pytest.approx(10.0)
    assert first.source_end == pytest.approx(10.0 + slot_len)
    assert second.clip_path == "/footage/a.mp4"
    assert second.source_start == pytest.approx(20.5)
    # the record grid stayed the engine's
    assert [e.record_start for e in plan.entries] == [
        e.record_start for e in baseline.entries
    ]
    assert "story: from first light to the summit" in plan.notes
    assert "act 1: open wide to establish" in plan.notes
    assert "act 2: then tighten the rhythm" in plan.notes
    assert any(
        n.startswith(f"composed by Claude: 2 of {len(plan.entries)} slots cast")
        for n in plan.notes
    )


def test_invalid_picks_fall_back_per_slot(monkeypatch):
    reply = empty_reply()
    reply["cast"] = [
        {"slot": 0, "clip": "nope.mp4", "start": 0.0},  # unknown clip
        {"slot": 1, "clip": "a.mp4", "start": 7.0},  # outside every moment
        {"slot": 2, "clip": "b.mp4", "start": 30.0},  # valid
        # slot 3+ missing entirely
    ]
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    baseline = plan_montage(make_reports(), make_music(), cut_lead=0.0)
    plan = compose_montage(make_reports(), make_music(), cut_lead=0.0)

    # invalid and missing slots keep the heuristic entries verbatim
    assert entry_tuple(plan.entries[0]) == entry_tuple(baseline.entries[0])
    assert entry_tuple(plan.entries[1]) == entry_tuple(baseline.entries[1])
    assert entry_tuple(plan.entries[3]) == entry_tuple(baseline.entries[3])
    # the valid pick landed
    assert plan.entries[2].clip_path == "/footage/b.mp4"
    assert plan.entries[2].source_start == pytest.approx(30.0)
    # ...and each fallback is noted
    assert any(
        "slot 1 kept the heuristic pick" in n and "nope.mp4" in n
        for n in plan.notes
    )
    assert any(
        "slot 2 kept the heuristic pick" in n and "outside every good moment" in n
        for n in plan.notes
    )
    assert any("slot 4 kept the heuristic pick" in n for n in plan.notes)
    assert any(
        n.startswith(f"composed by Claude: 1 of {len(plan.entries)} slots cast")
        for n in plan.notes
    )


def test_out_of_range_start_snaps_into_the_moment(monkeypatch):
    reply = empty_reply()
    # 14.2 overlaps a.mp4's 10-15 moment but leaves too little tail: the
    # window snaps back so the slot still gets its full duration.
    reply["cast"] = [{"slot": 0, "clip": "a.mp4", "start": 14.2}]
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    plan = compose_montage(make_reports(), make_music(), cut_lead=0.0)
    entry = plan.entries[0]
    slot_len = entry.record_end - entry.record_start
    assert entry.clip_path == "/footage/a.mp4"
    assert entry.source_end - entry.source_start == pytest.approx(slot_len)
    assert entry.source_start == pytest.approx(15.0 - slot_len)


def test_explicit_reuse_is_allowed_and_noted_with_repeats_on(monkeypatch):
    reply = empty_reply()
    reply["cast"] = [
        {"slot": 0, "clip": "a.mp4", "start": 10.0},
        {"slot": 1, "clip": "a.mp4", "start": 10.0},
    ]
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    plan = compose_montage(
        make_reports(), make_music(), cut_lead=0.0, allow_repeats=True
    )
    assert plan.entries[0].clip_path == plan.entries[1].clip_path == "/footage/a.mp4"
    assert plan.entries[0].source_start == pytest.approx(plan.entries[1].source_start)
    assert any("reused material in 1 slot" in n for n in plan.notes)
    assert any("same-clip cut" in n and "Claude's explicit choice" in n
               for n in plan.notes)


def test_reused_cast_is_rejected_when_repeats_off(monkeypatch):
    # Same reply, but repeats OFF (the default): the second, reused cast
    # falls back to the heuristic entry and the plan stays duplicate-free.
    reply = empty_reply()
    reply["cast"] = [
        {"slot": 0, "clip": "a.mp4", "start": 10.0},
        {"slot": 1, "clip": "a.mp4", "start": 10.0},
    ]
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    plan = compose_montage(make_reports(), make_music(), cut_lead=0.0)
    # slot 0 keeps the cast; slot 1's identical cast is rejected
    assert plan.entries[0].clip_path == "/footage/a.mp4"
    assert plan.entries[0].source_start == pytest.approx(10.0)
    assert any(
        "slot 2 kept the heuristic pick" in n and "repeats are off" in n
        for n in plan.notes
    )
    assert not any("reused material" in n for n in plan.notes)
    # zero duplicate (clip, source_start) pairs across the whole cut...
    pairs = [(e.clip_path, round(e.source_start, 3)) for e in plan.entries]
    assert len(pairs) == len(set(pairs))
    # ...and no two entries share source material at all
    for a_i, a in enumerate(plan.entries):
        for b in plan.entries[a_i + 1:]:
            assert not compose._shares_material(a, b)


def test_dossier_and_prompt_say_reuse_is_forbidden_when_repeats_off(monkeypatch):
    captured: dict = {}

    def spy(prompt, system=None, json_schema=None, on_delta=None, **kwargs):
        captured["prompt"] = prompt
        return json.dumps(empty_reply())

    monkeypatch.setattr(ai, "complete", spy)
    compose_montage(make_reports(), make_music(), cut_lead=0.0)
    assert '"reuse_forbidden": true' in captured["prompt"]
    assert "NEVER reuse a moment" in captured["prompt"]

    compose_montage(
        make_reports(), make_music(), cut_lead=0.0, allow_repeats=True
    )
    assert '"reuse_forbidden"' not in captured["prompt"]
    assert "reuse a moment only when the slot count leaves no alternative" in captured["prompt"]


# --- titles ------------------------------------------------------------------------


def compose_trailer(monkeypatch, titles):
    reply = empty_reply()
    reply["titles"] = titles
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    return compose_montage(
        make_reports(), make_arc_music(drops=[20.0]), style="trailer", cut_lead=0.0
    )


def test_titles_land_on_the_plan_and_in_titles_from_plan(monkeypatch):
    plan = compose_trailer(
        monkeypatch,
        [
            {"dip": 0, "text": "EIN SOMMER"},
            {"dip": 1, "text": "DREI FREUNDE"},
        ],
    )
    assert len(plan.dips) >= 2
    assert plan.title_texts[0] == "EIN SOMMER"
    assert plan.title_texts[1] == "DREI FREUNDE"
    assert len(plan.title_texts) == len(plan.dips)
    assert any("act title" in n for n in plan.notes)

    from monteur.resolve import titles_from_plan

    titles = titles_from_plan(plan)
    assert titles[0]["text"] == "EIN SOMMER"
    assert titles[1]["text"] == "DREI FREUNDE"
    # explicit texts still win over the plan-carried ones
    explicit = titles_from_plan(plan, texts=["X"])
    assert explicit[0]["text"] == "X"


def test_bad_title_indexes_are_ignored(monkeypatch):
    plan = compose_trailer(
        monkeypatch,
        [
            {"dip": 99, "text": "LOST"},
            {"dip": -1, "text": "ALSO LOST"},
            {"dip": 0, "text": "  KEPT  "},
            "not a dict",
        ],
    )
    assert plan.title_texts[0] == "KEPT"
    assert all(t in ("", "KEPT") for t in plan.title_texts)


def test_titles_round_trip_through_plan_json(monkeypatch):
    plan = compose_trailer(monkeypatch, [{"dip": 0, "text": "ACT ONE"}])
    data = json.loads(json.dumps(plan_to_dict(plan)))
    assert data["title_texts"][0] == "ACT ONE"
    restored = plan_from_dict(data)
    assert restored.title_texts == plan.title_texts

    # a plan without composed titles serializes exactly as before...
    plain = plan_montage(make_reports(), make_music(), cut_lead=0.0)
    assert "title_texts" not in plan_to_dict(plain)
    # ...and old JSON without the key loads with the tolerant default
    old = plan_to_dict(plain)
    assert plan_from_dict(old).title_texts == []


def test_composed_title_names_the_timeline_marker(monkeypatch):
    plan = compose_trailer(monkeypatch, [{"dip": 0, "text": "EIN SOMMER"}])
    timeline = montage_to_timeline(plan, fps=25.0)
    slots = [m for m in timeline.markers if m.name == "Title slot"]
    assert slots and "title: EIN SOMMER" in slots[0].note
    # dips without a composed text keep the old derivation
    assert all("title: EIN SOMMER" not in m.note for m in slots[1:])


# --- the CLI surface -----------------------------------------------------------------


def test_create_parses_ai_cut_flag():
    from monteur.cli import build_parser, cmd_create

    args = build_parser().parse_args(
        ["create", "clips", "song.mp3", "-o", "out.fcpxml", "--ai-cut"]
    )
    assert args.ai_cut is True
    assert args.func is cmd_create


def test_create_ai_cut_defaults_off():
    from monteur.cli import build_parser

    args = build_parser().parse_args(
        ["create", "clips", "song.mp3", "-o", "out.fcpxml"]
    )
    assert args.ai_cut is False


def test_cmd_create_routes_through_compose(tmp_path, monkeypatch, capsys):
    """--ai-cut sends the planning through compose_montage (monkeypatched)
    and prints the composer's story/act notes with the other plan notes."""
    import monteur.brief
    import monteur.music
    import monteur.sift
    from monteur.cli import build_parser

    reports = make_reports()
    music = make_music()
    monkeypatch.setattr(monteur.sift, "list_media", lambda folder: ["a.mp4", "b.mp4"])
    monkeypatch.setattr(
        monteur.sift, "sift_directory", lambda folder, progress=None: reports
    )
    monkeypatch.setattr(monteur.music, "analyze_music", lambda path: music)
    # --brief normally also derives style/order/length (possibly via the AI
    # backend) — stub it so this test only exercises the compose routing.
    monkeypatch.setattr(
        monteur.brief,
        "resolve_brief",
        lambda text: monteur.brief.BriefSettings(rationale="stubbed"),
    )

    calls = []

    def fake_compose(reports_, music_, **kwargs):
        calls.append(kwargs)
        plan = plan_montage(reports_, music_, cut_lead=0.0)
        plan.notes.append("story: ein Sommer in drei Akten")
        plan.notes.append("act 1: still beginnen")
        return plan

    monkeypatch.setattr("monteur.compose.compose_montage", fake_compose)

    out = tmp_path / "cut.fcpxml"
    args = build_parser().parse_args(
        [
            "create", str(tmp_path), "song.mp3", "-o", str(out),
            "--ai-cut", "--brief", "Alpen, episch, schnell",
        ]
    )
    args.func(args)

    assert len(calls) == 1
    assert calls[0]["brief"] == "Alpen, episch, schnell"
    assert calls[0]["style"] == "auto"
    assert out.exists()
    printed = capsys.readouterr().out
    assert "story: ein Sommer in drei Akten" in printed
    assert "act 1: still beginnen" in printed


# --- time-of-day (daylight) in the dossier and the prompt --------------------------


def test_dossier_carries_daylight_and_prompt_states_the_law(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    reports = make_reports()
    reports[0].moments[0].daylight = "day"
    reports[1].moments[0].daylight = "night"
    compose_montage(reports, make_music(), cut_lead=0.0)
    prompt = calls[0]["prompt"]
    assert '"daylight": "day"' in prompt
    assert '"daylight": "night"' in prompt
    # The coherence law + the block order as the composer's decision.
    assert "COHERENCE IS THE LAW" in prompt
    assert "day -> golden -> night" in prompt
    assert "say why in `why`" in prompt


def test_editor_clip_notes_reach_the_dossier_and_prompt(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    reports = make_reports()
    reports[0].user_note = "this is the hero shot — open the film on it"
    compose_montage(reports, make_music(), cut_lead=0.0)
    prompt = calls[0]["prompt"]
    # the note rides into the dossier keyed by clip name, and the prompt tells
    # Claude to weight it above the machine labels
    assert "clip_notes" in prompt
    assert "this is the hero shot" in prompt
    assert "THE EDITOR'S CLIP NOTES" in prompt


def test_compose_context_omits_clip_notes_when_none(monkeypatch):
    from monteur.compose import compose_context

    reports = make_reports()
    plan = plan_montage(reports, make_music(), cut_lead=0.0)
    context = compose_context(plan, reports, make_music())
    assert "clip_notes" not in context  # only-when-present


def test_editor_moment_notes_reach_the_matching_inventory_item_and_prompt(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    reports = make_reports()
    # a note on ONE moment — the finest steer there is
    target = reports[0].moments[0]
    target.user_note = "open on this — THE hero beat"
    compose_montage(reports, make_music(), cut_lead=0.0)
    prompt = calls[0]["prompt"]
    context = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])
    noted = [i for i in context["inventory"] if i.get("note")]
    assert len(noted) == 1  # only the annotated moment carries a note
    assert noted[0]["note"] == "open on this — THE hero beat"
    assert noted[0]["start"] == round(target.start, 2)  # on the RIGHT moment
    # ...and the prompt tells Claude to weight it above every machine label
    assert "THE EDITOR'S MOMENT NOTES" in prompt


def test_compose_context_omits_moment_note_when_none():
    from monteur.compose import compose_context

    reports = make_reports()
    plan = plan_montage(reports, make_music(), cut_lead=0.0)
    context = compose_context(plan, reports, make_music())
    assert not any("note" in i for i in context["inventory"])  # only-when-present


def test_editor_moment_rating_reaches_the_inventory_and_prompt(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    reports = make_reports()
    reports[0].moments[0].user_rating = 5  # the editor loves this beat
    compose_montage(reports, make_music(), cut_lead=0.0)
    prompt = calls[0]["prompt"]
    context = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])
    rated = [i for i in context["inventory"] if i.get("rating")]
    assert len(rated) == 1 and rated[0]["rating"] == 5
    assert "THE EDITOR'S MOMENT RATINGS" in prompt


def test_compose_context_omits_moment_rating_when_none():
    from monteur.compose import compose_context

    reports = make_reports()
    plan = plan_montage(reports, make_music(), cut_lead=0.0)
    context = compose_context(plan, reports, make_music())
    assert not any("rating" in i for i in context["inventory"])


# --- composer-driven breathing holds (contextual pacing) ----------------------


def _hold_plan(n=6, clip_dur=60.0):
    """N contiguous 1s shots from one long clip — a clean bed for _apply_holds."""
    from monteur.montage import MontageEntry, MontagePlan

    entries = [
        MontageEntry(
            clip_path="ride.mp4",
            source_start=float(i), source_end=float(i) + 1.0,
            record_start=float(i), record_end=float(i) + 1.0,
            score=0.5, clip_duration=clip_dur,
        )
        for i in range(n)
    ]
    return MontagePlan(music_path="song.mp3", duration=float(n), entries=entries)


def test_apply_holds_absorbs_the_next_shots_keeping_total_length():
    from monteur.compose import _apply_pacing

    plan = _hold_plan(6)  # six 1s shots, total 6s
    _apply_pacing(plan, None, [{"slot": 1, "seconds": 3.0}])
    assert len(plan.entries) == 4  # slot 1 absorbed two neighbours to reach 3s
    assert plan.entries[0].record_end == 1.0  # slot 0 untouched
    held = plan.entries[1]
    assert held.record_start == 1.0 and held.record_end == 4.0  # a 3s hold
    assert held.source_end == pytest.approx(held.source_start + 3.0)  # source stretched
    assert plan.entries[-1].record_end == 6.0  # TOTAL length unchanged
    # the timeline stays perfectly contiguous (no shot moved)
    for a, b in zip(plan.entries, plan.entries[1:]):
        assert a.record_end == pytest.approx(b.record_start)
    assert any("breathing" in n for n in plan.notes)


def test_apply_holds_never_crosses_a_dip():
    from monteur.compose import _apply_pacing

    plan = _hold_plan(6)
    plan.dips = [(2.0, 0.5)]  # a black title dip at the slot-1/2 boundary
    _apply_pacing(plan, None, [{"slot": 1, "seconds": 4.0}])
    assert len(plan.entries) == 6  # the hold can't swallow the dip -> no-op


def test_apply_holds_respects_the_source_footage():
    from monteur.compose import _apply_pacing

    # slot 1 starts at 1.0s of a 2.5s clip -> only 1.5s of footage to hold on
    plan = _hold_plan(6, clip_dur=2.5)
    _apply_pacing(plan, None, [{"slot": 1, "seconds": 5.0}])
    assert len(plan.entries) == 6  # can't stretch past the clip -> no-op


def test_apply_holds_never_touches_a_locked_slot():
    from monteur.compose import _apply_pacing

    plan = _hold_plan(6)
    _apply_pacing(plan, None, [{"slot": 1, "seconds": 3.0}], locked={1})
    assert len(plan.entries) == 6  # the editor's arrangement is final


def test_apply_holds_caps_at_the_max_cut_ceiling():
    from monteur import montage as _m
    from monteur.compose import _apply_pacing

    plan = _hold_plan(20)  # twenty 1s shots
    _apply_pacing(plan, None, [{"slot": 0, "seconds": 999.0}])  # absurd target
    assert plan.entries[0].record_end <= _m._MAX_CUT_SECONDS + 1e-6


def test_prompt_and_schema_offer_breathing_holds():
    from monteur.compose import COMPOSE_SCHEMA, _build_prompt, compose_context

    assert "holds" in COMPOSE_SCHEMA["properties"]
    assert "pace" in COMPOSE_SCHEMA["properties"]
    assert "pace_by_phase" in COMPOSE_SCHEMA["properties"]
    reports = make_reports()
    plan = plan_montage(reports, make_music(), cut_lead=0.0)
    prompt = _build_prompt(compose_context(plan, reports, make_music()), "auto", "")
    assert "PACING" in prompt and "hold" in prompt.lower()
    assert "pace_by_phase" in prompt  # the variable-speed lever is offered


def test_compose_forwards_the_composers_pace_and_holds(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "monteur.compose._apply_pacing",
        lambda plan, pace, holds, locked=frozenset(), pace_by_phase=None: captured.update(
            {"pace": pace, "holds": holds, "pace_by_phase": pace_by_phase}
        ),
    )
    reply = empty_reply()
    reply["pace"] = 4.0
    reply["holds"] = [{"slot": 2, "seconds": 6.0}]
    reply["pace_by_phase"] = {"opening": 6.0, "climax": 1.0}
    monkeypatch.setattr(ai, "complete", fake_complete(reply))
    compose_montage(make_reports(), make_music(), cut_lead=0.0)
    assert captured["pace"] == 4.0
    assert captured["holds"] == [{"slot": 2, "seconds": 6.0}]
    assert captured["pace_by_phase"] == {"opening": 6.0, "climax": 1.0}


def test_apply_pacing_slows_the_whole_cut_keeping_total_length():
    # a global pace re-paces EVERY shot (a landscape trailer that must breathe),
    # not just a couple — fewer, longer shots over the SAME length
    from monteur.compose import _apply_pacing

    plan = _hold_plan(12)  # twelve 1s shots, total 12s
    _apply_pacing(plan, 3.0, [])
    assert len(plan.entries) == 4  # ~3s shots throughout
    assert all(
        e.record_end - e.record_start == pytest.approx(3.0) for e in plan.entries
    )
    assert plan.entries[-1].record_end == 12.0  # total length unchanged
    assert any("re-paced" in n for n in plan.notes)


def test_apply_pacing_holds_extend_beyond_the_base_pace():
    from monteur.compose import _apply_pacing

    plan = _hold_plan(12)
    _apply_pacing(plan, 2.0, [{"slot": 0, "seconds": 4.0}])
    # slot 0 holds 4s (its own hold), the rest fall on the 2s base
    assert plan.entries[0].record_end == pytest.approx(4.0)
    assert plan.entries[1].record_end - plan.entries[1].record_start == pytest.approx(2.0)


def test_apply_pacing_varies_speed_by_phase_slow_fast_slow():
    # Maik's trailer: long 4s opening scenes -> a fast 1s climax -> long 4s
    # outro, all from per-phase pace. The arc stays crisp — a long opening
    # shot never bleeds across the boundary into the fast climax.
    from monteur.compose import _apply_pacing

    plan = _hold_plan(12)  # twelve 1s shots, total 12s
    plan.phases = [(0.0, 4.0, "opening"), (4.0, 8.0, "climax"), (8.0, 12.0, "outro")]
    _apply_pacing(
        plan, None, [], pace_by_phase={"opening": 4.0, "climax": 1.0, "outro": 4.0}
    )
    # opening: four 1s shots -> one 4s shot; climax: four fast 1s shots stay;
    # outro: four 1s shots -> one 4s shot => 6 entries.
    assert len(plan.entries) == 6
    assert plan.entries[0].record_start == 0.0 and plan.entries[0].record_end == 4.0
    fast = plan.entries[1:5]
    assert all(
        e.record_end - e.record_start == pytest.approx(1.0) for e in fast
    )  # the climax races
    assert plan.entries[-1].record_start == 8.0 and plan.entries[-1].record_end == 12.0
    assert plan.entries[-1].record_end == 12.0  # total length unchanged
    for a, b in zip(plan.entries, plan.entries[1:]):
        assert a.record_end == pytest.approx(b.record_start)  # still contiguous
    assert any("varies by phase" in n for n in plan.notes)


def test_apply_pacing_by_phase_falls_back_to_pace_for_omitted_phases():
    # a phase not named in pace_by_phase uses the flat `pace`.
    from monteur.compose import _apply_pacing

    plan = _hold_plan(12)
    plan.phases = [(0.0, 4.0, "opening"), (4.0, 8.0, "climax"), (8.0, 12.0, "outro")]
    # only the climax is named fast; opening + outro fall back to a 4s base
    _apply_pacing(plan, 4.0, [], pace_by_phase={"climax": 1.0})
    assert plan.entries[0].record_end == 4.0  # opening slowed by the base pace
    climax = [e for e in plan.entries if 4.0 <= e.record_start < 8.0]
    assert all(e.record_end - e.record_start == pytest.approx(1.0) for e in climax)
    assert plan.entries[-1].record_end == 12.0


def test_prompt_has_no_daylight_lines_without_classes(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ai, "complete", fake_complete(empty_reply(), calls))
    compose_montage(make_reports(), make_music(), cut_lead=0.0)
    prompt = calls[0]["prompt"]
    assert "COHERENCE IS THE LAW" not in prompt
    assert '"daylight"' not in prompt


def test_compose_context_omits_empty_daylight():
    from monteur.compose import compose_context

    reports = make_reports()
    reports[0].moments[0].daylight = "golden"
    plan = plan_montage(reports, make_music(), cut_lead=0.0)
    context = compose_context(plan, reports, make_music())
    flagged = [i for i in context["inventory"] if "daylight" in i]
    assert len(flagged) == 1
    assert flagged[0]["daylight"] == "golden"
