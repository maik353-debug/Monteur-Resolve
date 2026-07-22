"""Change list between two cut versions — for sound / VFX handoffs.

Diffs two ``MontagePlan`` dicts (exactly what the project version history
stores) into a plain, human-readable list of what changed between one cut and
the next: shots ADDED, REMOVED, TRIMMED (in/out moved), RETIMED (duration
changed) or their TRANSITION flipped (hard cut <-> dissolve), plus plan-level
LENGTH and TEMPO. That's the handoff a sound editor or VFX artist needs — "what
do I have to redo against the new cut?".

Pure stdlib and deterministic, so the same two versions always produce the same
list. Pure timeline-position shifts (a shot sliding later only because an
earlier shot was inserted) are intentionally NOT reported — they're ripple
consequences, not editorial decisions, and would bury the real changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

#: seconds of slack below which two times count as equal (a frame at 25fps is
#: 0.04s; 0.05 keeps rounding noise out without hiding a real trim)
_EPS = 0.05



@dataclass
class Change:
    kind: str          # added | removed | trimmed | retimed | transition | length | tempo
    at: float          # record position in the NEW cut (seconds; 0 for plan-level)
    clip: str          # clip file name ("" for plan-level)
    summary: str       # one human-readable line

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChangeList:
    changes: list[Change] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(c.kind == "added" for c in self.changes)

    @property
    def removed(self) -> int:
        return sum(c.kind == "removed" for c in self.changes)

    def to_dict(self) -> dict:
        return {"changes": [c.to_dict() for c in self.changes]}


def _entries(plan: dict) -> list[dict]:
    raw = plan.get("entries") if isinstance(plan, dict) else None
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _clip_name(path: str) -> str:
    path = str(path or "")
    for sep in ("/", "\\"):
        if sep in path:
            path = path.rsplit(sep, 1)[1]
    return path


def _mmss(seconds: float) -> str:
    total = int(round(max(0.0, float(seconds or 0.0))))
    return f"{total // 60}:{total % 60:02d}"


def _num(entry: dict, key: str) -> float:
    try:
        return float(entry.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_dissolve(entry: dict) -> bool:
    return _num(entry, "transition") > _EPS


def _overlap(a: dict, b: dict) -> float:
    """Seconds the two entries' SOURCE ranges overlap (0 if disjoint)."""
    lo = max(_num(a, "source_start"), _num(b, "source_start"))
    hi = min(_num(a, "source_end"), _num(b, "source_end"))
    return max(0.0, hi - lo)


def _match(old_entries: list[dict], new_entries: list[dict]) -> tuple[list, list[dict], list[dict]]:
    """Greedy shot matching by clip + SOURCE-range overlap.

    Two entries are the same shot (possibly re-trimmed) when they come from the
    same clip and their source ranges overlap; a re-trim keeps overlap, a
    different moment from the same clip does not. Returns (pairs, added,
    removed).
    """
    remaining = list(old_entries)
    pairs: list[tuple[dict, dict]] = []
    added: list[dict] = []
    for new in new_entries:
        clip = str(new.get("clip_path") or "")
        best = None
        best_ov = 0.0
        for old in remaining:
            if str(old.get("clip_path") or "") != clip:
                continue
            ov = _overlap(old, new)
            if ov > best_ov:
                best, best_ov = old, ov
        if best is not None:
            pairs.append((best, new))
            remaining.remove(best)
        else:
            added.append(new)
    return pairs, added, remaining


def diff_plans(old: dict, new: dict) -> ChangeList:
    """The change list turning ``old`` into ``new`` (both plan dicts)."""
    old_e, new_e = _entries(old), _entries(new)
    pairs, added, removed = _match(old_e, new_e)
    changes: list[Change] = []

    for entry in added:
        changes.append(Change(
            "added", _num(entry, "record_start"), _clip_name(entry.get("clip_path")),
            f"Added {_clip_name(entry.get('clip_path'))} at {_mmss(_num(entry, 'record_start'))}",
        ))
    for entry in removed:
        changes.append(Change(
            "removed", _num(entry, "record_start"), _clip_name(entry.get("clip_path")),
            f"Removed {_clip_name(entry.get('clip_path'))} (was at {_mmss(_num(entry, 'record_start'))})",
        ))

    for old_en, new_en in pairs:
        name = _clip_name(new_en.get("clip_path"))
        at = _num(new_en, "record_start")
        # trim: source in/out moved -> the frames used changed
        din = _num(new_en, "source_start") - _num(old_en, "source_start")
        dout = _num(new_en, "source_end") - _num(old_en, "source_end")
        if abs(din) > _EPS or abs(dout) > _EPS:
            changes.append(Change("trimmed", at, name, f"Re-trimmed {name} at {_mmss(at)}"))
        else:
            # retime: same in/out but the on-timeline duration changed
            old_dur = _num(old_en, "record_end") - _num(old_en, "record_start")
            new_dur = _num(new_en, "record_end") - _num(new_en, "record_start")
            if abs(new_dur - old_dur) > _EPS:
                verb = "longer" if new_dur > old_dur else "shorter"
                changes.append(Change("retimed", at, name,
                                      f"{name} is {abs(new_dur - old_dur):.1f}s {verb} at {_mmss(at)}"))
        # transition flip: hard cut <-> dissolve (matters for sound + VFX)
        if _is_dissolve(old_en) != _is_dissolve(new_en):
            now = "a dissolve" if _is_dissolve(new_en) else "a hard cut"
            changes.append(Change("transition", at, name, f"{name} is now {now} at {_mmss(at)}"))

    # plan-level: total length + tempo
    old_dur, new_dur = _num(old, "duration"), _num(new, "duration")
    if abs(new_dur - old_dur) > 0.25:
        verb = "longer" if new_dur > old_dur else "shorter"
        changes.append(Change("length", 0.0, "",
                              f"Cut is {abs(new_dur - old_dur):.1f}s {verb} ({_mmss(old_dur)} -> {_mmss(new_dur)})"))
    old_bpm, new_bpm = _num(old, "tempo"), _num(new, "tempo")
    if old_bpm and new_bpm and abs(new_bpm - old_bpm) > 0.5:
        changes.append(Change("tempo", 0.0, "", f"Tempo {old_bpm:.0f} -> {new_bpm:.0f} BPM"))

    # editorial changes first (by position), plan-level last
    changes.sort(key=lambda c: (c.at == 0.0 and c.kind in ("length", "tempo"), c.at))
    return ChangeList(changes=changes)


def format_change_list(cl: ChangeList, *, old_label: str = "previous", new_label: str = "current") -> str:
    """A plain-text handoff note (the same shape as an EDL change memo)."""
    if not cl.changes:
        return f"No editorial changes between {old_label} and {new_label}."
    lines = [f"Changes: {old_label} -> {new_label}",
             f"  {cl.added} added, {cl.removed} removed, "
             f"{len(cl.changes) - cl.added - cl.removed} other", ""]
    lines.extend(f"  - {c.summary}" for c in cl.changes)
    return "\n".join(lines)
