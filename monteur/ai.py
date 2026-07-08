"""AI assistance for the editing room, powered by Claude.

Optional feature — everything else in Monteur works without it. All of
Monteur's text generation goes through one seam, :func:`complete`, which
knows two ways to reach Claude:

* **api** — the Claude API via the ``anthropic`` package (``pip install
  monteur[ai]``), authenticated with ``ANTHROPIC_API_KEY`` or
  ``ANTHROPIC_AUTH_TOKEN``, the key saved in Studio's settings
  (:mod:`monteur.settings`), or an ``ant auth login`` profile.
* **claude-cli** — the Claude Code command-line tool (``claude``) in
  headless mode, using its own login/subscription. No API key and no
  ``anthropic`` package needed; Monteur runs it as a pure completion with
  all tools disabled.

Backend resolution order (see :func:`_resolve_backend`):

1. ``MONTEUR_AI_BACKEND=api|claude-cli`` in the environment — the
   developer escape hatch, it always wins;
2. else the settings file's ``ai_backend`` when Studio's settings panel
   forced ``"api"`` or ``"claude-cli"`` (:mod:`monteur.settings` —
   ``~/.monteur/settings.json``);
3. else auto: the API when a key exists (environment OR settings file),
   otherwise the ``claude`` CLI when it is on PATH, otherwise a
   :class:`MonteurAIError` explaining every option.

Within the API path the key precedence is: ``ANTHROPIC_API_KEY`` /
``ANTHROPIC_AUTH_TOKEN`` from the environment win (an explicit
machine-level override), else the key saved in Studio's settings is passed
to the client directly, else the SDK's own resolution (e.g. an ``ant auth
login`` profile) gets its chance.

Footage vision (:mod:`monteur.vision`) is the exception: it sends images,
which the CLI cannot, so it always needs the API key.

What this module does:

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
import os
import shutil
import subprocess
from dataclasses import asdict

from monteur.analysis import PacingStats
from monteur.model import Transcript

DEFAULT_MODEL = "claude-opus-4-8"

#: Environment variable that forces a backend ("api" or "claude-cli").
BACKEND_ENV = "MONTEUR_AI_BACKEND"

#: Wall-clock limit for one headless ``claude`` run. Generous on purpose:
#: a screenplay draft is a long completion.
CLI_TIMEOUT_SECONDS = 300.0

_SYSTEM = (
    "You are Monteur, an experienced film editor's assistant. You think like an "
    "editor: story first, rhythm second, coverage third. Be concrete and "
    "decisive; when you make a judgment call, state the editorial reason in "
    "one short clause. Answer in the language the user's material is in."
)


class MonteurAIError(RuntimeError):
    """Raised when the AI feature is unavailable or a request fails."""


def _env_credentials() -> bool:
    """True when the environment itself carries Claude API credentials."""
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


def _settings_api_key() -> str:
    """The API key saved in Studio's settings (``""`` when none)."""
    from monteur.settings import api_key

    return api_key()


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise MonteurAIError(
            "AI features need the 'anthropic' package: pip install 'monteur[ai]'"
        ) from exc
    # Env credentials win (explicit machine-level override); otherwise a key
    # saved in Studio's settings is passed to the client directly; otherwise
    # the SDK's own resolution (e.g. an `ant auth login` profile) may apply.
    try:
        if not _env_credentials():
            key = _settings_api_key()
            if key:
                return anthropic.Anthropic(api_key=key)
        return anthropic.Anthropic()
    except MonteurAIError:
        raise
    except Exception as exc:  # pragma: no cover - constructor-time auth failures
        raise MonteurAIError(
            "could not create the Claude client — the API backend needs an "
            "Anthropic API key: set ANTHROPIC_API_KEY, or paste a key in "
            f"Studio's settings: {exc}"
        ) from exc


def _cli_path() -> str | None:
    """Absolute path of the ``claude`` executable, or None.

    ``shutil.which`` resolves ``claude.cmd``/``claude.exe`` on Windows
    automatically. Kept as its own seam so tests can monkeypatch it.
    """
    return shutil.which("claude")


_NO_BACKEND_MESSAGE = (
    "No way to reach Claude found. Monteur's writing features need one of "
    "two things: set the ANTHROPIC_API_KEY environment variable (an API key "
    "from console.anthropic.com, billed per use) — or paste an API key in "
    "Studio's settings — OR install Claude Code "
    "(https://claude.com/claude-code) — then Monteur uses the 'claude' "
    "command with its login/subscription at no extra cost."
)


def _resolve_backend() -> str:
    """Pick the backend. Resolution order (documented in the module docstring):

    1. ``MONTEUR_AI_BACKEND`` in the environment (developer escape hatch),
    2. else a forced choice saved in Studio's settings file,
    3. else auto: the API when a key exists (environment or settings),
       else the ``claude`` CLI when on PATH, else a MonteurAIError
       explaining every option.
    """
    forced = os.environ.get(BACKEND_ENV, "").strip().lower()
    if forced:
        if forced not in ("api", "claude-cli"):
            raise MonteurAIError(
                f"unknown {BACKEND_ENV} value {forced!r} — use 'api' or 'claude-cli'"
            )
        if forced == "claude-cli" and _cli_path() is None:
            raise MonteurAIError(
                f"{BACKEND_ENV}=claude-cli, but no 'claude' executable is on "
                "PATH — install Claude Code (https://claude.com/claude-code) "
                f"or unset {BACKEND_ENV}"
            )
        return forced
    from monteur.settings import ai_backend

    chosen = ai_backend()
    if chosen == "claude-cli":
        if _cli_path() is None:
            raise MonteurAIError(
                "Monteur is set to use Claude Code (Studio settings), but no "
                "'claude' executable is on PATH — install Claude Code "
                "(https://claude.com/claude-code), or switch the setting "
                "back to Auto"
            )
        return "claude-cli"
    if chosen == "api":
        # A forced-api choice without any key fails inside the API request
        # with its own clear message; resolution honours the user's choice.
        return "api"
    if _env_credentials() or _settings_api_key():
        return "api"
    if _cli_path() is not None:
        return "claude-cli"
    raise MonteurAIError(_NO_BACKEND_MESSAGE)


def _strip_fences(text: str) -> str:
    """Strip one wrapping markdown code fence, if present.

    The CLI backend cannot enforce structured output, and models like to
    wrap requested JSON in ``` fences — unwrap that one common decoration
    and leave everything else to the caller's JSON parsing.
    """
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            return stripped[first_newline + 1 : -3].strip()
    return stripped


def _complete_api(
    prompt: str,
    *,
    system: str,
    model: str,
    max_tokens: int,
    effort: str | None,
    json_schema: dict | None,
) -> str:
    """The Claude API backend (anthropic SDK)."""
    client = _client()
    try:
        if json_schema is not None:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"format": {"type": "json_schema", "schema": json_schema}},
                messages=[{"role": "user", "content": prompt}],
            )
        else:
            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": effort or "high"},
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
    except MonteurAIError:
        raise
    except Exception as exc:  # pragma: no cover - network/auth failures
        raise MonteurAIError(f"Claude API request failed: {exc}") from exc
    if getattr(message, "stop_reason", None) == "refusal":
        raise MonteurAIError("The request was declined by the model's safety system.")
    return "".join(block.text for block in message.content if block.type == "text")


def _complete_cli(
    prompt: str,
    *,
    system: str,
    model: str,
    effort: str | None,
    json_schema: dict | None,
) -> str:
    """The Claude Code CLI backend: one headless, tool-less completion.

    The prompt travels on stdin (immune to Windows quoting and command-line
    length limits); ``--output-format json`` yields one result object whose
    ``result`` field is the completion text. All tools are disabled — this
    is a pure completion, the CLI must not touch files. With ``json_schema``
    the schema is appended to the prompt as an instruction (the CLI cannot
    enforce structured output); the caller's JSON parsing stays the judge.
    """
    exe = _cli_path()
    if exe is None:
        raise MonteurAIError(_NO_BACKEND_MESSAGE)
    if json_schema is not None:
        prompt = (
            prompt
            + "\n\nRespond with ONLY a single JSON object that matches this "
            "JSON Schema — no markdown fences, no prose before or after:\n"
            + json.dumps(json_schema)
        )
    cmd = [
        exe,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "",
        "--no-session-persistence",
    ]
    if system:
        cmd += ["--system-prompt", system]
    if effort:
        cmd += ["--effort", effort]
    try:
        result = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise MonteurAIError(
            f"the 'claude' CLI did not answer within {int(CLI_TIMEOUT_SECONDS)}s "
            "— try again, or set ANTHROPIC_API_KEY to use the API instead"
        ) from exc
    except OSError as exc:
        raise MonteurAIError(f"could not run the 'claude' CLI: {exc}") from exc
    stdout = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise MonteurAIError(
            f"the 'claude' CLI exited with code {result.returncode}: "
            f"{tail or stdout.strip()[-500:] or 'no error output'}"
        )
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise MonteurAIError(
            f"the 'claude' CLI returned unparseable output: {stdout[:200]!r}"
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("is_error")
        or data.get("subtype") != "success"
        or not isinstance(data.get("result"), str)
    ):
        detail = data.get("result") if isinstance(data, dict) else None
        raise MonteurAIError(
            f"the 'claude' CLI reported a failure: {detail or stdout[:200]!r}"
        )
    text = data["result"]
    return _strip_fences(text) if json_schema is not None else text


def complete(
    prompt: str,
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 16000,
    effort: str | None = None,
    json_schema: dict | None = None,
) -> str:
    """One Claude text completion through the selected backend.

    The single seam all of Monteur's writing features use (movie
    blueprints, brief interpretation, publish copy, the ``monteur ai``
    helpers). See the module docstring for how the backend is chosen.

    ``json_schema`` requests structured output: guaranteed by the API
    backend (``output_config.format``), instructed on the CLI backend —
    either way the caller parses the returned text as JSON. ``effort``
    tunes reasoning depth on plain-text completions (API default "high").
    ``max_tokens`` applies to the API backend; the CLI manages its own
    output limits.

    Raises :class:`MonteurAIError` when no backend is available or the
    request fails; a safety refusal on the API path raises too, while on
    the CLI path it simply comes back as text.
    """
    if _resolve_backend() == "api":
        return _complete_api(
            prompt,
            system=system,
            model=model,
            max_tokens=max_tokens,
            effort=effort,
            json_schema=json_schema,
        )
    return _complete_cli(
        prompt, system=system, model=model, effort=effort, json_schema=json_schema
    )


def _run(prompt: str, model: str = DEFAULT_MODEL, effort: str = "high") -> str:
    """A completion with Monteur's editor persona (the module's helpers)."""
    return complete(
        prompt, system=_SYSTEM, model=model, max_tokens=64000, effort=effort
    )


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
