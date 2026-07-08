"""Paper editing for Monteur: cut your film in a text editor.

A *papercut* is the oldest editing trick in the book, reborn as markdown.
You transcribe your footage, print the transcript as a checklist, and edit
with a pen: tick the takes you like, cross out the rest, shuffle the pages
into story order. This module does exactly that with plain text files:

1. ``create_papercut`` turns a :class:`~monteur.model.Transcript` into a
   markdown checklist. Every utterance becomes one line with its source
   timecode and text::

       - [ ] [00:00:01.000 --> 00:00:04.200] ANNA: text of the segment

2. You open that file in any editor. Tick a box (``- [x]``) to select a
   take. Move lines up or down to reorder the cut. Delete what you never
   want to see again. Nothing else matters; prose between entries is
   ignored.

3. ``parse_papercut`` reads the edited file back, and
   ``papercut_to_timeline`` turns the ticked lines -- in the order they
   appear in the file -- into a real :class:`~monteur.model.Timeline` with
   matching video and audio clips, ready for EDL/FCPXML export into your
   NLE.

Multiple source files can live in one papercut: ``## source: b_cam.mov``
section headers switch the source for all entries that follow, so a whole
multi-interview selects can be edited as a single document
(``create_papercut_multi`` / ``merge_papercuts`` build such files).
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field

from .model import AUDIO, VIDEO, Clip, Timeline, Transcript, seconds_to_frames

DEFAULT_FPS = 25.0

_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[(?P<tick>[ xX])\]\s*(?P<body>.*)$")
_SPAN_RE = re.compile(
    r"^\[\s*(?P<start>\d{1,2}:\d{1,2}:\d{1,2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{1,2}:\d{1,2}[.,]\d{1,3})\s*\]\s*(?P<text>.*)$"
)
_TS_RE = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})[.,](\d{1,3})$")
_SPEAKER_RE = re.compile(r"^(?P<name>[A-Z][A-Z0-9 _.'-]{0,29}):\s*(?P<text>.*)$")
_SECTION_RE = re.compile(r"^##\s*source\s*:\s*(?P<name>.*)$", re.IGNORECASE)
_TITLE_RE = re.compile(r"^#\s*(?:Papercut\s*:\s*)?(?P<title>.*)$")
_FIELD_RE = re.compile(r"^(?P<key>source|fps)\s*:\s*(?P<value>.*)$", re.IGNORECASE)


@dataclass
class PapercutEntry:
    """One transcript line in a papercut file."""

    source_name: str
    start: float
    end: float
    text: str
    selected: bool
    speaker: str = ""


@dataclass
class Papercut:
    """A parsed papercut document."""

    title: str
    fps: float
    entries: list[PapercutEntry] = field(default_factory=list)
    default_source: str = ""


def _format_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hh, rem = divmod(total_ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, ms = divmod(rem, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def _parse_timestamp(ts: str) -> float:
    m = _TS_RE.match(ts)
    if not m:
        raise ValueError(f"malformed timestamp: {ts!r}")
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ms = int(m.group(4).ljust(3, "0"))
    return hh * 3600.0 + mm * 60.0 + ss + ms / 1000.0


def _entry_line(
    start: float, end: float, text: str, speaker: str = "", selected: bool = False
) -> str:
    tick = "x" if selected else " "
    prefix = f"{speaker.upper()}: " if speaker else ""
    span = f"[{_format_timestamp(start)} --> {_format_timestamp(end)}]"
    return f"- [{tick}] {span} {prefix}{text}"


def _header_lines(title: str, source: str, fps: float) -> list[str]:
    return [
        f"# Papercut: {title}",
        "",
        f"source: {source}",
        f"fps: {fps:g}",
        "",
        "Tick the takes you want. Reorder lines to reorder the cut.",
        "",
    ]


def create_papercut(transcript: Transcript, fps: float, title: str = "") -> str:
    """Render a transcript as an editable markdown checklist."""
    lines = _header_lines(title, transcript.source_name, fps)
    for seg in transcript.segments:
        lines.append(_entry_line(seg.start, seg.end, seg.text, seg.speaker))
    return "\n".join(lines) + "\n"


def create_papercut_multi(
    transcripts: list[Transcript], fps: float, title: str = ""
) -> str:
    """Render several transcripts as one papercut with ``## source:`` sections.

    The first transcript's segments sit under the top-level ``source:``
    field; each further transcript opens a new section.
    """
    if not transcripts:
        return "\n".join(_header_lines(title, "", fps)) + "\n"
    first, rest = transcripts[0], transcripts[1:]
    lines = _header_lines(title, first.source_name, fps)
    for seg in first.segments:
        lines.append(_entry_line(seg.start, seg.end, seg.text, seg.speaker))
    for transcript in rest:
        lines.append("")
        lines.append(f"## source: {transcript.source_name}")
        lines.append("")
        for seg in transcript.segments:
            lines.append(_entry_line(seg.start, seg.end, seg.text, seg.speaker))
    return "\n".join(lines) + "\n"


def merge_papercuts(papercuts: list[str], title: str) -> str:
    """Combine several papercut files into one multi-source document.

    Selection state and entry order are preserved; the fps of the first
    papercut wins. Entries are regrouped under ``## source:`` headers
    whenever the source changes.
    """
    parsed = [parse_papercut(text) for text in papercuts]
    fps = parsed[0].fps if parsed else DEFAULT_FPS
    entries = [e for pc in parsed for e in pc.entries]
    default_source = entries[0].source_name if entries else ""
    lines = _header_lines(title, default_source, fps)
    current = default_source
    for entry in entries:
        if entry.source_name != current:
            current = entry.source_name
            lines.append("")
            lines.append(f"## source: {current}")
            lines.append("")
        lines.append(
            _entry_line(entry.start, entry.end, entry.text, entry.speaker, entry.selected)
        )
    return "\n".join(lines) + "\n"


def parse_papercut(text: str) -> Papercut:
    """Parse a (possibly hand-edited) papercut file.

    Tolerant of missing header fields: a missing ``fps:`` falls back to
    25.0 with a warning, a missing ``source:`` yields an empty source
    name. Lines that are not checklist entries or known headers are
    ignored. A checklist entry with a malformed timestamp raises
    ValueError naming the line number.
    """
    title = ""
    fps: float | None = None
    default_source = ""
    current_source = ""
    in_section = False
    entries: list[PapercutEntry] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        entry_m = _ENTRY_RE.match(line)
        if entry_m:
            span_m = _SPAN_RE.match(entry_m.group("body"))
            if not span_m:
                raise ValueError(
                    f"line {lineno}: papercut entry has a malformed or "
                    f"missing timestamp: {line!r}"
                )
            try:
                start = _parse_timestamp(span_m.group("start"))
                end = _parse_timestamp(span_m.group("end"))
            except ValueError as exc:
                raise ValueError(f"line {lineno}: {exc}") from None
            body = span_m.group("text").strip()
            speaker = ""
            speaker_m = _SPEAKER_RE.match(body)
            if speaker_m:
                speaker = speaker_m.group("name").strip()
                body = speaker_m.group("text").strip()
            entries.append(
                PapercutEntry(
                    source_name=current_source,
                    start=start,
                    end=end,
                    text=body,
                    selected=entry_m.group("tick").lower() == "x",
                    speaker=speaker,
                )
            )
            continue

        section_m = _SECTION_RE.match(line)
        if section_m:
            current_source = section_m.group("name").strip()
            in_section = True
            continue

        if line.startswith("#"):
            title_m = _TITLE_RE.match(line)
            if title_m and not title:
                title = title_m.group("title").strip()
            continue

        field_m = _FIELD_RE.match(line)
        if field_m:
            key = field_m.group("key").lower()
            value = field_m.group("value").strip()
            if key == "source":
                default_source = value
                if not in_section:
                    current_source = value
            elif key == "fps":
                try:
                    fps = float(value)
                except ValueError:
                    pass

    if fps is None:
        warnings.warn(
            f"papercut has no usable 'fps:' header; assuming {DEFAULT_FPS}",
            stacklevel=2,
        )
        fps = DEFAULT_FPS

    return Papercut(
        title=title, fps=fps, entries=entries, default_source=default_source
    )


def papercut_to_timeline(
    papercut: Papercut, handles: float = 0.0, name: str = ""
) -> Timeline:
    """Assemble the ticked entries, in file order, into a timeline.

    Each selected entry contributes one video clip on V1 and one matching
    audio clip on A1, so exports keep the interview sound. ``handles``
    extends every source range by that many seconds on both sides (the
    head is clamped at 0.0). Record ranges are packed back-to-back from
    frame 0.
    """
    timeline = Timeline(name=name or papercut.title, fps=papercut.fps)
    cursor = 0
    for entry in papercut.entries:
        if not entry.selected:
            continue
        src_start = max(0.0, entry.start - handles)
        src_end = entry.end + handles
        source_in = seconds_to_frames(src_start, papercut.fps)
        source_out = seconds_to_frames(src_end, papercut.fps)
        duration = source_out - source_in
        source_name = entry.source_name or papercut.default_source
        clip_name = entry.text[:40].strip()
        for track, kind in (("V1", VIDEO), ("A1", AUDIO)):
            timeline.clips.append(
                Clip(
                    name=clip_name,
                    track=track,
                    kind=kind,
                    source_in=source_in,
                    source_out=source_out,
                    record_in=cursor,
                    record_out=cursor + duration,
                    source_name=source_name,
                )
            )
        cursor += duration
    return timeline
