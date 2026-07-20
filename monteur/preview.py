"""Sehen ohne Resolve: render a MontagePlan to a small MP4, plus thumbnails.

This is the engine behind Studio's preview player and storyboard: a
:class:`~monteur.montage.MontagePlan` becomes a low-resolution, uniformly
encoded MP4 (:func:`render_preview`) and any clip position becomes a
storyboard thumbnail (:func:`extract_thumbnail`) — no DaVinci Resolve, no
export/import round-trip. The point is a fast, honest look at the cut the
plan describes: the same source ranges, the same record positions, the
same black dips, the same music offset the Resolve timeline would get.

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

Limitations (v1, deliberate): no titles/drawtext on the dips yet — they
render as plain black; SFX cues with placed ``file``\\ s are NOT mixed in;
dissolves (``MontageEntry.transition``) render as hard cuts. All three are
follow-ups once Studio's player needs them.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from monteur.media import MonteurMediaError, find_ffmpeg, probe
from monteur.montage import MontagePlan

# Fixed "mix" levels (documented above): the song stays at full level, the
# clips' own sound is ducked under it.
MIX_MUSIC_LEVEL = 1.0
MIX_ORIGINAL_LEVEL = 0.6

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


def _run_ffmpeg(args: list[str], label: str) -> None:
    """Run ffmpeg with ``args``; raise MonteurMediaError on any failure."""
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        result = subprocess.run(cmd, capture_output=True)
    except OSError as exc:  # binary vanished / not executable
        raise MonteurMediaError(f"could not run ffmpeg while {label}: {exc}") from exc
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace")[-_STDERR_TAIL:]
        raise MonteurMediaError(f"ffmpeg failed while {label}: {tail}")


def _even(n: float) -> int:
    """Nearest even int at/below ``n``, minimum 2 (codecs need even sizes)."""
    return max(2, int(n) // 2 * 2)


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
                _run_ffmpeg(
                    [
                        *args, "-map", "0:v:0", *encode, *audio_args, seg,
                    ],
                    f"encoding segment {i + 1}/{len(segments)} (black dip)",
                )
                tick("black")
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

        final = ["-f", "concat", "-safe", "0", "-i", str(list_path)]
        if audio in ("music", "mix"):
            final += [
                "-ss", f"{max(0.0, plan.music_start):.3f}",
                "-i", str(plan.music_path),
            ]
        final += ["-map", "0:v:0"]
        if vf:
            final += ["-vf", ",".join(vf)]
        final += encode
        if audio == "music":
            final += ["-map", "1:a:0"]
            if af:
                final += ["-af", ",".join(af)]
            final += audio_encode
        elif audio == "original":
            final += ["-map", "0:a:0"]
            if af:
                final += ["-af", ",".join(af)]
            final += audio_encode
        else:  # mix: music at MIX_MUSIC_LEVEL + own sound at MIX_ORIGINAL_LEVEL
            chain = (
                f"[1:a]volume={MIX_MUSIC_LEVEL:g}[m];"
                f"[0:a]volume={MIX_ORIGINAL_LEVEL:g}[o];"
                "[m][o]amix=inputs=2:duration=first:dropout_transition=0"
                ":normalize=0"
            )
            if af:
                chain += "," + ",".join(af)
            final += ["-filter_complex", chain + "[a]", "-map", "[a]", *audio_encode]
        final += ["-t", f"{plan.duration:.3f}", str(out_path)]
        _run_ffmpeg(final, f"muxing the preview to {Path(out_path).name}")
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
