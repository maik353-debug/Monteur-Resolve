"""Transcription helper: drive a local Whisper install and return Transcripts.

Monteur does not bundle a speech model. Instead it shells out to whichever
Whisper implementation is installed:

* **openai-whisper** — the ``whisper`` CLI (``pip install openai-whisper``).
* **whisper.cpp** — a compiled binary (usually named ``whisper-cli``,
  ``whisper-cpp`` or ``main``). Because those names are generic, this
  backend is only used when the ``WHISPER_CPP`` environment variable
  points at the binary, and ``WHISPER_CPP_MODEL`` points at a ggml/gguf
  model file. whisper.cpp only accepts 16 kHz mono WAV input; Monteur
  converts other media automatically when ``ffmpeg`` is on PATH.

All subprocess execution goes through an injectable ``runner`` callable
(defaulting to :func:`subprocess.run`) so the module is testable without
any real Whisper install.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from monteur.io.srt import read_srt
from monteur.io.whisperjson import read_whisper_json
from monteur.model import Transcript
from monteur.procio import NO_WINDOW

__all__ = [
    "MonteurTranscribeError",
    "Backend",
    "find_backend",
    "transcribe_file",
    "transcribe_directory",
    "scene_take_from_name",
    "MEDIA_EXTENSIONS",
]

#: Media file extensions considered by :func:`transcribe_directory`.
MEDIA_EXTENSIONS = frozenset(
    {".mov", ".mp4", ".mxf", ".mkv", ".avi", ".wav", ".mp3", ".m4a", ".aif", ".aiff", ".braw"}
)

_INSTALL_HELP = (
    "No transcription backend found. Install one of:\n"
    "  * openai-whisper:  pip install openai-whisper   (provides the 'whisper' CLI)\n"
    "  * whisper.cpp:     build it (https://github.com/ggml-org/whisper.cpp), then set\n"
    "        WHISPER_CPP=/path/to/whisper-cli    (the binary; also shipped as\n"
    "                                             'whisper-cpp' or 'main')\n"
    "        WHISPER_CPP_MODEL=/path/to/ggml-small.bin\n"
    "    Note: whisper.cpp needs 16 kHz mono WAV input; Monteur converts other\n"
    "    media automatically when ffmpeg is on PATH."
)


def _default_run(*args, **kwargs):
    """Default :func:`subprocess.run` with the Windows console-window flash
    suppressed (no-op off Windows)."""
    return subprocess.run(*args, **kwargs, **NO_WINDOW)


class MonteurTranscribeError(RuntimeError):
    """Raised when no backend is available or a transcription run fails."""


# A runner has the shape of subprocess.run(cmd, capture_output=True, text=True).
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _excerpt(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "... " + text[-limit:]


# --- Backends ----------------------------------------------------------------


@dataclass(frozen=True)
class Backend:
    """A transcription tool Monteur knows how to drive.

    ``build_command(executable, input_path, output_dir, model, language)``
    returns the argv to run. The tool must leave a Whisper-style JSON file
    at ``output_dir / (input_path.stem + ".json")``.
    """

    name: str
    executable: str
    build_command: Callable[[str, Path, Path, str, "str | None"], list]

    def command(
        self, input_path: Path, output_dir: Path, model: str, language: str | None
    ) -> list:
        return self.build_command(self.executable, input_path, output_dir, model, language)


def _openai_whisper_command(
    executable: str, input_path: Path, output_dir: Path, model: str, language: str | None
) -> list:
    cmd = [
        executable,
        str(input_path),
        "--output_format",
        "json",
        "--output_dir",
        str(output_dir),
    ]
    if model:
        cmd += ["--model", model]
    if language:
        cmd += ["--language", language]
    return cmd


def _whisper_cpp_command(
    executable: str, input_path: Path, output_dir: Path, model: str, language: str | None
) -> list:
    # `model` (a Whisper size name like "small") is ignored here: whisper.cpp
    # loads the model file named by WHISPER_CPP_MODEL.
    model_path = os.environ.get("WHISPER_CPP_MODEL", "")
    if not model_path:
        raise MonteurTranscribeError(
            "whisper.cpp backend needs WHISPER_CPP_MODEL set to a ggml/gguf "
            "model file (e.g. WHISPER_CPP_MODEL=~/models/ggml-small.bin)."
        )
    cmd = [
        executable,
        "-m",
        model_path,
        "-f",
        str(input_path),
        "-oj",
        "-of",
        str(output_dir / input_path.stem),
    ]
    if language:
        cmd += ["-l", language]
    return cmd


def find_backend() -> Backend:
    """Locate an installed Whisper backend.

    Probe order:

    1. ``whisper`` on PATH (openai-whisper CLI).
    2. A whisper.cpp binary (``whisper-cli`` / ``whisper-cpp`` / ``main``),
       but only when the ``WHISPER_CPP`` environment variable names it —
       either as an absolute path or a command name resolvable on PATH.
       Requires ``WHISPER_CPP_MODEL`` as well.

    Raises :class:`MonteurTranscribeError` with install instructions when
    nothing usable is found.
    """
    exe = shutil.which("whisper")
    if exe:
        return Backend("whisper", exe, _openai_whisper_command)

    cpp = os.environ.get("WHISPER_CPP", "").strip()
    if cpp:
        exe = shutil.which(cpp) or shutil.which(os.path.expanduser(cpp))
        if not exe:
            raise MonteurTranscribeError(
                f"WHISPER_CPP is set to {cpp!r} but no executable was found "
                f"there (or on PATH under that name). Point WHISPER_CPP at "
                f"the whisper.cpp binary (usually 'whisper-cli', "
                f"'whisper-cpp' or 'main')."
            )
        if not os.environ.get("WHISPER_CPP_MODEL"):
            raise MonteurTranscribeError(
                "WHISPER_CPP is set but WHISPER_CPP_MODEL is not. Set it to "
                "a ggml/gguf model file, e.g. "
                "WHISPER_CPP_MODEL=~/models/ggml-small.bin. Reminder: "
                "whisper.cpp needs 16 kHz mono WAV input; Monteur converts "
                "with ffmpeg when it is on PATH."
            )
        return Backend("whisper.cpp", exe, _whisper_cpp_command)

    raise MonteurTranscribeError(_INSTALL_HELP)


# --- Running -----------------------------------------------------------------


def _prepare_input_for_cpp(media: Path, workdir: Path, runner: Runner) -> Path:
    """Convert ``media`` to 16 kHz mono WAV for whisper.cpp, via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        if media.suffix.lower() == ".wav":
            # Pass the WAV through and hope it is already 16 kHz mono.
            return media
        raise MonteurTranscribeError(
            f"whisper.cpp needs 16 kHz mono WAV input, but {media.name!r} is "
            f"not a WAV and ffmpeg was not found on PATH. Install ffmpeg (so "
            f"Monteur can convert automatically) or convert manually:\n"
            f"  ffmpeg -i {media.name} -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
        )
    wav = workdir / (media.stem + "-16k.wav")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(media),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(wav),
    ]
    proc = runner(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MonteurTranscribeError(
            f"ffmpeg failed converting {media.name!r} to 16 kHz WAV for "
            f"whisper.cpp (exit {proc.returncode}): {_excerpt(proc.stderr)}"
        )
    return wav


def _parse_whisper_output(text: str, source_name: str = "") -> Transcript:
    """Parse tool output: openai-whisper JSON, or whisper.cpp ``-oj`` JSON.

    whisper.cpp emits ``{"transcription": [{"offsets": {"from": ms, "to": ms},
    "text": ...}], "result": {"language": ...}}``; that shape is converted to
    the openai-whisper ``segments`` shape and fed through
    :func:`monteur.io.whisperjson.read_whisper_json` (which handles everything
    else).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "segments" not in data and "transcription" in data:
        segments = []
        for entry in data.get("transcription") or []:
            offsets = entry.get("offsets") or {}
            segments.append(
                {
                    "start": float(offsets.get("from", 0)) / 1000.0,
                    "end": float(offsets.get("to", 0)) / 1000.0,
                    "text": entry.get("text", ""),
                }
            )
        language = str((data.get("result") or {}).get("language", "") or "")
        text = json.dumps({"segments": segments, "language": language})
    return read_whisper_json(text, source_name=source_name)


def transcribe_file(
    path: "str | Path",
    model: str = "small",
    language: str | None = None,
    runner: Runner | None = None,
    backend: Backend | None = None,
) -> Transcript:
    """Transcribe one media file with the best available Whisper backend.

    ``runner`` (default :func:`subprocess.run`) executes the tool; inject a
    fake in tests. Raises :class:`MonteurTranscribeError` when no backend is
    installed, the tool exits nonzero (message carries a stderr excerpt), or
    no parseable JSON output appears.
    """
    run = runner if runner is not None else _default_run
    media = Path(path)
    if not media.is_file():
        raise MonteurTranscribeError(f"media file not found: {media}")
    if backend is None:
        backend = find_backend()

    with tempfile.TemporaryDirectory(prefix="monteur-transcribe-") as tmp:
        workdir = Path(tmp)
        input_path = media
        if backend.name == "whisper.cpp":
            input_path = _prepare_input_for_cpp(media, workdir, run)
        cmd = backend.command(input_path, workdir, model, language)
        proc = run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise MonteurTranscribeError(
                f"{backend.name} failed on {media.name!r} "
                f"(exit {proc.returncode}): {_excerpt(proc.stderr)}"
            )
        json_path = workdir / (input_path.stem + ".json")
        if not json_path.is_file():
            raise MonteurTranscribeError(
                f"{backend.name} exited 0 but produced no JSON at "
                f"{json_path.name!r}; stderr: {_excerpt(proc.stderr)}"
            )
        try:
            transcript = _parse_whisper_output(
                json_path.read_text(encoding="utf-8"), source_name=media.name
            )
        except ValueError as exc:
            raise MonteurTranscribeError(
                f"{backend.name} output for {media.name!r} is not valid "
                f"Whisper JSON: {exc}"
            ) from None
    transcript.source_name = media.name
    return transcript


def transcribe_directory(
    path: "str | Path", pattern: str = "*", **kw
) -> "dict[str, Transcript]":
    """Transcribe every media file in ``path`` matching ``pattern``.

    Media files are recognized by extension (:data:`MEDIA_EXTENSIONS`,
    case-insensitive) and processed in sorted order. A file with a sibling
    ``.srt`` or ``.json`` transcript is skipped (announced via ``print``) and
    the existing transcript is loaded instead. Remaining keyword arguments
    (``model``, ``language``, ``runner``) go to :func:`transcribe_file`.

    Returns ``{str(media_path): Transcript}``.
    """
    root = Path(path)
    if not root.is_dir():
        raise MonteurTranscribeError(f"not a directory: {root}")
    media_files = sorted(
        p
        for p in root.glob(pattern)
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
    )
    results: dict[str, Transcript] = {}
    for media in media_files:
        sidecar_json = media.with_suffix(".json")
        sidecar_srt = media.with_suffix(".srt")
        if sidecar_json.is_file():
            print(
                f"monteur transcribe: skipping {media.name} "
                f"(existing transcript {sidecar_json.name})"
            )
            transcript = _parse_whisper_output(
                sidecar_json.read_text(encoding="utf-8"), source_name=media.name
            )
        elif sidecar_srt.is_file():
            print(
                f"monteur transcribe: skipping {media.name} "
                f"(existing transcript {sidecar_srt.name})"
            )
            transcript = read_srt(
                sidecar_srt.read_text(encoding="utf-8"), source_name=media.name
            )
        else:
            transcript = transcribe_file(media, **kw)
        results[str(media)] = transcript
    return results


# --- Scene/take name parsing --------------------------------------------------

_MARKED_SCENE_TAKE_RE = re.compile(
    r"(?:^|[^a-z0-9])"  # marker must not ride on the tail of another word
    r"(?:scene|sc|s)[ ._-]?(\d{1,4})([a-z]?)"  # scene number + optional letter
    r"[ ._-]*"
    r"(?:take|tk|t)[ ._-]?(\d{1,4})"  # take number
    r"(?![0-9])",
    re.IGNORECASE,
)

_BARE_SCENE_TAKE_RE = re.compile(
    r"^(\d{1,4})([a-z]?)\s*[-_]\s*(\d{1,4})$", re.IGNORECASE
)


def scene_take_from_name(filename: str) -> "tuple[str, str]":
    """Extract ``(scene, take)`` hints from a clip filename.

    Accepted patterns (case-insensitive; the extension is ignored):

    * ``S12_T03``, ``s12-t3``, ``S12.T3`` — scene/take markers ``S`` and
      ``T`` with optional ``space . _ -`` separators.
    * ``SC12_TK3`` — ``SC``/``TK`` marker spellings.
    * ``Scene12_Take3``, ``scene 12 take 3`` — full-word markers.
    * ``S12aT2`` — no separator; the scene may carry a letter suffix
      (``12a`` is returned as ``"12A"``).
    * ``12-3`` / ``12_3`` — a bare ``scene-take`` stem (whole name, minus
      extension, must be just the two numbers; ``12A-3`` also works).

    Numbers are normalized (leading zeros stripped: ``T03`` → ``"3"``) and a
    scene letter suffix is uppercased, so ``sc012a_tk07`` → ``("12A", "7")``.
    Returns ``("", "")`` when no pattern matches (e.g. ``interview.mov``,
    ``IMG_1234.MOV``). Feeds the auto-assembly's scene routing.
    """
    stem = Path(str(filename)).stem
    m = _MARKED_SCENE_TAKE_RE.search(stem) or _BARE_SCENE_TAKE_RE.match(stem.strip())
    if not m:
        return ("", "")
    scene_num, scene_letter, take_num = m.group(1), m.group(2), m.group(3)
    return (f"{int(scene_num)}{scene_letter.upper()}", str(int(take_num)))
