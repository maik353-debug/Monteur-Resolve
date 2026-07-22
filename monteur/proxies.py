"""Playback proxies: small, seek-friendly MP4s for Studio's instant players.

The field gap this closes: in an NLE you scrub, start, stop and judge in
real time — Studio's storyboard used to make you *render* before you could
watch anything, and clip decisions came from a single thumbnail frame. The
standard editor answer is lightweight proxies: every source clip is
transcoded ONCE into a small uniform H.264 file the browser's ``<video>``
element can decode and seek instantly, and Studio's moment player + the
virtual timeline playout run entirely on those files (served with HTTP
Range by ``GET /api/media`` in :mod:`monteur.web.server`).

The proxy profile (:data:`PROXY_PROFILE` — the string is part of the cache
key, so changing the profile naturally re-transcodes):

* **540p max** — ``scale='trunc(min(960,iw)/2)*2':-2``: capped at 960 px
  wide (540 lines for 16:9), never upscaled, both sides forced even for
  the codec.
* **H.264 main / yuv420p, CRF 26, preset veryfast** — decodes everywhere a
  browser runs, encodes at many times realtime, small enough to keep a
  whole shoot cached.
* **Dense keyframes** (``-g 25`` ≈ one keyframe per second) — THE detail
  that makes scrubbing snappy: a ``<video>`` seek lands on the previous
  keyframe and decodes forward, so with keyframes every ~25 frames every
  seek is near-instant instead of chewing through seconds of GOP. Costs a
  little bitrate; worth every byte for an editing surface.
* **AAC 96k audio** (source channel layout kept — mono stays mono) so the
  clips' own sound plays in "original"/"mix" audio modes.
* **+faststart** — the ``moov`` atom is moved before ``mdat``, so the
  browser can start playback and satisfy byte-range seeks before the
  whole file arrived.

Cache: ``~/.monteur/proxies/`` (override with ``MONTEUR_PROXIES_PATH`` —
tests point it at scratch space), keyed by
``sha256(absolute path | mtime_ns | PROXY_PROFILE)`` so an edited or
replaced clip re-transcodes and an unchanged one never does.
:func:`prune_proxies` keeps the cache bounded (default 5 GB), deleting
oldest-mtime files first — proxies are pure derivatives, deleting one
only costs a re-transcode.

Everything runs through the same ffmpeg discipline as :mod:`monteur.media`
/ :mod:`monteur.preview` (:func:`monteur.preview._run_ffmpeg`): list-argv,
no shell, failures raise :class:`monteur.media.MonteurMediaError` with the
stderr tail. Proxies are an upgrade, never a gate — every caller falls
back to serving the original file when a proxy is missing or failed.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from monteur.media import MediaCancelled, MonteurMediaError, probe

# Shared ffmpeg runner — one error contract for every engine (see the
# module docstring; reusing it here is deliberate, not an accident).
from monteur.preview import _run_ffmpeg as run_ffmpeg

#: Encode profile fingerprint — part of every cache key, so changing any
#: encode parameter below MUST bump this string (old proxies then age out
#: of the cache via :func:`prune_proxies`).
PROXY_PROFILE = "v1-540p-h264crf26-veryfast-g25-aac96k-faststart"

#: Default cache budget for :func:`prune_proxies`, in gigabytes.
PROXY_MAX_GB = 5.0

# 540p max, never upscaled, even dimensions (codec requirement): width is
# min(960, iw) truncated to even, height follows the aspect ratio (-2).
_SCALE = "scale='trunc(min(960,iw)/2)*2':-2"


def proxies_dir() -> Path:
    """The proxy cache directory (not created here — :func:`ensure_proxy`
    creates it on first write).

    ``MONTEUR_PROXIES_PATH`` overrides the default ``~/.monteur/proxies``
    — tests point it at scratch space so they never touch (or depend on)
    a developer's real cache.
    """
    override = os.environ.get("MONTEUR_PROXIES_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".monteur" / "proxies"


def proxy_path(clip_path: str | Path) -> Path:
    """The cache file a proxy of ``clip_path`` lives at (existing or not).

    Keyed by ``sha256(absolute path | mtime_ns | PROXY_PROFILE)`` — the
    mtime makes an edited/replaced clip a NEW key (the stale proxy simply
    ages out via :func:`prune_proxies`), the profile makes an encode
    change a new key. Raises :class:`MonteurMediaError` when the clip
    itself does not exist (there is no honest key without an mtime).
    """
    clip_abs = os.path.abspath(str(clip_path))
    try:
        mtime_ns = os.stat(clip_abs).st_mtime_ns
    except OSError as exc:
        raise MonteurMediaError(f"cannot stat {clip_abs}: {exc}") from exc
    key = f"{clip_abs}|{mtime_ns}|{PROXY_PROFILE}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return proxies_dir() / f"{digest}.mp4"


def fresh_proxy(clip_path: str | Path) -> Path | None:
    """The existing, non-empty proxy for ``clip_path`` — or ``None``.

    The soft sibling of :func:`proxy_path` for serving paths: any problem
    (clip gone, cache unreadable, empty file) is ``None``, never an error
    — the caller serves the original file instead.
    """
    try:
        proxy = proxy_path(clip_path)
    except MonteurMediaError:
        return None
    try:
        if proxy.is_file() and proxy.stat().st_size > 0:
            return proxy
    except OSError:
        return None
    return None


def ensure_proxy(clip_path: str | Path, *, progress=None, cancel=None) -> Path:
    """Transcode ``clip_path`` into the proxy cache (once) and return the
    proxy's path.

    Skips the transcode entirely when a fresh proxy already exists (same
    path, same mtime, same profile). The transcode writes to a private
    ``.part`` name and moves into place atomically, so two concurrent
    callers can never serve half a file. ``progress(done, total, name)``
    — when given — is called once when the file is ready (fresh or
    transcoded alike). ``cancel`` (anything with ``.is_set()``) is threaded
    into the transcode so a set flag kills the running ffmpeg within a poll
    interval and raises :class:`monteur.media.MediaCancelled`. Raises
    :class:`MonteurMediaError` when the clip is missing/unreadable or ffmpeg
    fails (passthrough — callers decide how soft to be).
    """
    clip = os.path.abspath(str(clip_path))
    name = Path(clip).name
    proxy = proxy_path(clip)  # MonteurMediaError when the clip is gone

    def tick() -> Path:
        if progress is not None:
            progress(1, 1, name)
        return proxy

    try:
        if proxy.is_file() and proxy.stat().st_size > 0:
            return tick()  # fresh — skip the transcode entirely
    except OSError:
        pass  # unreadable cache entry — re-transcode below
    proxy.parent.mkdir(parents=True, exist_ok=True)

    try:
        has_audio = probe(clip).has_audio
    except MonteurMediaError:
        has_audio = False  # let the video-only transcode try anyway

    part = proxy.with_name(f"{proxy.stem}.{secrets.token_hex(4)}.part.mp4")
    args = [
        "-i", clip,
        "-map", "0:v:0", "-vf", _SCALE,
        "-c:v", "libx264", "-profile:v", "main", "-preset", "veryfast",
        "-crf", "26", "-pix_fmt", "yuv420p",
        # Dense keyframes = snappy seeking (see the module docstring).
        "-g", "25",
    ]
    if has_audio:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "96k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(part)]
    try:
        run_ffmpeg(args, f"transcoding a playback proxy for {name}", cancel)
        if not part.is_file() or part.stat().st_size == 0:
            raise MonteurMediaError(f"ffmpeg wrote no proxy for {name}")
        os.replace(part, proxy)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
    return tick()


def ensure_proxies(
    paths, progress=None, *, cancel=None
) -> tuple[dict[str, str], dict[str, str]]:
    """Sequentially ensure a proxy for every path in ``paths``.

    Returns ``(made, errors)``: ``made`` maps clip path -> proxy path for
    every success, ``errors`` maps clip path -> message for every failure
    (per-file soft — one broken clip never blocks the rest; playback of
    that clip simply falls back to the original file).
    ``progress(done, total, name)`` fires once per finished file (fresh
    cache hits included). ``cancel`` — an object with ``is_set()`` (e.g.
    a ``threading.Event``) — stops the batch between files AND is threaded
    into each transcode, so the ffmpeg RUNNING when cancel is set is killed
    within a poll interval rather than finishing first.
    """
    items = [str(p) for p in paths]
    total = len(items)
    made: dict[str, str] = {}
    errors: dict[str, str] = {}
    for index, path in enumerate(items):
        if cancel is not None and cancel.is_set():
            break
        name = Path(path).name
        try:
            made[path] = str(ensure_proxy(path, cancel=cancel))
        except MediaCancelled:
            # The running transcode was killed mid-flight by a set cancel.
            # Stop the batch immediately (the caller re-checks cancel and
            # reports the job as cancelled); nothing partial is recorded.
            break
        except MonteurMediaError as exc:
            errors[path] = str(exc)
        if progress is not None:
            progress(index + 1, total, name)
    return made, errors


def prune_proxies(max_gb: float = PROXY_MAX_GB) -> list[Path]:
    """Delete oldest-mtime proxies until the cache fits ``max_gb``.

    Returns the deleted paths (oldest first). Best-effort throughout: a
    missing cache directory or an unlinkable file is skipped, never an
    error — proxies are derivatives, the worst case is a re-transcode.
    """
    directory = proxies_dir()
    try:
        candidates = [p for p in directory.iterdir() if p.suffix == ".mp4"]
    except OSError:
        return []
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    budget = max(0.0, float(max_gb)) * 1024**3
    removed: list[Path] = []
    for _mtime, size, path in sorted(entries):
        if total <= budget:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path)
    return removed


def cache_size() -> dict:
    """Total bytes and file count of the proxy cache (best-effort, never raises).

    Proxies are a global, project-independent cache (keyed by clip path), so
    deleting a project can't safely delete "its" proxies — a shared clip may
    still be used elsewhere. This backs the Settings display + the cap.
    """
    directory = proxies_dir()
    total = 0
    count = 0
    try:
        for path in directory.iterdir():
            if path.suffix != ".mp4":
                continue
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
    except OSError:
        pass
    return {"bytes": total, "count": count}


def clear_proxies() -> int:
    """Delete every cached proxy; returns how many were removed.

    Safe: proxies are pure derivatives — anything still needed is re-transcoded
    on next playback. Use for the Settings "Clear cache" action.
    """
    directory = proxies_dir()
    removed = 0
    try:
        candidates = [p for p in directory.iterdir() if p.suffix == ".mp4"]
    except OSError:
        return 0
    for path in candidates:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
