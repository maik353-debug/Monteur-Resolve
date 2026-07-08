"""Tests for monteur.settings — the persistent user settings file.

One plain JSON file (``~/.monteur/settings.json``, overridden via
``MONTEUR_SETTINGS_PATH`` — which every test here sets, so the real home
directory is never touched): merge-and-write saves, forgiving loads, and
0600 permissions on POSIX because the file can hold the API key.
"""

import json
import os
import stat

import pytest

from monteur.settings import (
    SETTINGS_PATH_ENV,
    ai_backend,
    api_key,
    load_settings,
    save_settings,
    settings_path,
)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point the settings at a scratch file; returns its Path (not created)."""
    path = tmp_path / "settings.json"
    monkeypatch.setenv(SETTINGS_PATH_ENV, str(path))
    return path


def test_env_override_controls_the_path(settings_file):
    assert settings_path() == settings_file


def test_default_path_is_in_home(monkeypatch):
    monkeypatch.delenv(SETTINGS_PATH_ENV, raising=False)
    path = settings_path()
    assert path.name == "settings.json"
    assert path.parent.name == ".monteur"


def test_missing_file_loads_as_empty(settings_file):
    assert load_settings() == {}
    assert ai_backend() == "auto"
    assert api_key() == ""


def test_save_and_load_round_trip(settings_file):
    save_settings({"ai_backend": "api", "api_key": "sk-ant-test1234"})
    assert load_settings() == {"ai_backend": "api", "api_key": "sk-ant-test1234"}
    assert ai_backend() == "api"
    assert api_key() == "sk-ant-test1234"


def test_save_creates_parent_directories(tmp_path, monkeypatch):
    path = tmp_path / "deep" / "nested" / "settings.json"
    monkeypatch.setenv(SETTINGS_PATH_ENV, str(path))
    save_settings({"ai_backend": "claude-cli"})
    assert path.exists()
    assert ai_backend() == "claude-cli"


def test_corrupt_file_loads_as_empty(settings_file):
    settings_file.write_text("{not json at all", encoding="utf-8")
    assert load_settings() == {}
    assert ai_backend() == "auto"
    assert api_key() == ""


def test_non_object_json_loads_as_empty(settings_file):
    settings_file.write_text('["a", "list"]', encoding="utf-8")
    assert load_settings() == {}


def test_save_merges_and_keeps_unknown_keys(settings_file):
    # A future (or past) Monteur version wrote a key this one doesn't know.
    settings_file.write_text(
        json.dumps({"api_key": "sk-old", "future_toggle": {"nested": True}}),
        encoding="utf-8",
    )
    merged = save_settings({"ai_backend": "api"})
    assert merged["future_toggle"] == {"nested": True}
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk == {
        "api_key": "sk-old",
        "future_toggle": {"nested": True},
        "ai_backend": "api",
    }


def test_save_over_corrupt_file_starts_fresh(settings_file):
    settings_file.write_text("garbage", encoding="utf-8")
    save_settings({"api_key": "sk-new"})
    assert load_settings() == {"api_key": "sk-new"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_settings_file_is_owner_only(settings_file):
    save_settings({"api_key": "sk-secret"})
    mode = stat.S_IMODE(settings_file.stat().st_mode)
    assert mode == 0o600  # the file can hold the API key


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_resave_keeps_owner_only_mode(settings_file):
    save_settings({"api_key": "sk-secret"})
    os.chmod(settings_file, 0o644)  # someone loosened it by hand
    save_settings({"ai_backend": "api"})
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600


def test_save_leaves_no_temp_files(settings_file):
    save_settings({"ai_backend": "api"})
    assert [p.name for p in settings_file.parent.iterdir()] == ["settings.json"]


def test_ai_backend_unknown_value_reads_as_auto(settings_file):
    save_settings({"ai_backend": "gemini"})
    assert ai_backend() == "auto"
    save_settings({"ai_backend": 42})
    assert ai_backend() == "auto"


def test_api_key_non_string_reads_as_empty(settings_file):
    save_settings({"api_key": ["not", "a", "key"]})
    assert api_key() == ""


def test_api_key_is_stripped_on_read(settings_file):
    settings_file.write_text(json.dumps({"api_key": "  sk-hand-edited \n"}))
    assert api_key() == "sk-hand-edited"
