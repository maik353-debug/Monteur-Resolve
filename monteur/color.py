"""Farbe ohne Resolve: a small, deterministic colour grade for the cut.

Monteur's grading is intentionally modest and honest: four normalized
controls the way a shooter thinks about a look — brightness, contrast,
colour (saturation) and warmth — plus a handful of subtle, genuinely
usable presets (a soft filmic look, a muted/desaturated look, warm, cool,
a faded matte). No gimmicks: no black-and-white, no sepia. Everything is
local and deterministic: a grade compiles to a plain ffmpeg filter chain
(:func:`grade_to_ffmpeg`) that the export bakes over the assembled cut, so
what you preview is what you render, offline and private.

The model is a single source of truth: a :class:`Grade` carries the four
values in a friendly ``-1..1`` range (``0`` = neutral). A named look is
just a preset that fills those four values (see :data:`LOOKS`); the
``look`` field is a label so the UI can show which preset a grade came
from, but the four numbers are what render. A neutral grade compiles to
the empty string, so an ungraded export stays byte-identical to one from
before grading existed.

The ffmpeg mapping is deliberately gentle — the ``-1..1`` range spans a
tasteful adjustment, not a destructive one — so the sliders stay usable
end to end. Warmth rides on ``colorbalance`` (red up / blue down in the
midtones for warm, the reverse for cool) rather than ``colortemperature``,
because its direction is unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, fields


# The friendly control range: -1 (full negative) .. 0 (neutral) .. +1.
_LO = -1.0
_HI = 1.0


def _clamp(value: float, lo: float = _LO, hi: float = _HI) -> float:
    return lo if value < lo else hi if value > hi else value


@dataclass
class Grade:
    """One colour grade for the whole cut — four normalized controls.

    Each value is ``-1..1`` with ``0`` neutral: ``brightness`` lifts or
    lowers the image, ``contrast`` widens or flattens it, ``saturation``
    is colour intensity (``-1`` = grayscale, ``+1`` = doubled), ``warmth``
    warms (positive) or cools (negative) the midtones. ``look`` is a label
    only — which preset these numbers came from ("" or a :data:`LOOKS`
    key or "custom") — and never changes the rendered result.
    """

    brightness: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    warmth: float = 0.0
    look: str = ""


# How far the -1..1 range reaches into each ffmpeg parameter. Chosen so the
# extremes are a strong-but-usable grade, and everyday tweaks stay subtle.
_BRIGHTNESS_SPAN = 0.25   # eq brightness is additive, -1..1 (we use ±0.25)
_CONTRAST_SPAN = 0.45     # eq contrast multiplier around 1.0 (0.55..1.45)
_SATURATION_SPAN = 1.0    # eq saturation around 1.0 (0..2.0)
_WARMTH_SPAN = 0.12       # colorbalance rm/bm midtone push (±0.12)


def is_neutral(grade: Grade) -> bool:
    """True when the grade changes nothing (all four controls at 0)."""
    return (
        abs(grade.brightness) < 1e-6
        and abs(grade.contrast) < 1e-6
        and abs(grade.saturation) < 1e-6
        and abs(grade.warmth) < 1e-6
    )


def clamp_grade(grade: Grade) -> Grade:
    """A copy with every control clamped into ``-1..1``."""
    return Grade(
        brightness=_clamp(grade.brightness),
        contrast=_clamp(grade.contrast),
        saturation=_clamp(grade.saturation),
        warmth=_clamp(grade.warmth),
        look=str(grade.look or ""),
    )


def _fmt(value: float) -> str:
    """A compact fixed-point number for an ffmpeg filter argument."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def grade_to_ffmpeg(grade: Grade) -> str:
    """Compile a grade to an ffmpeg video-filter snippet ("" when neutral).

    The snippet is a comma-joined chain suitable for dropping into a ``-vf``
    or a ``filter_complex`` link: an ``eq`` for brightness/contrast/
    saturation and a ``colorbalance`` for warmth. Only the parts that
    actually move are emitted, and a neutral grade returns "" so the caller
    can skip the filter entirely and keep the render untouched.
    """
    g = clamp_grade(grade)
    if is_neutral(g):
        return ""
    chain: list[str] = []
    eq: list[str] = []
    if abs(g.contrast) >= 1e-6:
        eq.append(f"contrast={_fmt(1.0 + g.contrast * _CONTRAST_SPAN)}")
    if abs(g.brightness) >= 1e-6:
        eq.append(f"brightness={_fmt(g.brightness * _BRIGHTNESS_SPAN)}")
    if abs(g.saturation) >= 1e-6:
        sat = max(0.0, 1.0 + g.saturation * _SATURATION_SPAN)
        eq.append(f"saturation={_fmt(sat)}")
    if eq:
        chain.append("eq=" + ":".join(eq))
    if abs(g.warmth) >= 1e-6:
        push = g.warmth * _WARMTH_SPAN
        # warm = red up / blue down in the midtones; cool = the reverse
        chain.append(f"colorbalance=rm={_fmt(push)}:bm={_fmt(-push)}")
    return ",".join(chain)


# ---- presets: subtle, usable looks (no B/W, no sepia) -------------------
# Each look just fills the four Grade controls. Kept gentle on purpose —
# these are grades a real edit would keep, not demo filters.

LOOKS: dict[str, Grade] = {
    "neutral": Grade(look="neutral"),
    "filmic": Grade(brightness=-0.05, contrast=0.28, saturation=-0.12, warmth=0.14, look="filmic"),
    "muted": Grade(brightness=0.03, contrast=-0.08, saturation=-0.35, warmth=-0.05, look="muted"),
    "warm": Grade(brightness=0.02, contrast=0.05, saturation=0.06, warmth=0.40, look="warm"),
    "cool": Grade(brightness=0.0, contrast=0.05, saturation=-0.05, warmth=-0.38, look="cool"),
    "faded": Grade(brightness=0.10, contrast=-0.22, saturation=-0.18, warmth=0.06, look="faded"),
}

# Display order + one-line copy for the UI (the picker chips).
LOOK_META: list[dict] = [
    {"key": "neutral", "label": "Neutral", "note": "No grade — your footage as shot."},
    {"key": "filmic", "label": "Filmic", "note": "Gentle contrast and a touch of warmth."},
    {"key": "muted", "label": "Muted", "note": "Softer, desaturated — calm and understated."},
    {"key": "warm", "label": "Warm", "note": "Golden-hour warmth in the midtones."},
    {"key": "cool", "label": "Cool", "note": "A cooler, cleaner cast."},
    {"key": "faded", "label": "Faded", "note": "Lifted, low-contrast matte film look."},
]


def look(name: str) -> Grade:
    """The preset :class:`Grade` for a look name (a copy; unknown -> neutral)."""
    base = LOOKS.get(str(name or "").lower(), LOOKS["neutral"])
    return Grade(
        brightness=base.brightness,
        contrast=base.contrast,
        saturation=base.saturation,
        warmth=base.warmth,
        look=base.look,
    )


def grade_to_dict(grade: Grade) -> dict:
    """A JSON-ready dict, written only when the grade is non-neutral.

    A neutral grade returns ``{}`` so plans/projects without a grade
    serialize exactly as before grading existed. ``look`` rides along only
    when set. Round-trips through :func:`grade_from_dict`.
    """
    g = clamp_grade(grade)
    if is_neutral(g):
        # a neutral grade writes nothing — or just its look label, never the
        # redundant zero controls
        return {"look": g.look} if g.look else {}
    data = {
        "brightness": g.brightness,
        "contrast": g.contrast,
        "saturation": g.saturation,
        "warmth": g.warmth,
    }
    if g.look:
        data["look"] = g.look
    return data


def grade_from_dict(data: dict | None) -> Grade:
    """Rebuild a :class:`Grade` from :func:`grade_to_dict` output.

    Tolerant by design: ``None``, ``{}`` or a partial dict all yield a
    valid (clamped) grade, so old plans without the key load as neutral.
    Unknown keys are ignored.
    """
    data = data or {}
    known = {f.name for f in fields(Grade)}
    kwargs = {k: data[k] for k in known if k in data}
    for k in ("brightness", "contrast", "saturation", "warmth"):
        if k in kwargs:
            kwargs[k] = float(kwargs[k])
    if "look" in kwargs:
        kwargs["look"] = str(kwargs["look"] or "")
    return clamp_grade(Grade(**kwargs))
