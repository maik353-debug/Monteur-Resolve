"""Shared test fixtures.

The sift's on-disk report cache (``.monteur-sift.json`` next to footage) is a
production optimisation — a re-opened project reuses the sift instead of
re-crunching. In tests it would let one test's sift leak into another through
shared footage folders (a test that asserts a *fresh* sift would be served a
stale sidecar), so it's disabled by default here. The tests that specifically
cover persistence re-enable it with ``MONTEUR_SIFT_CACHE=1``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _sift_cache_off(monkeypatch):
    monkeypatch.setenv("MONTEUR_SIFT_CACHE", "0")
