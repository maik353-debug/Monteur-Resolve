"""Tests for the version-to-version change list (monteur.changelist)."""

from __future__ import annotations

from monteur import changelist


def _entry(clip, rec_start, dur=2.0, src_start=0.0, transition=0.0):
    return {
        "clip_path": clip,
        "source_start": src_start,
        "source_end": src_start + dur,
        "record_start": rec_start,
        "record_end": rec_start + dur,
        "transition": transition,
        "score": 0.9,
    }


def _plan(entries, duration=None, tempo=0.0):
    total = duration if duration is not None else (max((e["record_end"] for e in entries), default=0.0))
    return {"monteur_plan": 1, "music_path": "m.mp3", "duration": total, "entries": entries, "tempo": tempo}


def _kinds(cl):
    return sorted(c.kind for c in cl.changes)


def test_no_changes_between_identical_plans():
    plan = _plan([_entry("a.mp4", 0), _entry("b.mp4", 2)])
    cl = changelist.diff_plans(plan, plan)
    assert cl.changes == []
    assert "No editorial changes" in changelist.format_change_list(cl)


def test_added_shot():
    old = _plan([_entry("a.mp4", 0)])
    new = _plan([_entry("a.mp4", 0), _entry("b.mp4", 2)])
    cl = changelist.diff_plans(old, new)
    assert cl.added == 1 and cl.removed == 0
    assert any(c.kind == "added" and "b.mp4" in c.summary for c in cl.changes)


def test_removed_shot():
    old = _plan([_entry("a.mp4", 0), _entry("b.mp4", 2)])
    new = _plan([_entry("a.mp4", 0)])
    cl = changelist.diff_plans(old, new)
    assert cl.removed == 1
    assert any(c.kind == "removed" and "b.mp4" in c.summary for c in cl.changes)


def test_pure_ripple_shift_is_not_reported():
    # inserting a shot pushes b later; b itself didn't change -> only "added"
    old = _plan([_entry("a.mp4", 0), _entry("b.mp4", 2)])
    new = _plan([_entry("a.mp4", 0), _entry("c.mp4", 2), _entry("b.mp4", 4)])
    cl = changelist.diff_plans(old, new)
    assert _kinds(cl) == ["added", "length"]  # b is matched & unchanged, not "moved"


def test_retrim_detected():
    old = _plan([_entry("a.mp4", 0, dur=2.0, src_start=0.0)])
    new = _plan([_entry("a.mp4", 0, dur=2.0, src_start=1.0)])  # same clip, later in-point
    cl = changelist.diff_plans(old, new)
    assert any(c.kind == "trimmed" and "a.mp4" in c.summary for c in cl.changes)


def test_retime_detected():
    old = _plan([_entry("a.mp4", 0, dur=2.0)])
    new_entry = _entry("a.mp4", 0, dur=2.0)
    new_entry["record_end"] = 3.5  # same source in, longer on the timeline
    new_entry["source_end"] = new_entry["source_start"] + 2.0  # in/out unchanged
    cl = changelist.diff_plans(old, _plan([new_entry]))
    kinds = _kinds(cl)
    assert "retimed" in kinds and "trimmed" not in kinds
    assert any("longer" in c.summary for c in cl.changes)


def test_transition_flip_detected():
    old = _plan([_entry("a.mp4", 0, transition=0.0)])
    new = _plan([_entry("a.mp4", 0, transition=0.5)])
    cl = changelist.diff_plans(old, new)
    assert any(c.kind == "transition" and "dissolve" in c.summary for c in cl.changes)


def test_length_and_tempo_changes():
    old = _plan([_entry("a.mp4", 0)], duration=10.0, tempo=120.0)
    new = _plan([_entry("a.mp4", 0)], duration=14.0, tempo=128.0)
    cl = changelist.diff_plans(old, new)
    kinds = _kinds(cl)
    assert "length" in kinds and "tempo" in kinds
    # plan-level changes sort to the end
    assert cl.changes[-1].kind in ("length", "tempo")


def test_format_lists_every_change():
    old = _plan([_entry("a.mp4", 0), _entry("b.mp4", 2)])
    new = _plan([_entry("a.mp4", 0), _entry("c.mp4", 2)])
    text = changelist.format_change_list(changelist.diff_plans(old, new),
                                         old_label="v1", new_label="v2")
    assert "v1 -> v2" in text
    assert "Added c.mp4" in text and "Removed b.mp4" in text
