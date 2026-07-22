"""Guards for the packaging helpers (scripts/): signing config + the icon.

Build tools, not library code — but the signing gate is security-adjacent
(don't silently always-sign or always-skip), so it's worth a couple of checks.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_signing_is_off_without_a_cert(monkeypatch):
    sign = _load("sign")
    monkeypatch.delenv("MONTEUR_SIGN_PFX", raising=False)
    monkeypatch.delenv("MONTEUR_SIGN_SHA1", raising=False)
    assert sign.signing_configured() is False
    # and sign_file is a clean no-op (returns False, never raises)
    assert sign.sign_file(ROOT / "pyproject.toml") is False


def test_signing_turns_on_with_a_cert(monkeypatch):
    sign = _load("sign")
    monkeypatch.setenv("MONTEUR_SIGN_PFX", "C:\\cert.pfx")
    assert sign.signing_configured() is True
    monkeypatch.delenv("MONTEUR_SIGN_PFX")
    monkeypatch.setenv("MONTEUR_SIGN_SHA1", "ABC123")
    assert sign.signing_configured() is True


def test_icon_is_committed_and_multisize():
    ico = ROOT / "packaging" / "monteur.ico"
    assert ico.exists(), "run scripts/make_icon.py"
    PIL = pytest.importorskip("PIL")  # noqa: N806
    from PIL import Image

    with Image.open(ico) as im:
        assert (256, 256) in im.info.get("sizes", [])
        assert (16, 16) in im.info.get("sizes", [])
