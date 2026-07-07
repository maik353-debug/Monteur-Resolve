"""OpenAI-Whisper-style JSON transcript reading.

Supported subset
----------------
* A top-level object with a ``"segments"`` list (the shape produced by
  ``whisper.transcribe`` / ``whisper --output_format json``), or a bare
  JSON list of segment objects.
* Each segment needs ``"start"``, ``"end"`` (numbers, seconds) and
  ``"text"``; optional ``"id"`` (used as the segment index) and
  ``"speaker"`` (as emitted by whisperX/diarization pipelines) are
  honored.
* A top-level ``"language"`` string is captured on the transcript.

Limitations
-----------
* Word-level timestamps (``"words"``), token/probability fields and
  everything else Whisper emits are ignored.
"""

from __future__ import annotations

import json

from fable.model import Transcript, TranscriptSegment


def read_whisper_json(text: str, source_name: str = "") -> Transcript:
    """Parse Whisper-style JSON ``text`` into a :class:`Transcript`.

    Raises ValueError on invalid JSON or segments missing required fields.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from None

    if isinstance(data, list):
        segments_raw = data
        language = ""
    elif isinstance(data, dict):
        segments_raw = data.get("segments")
        if not isinstance(segments_raw, list):
            raise ValueError(
                "expected Whisper JSON with a top-level 'segments' list "
                "(or a bare list of segments)"
            )
        language = str(data.get("language", "") or "")
    else:
        raise ValueError(
            f"expected a Whisper JSON object or list of segments, "
            f"got {type(data).__name__}"
        )

    transcript = Transcript(source_name=source_name, language=language)
    for i, raw in enumerate(segments_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"segment {i}: expected an object, got {raw!r}")
        try:
            start = float(raw["start"])
            end = float(raw["end"])
            seg_text = str(raw["text"])
        except KeyError as exc:
            raise ValueError(
                f"segment {i}: missing required field {exc.args[0]!r} "
                f"(need 'start', 'end', 'text')"
            ) from None
        except (TypeError, ValueError):
            raise ValueError(
                f"segment {i}: 'start'/'end' must be numbers, got "
                f"{raw.get('start')!r} / {raw.get('end')!r}"
            ) from None
        index = raw.get("id")
        transcript.segments.append(
            TranscriptSegment(
                index=int(index) if isinstance(index, (int, float)) else i,
                start=start,
                end=end,
                text=seg_text.strip(),
                speaker=str(raw.get("speaker", "") or ""),
            )
        )
    return transcript
