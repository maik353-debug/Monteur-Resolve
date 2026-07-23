"""Render → watch → refine: the closed loop (blueprint Wave 4.2).

The one-shot :func:`monteur.montage.plan_montage` builds a cut and stops.
This is the opt-in loop around it: plan, CRITIQUE the plan against the
Waves 1–3 acceptance metrics (:func:`monteur.critique.critique`), and when
a metric fails turn the RIGHT knob DETERMINISTICALLY and re-plan — keeping
the best-scoring plan across a bounded number of passes.

The knobs, chosen by which metric fails (sync first — it is the house's
first promise):

* **coincidence weak** → the picture peaks are not landing on their
  beats, usually because the slots are longer than the moments' peaks can
  sit inside. RAISE the cut density (a smaller ``pace``) so each slot fits
  within its moment and the peak aims onto the beat.
* **too many slivers** → LOWER the cut density (a larger ``pace``): fewer,
  longer slots.
* **shot-grammar violations** → raise the ordering weight
  (``CastingBias.grammar_scale``) so the cast pushes harder toward
  changing shot sizes.

Coincidence and slivers pull the pace in opposite directions on purpose;
the loop addresses the highest-priority failure each pass and keeps the
plan with the best aggregate scorecard, so the search settles instead of
oscillating. Everything is deterministic — the same inputs run the same
loop to the same winner — and offline (no render per pass; loudness is a
mix property validated at export, not re-plannable, so the loop scores on
plan-only metrics). The winning plan carries an honest ``refine:`` note.

:func:`monteur.montage.plan_montage` is untouched and remains the
byte-identical default; refine is strictly opt-in (a flag, the Studio's
"until it lands" button).
"""

from __future__ import annotations

from monteur.critique import Scorecard, critique, supersedes
from monteur.montage import (
    MIN_CUT_INTERVAL,
    CastingBias,
    MontagePlan,
    plan_montage,
    plan_pulse,
)

#: Default iteration budget (passes beyond the first plan).
DEFAULT_BUDGET = 3

#: Pace multipliers: denser to chase coincidence, looser to shed slivers.
_DENSER = 0.7
_LOOSER = 1.4
#: Keep pace inside a sane band (seconds of the fastest phase's clip).
_PACE_MIN = max(MIN_CUT_INTERVAL, 0.3)
_PACE_MAX = 8.0
#: Each grammar failure adds this to the ordering scale.
_GRAMMAR_STEP = 1.0
_GRAMMAR_MAX = 4.0

_EPS = 1e-6


def _clamp_pace(pace: float) -> float:
    return max(_PACE_MIN, min(_PACE_MAX, pace))


def _next_config(
    scorecard: Scorecard, pace: float, grammar_scale: float
) -> tuple[float, float] | None:
    """Deterministically derive the next (pace, grammar_scale) from a card.

    Addresses the highest-priority FAILING metric — coincidence (sync)
    first, then slivers, then grammar. Returns None when nothing
    actionable failed (the loop then stops on this plan).
    """
    metrics = scorecard.metrics
    coincidence = metrics.get("coincidence")
    slivers = metrics.get("slivers")
    grammar = metrics.get("grammar")
    if coincidence is not None and coincidence.sample and not coincidence.passed:
        new = _clamp_pace(pace * _DENSER)
        return (new, grammar_scale) if abs(new - pace) > _EPS else None
    if slivers is not None and slivers.sample and not slivers.passed:
        new = _clamp_pace(pace * _LOOSER)
        return (new, grammar_scale) if abs(new - pace) > _EPS else None
    if grammar is not None and grammar.sample and not grammar.passed:
        new_scale = min(_GRAMMAR_MAX, grammar_scale + _GRAMMAR_STEP)
        return (pace, new_scale) if new_scale > grammar_scale + _EPS else None
    return None


def _rate(scorecard: Scorecard, name: str) -> float | None:
    m = scorecard.metrics.get(name)
    return m.value if m is not None and m.sample else None


def refine_plan(
    reports,
    music=None,
    *,
    budget: int = DEFAULT_BUDGET,
    **plan_kwargs,
) -> tuple[MontagePlan, list[dict]]:
    """Plan, self-critique and refine until the acceptance metrics pass.

    ``reports`` / ``music`` and every ``plan_kwargs`` are the same inputs
    :func:`monteur.montage.plan_montage` takes (``style``, ``max_duration``,
    ``allow_repeats``, ``arrangement`` …); refine drives the ``pace`` and
    the shot-grammar ordering weight itself, so passing an explicit
    ``pace`` only sets the loop's starting point. ``budget`` bounds the
    extra passes beyond the first plan (``0`` degrades to a single plan).

    Returns ``(best_plan, history)`` where ``best_plan`` is the highest
    aggregate-scoring plan seen (ties keep the earlier pass — determinism)
    carrying an honest ``refine:`` note, and ``history`` is one dict per
    pass (``config``, ``aggregate``, ``passed``, ``scorecard``). Fully
    deterministic: identical inputs run the identical loop to the identical
    winner. Offline — no render happens here.
    """
    # An explicit casting_bias (learned preferences) rides through every
    # pass; refine only ADDS a grammar scale on top of it.
    base_bias: CastingBias | None = plan_kwargs.pop("casting_bias", None)
    pace = plan_kwargs.pop("pace", None)
    grammar_scale = 1.0

    def build(pace_val: float | None, scale: float) -> MontagePlan:
        bias = base_bias or CastingBias()
        if abs(scale - 1.0) > _EPS:
            bias = CastingBias(
                shot_size=bias.shot_size,
                fewer_dissolves=bias.fewer_dissolves,
                grammar_scale=scale,
            )
        kwargs = dict(plan_kwargs)
        if pace_val is not None:
            kwargs["pace"] = pace_val
        if not bias.is_neutral():
            kwargs["casting_bias"] = bias
        return plan_montage(reports, music, **kwargs)

    history: list[dict] = []
    best_plan: MontagePlan | None = None
    best_agg = -1.0
    best_card: Scorecard | None = None
    first_card: Scorecard | None = None

    # Pass 0: the plan the caller's inputs produce (its own pace/auto pace).
    plan = build(pace, grammar_scale)
    if pace is None:
        pace = plan_pulse(plan)  # a concrete starting point for the tweaks
    card = critique(plan)
    first_card = card
    history.append(
        {
            "config": {"pace": round(pace, 3), "grammar_scale": grammar_scale},
            "aggregate": card.aggregate(),
            "passed": card.passed(),
            "scorecard": card.as_dict(),
        }
    )
    best_plan, best_agg, best_card = plan, card.aggregate(), card

    passes = 1
    while passes <= max(0, budget) and not best_card.passed():
        nxt = _next_config(card, pace, grammar_scale)
        if nxt is None:
            break  # nothing actionable failed — refining further is noise
        new_pace, new_scale = nxt
        if abs(new_pace - pace) <= _EPS and abs(new_scale - grammar_scale) <= _EPS:
            break
        pace, grammar_scale = new_pace, new_scale
        plan = build(pace, grammar_scale)
        card = critique(plan)
        agg = card.aggregate()
        history.append(
            {
                "config": {"pace": round(pace, 3), "grammar_scale": grammar_scale},
                "aggregate": agg,
                "passed": card.passed(),
                "scorecard": card.as_dict(),
            }
        )
        # Keep the best — acceptance first (a passing plan always beats a
        # failing one), then higher aggregate; ties keep the earlier winner.
        if supersedes(card, agg, best_card, best_agg):
            best_plan, best_agg, best_card = plan, agg, card
        passes += 1

    _annotate(best_plan, first_card, best_card, len(history))
    return best_plan, history


def _annotate(
    plan: MontagePlan, first: Scorecard | None, best: Scorecard | None, passes: int
) -> None:
    """Append the honest ``refine:`` note narrating the loop (no silent iteration)."""
    if first is None or best is None:
        return
    bits: list[str] = [f"{passes} pass{'es' if passes != 1 else ''}"]
    fc, bc = _rate(first, "coincidence"), _rate(best, "coincidence")
    if fc is not None and bc is not None:
        bits.append(f"coincidence {fc * 100:.0f}%→{bc * 100:.0f}%")
    fs = first.metrics.get("slivers")
    bs = best.metrics.get("slivers")
    if fs is not None and bs is not None and (fs.value or bs.value):
        bits.append(f"slivers {int(fs.value)}→{int(bs.value)}")
    fg, bg = _rate(first, "grammar"), _rate(best, "grammar")
    if fg is not None and bg is not None and (fg or bg):
        bits.append(f"equal-size pairs {fg * 100:.0f}%→{bg * 100:.0f}%")
    verdict = "acceptance met" if best.passed() else "best effort kept"
    plan.notes.append("refine: " + ", ".join(bits) + f" — {verdict}")
