"""File I/O for Monteur: EDL, FCPXML, SRT and Whisper JSON.

``load_timeline`` / ``save_timeline`` / ``load_transcript`` dispatch on
file extension; the per-format ``read_*`` / ``write_*`` functions are
re-exported for direct use on strings.
"""

from __future__ import annotations

from pathlib import Path

from monteur.io.edl import read_edl, write_edl
from monteur.io.fcpxml import read_fcpxml, write_fcpxml
from monteur.io.srt import read_srt, write_srt
from monteur.io.whisperjson import read_whisper_json
from monteur.model import Timeline, Transcript

__all__ = [
    "load_timeline",
    "load_transcript",
    "save_timeline",
    "read_edl",
    "write_edl",
    "read_fcpxml",
    "write_fcpxml",
    "read_srt",
    "write_srt",
    "read_whisper_json",
]

_TIMELINE_EXTS = (".edl", ".xml", ".fcpxml", ".fcpxmld")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None


def load_timeline(path: str | Path, fps: float | None = None) -> Timeline:
    """Load a timeline from ``path``, dispatching on its extension.

    ``.edl`` requires ``fps`` (EDLs carry no frame rate); ``.xml`` /
    ``.fcpxml`` / ``.fcpxmld`` (bundle directory) are read as FCPXML.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".edl":
        if fps is None:
            raise ValueError(
                f"EDL files carry no frame rate; pass fps explicitly, e.g. "
                f"load_timeline({str(p)!r}, fps=25)"
            )
        return read_edl(_read_text(p), fps, name=p.stem)
    if ext in (".xml", ".fcpxml", ".fcpxmld"):
        if p.is_dir():
            inner = p / "Info.fcpxml"
            if not inner.exists():
                raise ValueError(f"FCPXML bundle {p} contains no Info.fcpxml")
            p = inner
        return read_fcpxml(_read_text(p))
    raise ValueError(
        f"unsupported timeline extension {ext!r} for {p} "
        f"(supported: {', '.join(_TIMELINE_EXTS)})"
    )


def load_transcript(path: str | Path) -> Transcript:
    """Load a transcript from a ``.srt`` or Whisper-style ``.json`` file."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".srt":
        transcript = read_srt(_read_text(p))
    elif ext == ".json":
        transcript = read_whisper_json(_read_text(p))
    else:
        raise ValueError(
            f"unsupported transcript extension {ext!r} for {p} "
            f"(supported: .srt, .json)"
        )
    if not transcript.source_name:
        transcript.source_name = p.stem
    return transcript


def save_timeline(timeline: Timeline, path: str | Path) -> None:
    """Write ``timeline`` to ``path`` as EDL or FCPXML, by extension."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".edl":
        text = write_edl(timeline)
    elif ext in (".xml", ".fcpxml"):
        text = write_fcpxml(timeline)
    else:
        raise ValueError(
            f"unsupported timeline extension {ext!r} for {p} "
            f"(supported: .edl, .xml, .fcpxml)"
        )
    p.write_text(text, encoding="utf-8")
