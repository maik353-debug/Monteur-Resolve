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
  sequence frame rate. An element's ``start`` is expressed in its asset's
  native timescale, which begins at the asset's own ``start`` (the file's
  embedded start timecode): the asset ``start`` is subtracted so
  ``source_in``/``source_out`` are always file-relative (0-based); a
  non-zero asset start is kept in ``clip.metadata["media_start_seconds"]``.
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
* Each asset claims its REAL source range so Resolve can verify it
  against the media file and link the clips: asset ``start`` is the
  file's embedded start timecode (``clip.metadata["media_start_seconds"]``,
  0s when absent) and asset ``duration`` is the file's real length
  (``metadata["media_duration_seconds"]``, else the furthest source
  frame used — never the timeline's duration). Every asset-clip's
  ``start`` (source position) is shifted into the asset's timescale:
  ``media_start_seconds + source_in``. Clip ``source_in``/``source_out``
  stay file-relative in the model; the shift happens only here.
* A video clip and an audio clip sharing the same source and record
  range are merged into a single asset-clip whose asset advertises audio
  channels.
* Dissolves: a video clip whose ``metadata["transition"]`` is
  ``"dissolve"`` (or ``"D"``) with a positive ``"transition_frames"``
  (or ``"transition_duration"``) gets a ``<transition name="Cross
  Dissolve" offset="..." duration="...">`` element in the spine directly
  before its asset-clip (offset = the cut point, both in rational time).
  DaVinci Resolve imports spine transitions; :func:`read_fcpxml` ignores
  the element, leaving the clips intact.
* Fades: ``timeline.metadata["fade_in_frames"]`` /
  ``["fade_out_frames"]`` are NOT written — FCPXML audio fades would
  need ``<param>``/``adjust-volume`` elements this writer does not
  emit. The plan notes the fade so the editor applies the music fade in
  Resolve.

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

import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction

from monteur.model import AUDIO, VIDEO, Clip, Timeline


def _file_uri(path: str) -> str:
    """``file://`` URI for a media path, correct for Windows OR POSIX.

    Formatted explicitly (not via ``Path.as_uri()``) so it produces the right
    Windows URI (``file:///C:/dir/clip.mp4``) even when Monteur is generating
    the FCPXML on a different OS, and percent-encodes spaces and other unsafe
    characters. Resolve links its media against this.
    """
    # Windows drive path, e.g. C:\dir\clip.mp4 or C:/dir/clip.mp4
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return "file:///" + urllib.parse.quote(path.replace("\\", "/"), safe="/:")
    # UNC share, e.g. \\server\share\clip.mp4
    if path.startswith("\\\\") or path.startswith("//"):
        unc = path.replace("\\", "/").lstrip("/")
        return "file://" + urllib.parse.quote(unc, safe="/:")
    # POSIX absolute, or best-effort for anything else
    return "file://" + urllib.parse.quote(path.replace("\\", "/"), safe="/:")

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
                # The file's embedded start timecode: element `start` values
                # are expressed in this timescale and must be shifted back to
                # file-relative positions when reading.
                "start": _time_attr(asset, "start"),
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
        # `start` is in the asset's timescale, which begins at the asset's
        # `start` (the file's embedded start timecode). Subtracting it keeps
        # source_in/source_out file-relative (0-based), so write -> read
        # roundtrips preserve source ranges exactly.
        asset_start = asset.get("start", Fraction(0))
        src_in = _to_frames(start - asset_start, frame_dur)
        src_out = src_in + (rec_out - rec_in)
        metadata: dict = {}
        if asset.get("src"):
            metadata["src"] = asset["src"]
        if asset_start > 0:
            metadata["media_start_seconds"] = float(asset_start)

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


def _media_seconds(clip: Clip, key: str) -> float:
    """A positive ``media_*_seconds`` value from clip metadata, else 0.0."""
    try:
        value = float(clip.metadata.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def write_fcpxml(timeline: Timeline, name: str = "") -> str:
    """Serialize ``timeline`` as a minimal FCPXML 1.9 document string.

    Video clips are laid out in the spine in record order (they must not
    overlap); an audio clip with the same source and record range as a
    video clip is folded into that clip's asset-clip via the asset's
    audio channels. Audio clips without such a video partner are not
    representable by this writer and are skipped.

    A video clip with dissolve metadata gets a ``<transition
    name="Cross Dissolve">`` element in the spine before its asset-clip
    (see the module docstring). Timeline fade metadata is not emitted —
    apply the music fade in Resolve.
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

    def seconds_frames(seconds: float) -> int:
        return round(seconds / frame_dur)

    asset_ids: dict[str, str] = {}
    asset_audio: dict[str, bool] = {}
    asset_src: dict[str, str] = {}
    asset_start: dict[str, int] = {}  # frames: the file's embedded start TC
    asset_dur: dict[str, int] = {}  # frames: the file's real duration, when known
    asset_max_out: dict[str, int] = {}  # frames: furthest source frame used (fallback)
    for clip in video:
        key = source_key(clip)
        asset_ids.setdefault(key, f"r{len(asset_ids) + 2}")
        if id(clip) in paired_audio:
            asset_audio[key] = True
        # Carry the real media path so Resolve can link the clip; without it
        # the import produces an empty timeline (offline media is dropped).
        path = clip.source_file or clip.metadata.get("src", "")
        if path and key not in asset_src:
            asset_src[key] = _file_uri(path)
        # Real source metadata: the asset's claimed [start, start+duration]
        # must match the actual file's timecode range, or Resolve reports
        # "Mismatch between specified target timecodes and located file
        # timecodes" / "No overlap" and drops the clips.
        start_s = _media_seconds(clip, "media_start_seconds")
        if start_s > 0 and key not in asset_start:
            asset_start[key] = seconds_frames(start_s)
        dur_s = _media_seconds(clip, "media_duration_seconds")
        if dur_s > 0 and key not in asset_dur:
            asset_dur[key] = seconds_frames(dur_s)
        asset_max_out[key] = max(asset_max_out.get(key, 0), clip.source_out)

    # Audio clips not folded into a video asset-clip (e.g. a montage's music
    # bed) become connected audio clips with their own asset, so the montage
    # actually carries its soundtrack instead of dropping it.
    connected_audio: list[tuple[Clip, str]] = []
    for aclip in audio:
        if id(aclip) in used:
            continue
        aid = f"r{len(asset_ids) + len(connected_audio) + 2}"
        connected_audio.append((aclip, aid))

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
        # start = the file's embedded start timecode (0s when unknown);
        # duration = the file's real length, else the furthest source frame
        # used as a safe lower bound — NEVER the timeline's duration, which
        # would claim source ranges the media does not have.
        attrs = {
            "id": rid,
            "name": key,
            "start": _fmt_time(asset_start.get(key, 0), frame_dur),
            "duration": _fmt_time(
                asset_dur.get(key, asset_max_out.get(key, 0)), frame_dur
            ),
            "hasVideo": "1",
            "format": "r1",
        }
        if asset_audio.get(key):
            attrs.update(hasAudio="1", audioSources="1", audioChannels="2")
        src = asset_src.get(key)
        if src:
            attrs["src"] = src  # older-style attribute (our own reader uses it)
        asset_el = ET.SubElement(resources, "asset", attrs)
        if src:
            # FCPXML 1.9 media reference — this is what Resolve links against.
            ET.SubElement(
                asset_el, "media-rep", kind="original-media", src=src
            )

    audio_start: dict[int, int] = {}  # id(aclip) -> asset start frames
    for aclip, aid in connected_audio:
        a_start = seconds_frames(_media_seconds(aclip, "media_start_seconds"))
        audio_start[id(aclip)] = a_start
        a_dur_s = _media_seconds(aclip, "media_duration_seconds")
        a_dur = seconds_frames(a_dur_s) if a_dur_s > 0 else aclip.source_out
        a_attrs = {
            "id": aid,
            "name": aclip.source_name or aclip.name or "Audio",
            "start": _fmt_time(a_start, frame_dur),
            "duration": _fmt_time(a_dur, frame_dur),
            "hasAudio": "1",
            "audioSources": "1",
            "audioChannels": "2",
        }
        a_src = _file_uri(aclip.source_file) if aclip.source_file else ""
        if a_src:
            a_attrs["src"] = a_src
        a_el = ET.SubElement(resources, "asset", a_attrs)
        if a_src:
            ET.SubElement(a_el, "media-rep", kind="original-media", src=a_src)

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
    first_clip_el: ET.Element | None = None
    first_clip_start = 0
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
        dissolve = _transition_frames(clip)
        if dissolve > 0 and clip.record_in > 0:
            # Cross dissolve INTO this clip, starting at the cut point.
            ET.SubElement(
                spine,
                "transition",
                name="Cross Dissolve",
                offset=_fmt_time(clip.record_in, frame_dur),
                duration=_fmt_time(dissolve, frame_dur),
            )
        # The source position is expressed in the asset's timescale, which
        # begins at the file's embedded start timecode: file TC + source_in.
        source_start = asset_start.get(source_key(clip), 0) + clip.source_in
        clip_el = ET.SubElement(
            spine,
            "asset-clip",
            ref=asset_ids[source_key(clip)],
            name=clip.name or source_key(clip),
            offset=_fmt_time(clip.record_in, frame_dur),
            duration=_fmt_time(clip.duration, frame_dur),
            start=_fmt_time(source_start, frame_dur),
            format="r1",
            tcFormat="NDF",
        )
        if first_clip_el is None:
            first_clip_el = clip_el
            first_clip_start = source_start
        playhead = clip.record_out

    # Attach unpaired audio (music bed) as a connected clip on the first video
    # clip. Its offset is expressed in the parent's local time, so offset ==
    # the parent's start places it at the montage's record 0 (this is exactly
    # what read_fcpxml reverses: record = p_offset + (offset - p_start)).
    for aclip, aid in connected_audio:
        attrs = {
            "ref": aid,
            "lane": "-1",
            "name": aclip.source_name or aclip.name or "Music",
            "duration": _fmt_time(aclip.duration, frame_dur),
            "start": _fmt_time(
                audio_start.get(id(aclip), 0) + aclip.source_in, frame_dur
            ),
            "audioRole": "music",
        }
        if first_clip_el is not None:
            attrs["offset"] = _fmt_time(first_clip_start + aclip.record_in, frame_dur)
            ET.SubElement(first_clip_el, "asset-clip", attrs)
        else:
            attrs["offset"] = _fmt_time(aclip.record_in, frame_dur)
            ET.SubElement(spine, "asset-clip", attrs)

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n'
