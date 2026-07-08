"""Tests for monteur.ai — the completion seam and its two backends.

The seam is :func:`monteur.ai.complete`: "api" (anthropic SDK) when
credentials exist, "claude-cli" (headless Claude Code) when only the
``claude`` executable is available, forced either way by
``MONTEUR_AI_BACKEND``. Everything here is offline: the SDK client,
the CLI path lookup and ``subprocess.run`` are all monkeypatched.
"""

import json
import subprocess
import sys
from unittest import mock

import pytest

import monteur.ai as ai
from monteur.ai import MonteurAIError, _client, complete


def test_missing_anthropic_raises_helpful_error():
    with mock.patch.dict(sys.modules, {"anthropic": None}):
        with pytest.raises(MonteurAIError, match="monteur\\[ai\\]"):
            _client()


# --- backend selection --------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No forced backend, no credentials, no settings file — the baseline.

    MONTEUR_SETTINGS_PATH points at a scratch file so these tests never
    read (or leak into) the developer's real ~/.monteur/settings.json.
    """
    monkeypatch.delenv("MONTEUR_AI_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return monkeypatch


def test_backend_auto_prefers_api_with_api_key(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "api"


def test_backend_auto_auth_token_counts_as_credentials(clean_env):
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    clean_env.setattr(ai, "_cli_path", lambda: None)
    assert ai._resolve_backend() == "api"


def test_backend_auto_uses_cli_without_credentials(clean_env):
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "claude-cli"


def test_backend_neither_raises_combined_message(clean_env):
    clean_env.setattr(ai, "_cli_path", lambda: None)
    with pytest.raises(MonteurAIError) as err:
        ai._resolve_backend()
    message = str(err.value)
    # one message, EVERY way out: the API key (env or Studio) and Claude Code
    assert "ANTHROPIC_API_KEY" in message
    assert "Claude Code" in message
    assert "'claude'" in message
    assert "Studio's settings" in message


def test_backend_env_forces_cli_over_credentials(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    clean_env.setenv("MONTEUR_AI_BACKEND", "claude-cli")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "claude-cli"


def test_backend_env_forces_api_without_credentials(clean_env):
    clean_env.setenv("MONTEUR_AI_BACKEND", "api")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "api"


def test_backend_env_cli_without_executable_raises(clean_env):
    clean_env.setenv("MONTEUR_AI_BACKEND", "claude-cli")
    clean_env.setattr(ai, "_cli_path", lambda: None)
    with pytest.raises(MonteurAIError, match="PATH"):
        ai._resolve_backend()


def test_backend_env_unknown_value_raises(clean_env):
    clean_env.setenv("MONTEUR_AI_BACKEND", "gemini")
    with pytest.raises(MonteurAIError, match="MONTEUR_AI_BACKEND"):
        ai._resolve_backend()


# --- backend selection via the settings file (Studio's settings panel) ---------------


def _write_settings(**settings):
    """Write the scratch settings file clean_env pointed MONTEUR_SETTINGS_PATH at."""
    from monteur.settings import save_settings

    save_settings(settings)


def test_settings_force_api_without_credentials(clean_env):
    _write_settings(ai_backend="api")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "api"


def test_settings_force_cli_over_credentials(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    _write_settings(ai_backend="claude-cli")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "claude-cli"


def test_settings_force_cli_without_executable_raises(clean_env):
    _write_settings(ai_backend="claude-cli")
    clean_env.setattr(ai, "_cli_path", lambda: None)
    with pytest.raises(MonteurAIError, match="PATH"):
        ai._resolve_backend()


def test_env_backend_wins_over_settings(clean_env):
    _write_settings(ai_backend="api")
    clean_env.setenv("MONTEUR_AI_BACKEND", "claude-cli")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "claude-cli"


def test_settings_key_selects_api_in_auto_mode(clean_env):
    _write_settings(api_key="sk-from-studio")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "api"


def test_settings_unknown_backend_reads_as_auto(clean_env):
    _write_settings(ai_backend="gemini")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "claude-cli"  # auto: no key -> CLI


def test_cleared_key_falls_back_to_cli(clean_env):
    # The Studio "Clear" button stores "" — auto must treat that as no key.
    _write_settings(api_key="")
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert ai._resolve_backend() == "claude-cli"


class _FakeAnthropicModule:
    """A stand-in anthropic module whose Anthropic() records its kwargs."""

    def __init__(self):
        self.constructed_with = []
        module = self

        class Anthropic:
            def __init__(self, **kwargs):
                module.constructed_with.append(kwargs)

        self.Anthropic = Anthropic


def test_client_passes_settings_key_when_no_env_key(clean_env):
    _write_settings(api_key="sk-from-studio")
    fake = _FakeAnthropicModule()
    with mock.patch.dict(sys.modules, {"anthropic": fake}):
        ai._client()
    assert fake.constructed_with == [{"api_key": "sk-from-studio"}]


def test_client_env_key_wins_over_settings_key(clean_env):
    _write_settings(api_key="sk-from-studio")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    fake = _FakeAnthropicModule()
    with mock.patch.dict(sys.modules, {"anthropic": fake}):
        ai._client()
    # no explicit kwarg: the SDK's own env resolution must win
    assert fake.constructed_with == [{}]


def test_client_without_any_key_uses_default_resolution(clean_env):
    fake = _FakeAnthropicModule()
    with mock.patch.dict(sys.modules, {"anthropic": fake}):
        ai._client()
    assert fake.constructed_with == [{}]  # e.g. an `ant auth login` profile


def test_vision_client_uses_settings_key(clean_env):
    """A key pasted in Studio must enable footage vision too."""
    import monteur.vision as vision

    _write_settings(api_key="sk-from-studio")
    fake = _FakeAnthropicModule()
    with mock.patch.dict(sys.modules, {"anthropic": fake}):
        vision._client()
    assert fake.constructed_with == [{"api_key": "sk-from-studio"}]


def test_vision_client_env_key_wins_over_settings_key(clean_env):
    import monteur.vision as vision

    _write_settings(api_key="sk-from-studio")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    fake = _FakeAnthropicModule()
    with mock.patch.dict(sys.modules, {"anthropic": fake}):
        vision._client()
    assert fake.constructed_with == [{}]


# --- the claude-cli backend -----------------------------------------------------------


def _cli_result(result_text="Hello from Claude", **overrides) -> bytes:
    """A claude -p --output-format json stdout payload (success by default)."""
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


@pytest.fixture
def cli_backend(clean_env):
    """Force the CLI backend with a fake exe; install() fakes subprocess.run."""
    clean_env.setenv("MONTEUR_AI_BACKEND", "claude-cli")
    clean_env.setattr(ai, "_cli_path", lambda: "/fake/claude")
    calls = []

    def install(stdout=b"", returncode=0, stderr=b"", raises=None):
        def fake_run(cmd, input=None, capture_output=None, timeout=None):
            calls.append({"cmd": list(cmd), "input": input, "timeout": timeout})
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(
                cmd, returncode, stdout=stdout, stderr=stderr
            )

        clean_env.setattr(ai.subprocess, "run", fake_run)
        return calls

    return install


def test_cli_success_parses_result_and_wires_flags(cli_backend):
    calls = cli_backend(stdout=_cli_result("Hallo Schnitt"))
    out = complete(
        "tick the best takes",
        system="be an editor",
        model="claude-opus-4-8",
        effort="medium",
    )
    assert out == "Hallo Schnitt"
    (call,) = calls
    cmd = call["cmd"]
    assert cmd[0] == "/fake/claude"
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
    assert cmd[cmd.index("--system-prompt") + 1] == "be an editor"
    assert cmd[cmd.index("--tools") + 1] == ""  # pure completion: no tools
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--effort") + 1] == "medium"
    # the prompt travels on stdin, not the command line
    assert call["input"] == "tick the best takes".encode("utf-8")
    assert call["timeout"] == ai.CLI_TIMEOUT_SECONDS


def test_cli_omits_optional_flags_when_unset(cli_backend):
    calls = cli_backend(stdout=_cli_result())
    complete("prompt only")
    cmd = calls[0]["cmd"]
    assert "--system-prompt" not in cmd
    assert "--effort" not in cmd


def test_cli_nonzero_exit_includes_stderr_tail(cli_backend):
    cli_backend(returncode=1, stderr=b"Invalid API key. Please run /login")
    with pytest.raises(MonteurAIError, match="exited with code 1.*Please run /login"):
        complete("prompt")


def test_cli_garbage_output_raises(cli_backend):
    cli_backend(stdout=b"totally not json")
    with pytest.raises(MonteurAIError, match="unparseable"):
        complete("prompt")


def test_cli_is_error_result_raises(cli_backend):
    cli_backend(stdout=_cli_result("credit balance too low", is_error=True))
    with pytest.raises(MonteurAIError, match="credit balance too low"):
        complete("prompt")


def test_cli_non_success_subtype_raises(cli_backend):
    cli_backend(stdout=_cli_result(subtype="error_during_execution"))
    with pytest.raises(MonteurAIError, match="reported a failure"):
        complete("prompt")


def test_cli_timeout_is_actionable(cli_backend):
    cli_backend(raises=subprocess.TimeoutExpired(cmd="claude", timeout=300))
    with pytest.raises(MonteurAIError, match="did not answer within"):
        complete("prompt")


def test_cli_missing_executable_is_actionable(cli_backend):
    cli_backend(raises=OSError("No such file or directory"))
    with pytest.raises(MonteurAIError, match="could not run the 'claude' CLI"):
        complete("prompt")


def test_cli_json_schema_instructs_and_strips_fences(cli_backend):
    calls = cli_backend(stdout=_cli_result('```json\n{"style": "trailer"}\n```'))
    out = complete("BRIEF: teaser", json_schema={"type": "object"})
    # the one common decoration is unwrapped; parsing stays the caller's job
    assert out == '{"style": "trailer"}'
    sent = calls[0]["input"].decode("utf-8")
    assert sent.startswith("BRIEF: teaser")
    assert "JSON Schema" in sent
    assert '"type": "object"' in sent


def test_cli_plain_text_is_not_fence_stripped(cli_backend):
    cli_backend(stdout=_cli_result("```json\nlooks like code\n```"))
    # without a schema the text is returned verbatim
    assert complete("prompt") == "```json\nlooks like code\n```"


# --- the api backend (anthropic SDK, faked) -------------------------------------------


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeSDKClient:
    """Records create/stream kwargs and serves a canned message."""

    def __init__(self, message):
        outer_message = message
        self.create_calls = []
        self.stream_calls = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.create_calls.append(kwargs)
                return outer_message

            def stream(self, **kwargs):
                outer.stream_calls.append(kwargs)
                return _FakeStream(outer_message)

        self.messages = _Messages()


@pytest.fixture
def api_backend(clean_env):
    def install(message):
        fake = _FakeSDKClient(message)
        clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")  # auto-selects "api"
        clean_env.setattr(ai, "_client", lambda: fake)
        # any subprocess use would be a routing bug
        clean_env.setattr(
            ai.subprocess, "run", lambda *a, **k: pytest.fail("CLI must not run")
        )
        return fake

    return install


def test_api_text_completion_streams_with_effort(api_backend):
    fake = api_backend(_FakeMessage("editorial notes"))
    out = complete("prompt", system="sys", model="m", max_tokens=64000, effort="high")
    assert out == "editorial notes"
    assert fake.create_calls == []
    (kwargs,) = fake.stream_calls
    assert kwargs["model"] == "m"
    assert kwargs["max_tokens"] == 64000
    assert kwargs["system"] == "sys"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]


def test_api_json_schema_uses_structured_output(api_backend):
    schema = {"type": "object", "additionalProperties": False}
    fake = api_backend(_FakeMessage('{"ok": true}'))
    out = complete("prompt", system="sys", max_tokens=1024, json_schema=schema)
    assert out == '{"ok": true}'
    assert fake.stream_calls == []
    (kwargs,) = fake.create_calls
    assert kwargs["max_tokens"] == 1024
    assert kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}
    }


def test_api_refusal_raises(api_backend):
    api_backend(_FakeMessage("", stop_reason="refusal"))
    with pytest.raises(MonteurAIError, match="declined"):
        complete("prompt")


def test_api_wins_over_cli_when_credentials_exist(api_backend, clean_env):
    """Credentials + claude on PATH: auto-selection must still pick the API."""
    fake = api_backend(_FakeMessage("api answer"))
    clean_env.setattr(ai, "_cli_path", lambda: "/usr/local/bin/claude")
    assert complete("prompt") == "api answer"
    assert len(fake.stream_calls) == 1
