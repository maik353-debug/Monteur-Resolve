"""AI assistance for the editing room, powered by the Claude API.

Optional feature: requires the ``anthropic`` package (``pip install
monteur[ai]``) and credentials (``ANTHROPIC_API_KEY`` or an ``ant auth
login`` profile). Everything else in Monteur works without it.

What it does:

* :func:`suggest_selects` — reads a papercut (the transcript checklist) and an
  editorial brief, and returns the papercut with the strongest takes ticked,
  so the editor reviews a suggestion instead of starting from zero.
* :func:`pacing_notes` — reads a :class:`~monteur.analysis.PacingStats` and
  writes editorial notes on rhythm and dramaturgy.
* :func:`summarize_footage` — condenses a transcript into a scene/topic
  overview for logging.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from monteur.analysis import PacingStats
from monteur.model import Transcript

DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "You are Monteur, an experienced film editor's assistant. You think like an "
    "editor: story first, rhythm second, coverage third. Be concrete and "
    "decisive; when you make a judgment call, state the editorial reason in "
    "one short clause. Answer in the language the user's material is in."
)


class MonteurAIError(RuntimeError):
    """Raised when the AI feature is unavailable or a request fails."""


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise MonteurAIError(
            "AI features need the 'anthropic' package: pip install 'monteur[ai]'"
        ) from exc
    return anthropic.Anthropic()


def _run(prompt: str, model: str = DEFAULT_MODEL, effort: str = "high") -> str:
    client = _client()
    try:
        with client.messages.stream(
            model=model,
            max_tokens=64000,
            system=_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:  # pragma: no cover - network/auth failures
        raise MonteurAIError(f"Claude API request failed: {exc}") from exc
    if message.stop_reason == "refusal":
        raise MonteurAIError("The request was declined by the model's safety system.")
    return "".join(block.text for block in message.content if block.type == "text")


def suggest_selects(papercut_text: str, brief: str, model: str = DEFAULT_MODEL) -> str:
    """Return the papercut with suggested takes ticked ``[x]``.

    ``brief`` describes what the cut should achieve (e.g. "90-second teaser,
    focus on the conflict between Anna and the mayor, keep it fast").
    The output preserves the papercut format exactly, so it can be parsed by
    :func:`monteur.papercut.parse_papercut` and reviewed line by line.
    """
    prompt = (
        "Below is a papercut: a transcript checklist where '- [ ]' lines are "
        "available takes. Tick the takes ('- [x]') that best serve the brief, "
        "and reorder ticked lines if a different order tells the story "
        "better. Do NOT change timestamps, source headers, or the text of "
        "any line beyond the checkbox and line order. After the papercut, "
        "add a short '## Notes' section explaining your key choices.\n\n"
        f"BRIEF:\n{brief}\n\nPAPERCUT:\n{papercut_text}"
    )
    return _run(prompt, model=model)


def pacing_notes(stats: PacingStats, model: str = DEFAULT_MODEL) -> str:
    """Editorial notes on a cut's rhythm, based on its pacing statistics."""
    payload = asdict(stats)
    payload["shots"] = payload["shots"][:200]
    prompt = (
        "Here are pacing statistics for a film cut (all durations in "
        "seconds). Write editorial notes: where does the rhythm work, where "
        "does it likely sag or rush, which sections deserve another pass, "
        "and 3 concrete experiments the editor could try. Reference "
        "positions as M:SS timestamps.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    return _run(prompt, model=model)


def summarize_footage(transcript: Transcript, model: str = DEFAULT_MODEL) -> str:
    """Condense a transcript into a logged overview: topics, moments, quotes."""
    lines = [
        f"[{int(s.start // 60):02d}:{int(s.start % 60):02d}] "
        + (f"{s.speaker}: " if s.speaker else "")
        + s.text
        for s in transcript.segments
    ]
    prompt = (
        "Summarize this footage transcript for an editor's log: main topics "
        "with timestamp ranges, the strongest moments/quotes (verbatim, with "
        "timestamps), and anything unusable or repeated.\n\n"
        f"SOURCE: {transcript.source_name}\n\n" + "\n".join(lines)
    )
    return _run(prompt, model=model, effort="medium")
