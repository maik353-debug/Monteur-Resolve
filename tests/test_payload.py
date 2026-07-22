"""Tests for the app-payload mechanism (monteur.payload)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from monteur import payload


def _make_payload_zip(version: str, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.json", json.dumps({"version": version}))
        zf.writestr("monteur/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr("monteur/web/app.html", "<title>Monteur</title>")
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


def _write_payload_tree(root, version):
    root.mkdir(parents=True, exist_ok=True)
    (root / "payload.json").write_text(json.dumps({"version": version}))
    (root / "monteur").mkdir(exist_ok=True)
    (root / "monteur" / "__init__.py").write_text(f'__version__ = "{version}"\n')


# -- reading a payload tree ---------------------------------------------------

def test_read_payload_version_valid(tmp_path):
    _write_payload_tree(tmp_path / "p", "0.2.0")
    assert payload.read_payload_version(tmp_path / "p") == "0.2.0"


def test_read_payload_version_rejects_incomplete(tmp_path):
    # payload.json but no monteur package -> not a valid payload
    (tmp_path / "payload.json").write_text(json.dumps({"version": "9"}))
    assert payload.read_payload_version(tmp_path) is None


def test_read_payload_version_missing(tmp_path):
    assert payload.read_payload_version(tmp_path / "nope") is None


def test_installed_payloads_lists_valid_only(tmp_path):
    _write_payload_tree(tmp_path / "0.2.0", "0.2.0")
    _write_payload_tree(tmp_path / "0.3.0", "0.3.0")
    (tmp_path / "garbage").mkdir()
    found = dict(payload.installed_payloads(tmp_path))
    assert set(found) == {"0.2.0", "0.3.0"}


# -- choosing the newest ------------------------------------------------------

def test_choose_root_prefers_newer_download(tmp_path):
    _write_payload_tree(tmp_path / "0.3.0", "0.3.0")
    baseline = tmp_path / "baseline"
    _write_payload_tree(baseline, "0.2.0")
    version, root = payload.choose_root("0.2.0", baseline, base=tmp_path)
    assert version == "0.3.0"
    assert root == tmp_path / "0.3.0"


def test_choose_root_keeps_baseline_when_newest(tmp_path):
    _write_payload_tree(tmp_path / "0.1.0", "0.1.0")
    baseline = tmp_path / "baseline"
    _write_payload_tree(baseline, "0.2.0")
    version, root = payload.choose_root("0.2.0", baseline, base=tmp_path)
    assert version == "0.2.0"
    assert root == baseline


def test_choose_root_baseline_wins_ties(tmp_path):
    _write_payload_tree(tmp_path / "0.2.0", "0.2.0")
    baseline = tmp_path / "baseline"
    _write_payload_tree(baseline, "0.2.0")
    _version, root = payload.choose_root("0.2.0", baseline, base=tmp_path)
    assert root == baseline  # a same-version download never shadows the shell


def test_choose_root_ignores_downgrade(tmp_path):
    _write_payload_tree(tmp_path / "0.1.0", "0.1.0")
    baseline = tmp_path / "baseline"
    _write_payload_tree(baseline, "0.5.0")
    _version, root = payload.choose_root("0.5.0", baseline, base=tmp_path)
    assert root == baseline


# -- checksum -----------------------------------------------------------------

def test_verify_sha256():
    data = b"hello"
    good = payload.sha256_of(data)
    assert payload.verify_sha256(data, good) is True
    assert payload.verify_sha256(data, good.upper()) is True
    assert payload.verify_sha256(data, "deadbeef") is False
    assert payload.verify_sha256(data, "") is False


# -- extraction ---------------------------------------------------------------

def test_extract_payload_unpacks_and_versions(tmp_path):
    blob = _make_payload_zip("0.4.0")
    version, root = payload.extract_payload(blob, base=tmp_path)
    assert version == "0.4.0"
    assert root == tmp_path / "0.4.0"
    assert (root / "monteur" / "__init__.py").is_file()
    assert (root / "monteur" / "web" / "app.html").is_file()


def test_extract_payload_replaces_existing(tmp_path):
    payload.extract_payload(_make_payload_zip("0.4.0"), base=tmp_path)
    # a re-extract of the same version overwrites cleanly (no leftover temp dirs)
    _v, root = payload.extract_payload(_make_payload_zip("0.4.0"), base=tmp_path)
    assert root.is_dir()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("monteur-payload-")]
    assert leftovers == []


def test_extract_payload_rejects_non_payload(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "not a payload")
    with pytest.raises(ValueError):
        payload.extract_payload(buf.getvalue(), base=tmp_path)


def test_extract_payload_blocks_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.json", json.dumps({"version": "1"}))
        zf.writestr("../escape.txt", "pwned")
    with pytest.raises(ValueError):
        payload.extract_payload(buf.getvalue(), base=tmp_path)
