"""SubRip (.srt) subtitle reading and writing.

Supported subset
----------------
* Standard SRT blocks: numeric index line (optional — blocks missing
  their index are tolerated), a time line
  ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` (a ``.`` millisecond separator is
  also accepted), then one or more text lines joined with single spaces.
* UTF-8 BOM, CRLF line endings and stray blank lines are tolerated.
* An all-uppercase ``SPEAKER:`` prefix on a block's text (e.g.
  ``ANNA: hello``) is detected and moved to ``segment.speaker``.

Limitations
-----------
* Formatting tags (``<i>``, ``{\\an8}`` etc.) are kept verbatim in the
  text; no styling model exists.
* Lowercase or mixed-case prefixes (``Note:``) are deliberately NOT
  treated as speakers.
* ``write_srt`` renumbers blocks sequentially from 1.
"""

from __future__ import annotations

import re

from monteur.model import Transcript, TranscriptSegment

_TIME_LINE_RE = re.compile(
    r"^(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
    r"\s*-->\s*"
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
    r"\s*(?:X1.*)?$"
)
_SPEAKER_RE = re.compile(r"^([A-Z][A-Z0-9 .'_-]{0,40}):\s*(.+)$")


def _block_time(groups: tuple[str, ...], offset: int) -> float:
    hh, mm, ss, ms = (int(groups[offset + i]) for i in range(4))
    return hh * 3600 + mm * 60 + ss + ms / 1000.0


def read_srt(text: str, source_name: str = "") -> Transcript:
    """Parse SRT ``text`` into a :class:`Transcript`.

    Raises ValueError on blocks that lack a valid time line.
    """
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    transcript = Transcript(source_name=source_name)
    blocks = re.split(r"\n\s*\n", text)
    index = 0
    for block_no, block in enumerate(blocks, start=1):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if lines[0].isdigit() and len(lines) > 1:
            lines = lines[1:]
        m = _TIME_LINE_RE.match(lines[0])
        if not m:
            raise ValueError(
                f"SRT block {block_no}: expected a time line like "
                f"'00:00:01,000 --> 00:00:02,000', got {lines[0]!r}"
            )
        start = _block_time(m.groups(), 0)
        end = _block_time(m.groups(), 4)
        if end < start:
            raise ValueError(
                f"SRT block {block_no}: end time {lines[0]!r} precedes start time"
            )
        joined = " ".join(lines[1:]).strip()
        speaker = ""
        sm = _SPEAKER_RE.match(joined)
        if sm:
            speaker, joined = sm.group(1).strip(), sm.group(2).strip()
        index += 1
        transcript.segments.append(
            TranscriptSegment(
                index=index, start=start, end=end, text=joined, speaker=speaker
            )
        )
    return transcript


def _fmt_time(seconds: float) -> str:
    if seconds < 0:
        raise ValueError(f"negative segment time: {seconds}")
    total_ms = round(seconds * 1000)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    return f"{total_s // 3600:02d}:{total_s // 60 % 60:02d}:{total_s % 60:02d},{ms:03d}"


def write_srt(transcript: Transcript) -> str:
    """Serialize ``transcript`` as an SRT string (blocks renumbered from 1)."""
    blocks: list[str] = []
    for i, seg in enumerate(transcript.segments, start=1):
        text = f"{seg.speaker}: {seg.text}" if seg.speaker else seg.text
        blocks.append(f"{i}\n{_fmt_time(seg.start)} --> {_fmt_time(seg.end)}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")
