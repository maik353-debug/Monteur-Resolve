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
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

from monteur.analysis import PacingStats
from monteur.model import Transcript

DEFAULT_MODEL = "claude-opus-4-8"

#: Environment variable that forces a backend ("api" or "claude-cli").
BACKEND_ENV = "MONTEUR_AI_BACKEND"

#: How long the streaming ``claude`` CLI may produce NOTHING at all before we
#: treat it as dead. This is an *inactivity* limit, not a wall-clock one: the
#: CLI streams thinking + text tokens the whole time it works, so a legitimately
#: slow completion (a big storyboard compose with deep reasoning) keeps resetting
#: it and is never killed mid-thought — only a truly silent process trips it.
#: The old hard 300s wall-clock cap was killing working builds; this replaces it.
CLI_TIMEOUT_SECONDS = 300.0

#: Absolute backstop: even while streaming, no single run may exceed this.
CLI_TOTAL_CAP_SECONDS = 3600.0

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


def _closed_schema(schema):
    """A copy of ``schema`` with ``additionalProperties: false`` on every
    object node (nested and top-level).

    The structured-output API requires object schemas to close themselves
    explicitly; a single missed nested object 400s the whole request. Closing
    centrally here means every schema (compose/vision/director/coverage/movie
    and any future one) is safe without each having to remember. Types given
    as a list (e.g. ``["object", "null"]``) count as objects too. The input
    is never mutated — the module-level SCHEMA constants stay as authored.
    """
    if isinstance(schema, dict):
        out = {k: _closed_schema(v) for k, v in schema.items()}
        t = schema.get("type")
        is_object = t == "object" or (isinstance(t, list) and "object" in t)
        if is_object and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_closed_schema(v) for v in schema]
    return schema


def _complete_api(
    prompt: str,
    *,
    system: str,
    model: str,
    max_tokens: int,
    effort: str | None,
    json_schema: dict | None,
    on_delta: Callable[[str], None] | None = None,
) -> str:
    """The Claude API backend (anthropic SDK)."""
    client = _client()

    def _emit(chunk: str) -> None:
        if chunk and on_delta is not None:
            try:
                on_delta(chunk)
            except Exception:  # a UI callback must never fail a completion
                pass

    try:
        if json_schema is not None:
            # Structured output is a single JSON answer — no extended thinking
            # (adaptive thinking here returned an empty text block: the JSON
            # never landed, "unparseable JSON: ''"). This matches the vision
            # pass, which uses the same output_config and works. We STREAM it
            # only when a caller is listening (on_delta) — e.g. the storyboard
            # build, so the cut can be shown as it's written; otherwise a plain
            # create() keeps the simple one-shot path every other caller uses.
            output_config = {
                "format": {
                    "type": "json_schema",
                    "schema": _closed_schema(json_schema),
                }
            }
            if on_delta is not None:
                with client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    output_config=output_config,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    for text in stream.text_stream:
                        _emit(text)
                    message = stream.get_final_message()
            else:
                message = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    output_config=output_config,
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
                for text in stream.text_stream:
                    _emit(text)
                message = stream.get_final_message()
    except MonteurAIError:
        raise
    except Exception as exc:  # pragma: no cover - network/auth failures
        raise MonteurAIError(f"Claude API request failed: {exc}") from exc
    if getattr(message, "stop_reason", None) == "refusal":
        raise MonteurAIError("The request was declined by the model's safety system.")
    text = "".join(
        block.text for block in message.content if block.type == "text"
    )
    if json_schema is not None and not text.strip():
        # a structured request that produced no text — surface WHY (e.g.
        # max_tokens) instead of a downstream "unparseable JSON: ''"
        reason = getattr(message, "stop_reason", None) or "unknown"
        raise MonteurAIError(
            "Claude returned no structured output (stop reason: "
            f"{reason}). If this persists the request may be hitting the "
            "token limit — try a shorter brief or fewer clips."
        )
    return text


def _complete_cli(
    prompt: str,
    *,
    system: str,
    model: str,
    effort: str | None,
    json_schema: dict | None,
    on_delta: Callable[[str], None] | None = None,
) -> str:
    """The Claude Code CLI backend: one headless, tool-less completion, STREAMED.

    The prompt travels on stdin (immune to Windows quoting and command-line
    length limits). We run ``--output-format stream-json`` so the CLI emits one
    JSON event per line as it works — thinking tokens, then the answer's text
    deltas, then a final ``result`` object. Streaming buys two things a single
    blocking ``--output-format json`` run could not:

    * **an inactivity timeout instead of a wall-clock one** — a big storyboard
      compose can reason for minutes; as long as the CLI keeps emitting events
      it is alive, and only a truly silent process (nothing for
      :data:`CLI_TIMEOUT_SECONDS`) is killed. The old hard 300s wall was
      terminating working builds;
    * **honest progress** — ``on_delta`` receives the answer's text as it is
      written, so a caller (the storyboard build) can show Claude composing
      live instead of a frozen spinner.

    All tools are disabled — this is a pure completion, the CLI must not touch
    files. With ``json_schema`` the schema is appended to the prompt as an
    instruction (the CLI cannot enforce structured output); the caller's JSON
    parsing stays the judge.
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
        "stream-json",
        "--verbose",
        "--include-partial-messages",
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
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise MonteurAIError(f"could not run the 'claude' CLI: {exc}") from exc

    # Drain stdout/stderr on threads (so a full pipe can never deadlock) and
    # feed the prompt on another (a prompt bigger than the pipe buffer would
    # block a same-thread write until the CLI drains it). The main loop then
    # consumes stdout lines with an inactivity deadline.
    lines: "queue.Queue[str | None]" = queue.Queue()
    stderr_buf: list[bytes] = []

    def _pump_stdout() -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                lines.put(raw.decode("utf-8", "replace"))
        finally:
            lines.put(None)  # sentinel: stdout closed → the run is over

    def _pump_stderr() -> None:
        try:
            stderr_buf.append(proc.stderr.read())  # type: ignore[union-attr]
        except OSError:
            pass

    def _feed_stdin() -> None:
        try:
            proc.stdin.write(prompt.encode("utf-8"))  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass

    for target in (_pump_stdout, _pump_stderr, _feed_stdin):
        threading.Thread(target=target, daemon=True).start()

    result_text: str | None = None
    failed = False
    started = time.monotonic()
    while True:
        try:
            line = lines.get(timeout=CLI_TIMEOUT_SECONDS)
        except queue.Empty:
            proc.kill()
            raise MonteurAIError(
                f"the 'claude' CLI went silent for {int(CLI_TIMEOUT_SECONDS)}s "
                "— try again, or set ANTHROPIC_API_KEY to use the API instead"
            )
        if line is None:
            break  # stdout closed — the run finished
        if time.monotonic() - started > CLI_TOTAL_CAP_SECONDS:
            proc.kill()
            raise MonteurAIError(
                "the 'claude' CLI ran too long — try again, or set "
                "ANTHROPIC_API_KEY to use the API instead"
            )
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue  # non-JSON noise (shouldn't happen on stdout) — skip
        if not isinstance(evt, dict):
            continue
        etype = evt.get("type")
        if etype == "stream_event":
            inner = evt.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text") or ""
                    if chunk and on_delta is not None:
                        try:
                            on_delta(chunk)
                        except Exception:  # a UI callback must never kill a build
                            pass
        elif etype == "result":
            if evt.get("is_error") or evt.get("subtype") not in (None, "success"):
                failed = True
            if isinstance(evt.get("result"), str):
                result_text = evt["result"]

    proc.wait()
    stderr_text = (b"".join(stderr_buf)).decode("utf-8", "replace").strip()
    if result_text is None:
        if proc.returncode:
            raise MonteurAIError(
                f"the 'claude' CLI exited with code {proc.returncode}: "
                f"{stderr_text[-500:] or 'no error output'}"
            )
        raise MonteurAIError(
            "the 'claude' CLI produced no result"
            + (f": {stderr_text[-300:]!r}" if stderr_text else "")
        )
    if failed:
        raise MonteurAIError(
            f"the 'claude' CLI reported a failure: {result_text[:200]!r}"
        )
    return _strip_fences(result_text) if json_schema is not None else result_text


def complete(
    prompt: str,
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 16000,
    effort: str | None = None,
    json_schema: dict | None = None,
    on_delta: Callable[[str], None] | None = None,
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

    ``on_delta`` (optional) receives the answer's text in the chunks the
    model streams it — so a long completion (a storyboard compose) can show
    live progress instead of a frozen spinner. It is best-effort: an
    exception from the callback never fails the completion, and a backend
    with nothing to stream simply never calls it.

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
            on_delta=on_delta,
        )
    return _complete_cli(
        prompt,
        system=system,
        model=model,
        effort=effort,
        json_schema=json_schema,
        on_delta=on_delta,
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
