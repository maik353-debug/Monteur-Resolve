"""Semantic vision: Claude looks at the footage and says what it shows.

The sift (:mod:`monteur.sift`) finds technically good moments — sharp,
well exposed, steadily moving — but it cannot tell a mountain pass from a
parking lot. This module extracts one keyframe per good moment, shows it to
a Claude vision model, and annotates each :class:`~monteur.sift.Moment`
IN PLACE with editorial meaning:

* ``label`` — one line of what the frame shows ("overtake in a left-hand curve")
* ``tags`` — 2-5 lowercase keywords for search and shot casting ("curve",
  "mountains")
* ``role`` — dramaturgical potential: ``"opener"`` (establishing /
  scene-setting), ``"build"`` (rising action), ``"climax"`` (peak action or
  spectacle), ``"closer"`` (calm, resolving); ``""`` = unknown
* ``hero`` — 0..1 hero-shot strength, so the montage planner can put its
  most striking images on the musical drops
* ``group`` — a short scene-similarity key: moments in the same group show
  visually the same scene, so the planner can avoid cutting two
  near-identical shots back to back

Cost control, because vision requests are billed per image:

* at most ``max_moments`` moments per run — best score first, but at least
  one per clip when possible (every clip deserves a chance in the cut);
* keyframes are small JPEGs (scaled to ``frame_height`` px) piped straight
  out of ffmpeg's stdout, never written to disk;
* moments travel to the API in batches of up to 8 per request;
* results are cached in ``.monteur-vision.json`` next to the footage, keyed
  by absolute path + file mtime + moment window + model, so re-running a
  sift costs nothing until the footage or the model changes. The cache is
  written after every successful batch, so an interrupted run keeps its
  progress.

A clip whose frame extraction fails gets a note and is skipped — a corrupt
file must never abort annotating the rest of the shoot.

Requires the optional AI extra (``pip install 'monteur[ai]'``) and Claude
credentials (``ANTHROPIC_API_KEY``), like everything in :mod:`monteur.ai`.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from monteur.media import find_ffmpeg
from monteur.sift import ClipReport, Moment

DEFAULT_VISION_MODEL = "claude-haiku-4-5-20251001"  # cheap+fast vision; env MONTEUR_VISION_MODEL overrides

# Cost / request tuning.
_BATCH_SIZE = 8  # moments (images) per API request
_MAX_TOKENS = 2000  # per batch: ~6 short fields x 8 moments needs far less
_JPEG_QUALITY = 4  # ffmpeg -q:v: 2 is visually lossless, 4 is plenty for scene description
_MAX_TAGS = 5  # keep tags a keyword set, not a caption
_VALID_ROLES = ("opener", "build", "climax", "closer")  # "" = unknown/not analyzed

#: Cache file name, created next to the first report's footage.
CACHE_FILENAME = ".monteur-vision.json"

_SYSTEM = (
    "You are an experienced film editor's assistant logging action-cam and "
    "travel footage — motorcycle POV rides, road trips, landscapes. For each "
    "numbered moment you see one frame. For every moment report: 'label' — one "
    "short line of WHAT the frame shows, concrete and visual ('overtake in a "
    "left-hand curve', 'sunset over a mountain ridge'); 'tags' — 2-5 lowercase "
    "keywords ('curve', 'mountains', 'tunnel'); 'role' — its dramaturgical "
    "potential in a montage: 'opener' for establishing / scene-setting images, "
    "'build' for rising action, 'climax' for peak action or spectacle, "
    "'closer' for calm resolving images, or '' when unclear; 'hero' — 0..1 "
    "hero-shot strength, where 1 is a striking montage-defining image and 0 is "
    "ordinary coverage; 'group' — a short lowercase key naming the scene or "
    "location, chosen so that visually-same scenes share the same group. Echo "
    "each moment's index unchanged."
)

# Structured output schema (see monteur.brief for the convention): the model
# MUST return one entry per shown moment, keyed back by index.
_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "label": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "role": {
                        "type": "string",
                        "enum": ["opener", "build", "climax", "closer", ""],
                    },
                    "hero": {"type": "number"},
                    "group": {"type": "string"},
                },
                "required": ["index", "label", "tags", "role", "hero", "group"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


class MonteurVisionError(RuntimeError):
    """Raised when vision analysis is unavailable or a request fails."""


def _client():
    """Create the Claude client; tests monkeypatch this single seam.

    A missing package raises immediately with an actionable message. A
    missing ANTHROPIC_API_KEY typically only surfaces at request time (the
    SDK resolves credentials lazily) — that path is caught broadly around
    the request itself in :func:`_describe_batch`.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise MonteurVisionError(
            "vision analysis needs the 'anthropic' package: pip install 'monteur[ai]'"
        ) from exc
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # pragma: no cover - constructor-time auth failures
        raise MonteurVisionError(
            "could not create the Claude client — footage vision needs an "
            "Anthropic API key (ANTHROPIC_API_KEY) specifically; unlike the "
            f"writing features it cannot use the Claude Code CLI: {exc}"
        ) from exc


# --- selection ----------------------------------------------------------------


def _select_moments(
    reports: list[ClipReport], max_moments: int
) -> list[tuple[ClipReport, Moment]]:
    """Pick up to ``max_moments`` moments across all reports.

    Best score first — but every clip gets its own best moment reserved when
    the budget allows, so a shoot with one spectacular clip does not starve
    the quieter clips out of the cut entirely. The returned list is in
    processing order (file order, then time), which keeps ffmpeg seeks and
    progress output tidy.
    """
    if max_moments <= 0:
        return []
    position = {id(r): i for i, r in enumerate(reports)}
    best: list[tuple[ClipReport, Moment]] = []
    rest: list[tuple[ClipReport, Moment]] = []
    for report in reports:
        if not report.moments:
            continue
        ranked = sorted(report.moments, key=lambda m: (-m.score, m.start))
        best.append((report, ranked[0]))
        rest.extend((report, m) for m in ranked[1:])
    # One reserved slot per clip; if there are more clips than slots, the
    # best-scoring clips win their slot.
    best.sort(key=lambda rm: (-rm[1].score, position[id(rm[0])]))
    picked = best[:max_moments]
    rest.sort(key=lambda rm: (-rm[1].score, position[id(rm[0])], rm[1].start))
    picked.extend(rest[: max(0, max_moments - len(picked))])
    picked.sort(key=lambda rm: (position[id(rm[0])], rm[1].start))
    return picked


# --- cache ----------------------------------------------------------------------


def _moment_key(path: str, model: str, moment: Moment) -> str:
    """Cache key: file identity (abspath + mtime) | moment window | model.

    The mtime makes re-exported or re-copied footage a cache MISS (the pixels
    may differ); the model makes an upgrade re-annotate rather than serve
    stale descriptions from a weaker model.
    """
    abspath = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        mtime = 0.0  # missing file: extraction will fail and note the clip
    return f"{abspath}|{mtime}|{moment.start:.2f}-{moment.end:.2f}|{model}"


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


# --- validation -------------------------------------------------------------------


def _clean_tags(raw: object) -> list[str]:
    """Lowercased, deduplicated, at most _MAX_TAGS non-empty tags."""
    tags: list[str] = []
    if isinstance(raw, (list, tuple)):
        for tag in raw:
            tag = str(tag).strip().lower()
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= _MAX_TAGS:
                break
    return tags


def _clamped(raw: object) -> dict | None:
    """Turn a (model- or cache-produced) entry into safe annotation values.

    Everything is validated defensively: an unknown role becomes "" rather
    than poisoning the planner's role matching, hero is clamped to 0..1, tags
    are normalized. Returns None for something that is not a dict at all.
    """
    if not isinstance(raw, dict):
        return None
    label = " ".join(str(raw.get("label", "")).split())  # force one line
    role = raw.get("role", "")
    if role not in _VALID_ROLES:
        role = ""
    try:
        hero = float(raw.get("hero", 0.0))
    except (TypeError, ValueError):
        hero = 0.0
    hero = min(1.0, max(0.0, hero))
    group = " ".join(str(raw.get("group", "")).split()).lower()
    return {
        "label": label,
        "tags": _clean_tags(raw.get("tags")),
        "role": role,
        "hero": hero,
        "group": group,
    }


def _apply(moment: Moment, entry: object) -> bool:
    """Copy a validated annotation onto the moment; False if unusable."""
    clean = _clamped(entry)
    if clean is None:
        return False
    moment.label = clean["label"]
    moment.tags = clean["tags"]
    moment.role = clean["role"]
    moment.hero = clean["hero"]
    moment.group = clean["group"]
    return True


# --- keyframes ----------------------------------------------------------------------


def _extract_frame(path: str, t: float, height: int) -> bytes:
    """One JPEG at ``t`` seconds, scaled to ``height`` px, from ffmpeg's stdout.

    ``-ss`` before ``-i`` seeks on keyframes (fast, and exactness does not
    matter for a representative frame); ``scale=-2:H`` keeps the aspect ratio
    with an even width, as codecs require. No temp files: the image is read
    from the pipe.
    """
    cmd = [
        find_ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
        "-frames:v", "1", "-vf", f"scale=-2:{height}",
        "-f", "image2", "-c:v", "mjpeg", "-q:v", str(_JPEG_QUALITY), "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", "replace")[-300:]
        raise MonteurVisionError(f"could not extract a frame from {path}: {stderr}")
    return result.stdout


def _mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


# --- the API call -------------------------------------------------------------------


def _describe_batch(client, model: str, batch: list[tuple]) -> dict[int, dict]:
    """One vision request for up to _BATCH_SIZE moments; {index: clean entry}.

    Each moment is a text block ("Moment N: <clipname> at MM:SS") followed by
    its keyframe as a base64 JPEG image block; the structured-output schema
    (guaranteed JSON, like monteur.brief) carries the annotations back keyed
    by that index.
    """
    content: list[dict] = []
    for n, (_i, report, moment, _key, jpeg) in enumerate(batch, start=1):
        midpoint = (moment.start + moment.end) / 2
        content.append(
            {
                "type": "text",
                "text": f"Moment {n}: {Path(report.path).name} at {_mmss(midpoint)}",
            }
        )
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(jpeg).decode("ascii"),
                },
            }
        )
    content.append(
        {"type": "text", "text": f"Describe all {len(batch)} moments above."}
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except MonteurVisionError:
        raise
    except Exception as exc:  # broad: missing ANTHROPIC_API_KEY surfaces here
        raise MonteurVisionError(
            f"Claude vision request failed: {exc} — footage vision sends "
            "images, so unlike Monteur's writing features it cannot use the "
            "Claude Code CLI: it needs the 'anthropic' package (pip install "
            "'monteur[ai]') and an Anthropic API key (ANTHROPIC_API_KEY)."
        ) from exc
    if getattr(response, "stop_reason", None) == "refusal":
        raise MonteurVisionError(
            "The request was declined by the model's safety system."
        )
    raw = "".join(b.text for b in response.content if b.type == "text")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MonteurVisionError(
            f"Claude returned unparseable vision annotations: {raw[:200]!r}"
        ) from exc
    items = data.get("moments") if isinstance(data, dict) else data
    described: dict[int, dict] = {}
    if isinstance(items, list):
        for entry in items:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            clean = _clamped(entry)
            if clean is not None:
                described[index] = clean
    return described


def _call_progress(progress, index, total, name, stage) -> None:
    """Invoke the progress callback, swallowing any exception it raises."""
    if progress is None:
        return
    try:
        progress(index, total, name, stage)
    except Exception:  # noqa: BLE001 — a broken callback must not abort analysis
        pass


# --- public API ----------------------------------------------------------------------


def analyze_reports(
    reports: list[ClipReport],
    *,
    model: str | None = None,
    max_moments: int = 48,
    frame_height: int = 360,
    progress: Callable | None = None,
    cache_path: str | Path | None = None,
) -> list[str]:
    """Annotate the reports' best moments with Claude vision, IN PLACE.

    Selects up to ``max_moments`` moments across all reports (best score
    first, but at least one per clip when possible), extracts one keyframe
    per moment, and fills each Moment's label/tags/role/hero/group. Returns
    human-readable notes (e.g. ``"14 moments analyzed, 6 from cache"``).

    ``model`` defaults to the ``MONTEUR_VISION_MODEL`` environment variable,
    then :data:`DEFAULT_VISION_MODEL`. ``progress`` is called as
    ``progress(index, total, name, stage)`` with stage ``"frames"`` (keyframe
    extraction), ``"vision"`` (annotation arrived from the API) or
    ``"cache"`` (served without an API call); exceptions it raises are
    swallowed. ``cache_path`` defaults to ``.monteur-vision.json`` in the
    folder of the first report's footage.

    Raises :class:`MonteurVisionError` when the ``anthropic`` package is
    missing or a request fails (e.g. no ``ANTHROPIC_API_KEY``); because the
    cache is written after each successful batch, an interrupted run keeps
    its progress. A clip whose frame extraction fails only gets a note.
    """
    model = model or os.environ.get("MONTEUR_VISION_MODEL") or DEFAULT_VISION_MODEL
    selected = _select_moments(reports, max_moments)
    if not selected:
        return ["no moments to analyze"]
    total_available = sum(len(r.moments) for r in reports)

    if cache_path is None:
        folder = os.path.dirname(os.path.abspath(reports[0].path))
        cache_path = Path(folder) / CACHE_FILENAME
    else:
        cache_path = Path(cache_path)
    cache = _load_cache(cache_path)

    notes: list[str] = []
    total = len(selected)
    cached_count = 0
    api_count = 0
    failed_paths: set[str] = set()
    # (selection index, report, moment, cache key, jpeg bytes)
    pending: list[tuple[int, ClipReport, Moment, str, bytes]] = []

    for i, (report, moment) in enumerate(selected, start=1):
        name = Path(report.path).name
        key = _moment_key(report.path, model, moment)
        entry = cache.get(key)
        if isinstance(entry, dict) and _apply(moment, entry):
            cached_count += 1
            _call_progress(progress, i, total, name, "cache")
            continue
        if report.path in failed_paths:
            continue  # the clip already failed to yield a frame — skip it
        _call_progress(progress, i, total, name, "frames")
        try:
            midpoint = (moment.start + moment.end) / 2
            jpeg = _extract_frame(report.path, midpoint, frame_height)
        except Exception as exc:  # noqa: BLE001 — one bad clip must not abort the run
            failed_paths.add(report.path)
            notes.append(f"{name}: could not extract a frame — clip skipped ({exc})")
            continue
        pending.append((i, report, moment, key, jpeg))

    client = None  # created lazily: an all-cache run never needs credentials
    for start in range(0, len(pending), _BATCH_SIZE):
        batch = pending[start : start + _BATCH_SIZE]
        if client is None:
            client = _client()
        described = _describe_batch(client, model, batch)
        for n, (i, report, moment, key, _jpeg) in enumerate(batch, start=1):
            entry = described.get(n)
            if entry is None:
                continue  # the model dropped a moment — leave it unannotated
            _apply(moment, entry)
            cache[key] = entry
            api_count += 1
            _call_progress(progress, i, total, Path(report.path).name, "vision")
        # Persist after every batch so an interrupted run keeps its progress.
        _save_cache(cache_path, cache)

    analyzed = cached_count + api_count
    notes.insert(0, f"{analyzed} moments analyzed, {cached_count} from cache")
    if total < total_available:
        notes.insert(
            1, f"selected the best {total} of {total_available} moments (cost cap)"
        )
    return notes
