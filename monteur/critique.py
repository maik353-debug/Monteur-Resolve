"""Self-critique: Monteur watches its own cut (blueprint Wave 4.1).

Until Wave 4 the montage engine built ONE cut and hoped it landed. This
module is the "watch" half of the closed loop: it scores a finished
:class:`monteur.montage.MontagePlan` against EACH acceptance metric the
earlier waves committed to — deterministically, purely from the plan
(plus, when a real export measured it, the integrated loudness), with no
video re-decode. The refine loop (:func:`monteur.montage.refine_plan`)
reads the scorecard to decide which knob to turn next; the Studio can
show it as an honest report card.

The metrics, one per acceptance line:

* **coincidence** (1.1) — the picture peak lands within ±0.25 s of its
  cut. Read from each entry's in-memory ``peak_source`` (the cast
  moment's ``peak_time``, in file coordinates): mapped into record time
  it must sit within :data:`_COINCIDENCE_TOL` of the slot's own cut. A
  peak that never reaches the screen is an honest miss. HARD (sync is
  the house's first promise); the pass bar is
  :data:`_COINCIDENCE_MIN_RATE`.
* **silence** (1.2) — every deliberate ``music_gap`` is CARRIED: an
  SFX carrier cue (sub-drop/impact) sits under it, or it resolves on a
  drop (the pre-drop beat, re-entry on the hit). An uncarried silence
  would be an accident; there must be none. HARD.
* **slivers** (1.7) — no produced slot is shorter than
  :data:`~monteur.montage._MIN_SLOT_SECONDS` (~0.3 s): entries AND black
  dips. HARD.
* **loudness** (1.4) — only scored when a real export handed us its
  measured integrated loudness: within ±:data:`_LOUDNESS_TOL` LU of the
  −14 LUFS target. HARD when present, absent otherwise (never guessed).
* **drops** (1.5/2.1) — how many of the plan's drop marks a cut actually
  hits (±:data:`_DROP_TOL`). SOFT (styles pin only the climax and strong
  secondaries; the metric reports, it does not gate).
* **grammar** (3.2) — how many adjacent slots share a shot size (the
  eye wants scale to keep changing). SOFT tie-breaker, only live when the
  plan carries shot-size classes.

A :class:`Metric` is a value + a pass/fail + the slots at fault. A
:class:`Scorecard` bundles them, separates HARD (acceptance) from SOFT
(polish), and exposes an ``aggregate`` in ``[0, 1]`` the refine loop
maximises. Everything here is pure and offline — the same plan always
scores the same card.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from monteur.montage import _MIN_SLOT_SECONDS, MontagePlan
from monteur.preview import _LOUDNORM_I

_EPS = 1e-6

#: Peak-on-beat honesty (1.1): the blueprint's ±0.25 s, not one frame.
_COINCIDENCE_TOL = 0.25
#: The pass bar for the coincidence rate (blueprint/test_peak: ≥ 80 %).
_COINCIDENCE_MIN_RATE = 0.8
#: A cut counts as hitting a drop mark within this many seconds (~2 frames
#: at 25 fps; both cut and mark are rounded to 2 decimals in the plan).
_DROP_TOL = 0.12
#: Integrated-loudness tolerance around −14 LUFS (blueprint 1.4/Abnahme).
_LOUDNESS_TOL = 1.0
#: Carrier search tolerance for a silence's SFX cue (mirrors the planner's
#: own :data:`monteur.montage._GAP_CARRIER_TOLERANCE`).
_CARRIER_TOL = 0.5


@dataclass
class Metric:
    """One acceptance metric's verdict.

    ``value`` is the measured number (a rate in ``[0, 1]``, a count, or a
    dB figure — ``unit`` says which); ``passed`` is the pass/fail against
    this metric's own bar; ``culprits`` are the 0-based slot indices at
    fault (empty for a global metric like loudness); ``hard`` marks an
    acceptance gate (vs a soft polish tie-breaker); ``detail`` is one
    honest line for the notes / the Studio card.
    """

    name: str
    value: float
    passed: bool
    unit: str = ""
    detail: str = ""
    culprits: list[int] = field(default_factory=list)
    hard: bool = True
    #: How many slots the rate was measured over (0 = not applicable, the
    #: metric is vacuously passed and does not weigh on the aggregate).
    sample: int = 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": round(float(self.value), 4),
            "passed": bool(self.passed),
            "unit": self.unit,
            "detail": self.detail,
            "culprits": list(self.culprits),
            "hard": bool(self.hard),
            "sample": int(self.sample),
        }


@dataclass
class Scorecard:
    """The full self-critique of one plan.

    ``metrics`` is name -> :class:`Metric`. :meth:`passed` is True when
    every HARD metric passes (the acceptance gate). :meth:`aggregate`
    scores the card in ``[0, 1]`` — the mean of the metrics that carry a
    sample, hard metrics double-weighted so the refine loop always favours
    sync/silence/slivers/loudness over grammar polish. Deterministic:
    equal plans produce equal cards.
    """

    metrics: dict[str, Metric] = field(default_factory=dict)

    def passed(self) -> bool:
        """True when every applicable HARD metric passes (acceptance)."""
        return all(m.passed for m in self.metrics.values() if m.hard and m.sample)

    def failing(self) -> list[Metric]:
        """The applicable metrics that failed, hard ones first (loop order)."""
        bad = [m for m in self.metrics.values() if m.sample and not m.passed]
        bad.sort(key=lambda m: (not m.hard, m.name))
        return bad

    def aggregate(self) -> float:
        """A single ``[0, 1]`` score the refine loop maximises.

        Each applicable metric contributes its ``score`` in ``[0, 1]``
        (a rate is itself, a boolean gate is 1.0/0.0, loudness decays with
        distance from target); hard metrics weigh double. No applicable
        metric (a hand-built plan with nothing to measure) scores a
        neutral 1.0 so refine never prefers noise to it.
        """
        total = 0.0
        weight = 0.0
        for m in self.metrics.values():
            if not m.sample:
                continue
            w = 2.0 if m.hard else 1.0
            total += w * _metric_score(m)
            weight += w
        return total / weight if weight > _EPS else 1.0

    def as_dict(self) -> dict:
        return {
            "passed": self.passed(),
            "aggregate": round(self.aggregate(), 4),
            "metrics": {name: m.as_dict() for name, m in self.metrics.items()},
        }


def _metric_score(m: Metric) -> float:
    """A metric's contribution to the aggregate, in ``[0, 1]``."""
    if m.name == "coincidence" or m.name == "drops":
        return max(0.0, min(1.0, m.value))
    if m.name == "silence":
        return max(0.0, min(1.0, m.value))
    if m.name == "slivers":
        # value is the sliver COUNT; every sliver is a fail.
        return 1.0 if m.value <= _EPS else 0.0
    if m.name == "grammar":
        # value is the equal-neighbour RATE (lower is better).
        return max(0.0, 1.0 - m.value)
    if m.name == "loudness":
        return max(0.0, 1.0 - abs(m.value - _LOUDNORM_I) / max(_LOUDNESS_TOL, _EPS))
    return 1.0 if m.passed else 0.0


def _coincidence(plan: MontagePlan) -> Metric:
    """Peak-on-beat rate (1.1) from the entries' in-memory ``peak_source``.

    An interior slot whose cast moment carries a peak (``peak_source >=
    0``) is a sample. The peak's record time is
    ``record_start + (peak_source - source_start)``; a peak off the slot's
    own source window never reaches the screen and is a miss. A hit lands
    within ±:data:`_COINCIDENCE_TOL` of the slot's cut. Slots without a
    peak signal (or the first slot, which opens ON its beat with no lead)
    do not count — an honest denominator.
    """
    hits = 0
    total = 0
    culprits: list[int] = []
    for i, e in enumerate(plan.entries):
        if e.record_start <= _EPS:  # the opening slot has no lead to score
            continue
        peak = getattr(e, "peak_source", -1.0)
        if peak is None or peak < 0:
            continue
        total += 1
        on_screen = e.source_start - _EPS <= peak <= e.source_end + _EPS
        peak_record = e.record_start + (peak - e.source_start)
        if on_screen and abs(peak_record - e.record_start) <= _COINCIDENCE_TOL + _EPS:
            hits += 1
        else:
            culprits.append(i)
    rate = hits / total if total else 1.0
    return Metric(
        name="coincidence",
        value=rate,
        passed=total == 0 or rate >= _COINCIDENCE_MIN_RATE - _EPS,
        unit="rate",
        detail=(
            f"picture peak on the beat in {hits}/{total} slots"
            if total
            else "no peak signal to score"
        ),
        culprits=culprits,
        hard=True,
        sample=total,
    )


def _silence(plan: MontagePlan) -> Metric:
    """Silence honesty (1.2): every ``music_gap`` is carried.

    A gap is carried when an SFX carrier cue (sub-drop/impact) overlaps it
    within tolerance, or it resolves on a drop mark (the pre-drop beat,
    re-entry on the hit). An uncarried silence is an accident — a fail
    naming the gap. No gaps ⇒ nothing to prove (not applicable).
    """
    from monteur.montage import _GAP_CARRIER_KINDS

    gaps = plan.music_gaps
    carried = 0
    culprits: list[int] = []
    drops = sorted(plan.drop_marks)
    for gi, (lo, hi) in enumerate(gaps):
        sfx_carrier = any(
            cue.kind in _GAP_CARRIER_KINDS
            and cue.time <= hi + _CARRIER_TOL + _EPS
            and cue.time + max(cue.duration, 0.0) >= lo - _CARRIER_TOL - _EPS
            for cue in plan.sfx
        )
        drop_carrier = any(abs(hi - d) <= _DROP_TOL + _EPS for d in drops)
        if sfx_carrier or drop_carrier:
            carried += 1
        else:
            culprits.append(gi)
    total = len(gaps)
    rate = carried / total if total else 1.0
    return Metric(
        name="silence",
        value=rate,
        passed=total == 0 or carried == total,
        unit="rate",
        detail=(
            f"{carried}/{total} deliberate silences carried"
            if total
            else "no deliberate silences"
        ),
        culprits=culprits,
        hard=True,
        sample=total,
    )


def _slivers(plan: MontagePlan) -> Metric:
    """Sliver freedom (1.7): no produced slot (entry or dip) below ~0.3 s."""
    culprits: list[int] = []
    for i, e in enumerate(plan.entries):
        if e.record_end - e.record_start < _MIN_SLOT_SECONDS - _EPS:
            culprits.append(i)
    dip_slivers = sum(
        1 for _, length in plan.dips if length < _MIN_SLOT_SECONDS - _EPS
    )
    count = len(culprits) + dip_slivers
    sample = len(plan.entries) + len(plan.dips)
    return Metric(
        name="slivers",
        value=float(count),
        passed=count == 0,
        unit="count",
        detail=(
            f"{count} slot{'s' if count != 1 else ''} below "
            f"{_MIN_SLOT_SECONDS:g}s"
            if count
            else "no slivers"
        ),
        culprits=culprits,
        hard=True,
        sample=sample,
    )


def _drops(plan: MontagePlan) -> Metric:
    """Drop hits (1.5/2.1): drop marks a cut lands on, ±:data:`_DROP_TOL`.

    Soft: styles pin only the climax (and strong secondaries), so an
    unhit secondary drop is not a defect — the metric reports the ratio,
    it does not gate the acceptance.
    """
    drops = sorted(plan.drop_marks)
    cuts = [e.record_start for e in plan.entries]
    hit = 0
    culprits: list[int] = []
    for di, d in enumerate(drops):
        if any(abs(c - d) <= _DROP_TOL + _EPS for c in cuts):
            hit += 1
        else:
            culprits.append(di)
    total = len(drops)
    rate = hit / total if total else 1.0
    return Metric(
        name="drops",
        value=rate,
        passed=total == 0 or hit == total,
        unit="rate",
        detail=(
            f"{hit}/{total} drop marks land on a cut"
            if total
            else "no drop marks"
        ),
        culprits=culprits,
        hard=False,
        sample=total,
    )


def _grammar(plan: MontagePlan) -> Metric:
    """Shot-grammar violations (3.2): adjacent slots sharing a shot size.

    Soft, and only live when the plan carries shot-size classes (the
    offline spatial signal). ``value`` is the equal-neighbour RATE over
    the scoreable boundaries — the eye wants scale to keep changing, so
    lower is better. Culprits name the incoming slot of each equal pair.
    """
    sizes = [(i, getattr(e, "shot_size", "")) for i, e in enumerate(plan.entries)]
    pairs = 0
    equal = 0
    culprits: list[int] = []
    for (pi, ps), (ci, cs) in zip(sizes, sizes[1:]):
        if not ps or not cs:
            continue
        pairs += 1
        if ps == cs:
            equal += 1
            culprits.append(ci)
    rate = equal / pairs if pairs else 0.0
    return Metric(
        name="grammar",
        value=rate,
        passed=equal == 0,
        unit="rate",
        detail=(
            f"{equal}/{pairs} neighbour pairs share a shot size"
            if pairs
            else "no shot-size signal"
        ),
        culprits=culprits,
        hard=False,
        sample=pairs,
    )


def _loudness(measured_lufs: float) -> Metric:
    """Integrated loudness (1.4): within ±1 LU of −14 LUFS."""
    off = abs(measured_lufs - _LOUDNORM_I)
    return Metric(
        name="loudness",
        value=float(measured_lufs),
        passed=off <= _LOUDNESS_TOL + _EPS,
        unit="LUFS",
        detail=f"measured {measured_lufs:.2f} LUFS (target {_LOUDNORM_I:g} ±1)",
        culprits=[],
        hard=True,
        sample=1,
    )


def critique(plan: MontagePlan, *, measured_lufs: float | None = None) -> Scorecard:
    """Score ``plan`` against every Waves 1–3 acceptance metric.

    Deterministic and offline — pure from the plan, plus the optional
    ``measured_lufs`` (the integrated loudness a real
    :func:`monteur.preview.render_export` handed back). No video is
    re-decoded. Returns a :class:`Scorecard`; ``.passed()`` is the
    acceptance gate (every applicable HARD metric passes) and
    ``.aggregate()`` the ``[0, 1]`` score the refine loop maximises.
    """
    metrics: dict[str, Metric] = {}
    for m in (
        _coincidence(plan),
        _silence(plan),
        _slivers(plan),
        _drops(plan),
        _grammar(plan),
    ):
        metrics[m.name] = m
    if measured_lufs is not None:
        m = _loudness(float(measured_lufs))
        metrics[m.name] = m
    return Scorecard(metrics=metrics)
