"""The Windows console-window flash suppression (``monteur.procio``).

On Windows a windowless GUI parent (``pythonw`` / a frozen ``--windowed``
build) pops a black console window for every console child it spawns. The
:data:`monteur.procio.NO_WINDOW` kwargs carry ``CREATE_NO_WINDOW`` so those
spawns stay invisible; off Windows the dict is empty so ``**NO_WINDOW`` is a
no-op. These tests pin both halves and confirm the real spawn sites pass it
through — verified without a Windows box by forcing ``sys.platform``.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


def _no_window_for(platform: str) -> dict:
    """The value :data:`NO_WINDOW` would take when imported on ``platform``.

    Returns a copy of the value so the restore-reload afterwards can't clobber
    it (the module object is reused across reloads).
    """
    import monteur.procio as procio

    saved = sys.platform
    try:
        sys.platform = platform
        value = dict(importlib.reload(procio).NO_WINDOW)
    finally:
        sys.platform = saved
        importlib.reload(procio)  # restore the real-platform module for others
    return value


def test_no_window_is_empty_off_windows():
    assert _no_window_for("linux") == {}


def test_no_window_carries_creationflags_on_windows():
    # On a non-Windows Python ``subprocess.CREATE_NO_WINDOW`` is absent, so the
    # module falls back to 0 — but the point is proven: on win32 the branch
    # produces a ``creationflags`` kwarg (the real CREATE_NO_WINDOW value on a
    # genuine Windows box).
    value = _no_window_for("win32")
    expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert value == {"creationflags": expected_flag}


def test_double_star_no_window_is_a_noop_off_windows():
    """``**NO_WINDOW`` must add nothing to a call on non-Windows platforms."""
    from monteur.procio import NO_WINDOW

    if sys.platform != "win32":
        assert NO_WINDOW == {}
    seen = {}

    def fake(cmd, **kwargs):
        seen.update(kwargs)
        return cmd

    fake(["x"], capture_output=True, **NO_WINDOW)
    assert "creationflags" not in seen or sys.platform == "win32"


def test_media_run_passes_no_window(monkeypatch):
    """media._run's default (non-injected) path spawns with NO_WINDOW."""
    import monteur.media as media

    calls = {}

    def fake_run(cmd, **kwargs):
        calls["kwargs"] = kwargs

        class _P:
            returncode = 0
            stdout = b""
            stderr = b""

        return _P()

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    media._run(["ffmpeg", "-version"])
    for key, value in media.NO_WINDOW.items():
        assert calls["kwargs"].get(key) == value


def test_transcribe_default_run_passes_no_window(monkeypatch):
    """transcribe._default_run forwards NO_WINDOW to subprocess.run."""
    import monteur.transcribe as transcribe

    calls = {}

    def fake_run(*args, **kwargs):
        calls["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(transcribe.subprocess, "run", fake_run)
    transcribe._default_run(["whisper"], capture_output=True)
    assert calls["kwargs"].get("capture_output") is True
    for key, value in transcribe.NO_WINDOW.items():
        assert calls["kwargs"].get(key) == value


def test_update_default_run_passes_no_window(monkeypatch):
    """update._default_run (the git runner) forwards NO_WINDOW."""
    import monteur.update as update

    calls = {}

    def fake_run(*args, **kwargs):
        calls["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    update._default_run(["git", "status"], capture_output=True)
    for key, value in update.NO_WINDOW.items():
        assert calls["kwargs"].get(key) == value
