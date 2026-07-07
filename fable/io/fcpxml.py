"""FCPXML 1.x reading and writing (the dialect DaVinci Resolve exchanges).

Supported subset
----------------
Reading (:func:`read_fcpxml`):

* ``<resources>``: ``<format>`` elements supply the frame rate via
  ``frameDuration`` (rational, e.g. ``"1001/24000s"``); ``<asset>``
  elements supply clip names, ``src`` paths and ``hasVideo``/``hasAudio``
  flags.
* The first ``<sequence>`` (inside project/event/library, wherever it
  lives) is read; its ``<spine>`` children are walked in order:
  ``<asset-clip>``, ``<clip>``, ``<video>``, ``<audio>`` and ``<gap>``.
* Rational time attributes (``offset``, ``duration``, ``start``) are
  parsed exactly with :mod:`fractions` and converted to frames at the
  sequence frame rate.
* An asset-clip whose asset has both video and audio yields a video clip
  on V1 plus an audio clip on A1 covering the same ranges.
* Connected clips (children carrying a ``lane`` attribute, inside spine
  clips or gaps) are handled one level deep: positive lanes land on
  V2/V3/..., negative lanes on A1/A2/... as audio.

Writing (:func:`write_fcpxml`):

* Emits a minimal valid FCPXML 1.9 document: one ``<format>``, one
  ``<asset>`` per distinct ``source_name`` (falling back to the clip
  name), and a library/event/project/sequence/spine of ``<asset-clip>``
  elements with ``<gap>`` filler between non-adjacent clips.
* A video clip and an audio clip sharing the same source and record
  range are merged into a single asset-clip whose asset advertises audio
  channels.

Limitations
-----------
* Only the first sequence of a document is read; markers, effects,
  retimes, audition/mc-clip containers and keywords are ignored.
* Connected clips nested more than one level deep are ignored.
* ``write_fcpxml`` assumes non-overlapping video clips (they are laid
  out in one spine); audio clips that do not pair with a video clip of
  the same source and record range are skipped on write.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

from fable.model import AUDIO, VIDEO, Clip, Timeline

_NTSC_FRAME_DURATIONS = {
    24: Fraction(1001, 24000),
    30: Fraction(1001, 30000),
    48: Fraction(1001, 48000),
    60: Fraction(1001, 60000),
    120: Fraction(1001, 120000),
}


def _parse_rational(value: str, what: str = "time") -> Fraction:
    v = value.strip()
    if not v.endswith("s"):
        raise ValueError(
            f"invalid FCPXML {what} value {value!r}: expected rational seconds "
            f"like '3600/25s' or '5s'"
        )
    v = v[:-1]
    try:
        if "/" in v:
            num, den = v.split("/", 1)
            return Fraction(int(num), int(den))
        return Fraction(v)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"invalid FCPXML {what} value {value!r}") from None


def _time_attr(elem: ET.Element, attr: str, default: str = "0s") -> Fraction:
    return _parse_rational(elem.get(attr, default), what=attr)


def _to_frames(t: Fraction, frame_dur: Fraction) -> int:
    return round(t / frame_dur)


def _lane_track(lane: int, kind: str) -> str:
    if kind == AUDIO:
        return f"A{max(1, -lane if lane < 0 else lane + 1)}" if lane else "A1"
    return f"V{lane + 1}" if lane > 0 else "V1"


class _Reader:
    def __init__(self, root: ET.Element) -> None:
        self.root = root
        self.formats: dict[str, Fraction] = {}
        self.assets: dict[str, dict] = {}
        for fmt in root.iter("format"):
            fid = fmt.get("id")
            fd = fmt.get("frameDuration")
            if fid and fd:
                self.formats[fid] = _parse_rational(fd, what="frameDuration")
        for asset in root.iter("asset"):
            aid = asset.get("id")
            if not aid:
                continue
            media_rep = asset.find("media-rep")
            self.assets[aid] = {
                "name": asset.get("name", ""),
                "src": asset.get("src")
                or (media_rep.get("src", "") if media_rep is not None else ""),
                "has_video": asset.get("hasVideo") == "1",
                "has_audio": asset.get("hasAudio") == "1",
            }

    def read(self) -> Timeline:
        sequence = self.root.find(".//sequence")
        if sequence is None:
            raise ValueError("no <sequence> element found in FCPXML document")
        fmt_id = sequence.get("format", "")
        frame_dur = self.formats.get(fmt_id)
        if frame_dur is None:
            if len(self.formats) == 1:
                frame_dur = next(iter(self.formats.values()))
            else:
                raise ValueError(
                    f"sequence references format {fmt_id!r} but no matching "
                    f"<format> with a frameDuration was found in <resources>"
                )
        fps = float(1 / frame_dur)
        project = self.root.find(".//project")
        name = (project.get("name", "") if project is not None else "") or ""
        timeline = Timeline(name=name, fps=fps)
        spine = sequence.find("spine")
        if spine is None:
            raise ValueError("sequence has no <spine> element")
        for child in spine:
            self._walk(child, timeline, frame_dur, parent_record=None, depth=0)
        return timeline

    def _walk(
        self,
        elem: ET.Element,
        timeline: Timeline,
        frame_dur: Fraction,
        parent_record: tuple[Fraction, Fraction] | None,
        depth: int,
    ) -> None:
        tag = elem.tag
        if tag not in ("asset-clip", "clip", "video", "audio", "gap", "ref-clip"):
            return
        offset = _time_attr(elem, "offset")
        duration = _time_attr(elem, "duration")
        start = _time_attr(elem, "start")
        lane = int(elem.get("lane", "0"))
        if parent_record is None:
            record_start = offset
        else:
            p_offset, p_start = parent_record
            record_start = p_offset + (offset - p_start)

        if tag != "gap":
            self._emit(elem, timeline, frame_dur, record_start, duration, start, lane)
        if depth < 1:
            for child in elem:
                if child.get("lane") is not None:
                    self._walk(
                        child,
                        timeline,
                        frame_dur,
                        parent_record=(record_start, start),
                        depth=depth + 1,
                    )

    def _emit(
        self,
        elem: ET.Element,
        timeline: Timeline,
        frame_dur: Fraction,
        record_start: Fraction,
        duration: Fraction,
        start: Fraction,
        lane: int,
    ) -> None:
        ref = elem.get("ref", "")
        asset = self.assets.get(ref, {})
        name = elem.get("name") or asset.get("name", "") or elem.tag
        source_name = asset.get("name", "") or name
        rec_in = _to_frames(record_start, frame_dur)
        rec_out = _to_frames(record_start + duration, frame_dur)
        src_in = _to_frames(start, frame_dur)
        src_out = src_in + (rec_out - rec_in)
        metadata: dict = {}
        if asset.get("src"):
            metadata["src"] = asset["src"]

        kinds: list[str]
        if elem.tag == "audio":
            kinds = [AUDIO]
        elif elem.tag in ("video", "clip", "ref-clip"):
            kinds = [VIDEO]
        else:
            has_video = asset.get("has_video", not asset)
            has_audio = asset.get("has_audio", False)
            if not has_video and not has_audio:
                has_video = True
            kinds = ([VIDEO] if has_video else []) + ([AUDIO] if has_audio else [])
            if lane < 0:
                kinds = [AUDIO]
        for kind in kinds:
            timeline.clips.append(
                Clip(
                    name=name,
                    track=_lane_track(lane, kind),
                    kind=kind,
                    source_in=src_in,
                    source_out=src_out,
                    record_in=rec_in,
                    record_out=rec_out,
                    source_name=source_name,
                    metadata=dict(metadata),
                )
            )


def read_fcpxml(text: str) -> Timeline:
    """Parse an FCPXML document into a :class:`Timeline`.

    Raises ValueError on XML that cannot be parsed or lacks the required
    sequence/format structure.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"not well-formed FCPXML: {exc}") from None
    if root.tag != "fcpxml":
        raise ValueError(
            f"root element is <{root.tag}>, expected <fcpxml> — is this an "
            f"FCPXML document?"
        )
    return _Reader(root).read()


def _frame_duration_for(fps: float) -> Fraction:
    for nominal, frac in _NTSC_FRAME_DURATIONS.items():
        if abs(fps - float(1 / frac)) < 0.005:
            return frac
    if abs(fps - round(fps)) < 1e-9:
        return Fraction(1, round(fps))
    return 1 / Fraction(fps).limit_denominator(100000)


def _fmt_time(frames: int, frame_dur: Fraction) -> str:
    t = frames * frame_dur
    if t.denominator == 1:
        return f"{t.numerator}s"
    return f"{t.numerator}/{t.denominator}s"


def write_fcpxml(timeline: Timeline, name: str = "") -> str:
    """Serialize ``timeline`` as a minimal FCPXML 1.9 document string.

    Video clips are laid out in the spine in record order (they must not
    overlap); an audio clip with the same source and record range as a
    video clip is folded into that clip's asset-clip via the asset's
    audio channels. Audio clips without such a video partner are not
    representable by this writer and are skipped.
    """
    if timeline.fps <= 0:
        raise ValueError(f"timeline fps must be positive, got {timeline.fps}")
    frame_dur = _frame_duration_for(timeline.fps)
    title = name or timeline.name or "Timeline"

    video = sorted(
        (c for c in timeline.clips if c.kind == VIDEO), key=lambda c: c.record_in
    )
    audio = [c for c in timeline.clips if c.kind == AUDIO]

    def source_key(clip: Clip) -> str:
        return clip.source_name or clip.name or "Untitled"

    paired_audio: dict[int, Clip] = {}
    used: set[int] = set()
    for vclip in video:
        for aclip in audio:
            if (
                id(aclip) not in used
                and source_key(aclip) == source_key(vclip)
                and aclip.record_in == vclip.record_in
                and aclip.record_out == vclip.record_out
            ):
                paired_audio[id(vclip)] = aclip
                used.add(id(aclip))
                break

    asset_ids: dict[str, str] = {}
    asset_audio: dict[str, bool] = {}
    for clip in video:
        key = source_key(clip)
        asset_ids.setdefault(key, f"r{len(asset_ids) + 2}")
        if id(clip) in paired_audio:
            asset_audio[key] = True

    root = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        id="r1",
        name=f"FFVideoFormat1080p{round(timeline.fps * 100) / 100:g}".replace(".", ""),
        frameDuration=_fmt_time(1, frame_dur),
        width="1920",
        height="1080",
    )
    total = _fmt_time(timeline.duration, frame_dur)
    for key, rid in asset_ids.items():
        attrs = {
            "id": rid,
            "name": key,
            "start": "0s",
            "duration": total,
            "hasVideo": "1",
            "format": "r1",
        }
        if asset_audio.get(key):
            attrs.update(hasAudio="1", audioSources="1", audioChannels="2")
        ET.SubElement(resources, "asset", attrs)

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name=title)
    project = ET.SubElement(event, "project", name=title)
    sequence = ET.SubElement(
        project,
        "sequence",
        format="r1",
        duration=total,
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate="48k",
    )
    spine = ET.SubElement(sequence, "spine")

    playhead = 0
    for clip in video:
        if clip.record_in < playhead:
            raise ValueError(
                f"video clips overlap at frame {clip.record_in} "
                f"({clip.name!r}); write_fcpxml requires a flat, "
                f"non-overlapping video track"
            )
        if clip.record_in > playhead:
            ET.SubElement(
                spine,
                "gap",
                name="Gap",
                offset=_fmt_time(playhead, frame_dur),
                duration=_fmt_time(clip.record_in - playhead, frame_dur),
                start="0s",
            )
        ET.SubElement(
            spine,
            "asset-clip",
            ref=asset_ids[source_key(clip)],
            name=clip.name or source_key(clip),
            offset=_fmt_time(clip.record_in, frame_dur),
            duration=_fmt_time(clip.duration, frame_dur),
            start=_fmt_time(clip.source_in, frame_dur),
            format="r1",
            tcFormat="NDF",
        )
        playhead = clip.record_out

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n'
