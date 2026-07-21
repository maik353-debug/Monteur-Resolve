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

from monteur.media import MonteurMediaError, find_ffmpeg, probe
from monteur.montage import MontagePlan, music_window_bounds

# Fixed "mix" levels (documented above): the song stays at full level, the
# clips' own sound is ducked under it.
MIX_MUSIC_LEVEL = 1.0
MIX_ORIGINAL_LEVEL = 0.6

# Adaptive music window (plan.music_in / plan.music_out): a delayed entry
# gets a short musical fade-in so the slam does not click; the bed is
# trimmed and delayed (adelay) to its record window in both renderers.
_MUSIC_FADE_IN = 0.5

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
_TITLE_FADE = 0.3
# Loudness finish: single-pass loudnorm at YouTube's streaming target
# (-14 LUFS integrated, -1 dBTP true peak, LRA 11) — the platform then
# leaves the level alone instead of turning it down.
_EXPORT_LOUDNORM = "loudnorm=I=-14:TP=-1:LRA=11"
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
                video_args: list[str] = []
                if i in titles:
                    # The act title over its black dip (blueprint 1.8) —
                    # the same drawtext the export uses, preview quality.
                    text_file = str(Path(tmpdir) / f"title_{i:04d}.txt")
                    Path(text_file).write_text(titles[i], encoding="utf-8")
                    video_args = ["-vf", _title_filter(text_file, font, h, length)]
                _run_ffmpeg(
                    [
                        *args, "-map", "0:v:0", *video_args,
                        *encode, *audio_args, seg,
                    ],
                    f"encoding segment {i + 1}/{len(segments)} "
                    f"({'title' if i in titles else 'black dip'})",
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


def _title_filter(text_file: str, font: str, height: int, dip_len: float) -> str:
    """The drawtext filter for one act title over a black dip segment.

    Plain white, centered, font size proportional to the canvas height.
    A dip long enough to afford it fades the text in and out over
    :data:`_TITLE_FADE` seconds INSIDE the dip; a short dip shows the
    text for the dip's full length. The alpha expression always cuts to
    0 after ``dip_len`` — a segment extended for a following dissolve
    must not carry the title into the extension.
    """
    fade = _TITLE_FADE if dip_len >= 2 * _TITLE_FADE + 0.2 else 0.0
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
    single-pass ``loudnorm`` at YouTube's -14 LUFS / -1 dBTP / LRA 11
    target, then a resample back to :data:`_AUDIO_RATE` (loudnorm
    upsamples internally).

    ``music_in`` / ``music_len`` (keyword-only, both 0 by default = the
    old full-length graph, byte-identical) apply the plan's adaptive
    music window to the song stream BEFORE it becomes the bed: trim to
    ``music_len`` seconds, delay by ``music_in`` (adelay) and — at a
    non-zero entry — a short :data:`_MUSIC_FADE_IN` musical fade-in. The
    caller reads the song from ``music_start + music_in``, so the beat
    grid alignment is untouched.
    """
    parts: list[str] = []
    if music_label is not None and (music_in > 0 or music_len > 0):
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
        parts.append(f"[{music_label}]{','.join(filters)}[xmw]")
        music_label = "xmw"
    if audio == "music":
        base = f"[{music_label}]"
    elif audio == "original":
        base = f"[{bed_label}]"
    else:  # mix
        parts.append(f"[{music_label}]volume={MIX_MUSIC_LEVEL:g}[xm]")
        parts.append(f"[{bed_label}]volume={MIX_ORIGINAL_LEVEL:g}[xo]")
        parts.append(
            "[xm][xo]amix=inputs=2:duration=first:dropout_transition=0"
            ":normalize=0[xbed]"
        )
        base = "[xbed]"
    if sfx:
        labels = ""
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
        parts.append(
            f"{base}{labels}amix=inputs={1 + len(sfx)}:duration=first"
            ":dropout_transition=0:normalize=0[xfx]"
        )
        base = "[xfx]"
    tail: list[str] = []
    if fade_in > 0:
        tail.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        st = max(0.0, duration - fade_out)
        tail.append(f"afade=t=out:st={st:.3f}:d={fade_out:.3f}")
    tail.append(_EXPORT_LOUDNORM)
    tail.append(f"aresample={_AUDIO_RATE}")
    parts.append(f"{base}{','.join(tail)}[a]")
    return ";".join(parts)


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
    * **Loudness** — the audio chain finishes with single-pass
      ``loudnorm`` at YouTube's target (-14 LUFS, -1 dBTP, LRA 11).
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
    total = len(segments) + (1 if own_audio else 0) + 1
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

    cover = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,fps={fps:g}"
    )
    seg_encode = [
        "-c:v", "libx264", "-preset", _SEG_PRESET, "-crf", profile["seg_crf"],
        "-pix_fmt", "yuv420p",
    ]
    silence = f"anullsrc=r={_AUDIO_RATE}:cl=stereo"

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
                _run_ffmpeg(
                    [
                        "-ss", f"{max(0.0, entry.source_start):.3f}",
                        "-t", f"{seg_len:.3f}", "-i", str(entry.clip_path),
                        "-map", "0:v:0", "-vf", cover, *seg_encode, "-an", seg,
                    ],
                    f"encoding export segment {i + 1}/{len(segments)} ({name})",
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
                    args += ["-vf", _title_filter(text_file, font, h, length)]
                args += [*seg_encode, "-an", seg]
                _run_ffmpeg(
                    args,
                    f"encoding export segment {i + 1}/{len(segments)} "
                    f"({'title' if i in titles else 'black dip'})",
                )
                tick("title" if i in titles else "black")
            paths.append(seg)
            lengths.append(seg_len)

        # -- the original-sound bed (nominal lengths — extensions are video-only)
        bed_path: str | None = None
        if own_audio:
            bed_parts: list[str] = []
            for i, (kind, length, entry) in enumerate(segments):
                part = str(Path(tmpdir) / f"aud_{i:04d}.wav")
                pcm = [
                    "-vn", "-c:a", "pcm_s16le",
                    "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CHANNELS),
                ]
                if kind == "clip" and _has_audio(entry.clip_path):
                    _run_ffmpeg(
                        [
                            "-ss", f"{max(0.0, entry.source_start):.3f}",
                            "-t", f"{length:.3f}", "-i", str(entry.clip_path),
                            "-map", "0:a:0", "-af", "apad",
                            "-t", f"{length:.3f}", *pcm, part,
                        ],
                        f"extracting sound for segment {i + 1}/{len(segments)}",
                    )
                else:
                    _run_ffmpeg(
                        [
                            "-f", "lavfi", "-t", f"{length:.3f}", "-i", silence,
                            *pcm, part,
                        ],
                        f"generating silence for segment {i + 1}/{len(segments)}",
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
            )
            tick("audio bed")

        # -- final pass: one graph for transitions, titles' bed, sfx, loudness
        final: list[str] = []
        for seg in paths:
            final += ["-i", seg]
        idx = len(paths)
        music_label: str | None = None
        bed_label: str | None = None
        # Adaptive music window: read the song from music_start + music_in
        # (the record<->song mapping is unchanged); the audio graph trims,
        # delays and fades the bed into its record window.
        m_in, m_end = music_window_bounds(plan)
        windowed = m_in > _MIN_GAP or m_end < plan.duration - _MIN_GAP
        if audio in ("music", "mix"):
            final += [
                "-ss", f"{max(0.0, plan.music_start + m_in):.3f}",
                "-i", str(plan.music_path),
            ]
            music_label = f"{idx}:a"
            idx += 1
        if bed_path is not None:
            final += ["-i", bed_path]
            bed_label = f"{idx}:a"
            idx += 1
        sfx_specs: list[tuple[str, float, float]] = []
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
            final += ["-i", str(cue_file)]
            sfx_specs.append((f"{idx}:a", max(0.0, cue.time), trim, offset))
            idx += 1
        graph = (
            _export_video_graph(lengths, trans, fade_in, fade_out, plan.duration)
            + ";"
            + _export_audio_graph(
                audio, music_label, bed_label, sfx_specs,
                fade_in, fade_out, plan.duration,
                music_in=m_in if windowed else 0.0,
                music_len=(m_end - m_in) if windowed else 0.0,
            )
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
        _run_ffmpeg(final, f"rendering the export to {Path(out_path).name}")
        tick("mux")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    info = probe(out_path)
    return {
        "path": str(out_path),
        "duration": info.duration,
        "width": w,
        "height": h,
        "seconds": time.monotonic() - started,
        "notes": notes,
    }
