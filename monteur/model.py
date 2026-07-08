"""Core data model for Monteur.

Everything in Monteur speaks this vocabulary: frames at a given frame rate.
Timelines are flat lists of clips with source and record ranges; all ranges
are half-open (`in` inclusive, `out` exclusive), matching EDL semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

# --- Timecode ---------------------------------------------------------------

_TC_RE = re.compile(r"^(\d{1,2})[:;.](\d{1,2})[:;.](\d{1,2})([:;.,])(\d{1,3})$")

# Nominal (integer) rates for NTSC-family fractional rates.
_NOMINAL_RATES = {23.976: 24, 23.98: 24, 29.97: 30, 59.94: 60, 47.952: 48, 119.88: 120}


def nominal_rate(fps: float) -> int:
    """Integer frame-numbering rate for a (possibly fractional) frame rate."""
    for known, nominal in _NOMINAL_RATES.items():
        if abs(fps - known) < 0.005:
            return nominal
    return round(fps)


def is_drop_frame_rate(fps: float) -> bool:
    return any(abs(fps - r) < 0.005 for r in (29.97, 59.94))


def parse_timecode(tc: str, fps: float) -> int:
    """Parse ``HH:MM:SS:FF`` (or ``;FF`` drop-frame) into a frame count.

    Drop-frame is honored for 29.97/59.94 when the frame separator is ``;``
    or ``,``. Raises ValueError on malformed input or out-of-range fields.
    """
    m = _TC_RE.match(tc.strip())
    if not m:
        raise ValueError(f"invalid timecode: {tc!r}")
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    sep, ff = m.group(4), int(m.group(5))
    rate = nominal_rate(fps)
    if mm > 59 or ss > 59 or ff >= rate:
        raise ValueError(f"timecode field out of range for {fps} fps: {tc!r}")
    drop = sep in (";", ",") and is_drop_frame_rate(fps)
    frames = ((hh * 60 + mm) * 60 + ss) * rate + ff
    if drop:
        drop_per_min = 2 if rate == 30 else 4
        total_minutes = hh * 60 + mm
        frames -= drop_per_min * (total_minutes - total_minutes // 10)
    return frames


def format_timecode(frames: int, fps: float, drop_frame: bool | None = None) -> str:
    """Format a frame count as ``HH:MM:SS:FF`` (``;FF`` when drop-frame)."""
    if frames < 0:
        raise ValueError("negative frame count")
    rate = nominal_rate(fps)
    if drop_frame is None:
        drop_frame = is_drop_frame_rate(fps)
    if drop_frame and is_drop_frame_rate(fps):
        drop_per_min = 2 if rate == 30 else 4
        frames_per_min = rate * 60 - drop_per_min
        frames_per_10min = frames_per_min * 10 + drop_per_min
        tens, rem = divmod(frames, frames_per_10min)
        if rem < rate * 60:
            extra_minutes = 0
        else:
            extra_minutes = 1 + (rem - rate * 60) // frames_per_min
        frames += drop_per_min * (tens * 9 + extra_minutes)
        sep = ";"
    else:
        sep = ":"
    ff = frames % rate
    total_seconds = frames // rate
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


def frames_to_seconds(frames: int, fps: float) -> float:
    return frames / fps


def seconds_to_frames(seconds: float, fps: float) -> int:
    return round(seconds * fps)


# --- Timeline ---------------------------------------------------------------

VIDEO = "video"
AUDIO = "audio"


@dataclass
class Clip:
    """One event on a timeline: a source range placed at a record range."""

    name: str
    track: str = "V1"
    kind: str = VIDEO
    source_in: int = 0
    source_out: int = 0
    record_in: int = 0
    record_out: int = 0
    source_name: str = ""
    source_file: str = ""  # absolute path to the media file, for relinking on export
    metadata: dict = field(default_factory=dict)

    @property
    def duration(self) -> int:
        return self.record_out - self.record_in

    def overlaps(self, other: "Clip") -> bool:
        return (
            self.track == other.track
            and self.record_in < other.record_out
            and other.record_in < self.record_out
        )


@dataclass
class Marker:
    frame: int
    name: str = ""
    note: str = ""
    color: str = ""


@dataclass
class Timeline:
    name: str
    fps: float
    clips: list[Clip] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    width: int = 1920  # canvas in pixels; 1080x1920 = vertical, 1920x804 = 2.39:1
    height: int = 1080

    @property
    def duration(self) -> int:
        return max((c.record_out for c in self.clips), default=0)

    @property
    def duration_seconds(self) -> float:
        return frames_to_seconds(self.duration, self.fps)

    def video_clips(self) -> list[Clip]:
        return sorted(
            (c for c in self.clips if c.kind == VIDEO), key=lambda c: c.record_in
        )

    def audio_clips(self) -> list[Clip]:
        return sorted(
            (c for c in self.clips if c.kind == AUDIO), key=lambda c: c.record_in
        )

    def tracks(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.clips:
            seen.setdefault(c.track, None)
        return list(seen)

    def track_clips(self, track: str) -> list[Clip]:
        return sorted(
            (c for c in self.clips if c.track == track), key=lambda c: c.record_in
        )

    def cuts(self, track: str | None = None) -> list[int]:
        """Record-frame positions where one clip ends and another begins."""
        clips = self.track_clips(track) if track else self.video_clips()
        cut_frames: list[int] = []
        for prev, nxt in zip(clips, clips[1:]):
            if nxt.record_in <= prev.record_out:
                cut_frames.append(nxt.record_in)
        return cut_frames

    def __iter__(self) -> Iterator[Clip]:
        return iter(sorted(self.clips, key=lambda c: (c.track, c.record_in)))


# --- Transcript -------------------------------------------------------------


@dataclass
class TranscriptSegment:
    """One utterance in a transcript, timed in seconds relative to its source."""

    index: int
    start: float
    end: float
    text: str
    speaker: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Transcript:
    segments: list[TranscriptSegment] = field(default_factory=list)
    source_name: str = ""
    language: str = ""

    @property
    def duration(self) -> float:
        return max((s.end for s in self.segments), default=0.0)

    def text(self) -> str:
        return "\n".join(s.text for s in self.segments)
