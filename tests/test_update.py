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


# -- payload-aware check ------------------------------------------------------

def _release_with_payload(tag="v1.5.0"):
    payload = {
        "tag_name": tag,
        "body": "notes",
        "html_url": "https://x",
        "assets": [
            {"name": "monteur-app-1.5.0.zip", "browser_download_url": "https://x/app.zip"},
            {"name": "monteur-app-1.5.0.zip.sha256", "browser_download_url": "https://x/app.zip.sha256"},
            {"name": "Monteur-1.5.0.exe", "browser_download_url": "https://x/win.exe"},
        ],
    }
    return json.dumps(payload).encode("utf-8")


def test_check_frozen_prefers_payload(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    info = update.check("1.0.0", fetch=lambda url: _release_with_payload(), system="win32")
    assert info.kind == "payload"
    assert info.payload_url == "https://x/app.zip"
    assert info.payload_name == "monteur-app-1.5.0.zip"
    assert info.sha256_url == "https://x/app.zip.sha256"


def test_check_frozen_falls_back_to_exe(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    # a release with only an exe (deps changed -> full shell update)
    info = update.check("1.0.0", fetch=lambda url: _release_json(), system="win32")
    assert info.kind == "exe"
    assert info.download_url == "https://x/win.exe"


def test_check_source_installs_nothing(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    info = update.check("1.0.0", fetch=lambda url: _release_with_payload(), system="win32")
    assert info.kind == "none"


# -- channels -----------------------------------------------------------------

def test_stable_channel_uses_latest_endpoint():
    seen = {}

    def fetch(url):
        seen["url"] = url
        return _release_json("v0.2.0", with_assets=False)

    info = update.check("0.1.0", repo="a/b", fetch=fetch, channel="stable")
    assert seen["url"].endswith("/releases/latest")
    assert info.channel == "stable"
    assert info.latest == "v0.2.0"


def test_dev_channel_lists_and_picks_newest_non_draft():
    def fetch(url):
        assert "/releases?" in url  # the list endpoint, not /latest
        return json.dumps([
            {"tag_name": "", "draft": True, "assets": []},
            {"tag_name": "v0.1.99", "draft": False, "assets": []},
            {"tag_name": "v0.1.98", "draft": False, "assets": []},
        ]).encode("utf-8")

    info = update.check("0.1.50", repo="a/b", fetch=fetch, channel="dev")
    assert info.channel == "dev"
    assert info.latest == "v0.1.99"
    assert info.available is True


def test_unknown_channel_falls_back_to_stable():
    def fetch(url):
        assert url.endswith("/releases/latest")
        return _release_json("v0.2.0", with_assets=False)

    info = update.check("0.1.0", repo="a/b", fetch=fetch, channel="nonsense")
    assert info.channel == "stable"


# -- install_payload ----------------------------------------------------------

def _payload_zip(version="1.5.0"):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.json", json.dumps({"version": version}))
        zf.writestr("monteur/__init__.py", f'__version__ = "{version}"\n')
    return buf.getvalue()


def test_install_payload_verifies_and_extracts(tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    blob = _payload_zip("1.5.0")
    digest = hashlib.sha256(blob).hexdigest()

    def fetch(url):
        if url.endswith(".sha256"):
            return (digest + "  monteur-app-1.5.0.zip\n").encode("utf-8")
        return blob

    info = update.UpdateInfo(
        current="1.0.0", latest="v1.5.0", available=True, kind="payload",
        payload_url="https://x/app.zip", payload_name="monteur-app-1.5.0.zip",
        sha256_url="https://x/app.zip.sha256", mode="frozen",
    )
    version = update.install_payload(info, fetch=fetch)
    assert version == "1.5.0"
    from monteur import payload as payload_mod
    installed = dict(payload_mod.installed_payloads())
    assert "1.5.0" in installed


def test_install_payload_rejects_bad_checksum(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))

    def fetch(url):
        if url.endswith(".sha256"):
            return b"deadbeef  monteur-app-1.5.0.zip\n"
        return _payload_zip("1.5.0")

    info = update.UpdateInfo(
        current="1.0.0", latest="v1.5.0", kind="payload",
        payload_url="https://x/app.zip", payload_name="monteur-app-1.5.0.zip",
        sha256_url="https://x/app.zip.sha256", mode="frozen",
    )
    with pytest.raises(ValueError, match="checksum"):
        update.install_payload(info, fetch=fetch)


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


# --- git-checkout updates -----------------------------------------------------


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _git_runner(mapping):
    """A subprocess.run stand-in dispatching on the git subcommand prefix."""
    def run(args, cwd=None, capture_output=None, text=None, timeout=None):
        key = " ".join(args[1:])  # drop the leading 'git'
        for prefix, proc in mapping.items():
            if key.startswith(prefix):
                return proc
        return _Proc(1, "", "unmapped: " + key)
    return run


def test_git_check_reports_commits_behind(tmp_path):
    runner = _git_runner({
        "rev-parse --abbrev-ref": _Proc(0, "main\n"),
        "fetch": _Proc(0),
        "rev-list --left-right --count": _Proc(0, "0\t3\n"),  # 0 ahead, 3 behind
        "log -1": _Proc(0, "Latest and greatest\n"),
    })
    info = update.git_check(tmp_path, runner=runner)
    assert info.mode == "git" and info.available is True
    assert info.latest == "3 commits behind"
    assert info.notes == "Latest and greatest"
    assert info.asset_name == "main"  # the branch name, for display


def test_git_check_up_to_date(tmp_path):
    runner = _git_runner({
        "rev-parse --abbrev-ref": _Proc(0, "main\n"),
        "fetch": _Proc(0),
        "rev-list --left-right --count": _Proc(0, "0\t0\n"),
    })
    info = update.git_check(tmp_path, runner=runner)
    assert info.available is False and not info.error


def test_git_check_no_upstream_is_soft(tmp_path):
    runner = _git_runner({
        "rev-parse --abbrev-ref": _Proc(0, "wip\n"),
        "fetch": _Proc(0),
        "rev-list --left-right --count": _Proc(128, "", "no upstream configured"),
    })
    info = update.git_check(tmp_path, runner=runner)
    assert info.available is False and info.error


def test_git_check_fetch_failure_is_soft(tmp_path):
    runner = _git_runner({
        "rev-parse --abbrev-ref": _Proc(0, "main\n"),
        "fetch": _Proc(1, "", "could not resolve host"),
    })
    info = update.git_check(tmp_path, runner=runner)
    assert info.available is False and "could not resolve host" in info.error


def test_git_pull_fast_forwards_cleanly(tmp_path):
    runner = _git_runner({
        "status --porcelain": _Proc(0, ""),          # clean tree
        "pull --ff-only": _Proc(0, "Updating a1b2..c3d4\n"),
    })
    res = update.git_pull(tmp_path, runner=runner)
    assert res.applied is True
    assert "Restart Monteur" in res.message


def test_git_pull_refuses_a_dirty_tree(tmp_path):
    runner = _git_runner({"status --porcelain": _Proc(0, " M monteur/x.py\n")})
    res = update.git_pull(tmp_path, runner=runner)
    assert res.applied is False
    assert "uncommitted local changes" in res.message


def test_git_pull_refuses_when_it_cannot_fast_forward(tmp_path):
    runner = _git_runner({
        "status --porcelain": _Proc(0, ""),
        "pull --ff-only": _Proc(1, "", "fatal: Not possible to fast-forward, aborting."),
    })
    res = update.git_pull(tmp_path, runner=runner)
    assert res.applied is False
    assert "fast-forward" in res.message
    assert "nothing was discarded" in res.message  # honest, non-destructive


def test_git_root_none_when_frozen(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    assert update.git_root() is None
