"""CMX3600 EDL reading and writing.

Supported subset
----------------
* ``TITLE:`` and ``FCM:`` header lines (``DROP FRAME`` / ``NON-DROP FRAME``).
* Standard 8/9-column event lines::

      001  REEL     V     C        01:00:10:00 01:00:15:00 00:00:00:00 00:00:05:00

  Channel field mapping: ``V`` -> video on V1, ``A`` -> audio on A1,
  ``A2``/``A3``/... -> audio on that track, ``AA`` -> audio on A1+A2,
  ``B`` -> video on V1 + audio on A1, and slash combinations such as
  ``AA/V`` or ``A2/V`` combine the parts. ``NONE`` produces no clips.
* ``* FROM CLIP NAME:`` / ``* TO CLIP NAME:`` comments name the clips of
  the preceding event (``TO`` wins for transition events, matching CMX
  dissolve semantics where the incoming clip is the ``TO`` side).
* Dissolves and wipes (transition ``D`` / ``Wxxx``) are imported as cuts;
  the transition type and duration are preserved in ``clip.metadata``
  under ``"transition"`` and ``"transition_duration"``. Zero-length
  outgoing events of a dissolve pair are skipped.

Dissolves on write
------------------
``write_edl`` re-emits dissolves: a clip whose ``metadata["transition"]``
is ``"dissolve"`` (or ``"D"``) with a positive ``"transition_frames"``
(or ``"transition_duration"``) and a same-track, same-kind predecessor
ending exactly at its record-in becomes a standard CMX3600 dissolve pair
sharing one event number::

    005  TAPE002  V     C        02:00:10:00 02:00:10:00 00:00:08:00 00:00:08:00
    005  TAPE004  V     D    012 04:00:00:00 04:00:05:00 00:00:08:00 00:00:13:00
    * FROM CLIP NAME: B-Roll Street
    * TO CLIP NAME: Sunset Drone

The first source line is the outgoing clip's zero-duration tail (cut
transition), the second the incoming clip with ``D`` and the dissolve
length in frames — exactly the form :func:`read_edl` parses back
(the zero-duration tail yields no clip; the incoming clip keeps the
transition in its metadata), so dissolves roundtrip.

Limitations
-----------
* Motion-effect (``M2``), split-edit and audio-level lines are ignored.
* ``write_edl`` emits one event per clip (no channel grouping); only
  dissolve metadata is re-emitted — wipes/keys imported by ``read_edl``
  are written back as plain cuts, as is a dissolve clip without a
  predecessor butting against its record-in.
* Key (``K``) transitions are imported as cuts like dissolves/wipes.
"""

from __future__ import annotations

import re

from monteur.model import (
    AUDIO,
    VIDEO,
    Clip,
    Timeline,
    format_timecode,
    is_drop_frame_rate,
    parse_timecode,
)

_FROM_CLIP_RE = re.compile(r"^\*\s*FROM CLIP NAME:\s*(.+?)\s*$", re.IGNORECASE)
_TO_CLIP_RE = re.compile(r"^\*\s*TO CLIP NAME:\s*(.+?)\s*$", re.IGNORECASE)
_AUDIO_TRACK_RE = re.compile(r"^A(\d+)$")
_REEL_SANITIZE_RE = re.compile(r"[^A-Z0-9_]")


def _channel_targets(channel: str, line_no: int) -> list[tuple[str, str]]:
    """Expand a CMX channel field into (kind, track) pairs."""
    targets: list[tuple[str, str]] = []
    for part in channel.upper().split("/"):
        if part == "V":
            targets.append((VIDEO, "V1"))
        elif part == "A":
            targets.append((AUDIO, "A1"))
        elif part == "AA":
            targets.append((AUDIO, "A1"))
            targets.append((AUDIO, "A2"))
        elif part == "B":
            targets.append((VIDEO, "V1"))
            targets.append((AUDIO, "A1"))
        elif part == "NONE":
            pass
        elif _AUDIO_TRACK_RE.match(part):
            targets.append((AUDIO, part))
        else:
            raise ValueError(
                f"line {line_no}: unrecognized EDL channel field {channel!r} "
                f"(expected V, A, A2, AA, B or a / combination)"
            )
    deduped: list[tuple[str, str]] = []
    for t in targets:
        if t not in deduped:
            deduped.append(t)
    return deduped


def read_edl(text: str, fps: float, name: str = "") -> Timeline:
    """Parse CMX3600 EDL ``text`` into a :class:`Timeline` at ``fps``.

    ``fps`` is required because EDLs do not carry a frame rate. Raises
    ValueError on malformed event lines or timecodes.
    """
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    text = text.lstrip("\ufeff")
    timeline = Timeline(name=name, fps=fps)
    last_event_clips: list[Clip] = []
    last_event_is_transition = False
    last_event_named_by_to = False

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("TITLE:"):
            title = line[len("TITLE:"):].strip()
            if not timeline.name:
                timeline.name = title
            timeline.metadata["title"] = title
            continue
        if upper.startswith("FCM:"):
            fcm = line[len("FCM:"):].strip().upper()
            timeline.metadata["fcm"] = fcm
            continue
        if line.startswith("*"):
            m = _FROM_CLIP_RE.match(line)
            if m and last_event_clips and not last_event_named_by_to:
                for clip in last_event_clips:
                    clip.name = m.group(1)
                continue
            m = _TO_CLIP_RE.match(line)
            if m and last_event_clips and last_event_is_transition:
                for clip in last_event_clips:
                    clip.name = m.group(1)
                last_event_named_by_to = True
            continue

        parts = line.split()
        if not parts[0].isdigit():
            continue
        if len(parts) not in (8, 9):
            raise ValueError(
                f"line {line_no}: malformed EDL event line (expected 8 or 9 "
                f"fields, got {len(parts)}): {raw!r}"
            )
        event_num, reel, channel, transition = parts[0], parts[1], parts[2], parts[3]
        trans_dur = parts[4] if len(parts) == 9 else ""
        try:
            src_in, src_out, rec_in, rec_out = (
                parse_timecode(tc, fps) for tc in parts[-4:]
            )
        except ValueError as exc:
            raise ValueError(f"line {line_no}: {exc}") from None

        last_event_clips = []
        last_event_is_transition = transition.upper() != "C"
        last_event_named_by_to = False
        if rec_out <= rec_in:
            continue

        metadata: dict = {"event": int(event_num)}
        if last_event_is_transition:
            metadata["transition"] = transition.upper()
            if trans_dur:
                try:
                    metadata["transition_duration"] = int(trans_dur)
                except ValueError:
                    metadata["transition_duration"] = trans_dur

        for kind, track in _channel_targets(channel, line_no):
            clip = Clip(
                name=reel,
                track=track,
                kind=kind,
                source_in=src_in,
                source_out=src_out,
                record_in=rec_in,
                record_out=rec_out,
                source_name=reel,
                metadata=dict(metadata),
            )
            timeline.clips.append(clip)
            last_event_clips.append(clip)

    return timeline


def _reel_for(clip: Clip) -> str:
    raw = (clip.source_name or clip.name).upper()
    reel = _REEL_SANITIZE_RE.sub("", raw.replace(" ", "_"))[:8]
    return reel or "AX"


def _channel_for(clip: Clip) -> str:
    if clip.kind == VIDEO:
        return "V"
    m = _AUDIO_TRACK_RE.match(clip.track.upper())
    if m and m.group(1) != "1":
        return f"A{m.group(1)}"
    return "A"


def _transition_frames(clip: Clip) -> int:
    """Dissolve length in frames from a clip's transition metadata (0 = cut)."""
    if str(clip.metadata.get("transition", "")).upper() not in ("D", "DISSOLVE"):
        return 0
    raw = clip.metadata.get(
        "transition_frames", clip.metadata.get("transition_duration", 0)
    )
    try:
        frames = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(frames, 0)


def _media_start_frames(clip: Clip, fps: float) -> int:
    """The file's embedded start TC (``metadata["media_start_seconds"]``) in frames.

    Resolve conforms an EDL by matching absolute source timecodes against the
    media's embedded timecode, so clips whose model-level source ranges are
    file-relative must be shifted by this offset on write. 0 when absent or
    invalid.
    """
    try:
        seconds = float(clip.metadata.get("media_start_seconds") or 0.0)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return round(seconds * fps)


def write_edl(timeline: Timeline, title: str = "") -> str:
    """Serialize ``timeline`` as a CMX3600 EDL string.

    Clips carrying dissolve metadata are written as CMX dissolve pairs
    (outgoing zero-duration tail with ``C``, incoming clip with ``D`` and
    the length in frames, plus ``FROM``/``TO CLIP NAME`` comments — see
    the module docstring); everything else is a plain cut.

    A clip with ``metadata["media_start_seconds"]`` (the file's embedded
    start timecode) has that offset added to its SOURCE in/out timecodes,
    since EDL conform matches by absolute source TC; record timecodes are
    never shifted.
    """
    drop = is_drop_frame_rate(timeline.fps)
    lines = [
        f"TITLE: {title or timeline.name or 'UNTITLED'}",
        f"FCM: {'DROP FRAME' if drop else 'NON-DROP FRAME'}",
        "",
    ]
    ordered = sorted(
        timeline.clips, key=lambda c: (c.record_in, c.kind != VIDEO, c.track)
    )

    def tc(frames: int) -> str:
        return format_timecode(frames, timeline.fps, drop_frame=drop)

    for num, clip in enumerate(ordered, start=1):
        src_offset = _media_start_frames(clip, timeline.fps)
        tcs = " ".join(
            tc(f)
            for f in (
                clip.source_in + src_offset,
                clip.source_out + src_offset,
                clip.record_in,
                clip.record_out,
            )
        )
        dissolve = _transition_frames(clip)
        outgoing = None
        if dissolve > 0:
            outgoing = next(
                (
                    c
                    for c in ordered
                    if c is not clip
                    and c.kind == clip.kind
                    and c.track == clip.track
                    and c.record_out == clip.record_in
                ),
                None,
            )
        full_name = clip.name or clip.source_name
        if outgoing is not None:
            # CMX dissolve pair: zero-duration outgoing tail, then the
            # incoming clip with the dissolve duration in frames.
            out_offset = _media_start_frames(outgoing, timeline.fps)
            tail = " ".join(
                tc(f)
                for f in (
                    outgoing.source_out + out_offset,
                    outgoing.source_out + out_offset,
                    clip.record_in,
                    clip.record_in,
                )
            )
            lines.append(
                f"{num:03d}  {_reel_for(outgoing):<8} {_channel_for(clip):<5} C        {tail}"
            )
            lines.append(
                f"{num:03d}  {_reel_for(clip):<8} {_channel_for(clip):<5} D    {dissolve:03d} {tcs}"
            )
            from_name = outgoing.name or outgoing.source_name
            if from_name:
                lines.append(f"* FROM CLIP NAME: {from_name}")
            if full_name:
                lines.append(f"* TO CLIP NAME: {full_name}")
        else:
            lines.append(
                f"{num:03d}  {_reel_for(clip):<8} {_channel_for(clip):<5} C        {tcs}"
            )
            if full_name:
                lines.append(f"* FROM CLIP NAME: {full_name}")
    lines.append("")
    return "\n".join(lines)
