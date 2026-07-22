"""Auto-reframe 9:16 — keep the subject framed when wide footage is cropped.

When 16:9 footage is delivered on a vertical (9:16) or cinemascope canvas,
the export COVERS the frame (aspect preserved, overflow cropped). By default
that crop is dead-centre, so a subject sitting off-centre — a rider entering
frame left, a face in the right third — gets sliced away. Wave 3 already
estimates each shot's attention point (:func:`monteur.spatial.focus_point`,
carried as :attr:`monteur.sift.Moment.entry_focus` / ``exit_focus``): this
module turns that soft signal into a hard crop offset so the subject stays
in the vertical frame.

The whole module is a **pure function of geometry** — source WxH, target
canvas WxH, and a focus point (x, y) in 0..1 — with no I/O, no ffmpeg, no
randomness. Given those three it returns the top-left corner of the crop
window inside the cover-scaled source, CLAMPED so the window never leaves
the source. Two invariants make it a safe drop-in:

* **Fallback parity.** A ``None`` focus (flat frame / unanalysed moment)
  yields the exact CENTRE offset — the same crop the pipeline drew before
  this feature existed. Same-aspect footage (16:9 on a 16:9 canvas) has no
  slack in either dimension, so the offset is ``(0, 0)`` = centre no matter
  what the focus says: nothing to reframe. A focus that already sits at the
  centre of the cropped dimension likewise lands on the centre offset. In
  all three the render keeps its byte-identical centre crop.

* **It never invents pixels.** The crop window is the delivery size and is
  clamped fully inside the scaled source, so reframing only ever *chooses*
  which existing pixels survive — it cannot show black bars or stretch.

The reframe changes FRAMING only. It never touches the cut: timing, peaks,
drops, silence and audio are all decided upstream and untouched here.
"""

from __future__ import annotations

#: How close (in scaled-source pixels) an offset must sit to the centre
#: before the renderer treats it as "no reframe" and keeps its exact,
#: byte-identical centre-crop string. Sub-pixel, so any genuine shift wins.
CENTER_EPS = 0.5


def _clamp(value: float, low: float, high: float) -> float:
    # ``high`` can be 0 (no slack in that dimension); keep the order safe.
    return max(low, min(high, value))


def cover_scale(
    src_w: float, src_h: float, dst_w: float, dst_h: float
) -> tuple[float, float]:
    """Scaled source dims after a COVER fit (ffmpeg ``increase``).

    The uniform scale that makes the source at least as large as the target
    in both dimensions (``force_original_aspect_ratio=increase``): the
    dimension needing the larger upscale fills exactly, the other overflows
    and gets cropped. Returns the resulting ``(width, height)`` as floats.
    """
    if src_w <= 0 or src_h <= 0:
        # Degenerate: no reframe possible, report the target (zero slack).
        return (float(dst_w), float(dst_h))
    scale = max(dst_w / src_w, dst_h / src_h)
    return (src_w * scale, src_h * scale)


def crop_offset(
    src_w: float,
    src_h: float,
    dst_w: float,
    dst_h: float,
    focus: tuple[float, float] | None,
) -> tuple[float, float]:
    """Top-left ``(x, y)`` of the crop window inside the cover-scaled source.

    ``focus`` is the source attention point ``(fx, fy)`` in 0..1 (x right,
    y down) or ``None``. The window is ``dst_w`` x ``dst_h`` and is placed
    so the focus point sits at its centre, then clamped so the window stays
    fully inside the scaled source. ``None`` focus returns the CENTRE offset
    (fallback parity); a dimension with no slack (the fitted side, or a
    same-aspect crop) returns 0 there — there is nothing to shift.
    """
    scaled_w, scaled_h = cover_scale(src_w, src_h, dst_w, dst_h)
    slack_x = scaled_w - dst_w
    slack_y = scaled_h - dst_h
    if focus is None:
        return (slack_x / 2.0, slack_y / 2.0)
    fx, fy = focus
    x = _clamp(fx * scaled_w - dst_w / 2.0, 0.0, slack_x)
    y = _clamp(fy * scaled_h - dst_h / 2.0, 0.0, slack_y)
    return (x, y)


def center_offset(
    src_w: float, src_h: float, dst_w: float, dst_h: float
) -> tuple[float, float]:
    """The plain centre-crop offset — what the pipeline drew before reframe."""
    scaled_w, scaled_h = cover_scale(src_w, src_h, dst_w, dst_h)
    return ((scaled_w - dst_w) / 2.0, (scaled_h - dst_h) / 2.0)


def is_centered(
    src_w: float,
    src_h: float,
    dst_w: float,
    dst_h: float,
    focus: tuple[float, float] | None,
    *,
    eps: float = CENTER_EPS,
) -> bool:
    """Whether reframing this shot lands within ``eps`` px of the centre crop.

    ``True`` means the renderer can keep its exact centre-crop string and
    stay byte-identical: a ``None`` focus, a same-aspect crop (no slack), or
    a focus already centred in the cropped dimension all report ``True``.
    """
    if focus is None:
        return True
    x, y = crop_offset(src_w, src_h, dst_w, dst_h, focus)
    cx, cy = center_offset(src_w, src_h, dst_w, dst_h)
    return abs(x - cx) <= eps and abs(y - cy) <= eps


def average_focus(
    entry_focus: tuple[float, float] | None,
    exit_focus: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """A single static per-shot focus from the moment's entry/exit points.

    The v1 offset is a static crop, so the shot's two sampled attention
    points (where the eye enters the cut, where it leaves) are averaged into
    one representative point. With only one point present that point is used;
    with neither, ``None`` (no signal — the render falls back to centre).
    """
    if entry_focus is not None and exit_focus is not None:
        return (
            (entry_focus[0] + exit_focus[0]) / 2.0,
            (entry_focus[1] + exit_focus[1]) / 2.0,
        )
    return entry_focus if entry_focus is not None else exit_focus
