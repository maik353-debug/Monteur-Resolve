"""Spatial analysis: shot size and eye-trace attention points — offline.

Where :mod:`monteur.daylight` answers "WHEN was this shot filmed?", this
module answers two questions about the FRAME itself, both from the same
tiny 64x36 RGB samples daylight already reads (no API, no cost, pure pixel
statistics — the picture-coherence half of the Magie blueprint's wave 3):

* **Shot size** (blueprint 3.2) — is this a *wide* establishing vista, a
  *medium* develop shot, or a *close* pay-off? Approximated from where the
  frame's detail lives: a wide shot is busy-uniform (edges spread into
  every corner), a close-up is busy-concentrated (a dominant low-frequency
  subject fills the middle while the background falls soft). Stored on
  :attr:`monteur.sift.Moment.shot_size`.

* **Attention point** (blueprint 3.1, Murch's eye-trace rule) — WHERE on
  screen the eye is drawn, estimated as the salience centroid (the
  gradient-magnitude-weighted centre of the frame). Sampled at the
  moment's START and END so the montage can carry the eye across a cut:
  the outgoing shot's :attr:`~monteur.sift.Moment.exit_focus` should sit
  near the incoming shot's :attr:`~monteur.sift.Moment.entry_focus`.
  Coordinates are (x, y) in 0..1 (x right, y down); a flat/textureless
  frame yields ``None`` (no discernible attention point).

Both signals are SOFT tie-breakers in casting — never a hard filter, never
over sync, drop or rhythm (Murch: eye-trace is a LOW rank, sacrificed for
the higher ones). A moment without them casts exactly as before.

Caching mirrors daylight exactly: results live in ``.monteur-spatial.json``
next to the footage, keyed by absolute path + file mtime + moment window,
so re-scanning a shoot costs nothing until the footage changes. A clip
whose frames cannot be read gets one note and is skipped; analysis must
never fail a scan.

Requires the media extra (numpy + ffmpeg), same as the sift itself.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from monteur.media import extract_rgb_frame
from monteur.sift import ClipReport

#: The three shot-size classes, wide -> medium -> close (the grammar arc).
SHOT_SIZES = ("wide", "medium", "close")

#: Cache file name, created next to the first report's footage (the
#: spatial sibling of daylight's ``.monteur-daylight.json``).
CACHE_FILENAME = ".monteur-spatial.json"

#: Sample size for analysis — the same tiny frame daylight decodes; mean
#: gradient statistics stabilize long before detail matters.
_FRAME_SIZE = (64, 36)

#: Grid the frame is tiled into to measure how detail is spread. A block
#: counts as "busy" when its mean gradient reaches _BUSY_FRACTION of the
#: whole frame's mean gradient.
_GRID = (4, 4)  # (cols, rows)
_BUSY_FRACTION = 0.5
#: Share of blocks that must be busy for a WIDE shot (detail everywhere)
#: and the ceiling below which the shot reads CLOSE (a lone subject).
_WIDE_MIN_SPREAD = 0.60
_CLOSE_MAX_SPREAD = 0.35
#: Below this mean gradient the frame is essentially flat — no reliable
#: shot size and no attention point (returns "" / None respectively).
_FLAT_GRADIENT = 0.5
#: Confidence floor: a frame on a decision border scores 0.5, one deep
#: inside its class approaches 1.0 (identical shape to daylight's).
_CONFIDENCE_FLOOR = 0.5


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _confidence(margin: float) -> float:
    return round(_CONFIDENCE_FLOOR + (1.0 - _CONFIDENCE_FLOOR) * _clamp01(margin), 3)


def _gradient_magnitude(rgb):
    """Per-pixel gradient magnitude of a frame's luma — the salience map.

    Reuses the sift's cheap sharpness math (``gx**2 + gy**2``): bright
    edges and textured subjects score high, flat sky/wall scores ~0.
    """
    import numpy as np

    frame = rgb.astype("float32")
    # Rec.709 luma keeps the map consistent with daylight's brightness.
    luma = 0.2126 * frame[..., 0] + 0.7152 * frame[..., 1] + 0.0722 * frame[..., 2]
    gy, gx = np.gradient(luma)
    return np.sqrt(gx * gx + gy * gy)


def focus_point(rgb) -> tuple[float, float] | None:
    """Attention point of one RGB frame: the salience centroid, (x, y) 0..1.

    The gradient-magnitude-weighted centre of the frame — where the eye is
    drawn. Returns ``None`` for a flat/textureless frame (mean gradient
    below :data:`_FLAT_GRADIENT`): no attention point to speak of, so
    eye-trace scoring stays neutral instead of chasing noise.
    """
    import numpy as np

    grad = _gradient_magnitude(rgb)
    total = float(grad.sum())
    height, width = grad.shape
    if total <= _FLAT_GRADIENT * grad.size or width < 2 or height < 2:
        return None
    ys, xs = np.mgrid[0:height, 0:width]
    cx = float((grad * xs).sum() / total) / (width - 1)
    cy = float((grad * ys).sum() / total) / (height - 1)
    return (round(_clamp01(cx), 4), round(_clamp01(cy), 4))


def classify_shot_size(rgb) -> dict:
    """Classify one RGB frame's shot size — pure math, no ML.

    Returns ``{"label": "wide"|"medium"|"close"|"", "confidence": 0.5..1.0,
    "spread": float, "detail": float}``. ``spread`` is the share of grid
    blocks whose detail reaches :data:`_BUSY_FRACTION` of the frame mean —
    high (detail in every corner) reads WIDE, low (a lone central subject
    over soft background) reads CLOSE, between reads MEDIUM. A flat frame
    (mean gradient < :data:`_FLAT_GRADIENT`) returns an empty label: no
    reliable size. See the module docstring for the thresholds.
    """
    grad = _gradient_magnitude(rgb)
    detail = float(grad.mean())
    if detail < _FLAT_GRADIENT:
        return {"label": "", "confidence": 0.0, "spread": 0.0, "detail": round(detail, 4)}

    cols, rows = _GRID
    height, width = grad.shape
    block_means = []
    for r in range(rows):
        y0, y1 = height * r // rows, height * (r + 1) // rows
        for c in range(cols):
            x0, x1 = width * c // cols, width * (c + 1) // cols
            block = grad[y0:y1, x0:x1]
            if block.size:
                block_means.append(float(block.mean()))
    threshold = _BUSY_FRACTION * detail
    busy = sum(1 for m in block_means if m >= threshold)
    spread = busy / len(block_means) if block_means else 0.0

    if spread >= _WIDE_MIN_SPREAD:
        margin = (spread - _WIDE_MIN_SPREAD) / (1.0 - _WIDE_MIN_SPREAD)
        label = "wide"
    elif spread <= _CLOSE_MAX_SPREAD:
        margin = (_CLOSE_MAX_SPREAD - spread) / _CLOSE_MAX_SPREAD
        label = "close"
    else:
        # Medium: most confident dead-centre between the two borders.
        band_mid = (_WIDE_MIN_SPREAD + _CLOSE_MAX_SPREAD) / 2.0
        band_half = (_WIDE_MIN_SPREAD - _CLOSE_MAX_SPREAD) / 2.0
        margin = 1.0 - abs(spread - band_mid) / band_half
        label = "medium"
    return {
        "label": label,
        "confidence": _confidence(margin),
        "spread": round(spread, 4),
        "detail": round(detail, 4),
    }


def analyse_moment(clip_path: str, start: float, end: float) -> dict:
    """Shot size + entry/exit attention points for one moment window.

    Decodes three tiny frames (start, midpoint, end) and returns
    ``{"shot_size": str, "confidence": float, "entry_focus": [x, y]|None,
    "exit_focus": [x, y]|None}``. Shot size is judged on the MIDPOINT frame
    (representative of the whole shot); the attention points are read at
    the true edges the cut lands on. Raises
    :class:`monteur.media.MonteurMediaError` when a frame cannot be read.
    """
    span = max(0.0, end - start)
    edge = min(0.1, span / 4.0)  # nudge off the exact edge (seek robustness)
    entry_rgb = extract_rgb_frame(clip_path, start + edge, size=_FRAME_SIZE)
    mid_rgb = extract_rgb_frame(clip_path, (start + end) / 2.0, size=_FRAME_SIZE)
    exit_rgb = extract_rgb_frame(clip_path, max(start, end - edge), size=_FRAME_SIZE)
    size = classify_shot_size(mid_rgb)
    return {
        "shot_size": size["label"],
        "confidence": size["confidence"],
        "entry_focus": list(focus_point(entry_rgb)) if focus_point(entry_rgb) else None,
        "exit_focus": list(focus_point(exit_rgb)) if focus_point(exit_rgb) else None,
    }


# --- cache (keyed exactly like monteur.daylight's) ---------------------------------


def _moment_key(path: str, start: float, end: float) -> str:
    abspath = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        mtime = 0.0
    return f"{abspath}|{mtime}|{start:.2f}-{end:.2f}"


def _load_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:  # a read-only footage folder must not abort the run
        pass


def _focus_tuple(value: object) -> tuple[float, float] | None:
    """Coerce a cached/analysed focus entry to a clean (x, y) tuple or None."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _valid_entry(entry: object) -> dict | None:
    """A cache/analysis entry with a usable shape, else None (defensive)."""
    if not isinstance(entry, dict):
        return None
    if "shot_size" not in entry:
        return None
    if entry.get("shot_size") not in ("", *SHOT_SIZES):
        return None
    return entry


def _apply_entry(moment, entry: dict) -> None:
    """Fill a moment's spatial fields from a (validated) entry, only-when-set."""
    if entry.get("shot_size"):
        moment.shot_size = entry["shot_size"]
    entry_focus = _focus_tuple(entry.get("entry_focus"))
    if entry_focus is not None:
        moment.entry_focus = entry_focus
    exit_focus = _focus_tuple(entry.get("exit_focus"))
    if exit_focus is not None:
        moment.exit_focus = exit_focus


def _call_progress(progress, index, total, name, stage) -> None:
    if progress is None:
        return
    try:
        progress(index, total, name, stage)
    except Exception:  # noqa: BLE001 — a broken callback must not abort the scan
        pass


# --- public API --------------------------------------------------------------------


def annotate_reports(
    reports: list[ClipReport],
    *,
    progress: Callable | None = None,
    cache_path: str | Path | None = None,
) -> list[str]:
    """Fill every moment's ``shot_size`` and ``entry/exit_focus``, IN PLACE.

    Mirrors :func:`monteur.daylight.annotate_reports` field for field:
    offline, cached in ``.monteur-spatial.json`` next to the first report's
    footage, per-clip failure isolated (one note, remaining moments of that
    clip skipped, the scan never raised). Every field is written
    only-when-set — a flat frame leaves the focus points ``None`` and an
    unreadable clip leaves all three empty, so those moments cast exactly
    as before. Returns human-readable notes (summary first).
    """
    todo = [(report, moment) for report in reports for moment in report.moments]
    if not todo:
        return ["no moments to analyse"]

    if cache_path is None:
        folder = os.path.dirname(os.path.abspath(reports[0].path))
        cache_path = Path(folder) / CACHE_FILENAME
    else:
        cache_path = Path(cache_path)
    cache = _load_cache(cache_path)

    notes: list[str] = []
    total = len(todo)
    cached_count = 0
    fresh_count = 0
    counts = {label: 0 for label in SHOT_SIZES}
    failed_paths: set[str] = set()
    dirty = False

    for i, (report, moment) in enumerate(todo, start=1):
        name = Path(report.path).name
        key = _moment_key(report.path, moment.start, moment.end)
        entry = _valid_entry(cache.get(key))
        if entry is not None:
            _apply_entry(moment, entry)
            if entry.get("shot_size"):
                counts[entry["shot_size"]] += 1
            cached_count += 1
            _call_progress(progress, i, total, name, "cache")
            continue
        if report.path in failed_paths:
            continue  # the clip already failed to yield a frame — skip it
        try:
            entry = analyse_moment(report.path, moment.start, moment.end)
        except Exception as exc:  # noqa: BLE001 — one bad clip must never fail a scan
            failed_paths.add(report.path)
            notes.append(
                f"{name}: could not read a frame for spatial analysis — "
                f"clip skipped ({exc})"
            )
            continue
        _apply_entry(moment, entry)
        if entry.get("shot_size"):
            counts[entry["shot_size"]] += 1
        cache[key] = entry
        dirty = True
        fresh_count += 1
        _call_progress(progress, i, total, name, "frame")

    if dirty:
        _save_cache(cache_path, cache)

    classified = cached_count + fresh_count
    breakdown = ", ".join(
        f"{counts[label]} {label}" for label in SHOT_SIZES if counts[label]
    )
    summary = f"spatial: {classified} of {total} moments analysed"
    if breakdown:
        summary += f" ({breakdown})"
    if cached_count:
        summary += f", {cached_count} from cache"
    notes.insert(0, summary)
    return notes
