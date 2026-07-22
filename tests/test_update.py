"""Tests for the in-app updater (monteur.update).

Network is always injected as a fake ``fetch`` — nothing here touches GitHub.
"""

from __future__ import annotations

import json

import pytest

from monteur import update


# -- version parsing / comparison ---------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.2.3", (1, 2, 3)),
        ("v1.2.3", (1, 2, 3)),
        ("V0.1.0", (0, 1, 0)),
        ("1.2", (1, 2)),
        ("1.2.0-rc1", (1, 2, 0)),
        ("2.0.0+build.7", (2, 0, 0)),
        ("", ()),
        ("nonsense", ()),
    ],
)
def test_parse_version(text, expected):
    assert update.parse_version(text) == expected


@pytest.mark.parametrize(
    "cand,cur,newer",
    [
        ("1.2.4", "1.2.3", True),
        ("v1.3.0", "1.2.9", True),
        ("1.2.3", "1.2.3", False),
        ("1.2.3", "1.2.4", False),
        ("1.2", "1.2.0", False),      # padded equal
        ("1.2.1", "1.2", True),       # padded greater
        ("garbage", "1.0.0", False),  # unparseable is never newer
        ("2.0.0", "", True),          # anything beats empty current
    ],
)
def test_is_newer(cand, cur, newer):
    assert update.is_newer(cand, cur) is newer


# -- platform asset selection -------------------------------------------------

def _assets():
    return [
        {"name": "Monteur-1.2.0.exe", "browser_download_url": "https://x/win.exe"},
        {"name": "Monteur-1.2.0.dmg", "browser_download_url": "https://x/mac.dmg"},
        {"name": "Monteur-1.2.0-linux.AppImage", "browser_download_url": "https://x/lin"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"},
    ]


def test_asset_for_platform_picks_the_right_file():
    assert update.asset_for_platform(_assets(), system="win32")["name"].endswith(".exe")
    assert update.asset_for_platform(_assets(), system="darwin")["name"].endswith(".dmg")
    assert update.asset_for_platform(_assets(), system="linux")["name"].endswith(".AppImage")


def test_asset_for_platform_none_when_no_match():
    only_source = [{"name": "source.tar.gz", "browser_download_url": "https://x/s"}]
    assert update.asset_for_platform(only_source, system="win32") is None


# -- check() ------------------------------------------------------------------

def _release_json(tag="v1.5.0", with_assets=True):
    payload = {
        "tag_name": tag,
        "body": "Shiny new things.",
        "html_url": "https://github.com/x/releases/tag/" + tag,
        "assets": _assets() if with_assets else [],
    }
    return json.dumps(payload).encode("utf-8")


def test_check_reports_an_available_update():
    info = update.check("1.0.0", fetch=lambda url: _release_json(), system="win32")
    assert info.available is True
    assert info.latest == "v1.5.0"
    assert info.notes == "Shiny new things."
    assert info.download_url == "https://x/win.exe"
    assert info.asset_name == "Monteur-1.2.0.exe"
    assert info.error == ""


def test_check_up_to_date():
    info = update.check("1.5.0", fetch=lambda url: _release_json("v1.5.0"), system="win32")
    assert info.available is False
    assert info.latest == "v1.5.0"


def test_check_hits_the_right_repo_url():
    seen = {}

    def fake(url):
        seen["url"] = url
        return _release_json()

    update.check("1.0.0", repo="acme/widget", fetch=fake)
    assert seen["url"] == "https://api.github.com/repos/acme/widget/releases/latest"


def test_check_network_failure_is_soft():
    def boom(url):
        raise OSError("no network")

    info = update.check("1.0.0", fetch=boom)
    assert info.available is False
    assert "couldn't reach" in info.error


def test_check_bad_json_is_soft():
    info = update.check("1.0.0", fetch=lambda url: b"{not json")
    assert info.available is False
    assert info.error


def test_check_no_releases_yet_is_up_to_date():
    # GitHub returns {"message": "Not Found"} with no tag_name when there are 0 releases
    info = update.check("1.0.0", fetch=lambda url: b'{"message": "Not Found"}')
    assert info.available is False
    assert info.latest == ""


def test_check_mode_reflects_frozen(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    info = update.check("1.0.0", fetch=lambda url: _release_json())
    assert info.mode == "frozen"


# -- download + staging -------------------------------------------------------

def test_download_stages_and_marks_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    info = update.check("1.0.0", fetch=lambda url: _release_json(), system="win32")
    staged = update.download(info, fetch=lambda url: b"MZ-fake-exe-bytes")
    assert staged.exists()
    assert staged.read_bytes() == b"MZ-fake-exe-bytes"
    pending = update.read_pending()
    assert pending is not None
    assert pending["file"] == str(staged)
    assert pending["version"] == "v1.5.0"


def test_download_without_asset_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    info = update.UpdateInfo(current="1.0.0", latest="1.5.0", available=True)
    with pytest.raises(ValueError):
        update.download(info, fetch=lambda url: b"x")


def test_pending_roundtrip_and_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert update.read_pending() is None
    staged = tmp_path / "Monteur-9.exe"
    staged.write_bytes(b"x")
    update.write_pending(staged, "9.0.0")
    assert update.read_pending()["version"] == "9.0.0"
    update.clear_pending()
    assert update.read_pending() is None


# -- apply_pending ------------------------------------------------------------

def test_apply_pending_none_when_nothing_staged(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert update.apply_pending() is None


def test_apply_pending_source_checkout_is_advisory(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    staged = tmp_path / "Monteur-2.exe"
    staged.write_bytes(b"new")
    update.write_pending(staged, "2.0.0")
    result = update.apply_pending()
    assert result is not None
    assert result.applied is False
    assert "source checkout" in result.message
    assert staged.exists()  # nothing touched


def test_apply_pending_frozen_swaps_the_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    fake_exe = tmp_path / "Monteur.exe"
    fake_exe.write_bytes(b"OLD")
    staged = tmp_path / "staged" / "Monteur-3.exe"
    staged.parent.mkdir()
    staged.write_bytes(b"NEW")
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update.sys, "executable", str(fake_exe))
    update.write_pending(staged, "3.0.0")

    result = update.apply_pending()
    assert result.applied is True
    assert result.version == "3.0.0"
    assert fake_exe.read_bytes() == b"NEW"                 # swapped in
    assert (tmp_path / "Monteur.exe.old").read_bytes() == b"OLD"  # old moved aside
    assert update.read_pending() is None                   # marker cleared


def test_apply_pending_missing_file_clears_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    update.write_pending(tmp_path / "gone.exe", "1.0.0")
    result = update.apply_pending()
    assert result.applied is False
    assert update.read_pending() is None
