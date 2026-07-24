"""Time-of-day classification: day, golden hour or night — offline and free.

The sift (:mod:`monteur.sift`) judges moments technically and the vision
pass (:mod:`monteur.vision`) says what is IN the picture, but neither
answers the question a shoot spread over several days raises first: does
this shot belong to the day, the golden hour or the night? Without that,
material filmed across days lands in the cut shuffled — a night shot
between two morning shots reads like a mistake, not a choice.

This module fills :attr:`monteur.sift.Moment.daylight` with a coarse
time-of-day class from ONE small color frame per moment (sampled at the
moment's midpoint). No API, no cost: plain pixel statistics.

Heuristic (deliberately simple, thresholds are module constants):

* brightness — mean Rec.709 luma of the frame, 0..255;
* warmth — mean(R) - mean(B), positive = warm light;
* saturation — mean per-pixel (max(R,G,B) - min(R,G,B)), 0..255.

Classes, first match wins:

* **night** — ``brightness < _NIGHT_MAX_BRIGHTNESS`` (60): genuinely dark
  frames. Artificial lights (street lamps, headlights) create local
  bright spots but leave the MEAN low, so a lit night street still reads
  night. Confidence grows the darker the frame is.
* **golden** — warm AND mid-bright AND actually colorful:
  ``warmth >= _GOLDEN_MIN_WARMTH`` (18), brightness inside
  ``[_NIGHT_MAX_BRIGHTNESS, _GOLDEN_MAX_BRIGHTNESS)`` (60..170) and
  ``saturation >= _GOLDEN_MIN_SATURATION`` (25). Low, warm sun saturates
  color; a gray frame with a mild warm cast is more likely white-balance
  drift than golden hour and stays "day". Confidence grows with the
  warmth margin and with the distance from the brightness band's edges.
* **day** — everything else: neutral/cool light, or bright frames even
  when warm (a sunlit noon wall is warm-ish AND bright — that is day).
  Confidence grows with the distance from the night and golden borders.

Honest limits: a dim tungsten interior can read "golden" and heavy Log
footage biases toward "night" — the confidence value says how sure the
call is, and every consumer treats the class as a SOFT signal (a casting
tie-breaker, never a hard filter).

Caching, keyed like the vision cache (:mod:`monteur.vision`): results are
stored in ``.monteur-daylight.json`` next to the footage, keyed by
absolute path + file mtime + moment window — re-scanning a shoot costs
nothing until the footage changes. A clip whose frame extraction fails
gets a note and is skipped; classification must never fail a scan.

Requires the media extra (numpy + ffmpeg), same as the sift itself.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from monteur.media import extract_rgb_frame
from monteur.sift import ClipReport

#: The three time-of-day classes, in the natural arc order.
DAYLIGHT_CLASSES = ("day", "golden", "night")

#: Cache file name, created next to the first report's footage (the
#: daylight sibling of vision's ``.monteur-vision.json``).
CACHE_FILENAME = ".monteur-daylight.json"

# Classification thresholds (see the module docstring).
_NIGHT_MAX_BRIGHTNESS = 60.0  # mean luma below this = night
_GOLDEN_MIN_WARMTH = 18.0  # mean(R) - mean(B) at/above this = warm light
_GOLDEN_MAX_BRIGHTNESS = 170.0  # warm frames at/above this are day, not golden
_GOLDEN_MIN_SATURATION = 25.0  # golden light saturates; gray warm casts stay day
#: Confidence floor: a frame exactly on a decision border scores 0.5, a
#: frame deep inside its class approaches 1.0.
_CONFIDENCE_FLOOR = 0.5

#: Sample size for the classification frame — tiny on purpose: mean
#: statistics stabilize long before detail matters, and the decode stays
#: nearly free.
_FRAME_SIZE = (64, 36)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _confidence(margin: float) -> float:
    """Map a 0..1 border margin onto the documented 0.5..1.0 range."""
    return round(_CONFIDENCE_FLOOR + (1.0 - _CONFIDENCE_FLOOR) * _clamp01(margin), 3)


def classify_frame(rgb) -> dict:
    """Classify one RGB frame (numpy uint8 array, H x W x 3) — pure math.

    Returns ``{"label": "day"|"golden"|"night", "confidence": 0.5..1.0,
    "brightness": float, "warmth": float, "saturation": float}`` — the
    measured statistics ride along so callers (and the cache) can audit
    the call. See the module docstring for the exact thresholds.
    """
    frame = rgb.astype("float32")
    r = float(frame[..., 0].mean())
    g = float(frame[..., 1].mean())
    b = float(frame[..., 2].mean())
    brightness = 0.2126 * r + 0.7152 * g + 0.0722 * b
    warmth = r - b
    saturation = float((frame.max(axis=2) - frame.min(axis=2)).mean())

    if brightness < _NIGHT_MAX_BRIGHTNESS:
        # Darker = more clearly night; a frame at the border scores 0.5.
        margin = (_NIGHT_MAX_BRIGHTNESS - brightness) / _NIGHT_MAX_BRIGHTNESS
        label, confidence = "night", _confidence(margin)
    elif (
        warmth >= _GOLDEN_MIN_WARMTH
        and brightness < _GOLDEN_MAX_BRIGHTNESS
        and saturation >= _GOLDEN_MIN_SATURATION
    ):
        # Confidence: the weaker of "how warm beyond the threshold" and "how
        # DIM inside the band". Golden hour is low, warm sun — a dim warm frame
        # is more clearly golden than a bright one, so confidence rises as the
        # brightness falls toward the night border (not, as before, peaking at
        # the band's mid-point, which is the least golden-looking part of it).
        warm_margin = (warmth - _GOLDEN_MIN_WARMTH) / _GOLDEN_MIN_WARMTH
        dim_margin = (_GOLDEN_MAX_BRIGHTNESS - brightness) / (
            _GOLDEN_MAX_BRIGHTNESS - _NIGHT_MAX_BRIGHTNESS
        )
        label, confidence = "golden", _confidence(min(warm_margin, dim_margin))
    else:
        # Day is the residual class: confident when clearly brighter than
        # night AND clearly not golden (cool, or bright beyond the band).
        bright_margin = (brightness - _NIGHT_MAX_BRIGHTNESS) / _NIGHT_MAX_BRIGHTNESS
        if warmth >= _GOLDEN_MIN_WARMTH and saturation >= _GOLDEN_MIN_SATURATION:
            # Warm and colorful, so it escaped golden by brightness alone.
            not_golden = (brightness - _GOLDEN_MAX_BRIGHTNESS) / (
                255.0 - _GOLDEN_MAX_BRIGHTNESS
            )
        else:
            not_golden = (_GOLDEN_MIN_WARMTH - warmth) / _GOLDEN_MIN_WARMTH
        label, confidence = "day", _confidence(min(bright_margin, not_golden))

    return {
        "label": label,
        "confidence": confidence,
        "brightness": round(brightness, 2),
        "warmth": round(warmth, 2),
        "saturation": round(saturation, 2),
    }


def classify_moment(clip_path: str, time_s: float) -> dict:
    """Classify the time of day at ``time_s`` seconds into ``clip_path``.

    Extracts one small RGB frame (:func:`monteur.media.extract_rgb_frame`,
    the same fast keyframe seek the thumbnails use) and runs
    :func:`classify_frame` on it. Raises
    :class:`monteur.media.MonteurMediaError` when the frame cannot be
    read — callers that must not fail (the scan) catch it per clip.
    """
    return classify_frame(extract_rgb_frame(clip_path, time_s, size=_FRAME_SIZE))


# --- cache (keyed like monteur.vision's) ------------------------------------------


def _moment_key(path: str, start: float, end: float) -> str:
    """Cache key: file identity (abspath + mtime) | moment window.

    Same shape as the vision cache key (minus the model — there is none);
    the mtime makes re-exported footage a cache MISS.
    """
    abspath = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        mtime = 0.0  # missing file: extraction will fail and note the clip
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


def _valid_entry(entry: object) -> dict | None:
    """A cache/classify entry with a known label, else None (defensive)."""
    if not isinstance(entry, dict):
        return None
    if entry.get("label") not in DAYLIGHT_CLASSES:
        return None
    return entry


def _call_progress(progress, index, total, name, stage) -> None:
    """Invoke the progress callback, swallowing any exception it raises."""
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
    """Fill every moment's ``daylight`` class, IN PLACE; return notes.

    Samples one frame at each moment's midpoint and stores the class on
    :attr:`monteur.sift.Moment.daylight`. All moments are classified —
    the pass is offline and free, so there is no cost cap. ``progress``
    is called as ``progress(index, total, clip_name, stage)`` with stage
    ``"frame"`` (a frame was classified) or ``"cache"`` (served from the
    sidecar); exceptions it raises are swallowed. ``cache_path`` defaults
    to ``.monteur-daylight.json`` next to the first report's footage.

    Failure semantics match the vision pass: a clip whose frame
    extraction fails gets one note and its remaining moments are skipped
    (their ``daylight`` stays ``""``); the function itself never raises
    on a bad clip — a corrupt file must not fail the scan.
    """
    todo = [
        (report, moment) for report in reports for moment in report.moments
    ]
    if not todo:
        return ["no moments to classify"]

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
    counts = {label: 0 for label in DAYLIGHT_CLASSES}
    failed_paths: set[str] = set()
    dirty = False

    for i, (report, moment) in enumerate(todo, start=1):
        name = Path(report.path).name
        key = _moment_key(report.path, moment.start, moment.end)
        entry = _valid_entry(cache.get(key))
        if entry is not None:
            moment.daylight = entry["label"]
            counts[entry["label"]] += 1
            cached_count += 1
            _call_progress(progress, i, total, name, "cache")
            continue
        if report.path in failed_paths:
            continue  # the clip already failed to yield a frame — skip it
        midpoint = (moment.start + moment.end) / 2.0
        try:
            entry = classify_moment(report.path, midpoint)
        except Exception as exc:  # noqa: BLE001 — one bad clip must never fail a scan
            failed_paths.add(report.path)
            notes.append(
                f"{name}: could not read a frame for daylight — clip skipped ({exc})"
            )
            continue
        moment.daylight = entry["label"]
        counts[entry["label"]] += 1
        cache[key] = entry
        dirty = True
        fresh_count += 1
        _call_progress(progress, i, total, name, "frame")

    if dirty:
        _save_cache(cache_path, cache)

    classified = cached_count + fresh_count
    breakdown = ", ".join(
        f"{counts[label]} {label}" for label in DAYLIGHT_CLASSES if counts[label]
    )
    summary = f"daylight: {classified} of {total} moments classified"
    if breakdown:
        summary += f" ({breakdown})"
    if cached_count:
        summary += f", {cached_count} from cache"
    notes.insert(0, summary)
    return notes
