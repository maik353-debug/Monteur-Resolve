"""Sehen ohne Resolve: render a MontagePlan to video, plus thumbnails.

This is the engine behind Studio's preview player, storyboard AND the
Direct Export: a :class:`~monteur.montage.MontagePlan` becomes a
low-resolution, uniformly encoded MP4 (:func:`render_preview`), a
full-quality upload-ready MP4 (:func:`render_export`), and any clip
position becomes a storyboard thumbnail (:func:`extract_thumbnail`) — no
DaVinci Resolve, no export/import round-trip. The point is an honest
render of the cut the plan describes: the same source ranges, the same
record positions, the same black dips, the same music offset the Resolve
timeline would get.

Dependency story: exactly like :mod:`monteur.media` — everything runs
through the same ffmpeg binary located by :func:`monteur.media.find_ffmpeg`
($FFMPEG_BINARY, then PATH, then the ``[media]`` extra's bundled
imageio-ffmpeg). A missing binary or a failing ffmpeg run raises
:class:`monteur.media.MonteurMediaError` (with the stderr tail), never a
bare subprocess error. All invocations are list-argv (no ``shell=True``),
so paths with spaces work on every platform including Windows.

Pipeline shape (:func:`render_preview`): one segment file per plan entry
(fast-seek trim of the entry's real source range, scaled/padded to a
uniform even-sized canvas, h264 ``ultrafast`` / crf 28 / yuv420p),
lavfi ``color=black`` segments for the record gaps between entries (the
plan's dips ARE those gaps — entries' record ranges tile the montage
around them), then one concat-demuxer pass that muxes the audio, applies
the plan's fades (video ``fade`` + ``afade``) and writes the final MP4.
Segments are small MP4s with identical codec/size/rate parameters — safe
for the concat demuxer, and (unlike mpegts) readable by every ffmpeg
build we ship against; the final pass re-encodes, so the container choice
costs nothing. Intermediate files live in a private ``tempfile.mkdtemp``
directory that is removed on success AND failure.

Audio modes mirror :func:`monteur.montage.montage_to_timeline`:

* ``"music"`` — the plan's song, trimmed from ``music_start`` for
  ``duration`` seconds (the segments carry no audio).
* ``"original"`` — each entry's own sound over its source range; dips get
  ``anullsrc`` silence so the concat stream stays continuous.
* ``"mix"`` — both, summed at FIXED levels: music at
  :data:`MIX_MUSIC_LEVEL` (1.0) and the clips' original sound ducked to
  :data:`MIX_ORIGINAL_LEVEL` (0.6) — a preview approximation of "song on
  A1, camera sound under it", not a mixing console.

Limitations (deliberate, PREVIEW only): SFX cues with placed ``file``\\ s
are NOT mixed in; dissolves (``MontageEntry.transition``) render as hard
cuts. The preview is a fast, rough look — both ARE rendered by
:func:`render_export`, the full-quality path (real ``xfade`` dissolves,
placed sound effects, YouTube-target loudness), which shares the segment
machinery above but encodes for delivery instead of speed. Act titles
(``plan.title_texts``) ARE drawn in BOTH renderers (blueprint 1.8 — a
preview without the titles makes every A/B look review a surrogate):
each titled black dip gets the same centered ``drawtext``, with the same
defensive probing (an ffmpeg build without the filter, or no usable
font, silently leaves the preview dips plain black; the export
additionally notes the degradation).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import json
import math
import re

from monteur import reframe as _reframe
from monteur.media import (
    MediaCancelled,
    MonteurMediaError,
    _CANCEL_POLL_S,
    find_ffmpeg,
    probe,
)
from monteur.montage import (
    MontagePlan,
    jl_audio_edits,
    music_bed_gaps,
    music_window_bounds,
    plan_pulse,
    quantize_finish,
)

# Fixed "mix" levels (documented above): the song stays at full level, the
# clips' own sound is ducked under it.
MIX_MUSIC_LEVEL = 1.0
MIX_ORIGINAL_LEVEL = 0.6

# Adaptive music window (plan.music_in / plan.music_out): a delayed entry
# gets a short musical fade-in so the slam does not click; the bed is
# trimmed and delayed (adelay) to its record window in both renderers.
_MUSIC_FADE_IN = 0.5
# Deliberate silence (plan.music_gaps): the bed's volume is gated to 0
# inside every gap via one `volume` filter per gap, chained linearly
# (chained volume envelopes multiply, so the gates compose). Each gate is
# a trapezoid with a _GAP_FADE micro-fade on both edges — 50 ms sits in
# the click-free 30-60 ms band while the re-entry still reads SHARP (the
# 0.5 s _MUSIC_FADE_IN stays exclusive to the music_in entry). The
# expression is evaluated per audio frame (eval=frame), so an
# `asetnsamples` in front forces _GAP_GATE_SAMPLES-sample frames (~5 ms
# at 48 kHz — ~10 evaluation steps across each micro-fade, inaudible
# stepping). Chosen over an asplit/atrim/amix segment graph: one linear
# chain works identically in the preview's -af string, the mix chain and
# the export's filter_complex, with no per-gap stream bookkeeping.
_GAP_FADE = 0.05
_GAP_GATE_SAMPLES = 256

# Preview encode: speed over size — this is a look, not a deliverable.
_PRESET = "ultrafast"
_CRF = "28"
_AUDIO_RATE = 48000
_AUDIO_CHANNELS = 2
# Record gaps shorter than this are rounding noise, not dips.
_MIN_GAP = 0.01
# Same mode names as montage_to_timeline.
_AUDIO_MODES = ("music", "mix", "original")
# How much ffmpeg stderr to carry into the error message.
_STDERR_TAIL = 400

# --- Direct Export (render_export) -------------------------------------------
#
# Encode profiles for the finished video. "high" is the upload master:
# visually transparent H.264 (crf 18) with 320 kbit/s AAC at 48 kHz —
# comfortably above YouTube's own re-encode, so nothing is lost twice.
# Preset choice was measured on real 1080p material: "slow" took ~1.8x
# "medium" (5.7 s vs 3.2 s for 6 s of footage) — under the 2x line, so
# "high" keeps the better-compressing "slow". "medium" (crf 21, preset
# medium, 192 kbit/s AAC) is the smaller share/review file. Both are
# yuv420p with the moov atom up front (+faststart), the YouTube-friendly
# layout that lets playback start before the download finishes.
EXPORT_QUALITIES: dict[str, dict[str, str]] = {
    "high": {"crf": "18", "preset": "slow", "audio_bitrate": "320k", "seg_crf": "14"},
    "medium": {"crf": "21", "preset": "medium", "audio_bitrate": "192k", "seg_crf": "17"},
}
# Segment intermediates are encoded near-lossless (profile crf - 4) with a
# fast preset: the FINAL pass defines the delivered quality, the segment
# pass must only not lose anything on the way there.
_SEG_PRESET = "faster"
# Act titles on the dips: plain white, centered, sized from the canvas
# height; when the dip is long enough the text fades in/out this long.
# Blueprint 1.7: 0.3 s is the TARGET — a plan that knows its tempo
# (persisted downbeat marks) beat-quantizes the fade through the shared
# monteur.montage.quantize_finish helper (capped at _TITLE_FADE_MAX so a
# slow song cannot eat the dip); beatless plans keep 0.3 s byte-for-byte.
_TITLE_FADE = 0.3
_TITLE_FADE_MAX = 0.5
# Loudness finish (blueprint 1.4): render_export runs TRUE two-pass
# loudnorm at YouTube's streaming target (-14 LUFS integrated, -1 dBTP
# true peak, LRA 11) — pass 1 renders the finished audio chain into a
# null muxer with print_format=json and measures it, pass 2 feeds the
# measured values back with linear=true, so the correction is one clean
# gain instead of a dynamic compressor chasing the mix. The measurement
# is cached only inside the one render call (same-call, never across
# calls — the chain's inputs define it). A failed/unparseable
# measurement degrades to this single-pass string with an honest note;
# render_preview stays a single pass with no loudness finish at all —
# it is a look, not a deliverable, and speed wins there.
_EXPORT_LOUDNORM = "loudnorm=I=-14:TP=-1:LRA=11"
_LOUDNORM_I = -14.0
_LOUDNORM_TP = -1.0
_LOUDNORM_LRA = 11.0
# --- Bed ducking (blueprint 1.4) ---------------------------------------------
# The MUSIC bed ducks under every placed SFX accent and (mix mode) under
# prominent original-sound moments — multiplying volume envelopes in the
# same linear chain as the music-gap gates: chained `volume` filters
# multiply per sample, so gates (x0) and ducks (x0.5) compose without any
# stream splitting, and a duck inside a gap simply multiplies into the
# mute. Fixed, documented depths, deterministic by construction.
# W2 seam (O-Ton pops, blueprint 2.2): the SAME envelope machinery LIFTS
# the original bed over the song for a marked original-sound moment — the
# other side of the 1.4 ducking coin. Under a prominent O-Ton window the
# MUSIC ducks (1.4, already here); in the SAME window the ORIGINAL now
# lifts (:func:`oton_lift_windows`, applied on the original chain in
# render_export mix mode). Trapezoid + floor > 1, one linear chain — the
# lift is a duck with a gain above 1. Export only, mix only: the preview
# stays lean exactly as it skips the 1.4 ducking (a pop is a deliverable).
_DUCK_ACCENT_DB = -6.0  # impact / sub-drop (braam): the hit owns its window
_DUCK_RISER_DB = -4.0  # riser: a gentler shelf — it must READ above the bed
_DUCK_OTON_DB = -4.0  # mix mode: prominent original sound gets room
_DUCK_ACCENT_MAX_S = 1.5  # an accent duck never dips longer than this
_DUCK_FADE = 0.05  # accent edges: the gap gates' click-free micro-fade
_DUCK_SHELF_FADE = 0.25  # riser/O-Ton edges: a shelf eases, it does not dip
# Mix mode: a bed part whose mean level stands this many dB above the
# median of all sounding parts is a "prominent" original-sound moment.
_DUCK_OTON_STANDOUT_DB = 6.0
# O-Ton pop (blueprint 2.2): the documented LIFT applied to the original
# chain over each prominent window — the mirror of _DUCK_OTON_DB. Honest
# headroom: after the lift AND the MIX_ORIGINAL_LEVEL attenuation the
# original's measured true peak must not break _LIFT_OTON_TP_CEIL (−1
# dBTP); the boost is clamped per window and the plan says when it clamped.
_LIFT_OTON_DB = 3.5
_LIFT_OTON_TP_CEIL = -1.0
# Dissolves shorter than this are rounding noise, not transitions.
_MIN_TRANSITION = 0.01
# Font candidates for drawtext, probed in order (Linux, macOS, Windows).
# Missing everywhere = titles are skipped with a note, never a failure.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _subprocess_run_cancellable(cmd: list[str], cancel):
    """``subprocess.run(cmd, capture_output=True)`` that a cancel can kill.

    ``cancel is None`` (the default everywhere) → the plain blocking
    ``subprocess.run`` call, byte-identical to before. When a cancel object
    (anything with ``.is_set()``) is passed, ffmpeg runs under a Popen that is
    polled every ``_CANCEL_POLL_S``; the moment the flag is set the process is
    killed (and reaped — no zombies) and :class:`MediaCancelled` is raised.
    Mirrors :func:`monteur.media._run`'s poll-and-kill loop.
    """
    if cancel is None:
        return subprocess.run(cmd, capture_output=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_CANCEL_POLL_S)
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                proc.kill()
                proc.wait()  # reap — never leave a zombie behind
                raise MediaCancelled("ffmpeg run cancelled")
            continue
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _run_ffmpeg(args: list[str], label: str, cancel=None) -> None:
    """Run ffmpeg with ``args``; raise MonteurMediaError on any failure.

    ``cancel`` (anything with ``.is_set()``) makes the run killable: a set
    flag kills the running ffmpeg within a poll interval and raises
    :class:`MediaCancelled`. ``cancel=None`` (the default) is byte-identical
    to the original blocking ``subprocess.run`` behaviour.
    """
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        result = _subprocess_run_cancellable(cmd, cancel)
    except OSError as exc:  # binary vanished / not executable
        raise MonteurMediaError(f"could not run ffmpeg while {label}: {exc}") from exc
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace")[-_STDERR_TAIL:]
        raise MonteurMediaError(f"ffmpeg failed while {label}: {tail}")


def _run_ffmpeg_capture(args: list[str], label: str, cancel=None) -> str:
    """Like :func:`_run_ffmpeg` but at ``-loglevel info``, returning stderr.

    The measurement runs need what ffmpeg PRINTS: loudnorm's
    ``print_format=json`` block (the two-pass first pass, blueprint 1.4)
    and ``volumedetect``'s levels (the mix-mode prominence measure) both
    land on stderr at info level. Failures raise exactly like
    :func:`_run_ffmpeg`; ``cancel`` behaves identically too.
    """
    cmd = [find_ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "info", "-y", *args]
    try:
        result = _subprocess_run_cancellable(cmd, cancel)
    except OSError as exc:
        raise MonteurMediaError(f"could not run ffmpeg while {label}: {exc}") from exc
    stderr = result.stderr.decode("utf-8", "replace")
    if result.returncode != 0:
        raise MonteurMediaError(
            f"ffmpeg failed while {label}: {stderr[-_STDERR_TAIL:]}"
        )
    return stderr


def _even(n: float) -> int:
    """Nearest even int at/below ``n``, minimum 2 (codecs need even sizes)."""
    return max(2, int(n) // 2 * 2)


def _reframe_cover(
    entry,
    base_cover: str,
    w: int,
    h: int,
    fps: float,
    dims: dict[str, tuple[int, int]],
) -> str:
    """The cover filter for one clip entry, reframed toward its focus point.

    Auto-reframe 9:16 (:mod:`monteur.reframe`): when the entry carries a
    ``reframe_focus`` (the cast moment's attention point, set in memory by
    the planner), shift the centre crop so that point stays framed instead of
    sliced off. Returns ``base_cover`` UNCHANGED — byte-for-byte — whenever
    there is nothing to reframe: no focus signal, a plan loaded from disk
    (the field does not serialize), a source whose dimensions cannot be read,
    or a focus that already resolves to the centre (same-aspect footage, or a
    focus centred in the cropped dimension). ``dims`` caches probed source
    sizes across segments so a repeated clip is measured once.
    """
    focus = getattr(entry, "reframe_focus", None)
    if focus is None:
        return base_cover
    path = str(entry.clip_path)
    size = dims.get(path)
    if size is None:
        try:
            info = probe(path)
            size = (info.width, info.height)
        except Exception:  # noqa: BLE001 — an unreadable size just center-crops
            size = (0, 0)
        dims[path] = size
    src_w, src_h = size
    if src_w <= 0 or src_h <= 0:
        return base_cover
    if _reframe.is_centered(src_w, src_h, w, h, focus):
        return base_cover  # no shift → keep the byte-identical centre crop
    x, y = _reframe.crop_offset(src_w, src_h, w, h, focus)
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}:{round(x)}:{round(y)},setsar=1,fps={fps:g}"
    )


def extract_thumbnail(
    clip_path: str, time_s: float, out_path: str, *, width: int = 320
) -> str:
    """One frame of ``clip_path`` at ``time_s`` as an image file.

    Fast keyframe seek (``-ss`` before ``-i``, exactness does not matter
    for a storyboard thumbnail), scaled to ``width`` px with the aspect
    ratio kept (``scale=W:-2``). The format follows ``out_path``'s suffix
    (``.jpg``/``.jpeg`` or ``.png``). Returns ``out_path``.

    Raises :class:`MonteurMediaError` when ffmpeg is unavailable (missing
    ``[media]`` extra and no system binary) or the frame cannot be read.
    """
    _run_ffmpeg(
        [
            "-ss", f"{max(0.0, time_s):.3f}", "-i", str(clip_path),
            "-frames:v", "1", "-vf", f"scale={_even(width)}:-2",
            str(out_path),
        ],
        f"extracting a thumbnail from {Path(clip_path).name}",
    )
    if not Path(out_path).is_file() or Path(out_path).stat().st_size == 0:
        raise MonteurMediaError(f"ffmpeg wrote no thumbnail to {out_path}")
    return str(out_path)


def _segments(plan: MontagePlan) -> list[tuple[str, float, object]]:
    """The montage as an ordered list of ("clip"|"black", length, entry|None).

    Entries' record ranges tile the montage; any record gap — before the
    first entry, between entries, after the last one — is a black dip.
    """
    out: list[tuple[str, float, object]] = []
    cursor = 0.0
    for entry in sorted(plan.entries, key=lambda e: e.record_start):
        if entry.record_start - cursor > _MIN_GAP:
            out.append(("black", entry.record_start - cursor, None))
        length = entry.record_end - entry.record_start
        if length > _MIN_GAP:
            out.append(("clip", length, entry))
        cursor = max(cursor, entry.record_end)
    if plan.duration - cursor > _MIN_GAP:
        out.append(("black", plan.duration - cursor, None))
    return out


def _dip_titles(
    plan: MontagePlan, segments: list[tuple[str, float, object]]
) -> dict[int, str]:
    """Segment index -> composed act title for that black dip.

    ``plan.dips`` aligns with ``plan.title_texts`` by index; a black
    segment is matched to its dip by record position. Shared by both
    renderers (blueprint 1.8: the preview must show the same titles the
    export shows). Empty when the plan has no composed titles.
    """
    starts: list[float] = []
    cursor = 0.0
    for _kind, length, _entry in segments:
        starts.append(cursor)
        cursor += length
    titles: dict[int, str] = {}
    for j, (dip_start, _dip_len) in enumerate(plan.dips):
        text = plan.title_texts[j].strip() if j < len(plan.title_texts) else ""
        if not text:
            continue
        for i, (kind, _length, _entry) in enumerate(segments):
            if kind == "black" and abs(starts[i] - dip_start) < 0.05:
                titles[i] = text
                break
    return titles


def _canvas_size(plan: MontagePlan, width: int) -> tuple[int, int]:
    """Even preview canvas (W, H): ``width`` x the first entry's aspect.

    Falls back to 16:9 when the first clip cannot be probed for a size.
    """
    width = _even(width)
    aspect = 9.0 / 16.0
    for entry in plan.entries:
        try:
            info = probe(entry.clip_path)
        except MonteurMediaError:
            break
        if info.width > 0 and info.height > 0:
            aspect = info.height / info.width
        break
    return width, _even(round(width * aspect))


def _has_audio(path: str) -> bool:
    try:
        return probe(path).has_audio
    except MonteurMediaError:
        return False


def _gap_gate_filters(gaps: list[tuple[float, float]]) -> list[str]:
    """Filter-chain pieces muting the music bed over the deliberate gaps.

    ``gaps`` are RECORD-time windows (:func:`monteur.montage.
    music_bed_gaps` — already clamped and merged); the returned filters
    expect the stream's ``t`` to BE record time, i.e. they belong AFTER
    the music window's ``adelay`` (or directly on the bed when no window
    is set). One ``volume`` gate per gap — a trapezoid that is exactly 0
    over ``[start, end]`` with a :data:`_GAP_FADE` micro-fade ramping
    just OUTSIDE each edge, so the mute covers the full gap and the
    re-entry lands at the gap end (+50 ms ramp against clicks). Chained
    volume envelopes multiply, so multiple gaps compose. Empty gaps =
    empty list: the untouched old chains stay byte-identical.
    """
    if not gaps:
        return []
    filters = [f"asetnsamples=n={_GAP_GATE_SAMPLES}"]
    fade = _GAP_FADE
    for lo, hi in gaps:
        expr = (
            f"1-min(1,max(0,min((t-{lo - fade:.3f})/{fade:.3f},"
            f"({hi + fade:.3f}-t)/{fade:.3f})))"
        )
        filters.append(f"volume=volume={_fq(expr)}:eval=frame")
    return filters


def ducking_windows(
    cues: list,
    duration: float,
    *,
    prominent: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float, float, float]]:
    """Bed-ducking windows ``(start, end, floor_gain, edge_fade)`` — 1.4.

    Built from the plan's PLACED sound elements (cues with a ``file`` —
    a marker cue makes no sound and ducks nothing) plus, in mix mode, the
    caller-measured ``prominent`` original-sound record windows:

    * **impact / sub-drop** — a :data:`_DUCK_ACCENT_DB` (−6 dB) dip over
      the accent window (``cue.time`` for ``min(cue.duration,
      _DUCK_ACCENT_MAX_S)`` — the hit and its first ring, not the whole
      tail), gap-gate micro-fade edges;
    * **riser** — a gentler :data:`_DUCK_RISER_DB` (−4 dB) SHELF over the
      riser's full play window with eased :data:`_DUCK_SHELF_FADE`
      edges, so the build reads above the bed all the way into its hit;
    * **prominent original sound** (mix mode) — a −4 dB shelf over each
      measured window (:data:`_DUCK_OTON_DB`).

    Ambience and whoosh cues never duck — they sit level with the bed.
    Windows are clamped into ``[0, duration]``, sorted, and returned as
    LINEAR floor gains (``10^(dB/20)``); overlapping windows simply
    multiply in the chain (documented composition — an impact riding a
    riser's shelf dips −10 dB for its beat, which is the intent). Empty
    result = the untouched old chains, byte-identical.
    """
    windows: list[tuple[float, float, float, float]] = []
    duration = max(0.0, duration)
    for cue in cues:
        if not getattr(cue, "file", ""):
            continue
        kind = getattr(cue, "kind", "")
        length = max(0.0, float(getattr(cue, "duration", 0.0) or 0.0))
        if kind in ("impact", "sub-drop"):
            depth, fade = _DUCK_ACCENT_DB, _DUCK_FADE
            length = min(length, _DUCK_ACCENT_MAX_S) if length > 0 else _DUCK_ACCENT_MAX_S
        elif kind == "riser":
            depth, fade = _DUCK_RISER_DB, _DUCK_SHELF_FADE
        else:
            continue  # ambience/whoosh: level with the bed
        lo = max(0.0, float(cue.time))
        hi = min(duration, float(cue.time) + length)
        if hi - lo > _MIN_GAP:
            windows.append((lo, hi, 10 ** (depth / 20.0), fade))
    for lo, hi in prominent or []:
        lo = max(0.0, float(lo))
        hi = min(duration, float(hi))
        if hi - lo > _MIN_GAP:
            windows.append((lo, hi, 10 ** (_DUCK_OTON_DB / 20.0), _DUCK_SHELF_FADE))
    windows.sort(key=lambda w: (w[0], w[1]))
    return windows


def oton_lift_windows(
    prominent: list[tuple[float, float, float]],
    duration: float,
) -> tuple[list[tuple[float, float, float, float]], list[str]]:
    """O-Ton pop LIFT windows on the original chain (blueprint 2.2).

    The mirror of :func:`ducking_windows`' prominent-original shelf: under
    each measured prominent window the music ducks (−4 dB, 1.4) and the
    ORIGINAL now lifts (:data:`_LIFT_OTON_DB`, +3.5 dB). ``prominent`` is
    ``(start, end, true_peak_dB)`` per window — the peak measured via
    ``volumedetect``'s ``max_volume`` during the bed extraction.

    Honest headroom: after the lift and the :data:`MIX_ORIGINAL_LEVEL`
    attenuation the original's true peak must not break
    :data:`_LIFT_OTON_TP_CEIL` (−1 dBTP). The boost is clamped per window
    to whatever headroom remains (never negative — a window with no room
    just does not lift), and a note names each clamp. Returns
    ``(windows, notes)`` as LINEAR floor gains (``10^(dB/20)`` > 1),
    the same tuple shape :func:`_duck_filters` consumes. Empty input =
    the untouched old original chain, byte-identical.
    """
    windows: list[tuple[float, float, float, float]] = []
    notes: list[str] = []
    duration = max(0.0, duration)
    mix_db = 20.0 * math.log10(MIX_ORIGINAL_LEVEL) if MIX_ORIGINAL_LEVEL > 0 else 0.0
    for lo, hi, peak_db in prominent or []:
        lo = max(0.0, float(lo))
        hi = min(duration, float(hi))
        if hi - lo <= _MIN_GAP:
            continue
        # Headroom for the lift: the original part's true peak, dropped by
        # the mix attenuation, may rise to at most the −1 dBTP ceiling.
        headroom = _LIFT_OTON_TP_CEIL - (float(peak_db) + mix_db)
        boost = _LIFT_OTON_DB
        if boost > headroom + 1e-9:
            boost = max(0.0, headroom)
            if boost <= 1e-9:
                notes.append(
                    f"O-Ton pop at {lo:.1f}s: no headroom under -1 dBTP — "
                    "left at bed level (would have clipped)"
                )
                continue
            notes.append(
                f"O-Ton pop at {lo:.1f}s: lift clamped to {boost:+.1f} dB "
                "to hold -1 dBTP (the moment is already hot)"
            )
        windows.append((lo, hi, 10 ** (boost / 20.0), _DUCK_SHELF_FADE))
    windows.sort(key=lambda w: (w[0], w[1]))
    return windows, notes


def _duck_filters(windows: list[tuple[float, float, float, float]]) -> list[str]:
    """One ``volume`` envelope per ducking window (blueprint 1.4).

    The same trapezoid the gap gates use, with a FLOOR instead of a full
    mute: gain ``1 - (1 - floor) * ramp`` — 1 outside the window,
    ``floor`` inside, the ramp easing over ``fade`` seconds just outside
    each edge. Chained volume envelopes multiply, so ducks compose with
    each other and with the gap gates in one linear chain.
    """
    filters: list[str] = []
    for lo, hi, floor, fade in windows:
        ramp = (
            f"min(1,max(0,min((t-{lo - fade:.3f})/{fade:.3f},"
            f"({hi + fade:.3f}-t)/{fade:.3f})))"
        )
        expr = f"1-{1.0 - floor:.6f}*{ramp}"
        filters.append(f"volume=volume={_fq(expr)}:eval=frame")
    return filters


def _bed_envelope_filters(
    gaps: list[tuple[float, float]],
    ducks: list[tuple[float, float, float, float]],
) -> list[str]:
    """Gap gates + ducking envelopes as ONE linear chain (blueprint 1.4).

    The music-gap trapezoids and the ducking envelopes are the same
    mechanism at different depths; chained ``volume`` filters multiply
    per sample, so they compose in any combination — a duck inside a gap
    multiplies into the mute (still 0). ``asetnsamples`` is emitted once
    for the whole chain. No gaps and no ducks = the empty list, so
    untouched plans keep their exact old commands.
    """
    gates = _gap_gate_filters(gaps)
    duck = _duck_filters(ducks)
    if not duck:
        return gates
    if not gates:
        return [f"asetnsamples=n={_GAP_GATE_SAMPLES}"] + duck
    return gates + duck


def render_preview(
    plan: MontagePlan,
    out_path: str,
    *,
    width: int = 640,
    fps: float = 25.0,
    audio: str = "music",
    progress=None,
    fade_in_s: float | None = None,
    fade_out_s: float | None = None,
    cancel=None,
) -> dict:
    """Render ``plan`` to a small uniform MP4 at ``out_path`` — no Resolve.

    ``width`` is the preview width (forced even; height follows the first
    clip's aspect ratio, also even — other aspect ratios are letterboxed).
    ``audio`` is ``"music"`` / ``"original"`` / ``"mix"`` exactly as in
    :func:`monteur.montage.montage_to_timeline`; ``"music"``/``"mix"``
    without a ``plan.music_path`` raise ValueError with the same message.
    ``fade_in_s`` / ``fade_out_s`` override the plan's own ``fade_in`` /
    ``fade_out`` seconds (``None``, the default, uses the plan's values;
    pass 0 to disable). Fades apply on the final pass as video ``fade``
    and ``afade`` filters.

    ``progress(done, total, label)`` — when given — is called once per
    finished segment and once for the final mux.

    Returns ``{"path", "duration", "width", "segments"}`` with the
    duration probed from the finished file (honest, not assumed). Any
    ffmpeg failure raises :class:`MonteurMediaError` with the stderr
    tail; the intermediate segment directory is removed on success and
    failure alike.
    """
    if audio not in _AUDIO_MODES:
        valid = ", ".join(_AUDIO_MODES)
        raise ValueError(f"unknown audio mode {audio!r}; valid modes: {valid}")
    if audio in ("music", "mix") and not plan.music_path:
        raise ValueError(
            f'plan has no music; audio mode {audio!r} needs a song — '
            'use audio="original"'
        )
    if not plan.entries:
        raise ValueError("plan has no entries; nothing to render")

    segments = _segments(plan)
    fade_in = plan.fade_in if fade_in_s is None else max(0.0, fade_in_s)
    fade_out = plan.fade_out if fade_out_s is None else max(0.0, fade_out_s)
    total = len(segments) + 1  # + the final mux
    done = 0

    def tick(label: str) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total, label)

    w, h = _canvas_size(plan, width)
    own_audio = audio in ("original", "mix")
    # Act titles on the dips (blueprint 1.8): the preview shows the same
    # titles the export draws — otherwise every A/B look reviews an
    # unfinished surrogate. Probed defensively exactly like the export
    # (no drawtext filter / no font -> the dips stay plain black; a
    # preview never fails over a title), just without the export's note.
    titles = _dip_titles(plan, segments)
    font: str | None = None
    if titles and _supports_drawtext():
        font = _find_font()
    if not font:
        titles = {}
    # Title fade, beat-quantized against the plan's own pulse (1.7) —
    # the same shared helper the planner's dips/dissolves run through.
    title_fade = quantize_finish(_TITLE_FADE, plan_pulse(plan), max_s=_TITLE_FADE_MAX)
    # Uniform video for every segment: same size, sample-aspect, and rate,
    # so the concat demuxer joins them without a hiccup.
    video_filter = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:g}"
    )
    encode = [
        "-c:v", "libx264", "-preset", _PRESET, "-crf", _CRF,
        "-pix_fmt", "yuv420p",
    ]
    audio_encode = [
        "-c:a", "aac", "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CHANNELS),
    ]
    silence = f"anullsrc=r={_AUDIO_RATE}:cl=stereo"

    tmpdir = tempfile.mkdtemp(prefix="monteur-preview-")
    try:
        paths: list[str] = []
        for i, (kind, length, entry) in enumerate(segments):
            seg = str(Path(tmpdir) / f"seg_{i:04d}.mp4")
            if kind == "clip":
                name = Path(entry.clip_path).name
                args = [
                    "-ss", f"{max(0.0, entry.source_start):.3f}",
                    "-t", f"{length:.3f}", "-i", str(entry.clip_path),
                ]
                if own_audio:
                    if _has_audio(entry.clip_path):
                        audio_args = ["-map", "0:a:0", *audio_encode]
                    else:  # video-only source: keep the stream layout uniform
                        args += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", silence]
                        audio_args = ["-map", "1:a:0", *audio_encode]
                else:
                    audio_args = ["-an"]
                _run_ffmpeg(
                    [
                        *args, "-map", "0:v:0", "-vf", video_filter,
                        *encode, *audio_args, seg,
                    ],
                    f"encoding segment {i + 1}/{len(segments)} ({name})",
                    cancel,
                )
                tick(name)
            else:
                args = [
                    "-f", "lavfi", "-t", f"{length:.3f}",
                    "-i", f"color=black:s={w}x{h}:r={fps:g}",
                ]
                if own_audio:
                    args += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", silence]
                    audio_args = ["-map", "1:a:0", *audio_encode]
                else:
                    audio_args = ["-an"]
                video_args: list[str] = []
                if i in titles:
                    # The act title over its black dip (blueprint 1.8) —
                    # the same drawtext the export uses, preview quality.
                    text_file = str(Path(tmpdir) / f"title_{i:04d}.txt")
                    Path(text_file).write_text(titles[i], encoding="utf-8")
                    video_args = [
                        "-vf",
                        _title_filter(text_file, font, h, length, title_fade),
                    ]
                _run_ffmpeg(
                    [
                        *args, "-map", "0:v:0", *video_args,
                        *encode, *audio_args, seg,
                    ],
                    f"encoding segment {i + 1}/{len(segments)} "
                    f"({'title' if i in titles else 'black dip'})",
                    cancel,
                )
                tick("title" if i in titles else "black")
            paths.append(seg)

        # Concat list for the demuxer; single quotes escaped per its syntax.
        list_path = Path(tmpdir) / "concat.txt"
        list_path.write_text(
            "".join("file '{}'\n".format(p.replace("'", "'\\''")) for p in paths),
            encoding="utf-8",
        )

        vf = []
        af = []
        if fade_in > 0:
            vf.append(f"fade=t=in:st=0:d={fade_in:.3f}")
            af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            st = max(0.0, plan.duration - fade_out)
            vf.append(f"fade=t=out:st={st:.3f}:d={fade_out:.3f}")
            af.append(f"afade=t=out:st={st:.3f}:d={fade_out:.3f}")

        # Adaptive music window: the song is read from music_start + music_in,
        # trimmed to its record window, delayed to music_in and faded in
        # briefly at a non-zero entry — the cut opens dry, the music slams
        # in on the grid. A full-length window builds the exact old command.
        m_in, m_end = music_window_bounds(plan)
        windowed = audio in ("music", "mix") and (
            m_in > _MIN_GAP or m_end < plan.duration - _MIN_GAP
        )
        music_filters: list[str] = []
        if windowed:
            music_filters.append(f"atrim=0:{m_end - m_in:.3f}")
            if m_in > _MIN_GAP:
                ms = int(round(m_in * 1000))
                music_filters += [
                    f"adelay={ms}|{ms}",
                    f"afade=t=in:st={m_in:.3f}:d={_MUSIC_FADE_IN:.3f}",
                ]
        # Deliberate silence: gate the bed's volume to 0 over every
        # music gap (after adelay, so t is record time). The song keeps
        # RUNNING underneath — only the volume breaks, so the re-entry
        # continues exactly on the beat grid.
        if audio in ("music", "mix"):
            music_filters += _gap_gate_filters(music_bed_gaps(plan))

        final = ["-f", "concat", "-safe", "0", "-i", str(list_path)]
        if audio in ("music", "mix"):
            final += [
                "-ss", f"{max(0.0, plan.music_start + m_in):.3f}",
                "-i", str(plan.music_path),
            ]
        final += ["-map", "0:v:0"]
        if vf:
            final += ["-vf", ",".join(vf)]
        final += encode
        if audio == "music":
            final += ["-map", "1:a:0"]
            if music_filters or af:
                final += ["-af", ",".join(music_filters + af)]
            final += audio_encode
        elif audio == "original":
            final += ["-map", "0:a:0"]
            if af:
                final += ["-af", ",".join(af)]
            final += audio_encode
        else:  # mix: music at MIX_MUSIC_LEVEL + own sound at MIX_ORIGINAL_LEVEL
            music_chain = ",".join(music_filters + [f"volume={MIX_MUSIC_LEVEL:g}"])
            chain = (
                f"[1:a]{music_chain}[m];"
                f"[0:a]volume={MIX_ORIGINAL_LEVEL:g}[o];"
                "[m][o]amix=inputs=2:duration=first:dropout_transition=0"
                ":normalize=0"
            )
            if af:
                chain += "," + ",".join(af)
            final += ["-filter_complex", chain + "[a]", "-map", "[a]", *audio_encode]
        final += ["-t", f"{plan.duration:.3f}", str(out_path)]
        _run_ffmpeg(final, f"muxing the preview to {Path(out_path).name}", cancel)
        tick("mux")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    info = probe(out_path)
    return {
        "path": str(out_path),
        "duration": info.duration,
        "width": w,
        "segments": len(segments),
    }


# --- Direct Export: the finished video from Monteur's own engine --------------


def _find_font() -> str | None:
    """First existing drawtext font file, or None (titles are then skipped)."""
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


# Whether a given ffmpeg binary ships the drawtext filter (the bundled
# imageio-ffmpeg build does NOT — no libfreetype), cached per binary path.
_DRAWTEXT_CACHE: dict[str, bool] = {}


def _supports_drawtext() -> bool:
    """True when the resolved ffmpeg build ships the ``drawtext`` filter.

    Some builds (notably the ``[media]`` extra's bundled imageio-ffmpeg
    binary) are compiled without libfreetype and have no ``drawtext`` at
    all — titles must then be skipped with a note, never fail the export.
    """
    try:
        binary = find_ffmpeg()
    except MonteurMediaError:
        return False
    if binary not in _DRAWTEXT_CACHE:
        try:
            result = subprocess.run(
                [binary, "-hide_banner", "-filters"], capture_output=True
            )
            _DRAWTEXT_CACHE[binary] = b"drawtext" in result.stdout
        except OSError:
            _DRAWTEXT_CACHE[binary] = False
    return _DRAWTEXT_CACHE[binary]


def _fq(value: str) -> str:
    """Quote a value for use inside an ffmpeg filter option.

    Single-quotes the value (protecting ``:``/``,``/``;`` from the filter
    and graph parsers), escapes embedded quotes the ffmpeg way and turns
    backslashes into forward slashes (Windows paths; ffmpeg accepts ``/``
    everywhere and ``\\`` would start an escape).
    """
    return "'" + str(value).replace("\\", "/").replace("'", r"'\''") + "'"


def _title_filter(
    text_file: str,
    font: str,
    height: int,
    dip_len: float,
    fade_s: float = _TITLE_FADE,
) -> str:
    """The drawtext filter for one act title over a black dip segment.

    Plain white, centered, font size proportional to the canvas height.
    A dip long enough to afford it fades the text in and out over
    ``fade_s`` seconds INSIDE the dip (the :data:`_TITLE_FADE` target,
    beat-quantized by the renderers through the shared
    :func:`monteur.montage.quantize_finish` helper — blueprint 1.7; a
    beatless plan passes 0.3 s unchanged); a short dip shows the text
    for the dip's full length. The alpha expression always cuts to 0
    after ``dip_len`` — a segment extended for a following dissolve
    must not carry the title into the extension.
    """
    fade = fade_s if dip_len >= 2 * fade_s + 0.2 else 0.0
    if fade > 0:
        alpha = (
            f"if(lt(t,{fade:.3f}),t/{fade:.3f},"
            f"if(lt(t,{dip_len - fade:.3f}),1,"
            f"max(({dip_len:.3f}-t)/{fade:.3f},0)))"
        )
    else:
        alpha = f"lt(t,{dip_len:.3f})"
    size = max(12, round(height / 12))
    return (
        f"drawtext=fontfile={_fq(font)}:textfile={_fq(text_file)}"
        f":fontcolor=white:fontsize={size}"
        f":x=(w-text_w)/2:y=(h-text_h)/2:alpha={_fq(alpha)}"
    )


def _export_video_graph(
    lengths: list[float],
    transitions: list[float],
    fade_in: float,
    fade_out: float,
    duration: float,
) -> str:
    """The filter_complex video chain over the export's segment inputs.

    ``lengths[i]`` is segment i's RENDERED length (its nominal record
    length plus the extension that feeds the next boundary's dissolve);
    ``transitions[i]`` is the xfade seconds INTO segment i (0 = hard
    cut; ``transitions[0]`` is always 0). Segments are chained left to
    right: a dissolve boundary becomes ``xfade`` (offset = the incoming
    segment's record position, so the timeline timing never moves — the
    overlap material comes entirely from the outgoing segment's
    extension), a hard cut becomes a 2-input ``concat``. One graph, one
    decode, ONE final encode — no generation loss from pairwise
    intermediate files. The chain ends in the plan's fades and
    ``format=yuv420p`` under the output label ``[v]``.
    """
    # Normalize every input's timebase first: concat and xfade both refuse
    # mismatched timebases, and MP4 segments arrive with their container tb.
    parts = [f"[{i}:v]settb=AVTB[sv{i}]" for i in range(len(lengths))]
    cur = "[sv0]"
    acc = lengths[0]
    for i in range(1, len(lengths)):
        t = transitions[i]
        out = f"[vx{i}]"
        if t > 0:
            offset = acc - t
            parts.append(
                f"{cur}[sv{i}]xfade=transition=fade"
                f":duration={t:.3f}:offset={offset:.3f}{out}"
            )
            acc = offset + lengths[i]
        else:
            parts.append(f"{cur}[sv{i}]concat=n=2:v=1:a=0{out}")
            acc += lengths[i]
        cur = out
    tail: list[str] = []
    if fade_in > 0:
        tail.append(f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        st = max(0.0, duration - fade_out)
        tail.append(f"fade=t=out:st={st:.3f}:d={fade_out:.3f}")
    tail.append("format=yuv420p")
    parts.append(f"{cur}{','.join(tail)}[v]")
    return ";".join(parts)


def _export_audio_graph(
    audio: str,
    music_label: str | None,
    bed_label: str | None,
    sfx: list[tuple[str, float, float]],
    fade_in: float,
    fade_out: float,
    duration: float,
    *,
    music_in: float = 0.0,
    music_len: float = 0.0,
    music_gaps: list[tuple[float, float]] | None = None,
    duck_windows: list[tuple[float, float, float, float]] | None = None,
    lift_windows: list[tuple[float, float, float, float]] | None = None,
    jl_overlaps: list[tuple[str, float, float, float, float, float, float]] | None = None,
    loudnorm: str | None = None,
) -> str:
    """The filter_complex audio chain, ending in the output label ``[a]``.

    The bed follows the preview's audio-mode semantics exactly —
    ``"music"`` is the song alone, ``"original"`` the concatenated clip
    sound, ``"mix"`` both at the fixed :data:`MIX_MUSIC_LEVEL` /
    :data:`MIX_ORIGINAL_LEVEL` levels. Placed SFX cues (``(input label,
    start seconds, trimmed length)`` — optionally a fourth element, the
    ``source_offset`` seconds skipped from the file's head, blueprint
    1.3: a riser plays its LAST run-up seconds, a shifted impact keeps
    its peak on the hit) are each trimmed from their offset, resampled
    to the export rate, delayed to their cue time and ``amix``-ed ON TOP
    of the bed at full level (``normalize=0`` keeps everyone's gain
    honest). Tail rule: the trim is the file's REMAINDER past the
    offset, clamped by the caller only to the montage end — a hit's ring
    -out is never cut back to the planned cue length.
    The chain finishes with the plan's ``afade``\\ s and — always last —
    ``loudnorm`` at YouTube's -14 LUFS / -1 dBTP / LRA 11 target (the
    single-pass string by default; ``loudnorm=`` injects the two-pass
    second stage, blueprint 1.4), then a resample back to
    :data:`_AUDIO_RATE` (loudnorm upsamples internally).

    ``music_in`` / ``music_len`` (keyword-only, both 0 by default = the
    old full-length graph, byte-identical) apply the plan's adaptive
    music window to the song stream BEFORE it becomes the bed: trim to
    ``music_len`` seconds, delay by ``music_in`` (adelay) and — at a
    non-zero entry — a short :data:`_MUSIC_FADE_IN` musical fade-in. The
    caller reads the song from ``music_start + music_in``, so the beat
    grid alignment is untouched.

    ``music_gaps`` (keyword-only, None/empty = untouched old graph)
    additionally gates the SONG's volume to 0 over the plan's deliberate
    silences (:func:`_gap_gate_filters` — record-time windows, applied
    after the adelay so ``t`` is record time). Only the song: placed SFX
    (the braam under the title) and the original-sound bed play through
    the gap — that is what carries it.

    ``duck_windows`` (keyword-only, None/empty = untouched old graph)
    ducks the SONG under the accents (blueprint 1.4): the
    :func:`ducking_windows` tuples become multiplying volume envelopes
    chained right after the gap gates — same trapezoid machinery, a
    floor instead of a mute, one linear chain (see
    :func:`_bed_envelope_filters` for the composition rules). Only the
    song ducks; the SFX/original streams pass untouched.

    ``lift_windows`` (keyword-only, None/empty = untouched old graph, mix
    mode only) is the O-Ton pop (blueprint 2.2): the SAME trapezoid
    envelope with a floor ABOVE 1 (:func:`oton_lift_windows`,
    :func:`_duck_filters` reused verbatim — a lift is a duck with gain > 1)
    chained onto the ORIGINAL stream right after its
    :data:`MIX_ORIGINAL_LEVEL` gain, so a prominent original moment reads
    over the (already ducking) song. Music and SFX streams pass untouched.

    ``loudnorm`` (keyword-only) overrides the loudness tail: None keeps
    the classic single-pass :data:`_EXPORT_LOUDNORM` string
    (byte-identical), :func:`render_export` passes the measured
    second-pass ``linear=true`` string (blueprint 1.4).
    """
    parts: list[str] = []
    gates = _bed_envelope_filters(
        list(music_gaps or []), list(duck_windows or [])
    )
    if music_label is not None and (music_in > 0 or music_len > 0 or gates):
        filters: list[str] = []
        if music_len > 0:
            filters.append(f"atrim=0:{music_len:.3f}")
        if music_in > 0:
            ms = max(0, int(round(music_in * 1000)))
            filters += [
                f"aresample={_AUDIO_RATE}",
                f"adelay={ms}|{ms}",
                f"afade=t=in:st={music_in:.3f}:d={_MUSIC_FADE_IN:.3f}",
            ]
        filters += gates
        parts.append(f"[{music_label}]{','.join(filters)}[xmw]")
        music_label = "xmw"
    if audio == "music":
        base = f"[{music_label}]"
    elif audio == "original":
        base = f"[{bed_label}]"
    else:  # mix
        parts.append(f"[{music_label}]volume={MIX_MUSIC_LEVEL:g}[xm]")
        # O-Ton pop (blueprint 2.2): the lift rides the original chain right
        # after its mix gain — a floor-above-1 envelope, the same machinery
        # the song ducks with. Empty windows = the untouched old chain.
        lift = _duck_filters(list(lift_windows or []))
        orig_chain = f"volume={MIX_ORIGINAL_LEVEL:g}"
        if lift:
            orig_chain += "," + ",".join(lift)
        parts.append(f"[{bed_label}]{orig_chain}[xo]")
        parts.append(
            "[xm][xo]amix=inputs=2:duration=first:dropout_transition=0"
            ":normalize=0[xbed]"
        )
        base = "[xbed]"
    labels = ""
    n_extra = 0
    if sfx:
        for k, spec in enumerate(sfx):
            label, start, trim = spec[0], spec[1], spec[2]
            offset = float(spec[3]) if len(spec) > 3 else 0.0
            ms = max(0, int(round(start * 1000)))
            atrim = (
                f"atrim={offset:.3f}:{offset + trim:.3f}"
                if offset > 0
                else f"atrim=0:{trim:.3f}"
            )
            parts.append(
                f"[{label}]{atrim},aresample={_AUDIO_RATE},"
                f"aformat=channel_layouts=stereo,adelay={ms}|{ms}[xs{k}]"
            )
            labels += f"[xs{k}]"
            n_extra += 1
    # J/L overlaps (blueprint 2.3): a J-cut's anticipating head / an L-cut's
    # ringing tail is the ENTRY's own original sound, trimmed to the overlap
    # window, level-matched to the bed, micro-faded at its outer edge (the
    # crossfade seam) and mixed on top — the picture grid never moves.
    for k, spec in enumerate(jl_overlaps or []):
        label, start, trim, offset, fin, fout, level = spec
        ms = max(0, int(round(start * 1000)))
        chain = [
            f"atrim={offset:.3f}:{offset + trim:.3f}",
            f"aresample={_AUDIO_RATE}",
            "aformat=channel_layouts=stereo",
        ]
        if level != 1.0:
            chain.append(f"volume={level:g}")
        if fin > 0:
            chain.append(f"afade=t=in:st=0:d={fin:.3f}")
        if fout > 0:
            chain.append(f"afade=t=out:st={max(0.0, trim - fout):.3f}:d={fout:.3f}")
        chain.append(f"adelay={ms}|{ms}")
        parts.append(f"[{label}]{','.join(chain)}[xj{k}]")
        labels += f"[xj{k}]"
        n_extra += 1
    if n_extra:
        parts.append(
            f"{base}{labels}amix=inputs={1 + n_extra}:duration=first"
            ":dropout_transition=0:normalize=0[xfx]"
        )
        base = "[xfx]"
    tail: list[str] = []
    if fade_in > 0:
        tail.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        st = max(0.0, duration - fade_out)
        tail.append(f"afade=t=out:st={st:.3f}:d={fade_out:.3f}")
    tail.append(loudnorm if loudnorm else _EXPORT_LOUDNORM)
    tail.append(f"aresample={_AUDIO_RATE}")
    parts.append(f"{base}{','.join(tail)}[a]")
    return ";".join(parts)


_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB")
_MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?[\d.]+)\s*dB")


def _parse_mean_volume(stderr: str) -> float | None:
    """volumedetect's ``mean_volume`` (dB) from captured stderr, or None."""
    match = _MEAN_VOLUME_RE.search(stderr)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_max_volume(stderr: str) -> float | None:
    """volumedetect's ``max_volume`` (dB, the true peak) from stderr, or None.

    The O-Ton pop's honest-headroom clamp (blueprint 2.2) reads this: a
    part whose peak already sits near 0 dB gets a smaller lift so the
    boosted original still holds −1 dBTP.
    """
    match = _MAX_VOLUME_RE.search(stderr)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_loudnorm_stats(stderr: str) -> dict | None:
    """loudnorm's ``print_format=json`` block as floats, or None.

    The two-pass first pass (blueprint 1.4): the last ``{...}`` block on
    stderr carries ``input_i`` / ``input_tp`` / ``input_lra`` /
    ``input_thresh`` / ``target_offset``. Values are validated into
    loudnorm's own accepted ranges — anything non-finite or out of range
    (digital silence measures ``-inf``) returns None and the caller
    degrades to single-pass, honestly noted.
    """
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except ValueError:
        return None
    ranges = {
        "input_i": (-99.0, 0.0),
        "input_tp": (-99.0, 99.0),
        "input_lra": (0.0, 99.0),
        "input_thresh": (-99.0, 0.0),
        "target_offset": (-99.0, 99.0),
    }
    out: dict[str, float] = {}
    for key, (lo, hi) in ranges.items():
        try:
            value = float(data[key])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(value) or not (lo <= value <= hi):
            return None
        out[key] = value
    return out


def _export_transitions(
    plan: MontagePlan, segments: list[tuple[str, float, object]]
) -> tuple[list[float], list[str]]:
    """Per-segment dissolve lengths (INTO segment i) plus degradation notes.

    Mirrors the FCPXML semantics: ``MontageEntry.transition`` is a cross
    dissolve INTO the entry, starting AT its cut — so the overlap
    material must come from the OUTGOING side, extending ``transition``
    seconds past its own cut. A black dip has unlimited handles (black
    is generated); a clip has them only when its source file holds
    ``transition`` more seconds after the entry's cut
    (``clip_duration`` when the plan recorded it, a probe otherwise).
    Missing handles degrade to a hard cut with an honest note — the
    timeline timing is authoritative and never moves either way.
    """
    notes: list[str] = []
    trans = [0.0] * len(segments)
    durations: dict[str, float | None] = {}

    def file_duration(path: str, known: float) -> float | None:
        if known > 0:
            return known
        if path not in durations:
            try:
                durations[path] = probe(path).duration
            except MonteurMediaError:
                durations[path] = None
        return durations[path]

    for i in range(1, len(segments)):
        kind, length, entry = segments[i]
        if kind != "clip" or entry.transition <= 0:
            continue
        prev_kind, prev_len, prev_entry = segments[i - 1]
        t = min(float(entry.transition), length, prev_len)
        if t < _MIN_TRANSITION:
            continue
        if prev_kind == "clip":
            available = file_duration(
                prev_entry.clip_path, prev_entry.clip_duration
            )
            needed = prev_entry.source_start + prev_len + t
            if available is None or needed > available + 1e-3:
                notes.append(
                    f"dissolve into {Path(entry.clip_path).name} at "
                    f"{entry.record_start:.1f}s: the previous shot has no "
                    "spare material after its cut — hard cut instead"
                )
                continue
        trans[i] = t
    return trans, notes


def render_export(
    plan: MontagePlan,
    out_path: str,
    *,
    canvas: str = "uhd",
    fps: float = 25.0,
    audio: str = "music",
    quality: str = "high",
    progress=None,
    size: tuple[int, int] | None = None,
    jl_cuts: bool = False,
    cancel=None,
) -> dict:
    """Render ``plan`` to a finished, upload-ready MP4 — no Resolve.

    The full-quality sibling of :func:`render_preview`: same segment
    pipeline (one intermediate per entry, generated black for the record
    gaps), but rendered at a real delivery resolution with everything
    the preview deliberately skips:

    * **Canvas** — ``canvas`` is a :data:`monteur.montage.CANVASES`
      preset key; the export renders at the preset's exact WxH. Footage
      always COVERS the frame (aspect preserved, centered, overflow
      cropped): ``scale=W:H:force_original_aspect_ratio=increase`` +
      ``crop``. That one formula reproduces both of the Resolve build's
      scaling modes — "fill" (Scaling 3, non-cine canvases) and "scale
      full frame with crop" (Scaling 1, ``cine*``) — because both mean
      cover-the-frame on export; they differ only in WHICH source
      dimension wins: the dimension needing the larger upscale fills
      exactly and the other is center-cropped, so 16:9 footage on a
      cine canvas fills the width and loses top/bottom, while the same
      footage on a vertical canvas fills the height and loses the
      sides. ``size=(w, h)`` (advanced/testing) overrides the preset
      with an explicit even-forced resolution.
    * **Dissolves** — entries with ``transition > 0`` crossfade INTO the
      entry (the FCPXML semantics): the outgoing segment is extended
      past its cut by the transition length and the boundary becomes an
      ``xfade`` whose offset is the incoming entry's record position —
      record ranges stay authoritative, the timeline timing never
      moves. Extensions need source handles; where the outgoing clip
      has none (checked against ``clip_duration``, probed when the plan
      does not carry it) the boundary degrades to a hard cut and the
      returned ``"notes"`` say so. All segments are chained in ONE
      filter graph (xfade at dissolve boundaries, concat elsewhere), so
      the export encodes exactly twice per pixel: segment + final.
    * **Titles** — ``plan.title_texts`` are drawn over their black dips
      via ``drawtext`` (centered plain white, size ~height/12, a
      :data:`_TITLE_FADE` in/out when the dip affords it). Both the
      filter and the font are discovered defensively: an ffmpeg build
      without ``drawtext`` (the bundled imageio-ffmpeg binary has no
      libfreetype), or no font from :data:`_FONT_CANDIDATES` (DejaVu on
      Linux, Arial/Helvetica on macOS/Windows), skips the titles with a
      note — never a failed export.
    * **SFX** — cues with a placed ``file`` play from their
      ``source_offset`` (blueprint 1.3: a riser's head trim, a shifted
      hit's skip), delayed to ``cue.time`` and mixed on top of the audio
      bed (one final audio graph). The tail rule: the play length is
      ``min(cue.duration, file remainder past the offset)``, clamped to
      the montage end — when headroom exists the tail rings out rather
      than being hard-trimmed to the planned cue length. A missing or
      unreadable file is a note, not an error.
    * **Deliberate silence** — the plan's ``music_gaps`` gate the SONG's
      volume to 0 over their record windows (50 ms micro-fades against
      clicks, re-entry exactly at the gap end; see
      :func:`_gap_gate_filters`). Only the song: placed SFX and the
      original-sound bed play through — they carry the silence.
    * **Bed ducking** (blueprint 1.4) — the MUSIC bed ducks under every
      placed SFX accent: −6 dB under an impact/braam window, a gentler
      −4 dB shelf under a riser (it must read above the bed into its
      hit), and in ``"mix"`` mode −4 dB under prominent original-sound
      moments (parts whose measured mean level stands
      :data:`_DUCK_OTON_STANDOUT_DB` above the median — measured via
      ``volumedetect`` during the bed extraction, deterministic for the
      same inputs). Implemented as multiplying volume envelopes chained
      onto the same linear gate architecture as the music gaps
      (:func:`_bed_envelope_filters`); the preview skips ducking along
      with the SFX it deliberately does not mix.
    * **O-Ton pops** (blueprint 2.2, ``"mix"`` export only) — the mirror of
      the duck: over each prominent original-sound window the ORIGINAL
      chain LIFTS by :data:`_LIFT_OTON_DB` (+3.5 dB) while the music ducks
      under it, so the marked moment reads over the song. Same trapezoid
      envelope with a floor above 1 (:func:`oton_lift_windows` +
      :func:`_duck_filters`), applied right after the original's mix gain.
      Honest headroom: the lift is clamped per window (using the part's
      ``volumedetect`` ``max_volume``) so the boosted original holds
      −1 dBTP, and a note names any clamp. The preview skips it exactly as
      it skips the 1.4 duck — a pop is a deliverable.
    * **J/L cuts** (blueprint 2.3, ``jl_cuts=True``, own-audio modes only)
      — at chosen quiet transitions the ORIGINAL-sound edit is decoupled
      from the picture cut (:func:`monteur.montage.jl_audio_edits`): a
      J-cut brings the next shot's audio in early, an L-cut lets the
      previous shot's audio ring past. Each is the entry's own clip audio,
      trimmed to the lead/lag window, level-matched to the bed and
      micro-faded at its outer edge (the crossfade seam), mixed on top of
      the bed. Never at a drop / music-gap / placed-SFX / climax boundary;
      the music bed and the picture grid never move. Default off =
      byte-identical.
    * **Loudness** (blueprint 1.4) — TRUE two-pass ``loudnorm`` at
      YouTube's target (-14 LUFS, -1 dBTP, LRA 11): a first audio-only
      pass renders the finished chain into a null muxer and measures it
      (``print_format=json``), the final pass applies the measured
      values with ``linear=true`` — one clean gain, no compressor
      pumping. The measurement lives only inside this one call; an
      unusable measurement degrades to the single-pass filter with a
      note, and material whose true peaks leave no headroom for the
      linear gain (extreme crest factors) is normalized dynamically by
      loudnorm's own documented fallback — noted, landing under target
      rather than crushing transients.
    * **Fades** — the plan's ``fade_in``/``fade_out``, exactly as in
      the preview.

    ``audio`` follows the preview's modes (``"music"``/``"original"``/
    ``"mix"``, same validation); ``quality`` picks an
    :data:`EXPORT_QUALITIES` profile ("high": crf 18 preset slow + AAC
    320k, "medium": crf 21 preset medium + AAC 192k — see the profile
    comment for the measured preset choice); both land as yuv420p with
    ``+faststart``. ``progress(done, total, label)`` ticks once per
    segment, once for the audio bed (own-sound modes) and once for the
    final transitions + mux pass.

    Returns ``{"path", "duration", "width", "height", "seconds",
    "notes"}`` — duration probed from the finished file, ``seconds``
    the wall-clock render time, ``notes`` every graceful degradation
    (missing dissolve handles, skipped titles, missing SFX files).
    Raises :class:`MonteurMediaError` with the ffmpeg stderr tail on
    any failure; the intermediate directory is removed on success and
    failure alike.
    """
    started = time.monotonic()
    if audio not in _AUDIO_MODES:
        valid = ", ".join(_AUDIO_MODES)
        raise ValueError(f"unknown audio mode {audio!r}; valid modes: {valid}")
    if audio in ("music", "mix") and not plan.music_path:
        raise ValueError(
            f'plan has no music; audio mode {audio!r} needs a song — '
            'use audio="original"'
        )
    if not plan.entries:
        raise ValueError("plan has no entries; nothing to render")
    if quality not in EXPORT_QUALITIES:
        valid = ", ".join(EXPORT_QUALITIES)
        raise ValueError(f"unknown quality {quality!r}; valid qualities: {valid}")
    profile = EXPORT_QUALITIES[quality]
    if size is not None:
        w, h = _even(size[0]), _even(size[1])
    else:
        from monteur.montage import CANVASES  # lazy, like the other engines

        if canvas not in CANVASES:
            valid = ", ".join(CANVASES)
            raise ValueError(f"unknown canvas {canvas!r}; valid canvases: {valid}")
        w, h = CANVASES[canvas]

    notes: list[str] = []
    segments = _segments(plan)
    trans, handle_notes = _export_transitions(plan, segments)
    notes.extend(handle_notes)
    fade_in = max(0.0, plan.fade_in)
    fade_out = max(0.0, plan.fade_out)
    own_audio = audio in ("original", "mix")
    # + the loudness measurement pass (blueprint 1.4) + the final mux.
    total = len(segments) + (1 if own_audio else 0) + 2
    done = 0

    def tick(label: str) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total, label)

    # Which black segments carry an act title (dips align with title_texts
    # by index; a black segment is matched to its dip by record position —
    # the same mapping render_preview draws from, blueprint 1.8).
    titles = _dip_titles(plan, segments)
    font: str | None = None
    if titles:
        if not _supports_drawtext():
            notes.append(
                "this ffmpeg build cannot draw text (no drawtext filter) — "
                "the act titles were skipped (the dips stay plain black)"
            )
            titles = {}
        else:
            font = _find_font()
            if not font:
                notes.append(
                    "no usable title font found on this system — the act "
                    "titles were skipped (the dips stay plain black)"
                )
                titles = {}
    # Title fade, beat-quantized against the plan's own pulse (1.7) —
    # the same shared helper the planner's dips/dissolves run through.
    title_fade = quantize_finish(_TITLE_FADE, plan_pulse(plan), max_s=_TITLE_FADE_MAX)

    cover = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,fps={fps:g}"
    )
    seg_encode = [
        "-c:v", "libx264", "-preset", _SEG_PRESET, "-crf", profile["seg_crf"],
        "-pix_fmt", "yuv420p",
    ]
    silence = f"anullsrc=r={_AUDIO_RATE}:cl=stereo"
    # Auto-reframe 9:16 caches each clip's probed source size (shift is a pure
    # function of source dims x canvas dims x focus; nothing else touched).
    reframe_dims: dict[str, tuple[int, int]] = {}

    tmpdir = tempfile.mkdtemp(prefix="monteur-export-")
    try:
        # -- video segments (extended where the NEXT boundary dissolves) --
        paths: list[str] = []
        lengths: list[float] = []
        for i, (kind, length, entry) in enumerate(segments):
            ext = trans[i + 1] if i + 1 < len(segments) else 0.0
            seg_len = length + ext
            seg = str(Path(tmpdir) / f"seg_{i:04d}.mp4")
            if kind == "clip":
                name = Path(entry.clip_path).name
                seg_cover = _reframe_cover(entry, cover, w, h, fps, reframe_dims)
                _run_ffmpeg(
                    [
                        "-ss", f"{max(0.0, entry.source_start):.3f}",
                        "-t", f"{seg_len:.3f}", "-i", str(entry.clip_path),
                        "-map", "0:v:0", "-vf", seg_cover, *seg_encode, "-an", seg,
                    ],
                    f"encoding export segment {i + 1}/{len(segments)} ({name})",
                    cancel,
                )
                tick(name)
            else:
                args = [
                    "-f", "lavfi", "-t", f"{seg_len:.3f}",
                    "-i", f"color=black:s={w}x{h}:r={fps:g}",
                    "-map", "0:v:0",
                ]
                if i in titles:
                    text_file = str(Path(tmpdir) / f"title_{i:04d}.txt")
                    Path(text_file).write_text(titles[i], encoding="utf-8")
                    args += [
                        "-vf",
                        _title_filter(text_file, font, h, length, title_fade),
                    ]
                args += [*seg_encode, "-an", seg]
                _run_ffmpeg(
                    args,
                    f"encoding export segment {i + 1}/{len(segments)} "
                    f"({'title' if i in titles else 'black dip'})",
                    cancel,
                )
                tick("title" if i in titles else "black")
            paths.append(seg)
            lengths.append(seg_len)

        # -- the original-sound bed (nominal lengths — extensions are video-only)
        bed_path: str | None = None
        # Mix mode measures each sounding part's mean level on the way
        # (volumedetect before the pad): the parts whose level stands out
        # are the "prominent original-sound moments" the music bed ducks
        # under (blueprint 1.4) — measured, not guessed, deterministic
        # for the same inputs.
        part_levels: list[tuple[int, float]] = []  # (segment index, mean dB)
        part_peaks: dict[int, float] = {}  # segment index -> max dB (true peak)
        if own_audio:
            bed_parts: list[str] = []
            for i, (kind, length, entry) in enumerate(segments):
                part = str(Path(tmpdir) / f"aud_{i:04d}.wav")
                pcm = [
                    "-vn", "-c:a", "pcm_s16le",
                    "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CHANNELS),
                ]
                if kind == "clip" and _has_audio(entry.clip_path):
                    extract = [
                        "-ss", f"{max(0.0, entry.source_start):.3f}",
                        "-t", f"{length:.3f}", "-i", str(entry.clip_path),
                        "-map", "0:a:0",
                        "-af", "volumedetect,apad" if audio == "mix" else "apad",
                        "-t", f"{length:.3f}", *pcm, part,
                    ]
                    label = f"extracting sound for segment {i + 1}/{len(segments)}"
                    if audio == "mix":
                        stderr = _run_ffmpeg_capture(extract, label, cancel)
                        level = _parse_mean_volume(stderr)
                        if level is not None:
                            part_levels.append((i, level))
                        peak = _parse_max_volume(stderr)
                        if peak is not None:
                            part_peaks[i] = peak
                    else:
                        _run_ffmpeg(extract, label, cancel)
                else:
                    _run_ffmpeg(
                        [
                            "-f", "lavfi", "-t", f"{length:.3f}", "-i", silence,
                            *pcm, part,
                        ],
                        f"generating silence for segment {i + 1}/{len(segments)}",
                        cancel,
                    )
                bed_parts.append(part)
            bed_list = Path(tmpdir) / "bed.txt"
            bed_list.write_text(
                "".join(
                    "file '{}'\n".format(p.replace("'", "'\\''"))
                    for p in bed_parts
                ),
                encoding="utf-8",
            )
            bed_path = str(Path(tmpdir) / "bed.wav")
            _run_ffmpeg(
                [
                    "-f", "concat", "-safe", "0", "-i", str(bed_list),
                    "-c", "copy", bed_path,
                ],
                "joining the original-sound bed",
                cancel,
            )
            tick("audio bed")

        # -- final pass: one graph for transitions, titles' bed, sfx, loudness
        # Adaptive music window: read the song from music_start + music_in
        # (the record<->song mapping is unchanged); the audio graph trims,
        # delays and fades the bed into its record window.
        m_in, m_end = music_window_bounds(plan)
        windowed = m_in > _MIN_GAP or m_end < plan.duration - _MIN_GAP
        music_args: list[str] | None = None
        if audio in ("music", "mix"):
            # The input-side -t bounds the decoded song to the montage:
            # without it the loudness measurement (and the mix bed) would
            # include the song's tail past the montage end — the trimmed
            # output would then be normalized against audio nobody hears
            # (blueprint 1.4: measure what ships, nothing else).
            music_args = [
                "-ss", f"{max(0.0, plan.music_start + m_in):.3f}",
                "-t", f"{max(0.0, plan.duration - m_in):.3f}",
                "-i", str(plan.music_path),
            ]
        sfx_files: list[str] = []
        sfx_data: list[tuple[float, float, float]] = []
        placed_cues: list = []
        for cue in plan.sfx:
            if not cue.file:
                continue  # a search-query marker, not a placed element
            cue_file = Path(cue.file)
            if not cue_file.is_file():
                notes.append(
                    f"sound element missing: {cue_file.name} "
                    f"(cue at {cue.time:.1f}s left out)"
                )
                continue
            try:
                file_len = probe(str(cue_file)).duration
            except MonteurMediaError:
                notes.append(
                    f"sound element unreadable: {cue_file.name} "
                    f"(cue at {cue.time:.1f}s left out)"
                )
                continue
            # Peak-aligned play window (blueprint 1.3): the file plays from
            # its source_offset (a riser's head trim, a shifted hit's skip)
            # and the tail RINGS OUT — available headroom is the file's
            # remainder, clamped to the montage end, never hard-trimmed
            # back to the planned cue length.
            offset = max(0.0, float(getattr(cue, "source_offset", 0.0) or 0.0))
            headroom = max(0.0, file_len - offset)
            trim = min(cue.duration, headroom) if cue.duration > 0 else headroom
            trim = min(trim, max(0.0, plan.duration - cue.time))
            if trim < 1e-3:
                continue
            sfx_files.append(str(cue_file))
            sfx_data.append((max(0.0, cue.time), trim, offset))
            placed_cues.append(cue)

        # Bed ducking (blueprint 1.4): the song ducks under the accents
        # that actually SOUND (the placed cues above) and, in mix mode,
        # under the measured prominent original-sound parts.
        prominent: list[tuple[float, float]] = []
        # O-Ton pop (blueprint 2.2): the SAME prominent windows that duck
        # the music now LIFT the original, each carrying its measured true
        # peak so the lift can be clamped honestly under -1 dBTP.
        prominent_peaks: list[tuple[float, float, float]] = []
        if audio == "mix" and len(part_levels) >= 2:
            vals = sorted(level for _i, level in part_levels)
            mid = len(vals) // 2
            median = (
                vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
            )
            for i, level in part_levels:
                if level >= median + _DUCK_OTON_STANDOUT_DB - 1e-9:
                    _kind, _length, entry = segments[i]
                    prominent.append((entry.record_start, entry.record_end))
                    # Fall back to the mean when the peak was unreadable —
                    # a conservative (louder) clamp, never a hotter lift.
                    peak_db = part_peaks.get(i, level)
                    prominent_peaks.append(
                        (entry.record_start, entry.record_end, peak_db)
                    )
        ducks = (
            ducking_windows(placed_cues, plan.duration, prominent=prominent)
            if audio in ("music", "mix")
            else []
        )
        # O-Ton pop lift (blueprint 2.2): export-only, mix-only, on the
        # ORIGINAL chain — the other side of the 1.4 duck. Empty in every
        # other mode, so those graphs stay byte-identical.
        lifts: list[tuple[float, float, float, float]] = []
        if audio == "mix":
            lifts, lift_notes = oton_lift_windows(prominent_peaks, plan.duration)
            notes.extend(lift_notes)
            if lifts:
                notes.append(
                    f"O-Ton pops: lifted the original +{_LIFT_OTON_DB:g} dB over "
                    f"{len(lifts)} prominent moment"
                    f"{'s' if len(lifts) != 1 else ''} (the music ducks under them)"
                )

        # J/L cuts (blueprint 2.3): the ORIGINAL-sound edit decoupled from
        # the picture cut. Each overlap is the ENTRY's own clip audio,
        # trimmed to the lead/lag window and micro-faded at its outer edge
        # (the crossfade seam), placed on top of the bed so it rings past /
        # anticipates the picture cut. Music bed and grid untouched;
        # export-only, own-audio modes only, opt-in (or hand-authored).
        jl_files: list[str] = []
        # (start, trim, offset, fade_in, fade_out, level)
        jl_data: list[tuple[float, float, float, float, float, float]] = []
        if own_audio and (
            jl_cuts or any(e.audio_lead or e.audio_lag for e in plan.entries)
        ):
            jl_edits, jl_notes = jl_audio_edits(plan, fps)
            notes.extend(jl_notes)
            level = MIX_ORIGINAL_LEVEL if audio == "mix" else 1.0
            for idx, (lead, lag) in sorted(jl_edits.items()):
                entry = plan.entries[idx]
                if not (Path(entry.clip_path).is_file() and _has_audio(entry.clip_path)):
                    continue
                if lead > _MIN_GAP:
                    # J-cut: the incoming shot's HEAD, anticipating — plays
                    # the clip's own material just before its in-point,
                    # starting `lead` before the picture cut. Fade IN at its
                    # head (rises from silence); full into the cut where it
                    # meets the shot's own bed audio.
                    offset = max(0.0, entry.source_start - lead)
                    start = max(0.0, entry.record_start - lead)
                    trim = min(lead, max(0.0, plan.duration - start))
                    if trim > _MIN_GAP:
                        jl_files.append(str(entry.clip_path))
                        jl_data.append((start, trim, offset, _GAP_FADE, 0.0, level))
                if lag > _MIN_GAP:
                    # L-cut: the outgoing shot's TAIL, ringing — plays the
                    # clip's own material just after its out-point, from the
                    # picture cut onward. Full from the cut (continuous with
                    # the bed) and fades OUT at its tail.
                    offset = max(0.0, entry.source_end)
                    start = max(0.0, entry.record_end)
                    trim = min(lag, max(0.0, plan.duration - start))
                    if trim > _MIN_GAP:
                        jl_files.append(str(entry.clip_path))
                        jl_data.append((start, trim, offset, 0.0, _GAP_FADE, level))

        audio_inputs = list(music_args or [])
        if bed_path is not None:
            audio_inputs += ["-i", bed_path]

        def _audio_graph(base_idx: int, loudnorm_arg: str | None) -> str:
            """The audio chain with input labels starting at ``base_idx``
            — built twice: labels 0.. for the measurement pass, labels
            after the video segments for the final mux (same chain,
            byte-for-byte apart from the labels and the loudnorm tail)."""
            i2 = base_idx
            m_label = b_label = None
            if music_args is not None:
                m_label = f"{i2}:a"
                i2 += 1
            if bed_path is not None:
                b_label = f"{i2}:a"
                i2 += 1
            specs = []
            for start, trim, offset in sfx_data:
                specs.append((f"{i2}:a", start, trim, offset))
                i2 += 1
            jl_specs = []
            for start, trim, offset, fin, fout, level in jl_data:
                jl_specs.append((f"{i2}:a", start, trim, offset, fin, fout, level))
                i2 += 1
            return _export_audio_graph(
                audio, m_label, b_label, specs,
                fade_in, fade_out, plan.duration,
                music_in=m_in if windowed else 0.0,
                music_len=(m_end - m_in) if windowed else 0.0,
                music_gaps=music_bed_gaps(plan),
                duck_windows=ducks,
                lift_windows=lifts,
                jl_overlaps=jl_specs,
                loudnorm=loudnorm_arg,
            )

        for f in sfx_files:
            audio_inputs += ["-i", f]
        for f in jl_files:
            audio_inputs += ["-i", f]

        # -- loudness pass 1 (blueprint 1.4): render the finished audio
        # chain into a null muxer, measure it. The values live only
        # inside this one call; failure degrades to single-pass.
        measured = None
        try:
            stderr = _run_ffmpeg_capture(
                [
                    *audio_inputs,
                    "-filter_complex",
                    _audio_graph(0, _EXPORT_LOUDNORM + ":print_format=json"),
                    "-map", "[a]", "-f", "null", "-",
                ],
                "measuring the export loudness",
                cancel,
            )
            measured = _parse_loudnorm_stats(stderr)
        except MonteurMediaError:
            measured = None
        if measured is None:
            loudnorm_final: str | None = None  # single-pass fallback
            notes.append(
                "loudness: the measurement pass failed — single-pass "
                "loudnorm applied instead of the two-pass linear finish"
            )
        else:
            loudnorm_final = (
                f"loudnorm=I={_LOUDNORM_I:g}:TP={_LOUDNORM_TP:g}"
                f":LRA={_LOUDNORM_LRA:g}"
                f":measured_I={measured['input_i']:.2f}"
                f":measured_TP={measured['input_tp']:.2f}"
                f":measured_LRA={measured['input_lra']:.2f}"
                f":measured_thresh={measured['input_thresh']:.2f}"
                f":offset={measured['target_offset']:.2f}:linear=true"
            )
            gain = _LOUDNORM_I - measured["input_i"]
            if measured["input_tp"] + gain > _LOUDNORM_TP + 1e-9:
                # Honest, predictable degradation: when the needed gain
                # would break the -1 dBTP ceiling (extreme crest factor),
                # loudnorm's linear mode reverts to dynamic normalization
                # by its own documented rule — the export still ships,
                # slightly under target rather than clipped. Say so.
                notes.append(
                    "loudness: the mix's true peaks leave no headroom for "
                    f"the {gain:+.1f} dB linear gain — loudnorm normalized "
                    "dynamically and the export sits below the -14 LUFS "
                    "target rather than crushing the transients"
                )
        tick("loudness")

        final: list[str] = []
        for seg in paths:
            final += ["-i", seg]
        final += audio_inputs
        graph = (
            _export_video_graph(lengths, trans, fade_in, fade_out, plan.duration)
            + ";"
            + _audio_graph(len(paths), loudnorm_final)
        )
        final += [
            "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", profile["preset"],
            "-crf", profile["crf"], "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", profile["audio_bitrate"],
            "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CHANNELS),
            "-movflags", "+faststart",
            "-t", f"{plan.duration:.3f}", str(out_path),
        ]
        _run_ffmpeg(final, f"rendering the export to {Path(out_path).name}", cancel)
        tick("mux")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    info = probe(out_path)
    result = {
        "path": str(out_path),
        "duration": info.duration,
        "width": w,
        "height": h,
        "seconds": time.monotonic() - started,
        "notes": notes,
    }
    # Self-critique support (blueprint 4.1): expose the two-pass loudnorm's
    # measured integrated loudness (pass 1's ``input_i``, in LUFS) so the
    # refine loop can score the export against the -14 LUFS target WITHOUT
    # a second measuring pass. Only present when the measurement succeeded
    # (a failed/degraded single-pass finish omits it — honestly absent, not
    # a guessed value).
    if measured is not None:
        result["measured_lufs"] = measured["input_i"]
    return result
