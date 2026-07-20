"""Sound-elements library: scan a folder of SFX snippets, classify them
offline, and place them into a montage plan as REAL audio clips.

The product story: "I have a whole folder of exciting sounds (impacts,
whooshes, risers). Give Monteur the folder, it rates the snippets and
builds them into the right places." Audio only — visual effects stay in
Resolve by hand; the plan's SFX markers already point at those spots.

Scanning & classification (:func:`scan_elements`)
-------------------------------------------------
Every audio file in the folder (``monteur.media.AUDIO_EXTENSIONS``) is
decoded to a mono envelope (RMS over ~46 ms windows) and classified
deterministically — no AI, no network:

* **impact** — short (<= 2 s), energy peaks in the first ~15% and decays
  (mostly) monotonically after: the classic hit.
* **braam** — the impact shape but LONG (> 2 s) and low-band dominant
  (>= 50% of the spectral energy below 150 Hz): the trailer boom. Braams
  serve the plan's "sub-drop" cues.
* **riser** — >= 1.5 s, energy rises through the file and peaks in the
  last ~20% (risers often end in a hit — a peak at the very end counts).
* **whoosh** — ~0.3–3 s, energy arches: quiet edges, peak mid-file-ish.
* **other** — anything unclassifiable (still listed, never placed
  automatically).

Each :class:`SoundElement` carries the measured ``features`` (peak_time,
peak_pos, rise_ratio, decay_score, low_ratio, edge_ratio) and a 0..1
``confidence`` — how textbook the shape is within its kind. Results are
cached in ``.monteur-elements.json`` next to the folder, keyed
``path|mtime`` (the vision-cache conventions: a re-exported file is a
cache miss, a corrupt cache file is ignored, stale entries are simply
never matched). Decoding needs the media extra (numpy + ffmpeg); a
missing dependency raises a clean :class:`~monteur.media.MonteurMediaError`
before any file work. A single undecodable file never fails the scan —
it is listed as "other" with confidence 0.

Assignment (:func:`assign_elements`)
------------------------------------
Extends a plan's SFX layer (:class:`monteur.montage.SfxCue`) with concrete
files — every decision deterministic:

* existing cues get the best-matching file: kind match (impact->impact,
  whoosh->whoosh, riser->riser, sub-drop->braam; ambience cues stay
  search-query markers), best duration fit, confidence as the tiebreak.
  A filed cue's duration becomes the file's play length, clamped to the
  montage end (with a trim note when the file is longer than fits).
* **riser**: one through the build ENDING exactly on the drop — start =
  drop − riser duration, clamped into the montage; the riser whose length
  best fits the run-up wins. Only when the music has an in-range drop.
* **impact**: ON the drop, and right AFTER each smash-to-black dip (the
  hit out of the black).
* **whoosh**: on the fast-cut spots the plan already marked —
  :func:`monteur.montage._plan_sfx` found them; this module only files
  those cues, it never invents new whoosh spots.

Style-aware density (the plan's own ``style "<key>"`` note): **trailer**
and **music_video** run the full program (all whoosh cues, dip impacts);
**travel** stays sparse (one whoosh at most, none within 4 s before the
drop — the stutter burst lives there); **wedding** is minimal (no
whooshes, no dip impacts, and nothing at all in the quiet opening/outro
shares of the arc). Two rules always hold: no two placed elements of the
same kind ever overlap, and no file is reused within 10 seconds.

The returned notes say what was placed where; the caller decides whether
they join ``plan.notes``. :func:`carry_element_files` is the revision
hook: it copies files from an old plan's cues onto same-kind, same-time
cues of a re-planned one, so untouched regions keep their sounds.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from monteur.media import AUDIO_EXTENSIONS, MonteurMediaError, find_ffmpeg, read_audio
from monteur.montage import MontagePlan, SfxCue
from monteur.music import MusicAnalysis

CACHE_FILENAME = ".monteur-elements.json"

# The kinds scan_elements can decide on ("other" = listed, never placed).
ELEMENT_KINDS = ("impact", "whoosh", "riser", "braam", "other")

# Which element kind serves which planned cue kind (ambience has none:
# an ambience bed is a search, not a snippet).
_CUE_TO_ELEMENT = {
    "impact": "impact",
    "whoosh": "whoosh",
    "riser": "riser",
    "sub-drop": "braam",
}

# --- classifier tuning (envelope shapes; see the module docstring) ----------------
_ENV_WINDOW = 1024  # samples per RMS window at 22050 Hz (~46 ms)
_IMPACT_MAX_LEN = 2.0  # s: longer early-peak hits become braam candidates
_RISER_MIN_LEN = 1.5  # s: anything shorter can't build
_WHOOSH_MIN_LEN = 0.3
_WHOOSH_MAX_LEN = 3.0
_EARLY_PEAK = 0.15  # impact: peak inside the first 15%
_BRAAM_PEAK = 0.35  # braam: still front-loaded, but a swell is allowed
_LATE_PEAK = 0.8  # riser: peak in the last 20% (peak AT the end counts)
_DECAY_MIN = 0.6  # impact: at least this share of post-peak steps decay
_RISE_MIN = 1.5  # riser: last third at least this x louder than the first
_LOW_DOMINANT = 0.5  # braam: share of spectral energy below _LOW_BAND_HZ
_LOW_BAND_HZ = 150.0
_WHOOSH_EDGE_MAX = 0.6  # whoosh: edges at most this share of the peak
_CONFIDENCE_FLOOR = 0.1  # a classified element is never 0-confidence

# --- assignment tuning -------------------------------------------------------------
_REUSE_GAP = 10.0  # s: the same file never plays twice within this
_NEAR_CUE = 0.5  # s: an existing cue this close to the drop is "the drop cue"

_EPS = 1e-6


@dataclass
class SoundElement:
    """One classified snippet from the user's sound library."""

    path: str
    duration: float  # seconds
    kind: str  # one of ELEMENT_KINDS
    confidence: float  # 0..1, how textbook the envelope shape is
    features: dict = field(default_factory=dict)  # measured envelope features


# --- cache (vision-cache conventions) ---------------------------------------------


def _cache_key(path: Path) -> str:
    """File identity: abspath + mtime, so a re-exported file is a MISS."""
    abspath = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        mtime = 0.0
    return f"{abspath}|{mtime}"


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
    except OSError:  # a read-only library folder must not abort the scan
        pass


def _element_from_entry(path: str, entry: object) -> SoundElement | None:
    """Rebuild a SoundElement from a cache entry; None when unusable."""
    if not isinstance(entry, dict):
        return None
    try:
        kind = str(entry["kind"])
        duration = float(entry["duration"])
        confidence = float(entry["confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    if kind not in ELEMENT_KINDS:
        return None
    features = entry.get("features")
    return SoundElement(
        path=path,
        duration=duration,
        kind=kind,
        confidence=confidence,
        features=dict(features) if isinstance(features, dict) else {},
    )


# --- classification ---------------------------------------------------------------


def _ramp(value: float, at_zero: float, at_one: float) -> float:
    """Linear 0..1 ramp: 0 at ``at_zero``, 1 at ``at_one`` (either direction)."""
    if abs(at_one - at_zero) < _EPS:
        return 1.0
    t = (value - at_zero) / (at_one - at_zero)
    return max(0.0, min(1.0, t))


def _confidence(*parts: float) -> float:
    return max(_CONFIDENCE_FLOOR, sum(parts) / len(parts))


def classify_features(duration: float, features: dict) -> tuple[str, float]:
    """(kind, confidence) from measured envelope features — pure and testable."""
    peak_pos = float(features.get("peak_pos", 0.0))
    decay = float(features.get("decay_score", 0.0))
    rise = float(features.get("rise_ratio", 0.0))
    low = float(features.get("low_ratio", 0.0))
    edge = float(features.get("edge_ratio", 1.0))

    if duration <= _IMPACT_MAX_LEN and peak_pos <= _EARLY_PEAK and decay >= _DECAY_MIN:
        return "impact", _confidence(
            _ramp(peak_pos, _EARLY_PEAK, 0.0), _ramp(decay, _DECAY_MIN, 1.0)
        )
    if duration > _IMPACT_MAX_LEN and peak_pos <= _BRAAM_PEAK and low >= _LOW_DOMINANT:
        return "braam", _confidence(
            _ramp(low, _LOW_DOMINANT, 0.9), _ramp(peak_pos, _BRAAM_PEAK, 0.0)
        )
    if duration >= _RISER_MIN_LEN and peak_pos >= _LATE_PEAK and rise >= _RISE_MIN:
        return "riser", _confidence(
            _ramp(peak_pos, _LATE_PEAK, 1.0), _ramp(min(rise, 6.0), _RISE_MIN, 4.0)
        )
    if (
        _WHOOSH_MIN_LEN <= duration <= _WHOOSH_MAX_LEN
        and 0.2 <= peak_pos <= 0.8
        and edge <= _WHOOSH_EDGE_MAX
    ):
        return "whoosh", _confidence(
            _ramp(abs(peak_pos - 0.5), 0.3, 0.0), _ramp(edge, _WHOOSH_EDGE_MAX, 0.1)
        )
    return "other", 0.0


def _analyze(path: Path, rate: int = 22050) -> SoundElement:
    """Decode one file and classify its envelope (raises MonteurMediaError)."""
    import numpy as np

    samples = read_audio(path, rate=rate)
    duration = len(samples) / float(rate)
    if duration <= _EPS:
        return SoundElement(str(path), 0.0, "other", 0.0, {})

    count = max(1, len(samples) // _ENV_WINDOW)
    trimmed = samples[: count * _ENV_WINDOW].astype(np.float64)
    env = np.sqrt((trimmed.reshape(count, _ENV_WINDOW) ** 2).mean(axis=1))
    peak = float(env.max())
    if peak <= _EPS:  # digital silence
        return SoundElement(
            str(path), duration, "other", 0.0, {"peak_time": 0.0, "peak_pos": 0.0}
        )

    peak_index = int(env.argmax())
    window_s = _ENV_WINDOW / float(rate)
    peak_time = (peak_index + 0.5) * window_s
    peak_pos = min(1.0, peak_time / duration)

    third = max(1, count // 3)
    head = float(env[:third].mean())
    tail = float(env[-third:].mean())
    rise_ratio = min(99.0, tail / (head + _EPS))

    after = env[peak_index:]
    if len(after) >= 3:
        steps = [
            1.0 if after[i + 1] <= after[i] * 1.05 + _EPS else 0.0
            for i in range(len(after) - 1)
        ]
        decay_score = sum(steps) / len(steps)
    else:
        decay_score = 1.0

    energy = np.abs(np.fft.rfft(trimmed)) ** 2
    freqs = np.fft.rfftfreq(len(trimmed), d=1.0 / rate)
    total = float(energy.sum())
    low_ratio = float(energy[freqs < _LOW_BAND_HZ].sum() / total) if total > 0 else 0.0

    edge_ratio = float(max(env[0], env[-1]) / peak)

    features = {
        "peak_time": round(peak_time, 4),
        "peak_pos": round(peak_pos, 4),
        "rise_ratio": round(rise_ratio, 4),
        "decay_score": round(decay_score, 4),
        "low_ratio": round(low_ratio, 4),
        "edge_ratio": round(edge_ratio, 4),
    }
    kind, confidence = classify_features(duration, features)
    return SoundElement(str(path), duration, kind, round(confidence, 4), features)


def scan_elements(
    folder: str | Path, cache_path: str | Path | None = None
) -> list[SoundElement]:
    """Scan & classify every audio file in ``folder`` (sorted by name).

    Results are cached in ``.monteur-elements.json`` inside the folder
    (``cache_path`` overrides), keyed ``path|mtime``: unchanged files are
    served from the cache, changed files re-analyze (their stale entries
    simply never match again), a corrupt cache is ignored wholesale.
    Raises :class:`MonteurMediaError` when the folder is missing or the
    media dependencies (numpy + ffmpeg) are not installed; a single
    undecodable FILE is listed as kind ``"other"`` with confidence 0
    instead of failing the scan.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise MonteurMediaError(f"not a directory: {folder}")
    files = sorted(
        p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS
    )
    if not files:
        return []

    # Fail on missing dependencies BEFORE any per-file work, with the same
    # clean install-hint errors the music analysis gives.
    try:
        import numpy  # noqa: F401
    except ImportError as exc:
        raise MonteurMediaError(
            "media features need numpy: pip install 'monteur[media]'"
        ) from exc
    find_ffmpeg()

    cache_file = Path(cache_path) if cache_path is not None else folder / CACHE_FILENAME
    cache = _load_cache(cache_file)
    elements: list[SoundElement] = []
    dirty = False
    for path in files:
        key = _cache_key(path)
        element = _element_from_entry(str(path), cache.get(key))
        if element is None:
            try:
                element = _analyze(path)
            except MonteurMediaError:
                # One broken file must not kill the library scan: keep it
                # visible (never placeable), don't cache the failure.
                elements.append(
                    SoundElement(str(path), 0.0, "other", 0.0, {"decode_error": 1.0})
                )
                continue
            cache[key] = {
                "duration": element.duration,
                "kind": element.kind,
                "confidence": element.confidence,
                "features": element.features,
            }
            dirty = True
        elements.append(element)
    if dirty:
        _save_cache(cache_file, cache)
    return elements


# --- assignment --------------------------------------------------------------------


@dataclass(frozen=True)
class _StyleRules:
    max_whooshes: int  # whoosh cues that may get a real file
    dip_impacts: bool  # place a hit right after each smash-to-black dip
    quiet_edges: bool  # leave the arc's opening/outro shares untouched
    drop_guard: float  # s: no whoosh this close BEFORE the drop (stutter zone)


_STYLE_RULES: dict[str, _StyleRules] = {
    "trailer": _StyleRules(3, True, False, 0.0),  # the full program
    "music_video": _StyleRules(3, True, False, 0.0),  # punchy
    "travel": _StyleRules(1, True, False, 4.0),  # sparse
    "wedding": _StyleRules(0, False, True, 4.0),  # minimal
    "auto": _StyleRules(2, True, False, 0.0),
}

_STYLE_NOTE_RE = re.compile(r'^style "([a-z_]+)"')


def _style_of(plan: MontagePlan) -> str:
    """The style key from the plan's own notes (plan_montage always writes it)."""
    from monteur.montage import STYLES

    for note in plan.notes:
        match = _STYLE_NOTE_RE.match(note)
        if match and match.group(1) in STYLES:
            return match.group(1)
    return "auto"


def _quiet_spans(style: str, duration: float) -> list[tuple[float, float]]:
    """The arc's opening and outro spans (approximate, from the raw shares)."""
    from monteur.montage import STYLES

    arc = STYLES[style].arc if style in STYLES else []
    spans: list[tuple[float, float]] = []
    t = 0.0
    for share, label in arc:
        span = (t, t + share * duration)
        if label in ("opening", "outro"):
            spans.append(span)
        t = span[1]
    return spans


def _drop_in_plan(plan: MontagePlan, music: MusicAnalysis | None) -> float | None:
    """The first drop in montage time, or None when out of range / no music."""
    if music is None:
        return None
    for d in sorted(music.drops):
        t = d - plan.music_start
        if _EPS < t < plan.duration - _EPS:
            return t
    return None


def assign_elements(
    plan: MontagePlan, music: MusicAnalysis | None, elements: list[SoundElement]
) -> list[str]:
    """Match & extend ``plan.sfx`` with concrete library files (in place).

    See the module docstring for the placement and density rules. Returns
    human-readable notes describing what was placed (and what stayed a
    search-query marker); the plan's cues gain ``file`` (and a duration
    equal to the file's play length, clamped to the montage end). Purely
    deterministic — same plan + same library = same result.
    """
    notes: list[str] = []
    pool = [
        e
        for e in elements
        if e.kind in _CUE_TO_ELEMENT.values() and e.duration > _EPS
    ]
    if not pool:
        return [
            "sound elements: no usable impact/whoosh/riser/braam files found — "
            "cues stay search-query markers"
        ]
    duration = plan.duration
    if duration <= _EPS:
        return ["sound elements: the plan has no duration; nothing placed"]

    style = _style_of(plan)
    rules = _STYLE_RULES.get(style, _STYLE_RULES["auto"])
    quiet = _quiet_spans(style, duration) if rules.quiet_edges else []
    already_filed = sum(1 for c in plan.sfx if getattr(c, "file", ""))

    uses: dict[str, list[float]] = {}
    for cue in plan.sfx:
        if cue.file:
            uses.setdefault(cue.file, []).append(cue.time)

    def in_quiet(t: float) -> bool:
        return any(lo - _EPS <= t < hi - _EPS for lo, hi in quiet)

    def reusable(path: str, t: float) -> bool:
        return all(abs(t - u) >= _REUSE_GAP - _EPS for u in uses.get(path, []))

    def overlaps_same_kind(kind: str, t: float, length: float) -> bool:
        for cue in plan.sfx:
            if (
                cue.file
                and cue.kind == kind
                and t < cue.time + cue.duration - _EPS
                and cue.time < t + length - _EPS
            ):
                return True
        return False

    def pick(
        element_kind: str, at: float, target: float | None = None
    ) -> SoundElement | None:
        candidates = [
            e for e in pool if e.kind == element_kind and reusable(e.path, at)
        ]
        if not candidates:
            return None
        if target is None:
            candidates.sort(key=lambda e: (-e.confidence, e.path))
        else:
            candidates.sort(
                key=lambda e: (abs(e.duration - target), -e.confidence, e.path)
            )
        return candidates[0]

    def file_cue(cue: SfxCue, element: SoundElement, why: str) -> None:
        room = duration - cue.time
        trimmed = element.duration > room + _EPS
        cue.file = element.path
        cue.duration = min(element.duration, room)
        uses.setdefault(element.path, []).append(cue.time)
        line = (
            f"{cue.kind} {Path(element.path).name} at "
            f"{cue.time:.1f}s ({why})"
        )
        if trimmed:
            line += (
                f" — trimmed to {cue.duration:.1f}s "
                f"(the file is {element.duration:.1f}s)"
            )
        notes.append(line)

    drop = _drop_in_plan(plan, music)

    # 1. The riser through the build, ENDING exactly on the drop. An existing
    #    riser cue already aimed at the drop is re-timed to the file; without
    #    one a new cue is added. The riser whose length best fits the run-up
    #    wins; a longer file is clamped (it still ends on the drop).
    if drop is not None and not in_quiet(drop):
        candidates = [
            e
            for e in pool
            if e.kind == "riser" and reusable(e.path, max(0.0, drop - e.duration))
        ]
        candidates.sort(key=lambda e: (abs(e.duration - drop), -e.confidence, e.path))
        chosen = candidates[0] if candidates else None
        if chosen is not None and not overlaps_same_kind(
            "riser", max(0.0, drop - chosen.duration), min(chosen.duration, drop)
        ):
            cue = next(
                (
                    c
                    for c in plan.sfx
                    if c.kind == "riser"
                    and not c.file
                    and abs(c.time + c.duration - drop) <= _NEAR_CUE
                ),
                None,
            )
            if cue is None:
                cue = SfxCue(
                    time=0.0,
                    duration=0.0,
                    kind="riser",
                    query=Path(chosen.path).stem,
                    note="riser into the drop",
                )
                plan.sfx.append(cue)
            length = min(chosen.duration, drop)  # clamped into the montage
            cue.time = drop - length
            cue.duration = length
            cue.file = chosen.path
            uses.setdefault(chosen.path, []).append(cue.time)
            line = (
                f"riser {Path(chosen.path).name} ends on the drop at {drop:.1f}s"
            )
            if chosen.duration > drop + _EPS:
                line += (
                    f" — trimmed to {length:.1f}s "
                    f"(the file is {chosen.duration:.1f}s)"
                )
            notes.append(line)

    # 2. An impact ON the drop: the plan's own drop/climax impact cue gets the
    #    best-fitting file (kind match + duration fit); without one a new cue
    #    is added with the most confident hit.
    if drop is not None and not in_quiet(drop):
        cue = next(
            (
                c
                for c in plan.sfx
                if c.kind == "impact" and not c.file and abs(c.time - drop) <= _NEAR_CUE
            ),
            None,
        )
        element = pick("impact", drop, target=cue.duration if cue else None)
        if element is not None and not overlaps_same_kind(
            "impact", drop, min(element.duration, duration - drop)
        ):
            if cue is None:
                cue = SfxCue(
                    time=drop,
                    duration=0.0,
                    kind="impact",
                    query=Path(element.path).stem,
                    note="on the drop",
                )
                plan.sfx.append(cue)
            file_cue(cue, element, "on the drop")

    # 3. An impact right AFTER each smash-to-black dip: the smash-cut hit,
    #    landing when the picture hits out of the black.
    if rules.dip_impacts:
        for dip_start, dip_len in sorted(plan.dips):
            t = dip_start + dip_len
            if not (_EPS < t < duration - _EPS) or in_quiet(t):
                continue
            element = pick("impact", t)
            if element is None:
                continue
            if overlaps_same_kind("impact", t, min(element.duration, duration - t)):
                continue
            cue = SfxCue(
                time=t,
                duration=0.0,
                kind="impact",
                query=Path(element.path).stem,
                note="hit out of the black",
            )
            plan.sfx.append(cue)
            file_cue(cue, element, "hit out of the black")

    # 4. The remaining planned cues, in time order: kind match + duration fit.
    #    Whooshes respect the style budget and the stutter guard before the
    #    drop; ambience cues have no snippet kind and stay markers.
    whoosh_budget = rules.max_whooshes
    for cue in sorted(plan.sfx, key=lambda c: (c.time, c.kind)):
        if cue.file:
            continue
        element_kind = _CUE_TO_ELEMENT.get(cue.kind)
        if element_kind is None or in_quiet(cue.time):
            continue
        if cue.kind == "whoosh":
            if whoosh_budget <= 0:
                continue
            center = cue.time + cue.duration / 2.0
            if (
                drop is not None
                and rules.drop_guard > 0
                and drop - rules.drop_guard - _EPS <= center < drop + _EPS
            ):
                continue  # the stutter burst owns the run-in to the drop
        element = pick(element_kind, cue.time, target=cue.duration)
        if element is None:
            continue
        if cue.kind == "riser":
            # A riser builds INTO its boundary: keep the cue's END anchored
            # and let the file run up to it (clamped at the montage start).
            end = cue.time + cue.duration
            length = min(element.duration, end)
            if length <= _EPS or overlaps_same_kind("riser", end - length, length):
                continue
            trimmed = element.duration > end + _EPS
            cue.time = end - length
            cue.duration = length
            cue.file = element.path
            uses.setdefault(element.path, []).append(cue.time)
            line = (
                f"riser {Path(element.path).name} ends at {end:.1f}s "
                f"({cue.note or 'act change'})"
            )
            if trimmed:
                line += (
                    f" — trimmed to {length:.1f}s "
                    f"(the file is {element.duration:.1f}s)"
                )
            notes.append(line)
            continue
        if overlaps_same_kind(
            cue.kind, cue.time, min(element.duration, duration - cue.time)
        ):
            continue
        file_cue(cue, element, cue.note or cue.kind)
        if cue.kind == "whoosh":
            whoosh_budget -= 1

    plan.sfx.sort(key=lambda c: c.time)
    placed = sum(1 for c in plan.sfx if c.file) - already_filed
    open_cues = sum(1 for c in plan.sfx if not c.file)
    summary = (
        f"sound elements: {placed} placed as real clips from "
        f"{len(pool)} usable files ({style} density)"
    )
    if open_cues:
        summary += f"; {open_cues} cue{'s' if open_cues != 1 else ''} stay search-query markers"
    notes.insert(0, summary)
    return notes


def carry_element_files(old_plan: MontagePlan, new_plan: MontagePlan) -> int:
    """Copy files from an old plan's cues onto a re-planned plan's cues.

    The revision hook: a re-plan rebuilds the SFX layer without files, but
    in untouched regions the new cues land at the same times — those get
    their old file (and its play length) back. Matching is strict (same
    kind, time within 50 ms, each old cue used once), so cues in genuinely
    replanned regions stay unfiled — re-run :func:`assign_elements` to
    fill them when the library folder is at hand. Returns how many cues
    were carried.
    """
    carried = 0
    used: set[int] = set()
    for cue in new_plan.sfx:
        if cue.file:
            continue
        for old in old_plan.sfx:
            if (
                id(old) not in used
                and old.file
                and old.kind == cue.kind
                and abs(old.time - cue.time) <= 0.05
            ):
                cue.file = old.file
                cue.duration = old.duration
                used.add(id(old))
                carried += 1
                break
    return carried
