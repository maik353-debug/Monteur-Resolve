"""Cancelling a scan/build actually KILLS the running ffmpeg.

The user-reported bug: pressing Cancel during a video scan or build left
the button inactive but ffmpeg kept running (the machine kept roaring) —
the job only stopped accepting NEW clips, because every ffmpeg call blocked
in ``subprocess.run`` and cancellation was checked only BETWEEN items.

These tests prove the fix end to end:

* the cancellable ``_run`` primitive kills a RUNNING subprocess within a
  poll interval of the cancel and raises :class:`MediaCancelled`;
* the default (``cancel=None``) path is byte-identical — the plain
  ``subprocess.run`` is used and ``Popen`` is never touched;
* ``sift_directory`` with a cancel set mid-run raises ``SiftCancelled``
  promptly, without draining every clip;
* the preview/export ffmpeg runner and the proxy batch are cancellable too.

Real subprocesses here are just ``python -c 'time.sleep(5)'`` — killed in a
fraction of a second, so the suite stays fast and needs no ffmpeg/numpy.
"""

import subprocess
import sys
import threading
import time

import pytest

from monteur.media import MediaCancelled, _CANCEL_POLL_S, _run

# A process that would run for 5 s if left alone — every kill test asserts it
# dies far sooner, proving the cancel does not wait for it to finish.
SLOW_CMD = [sys.executable, "-c", "import time; time.sleep(5)"]

# Generous upper bound on kill latency: the cancel fires at 0.2 s and the
# poll interval is ~0.15 s, so a real kill lands well under 1 s (5 s means
# the old drain-to-completion bug is back).
KILL_DEADLINE_S = 1.0


def _raises(exc):
    def boom(*_args, **_kwargs):
        raise exc

    return boom


# --------------------------------------------------------------------------
# media._run — the cancellable subprocess primitive
# --------------------------------------------------------------------------

def test_run_kills_running_process_when_cancel_fires_midway():
    """A cancel set WHILE ffmpeg runs kills it within a poll interval."""
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(MediaCancelled):
            _run(SLOW_CMD, cancel=cancel)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - start
    # Killed shortly after the 0.2 s cancel — NOT at the 5 s process end.
    assert elapsed < KILL_DEADLINE_S, f"kill took {elapsed:.3f}s"
    # And demonstrably close to (cancel time + one poll interval).
    assert elapsed < 0.2 + _CANCEL_POLL_S + 0.5


def test_run_kills_immediately_when_cancel_already_set():
    """A cancel already set before the call kills within one poll tick."""
    cancel = threading.Event()
    cancel.set()
    start = time.monotonic()
    with pytest.raises(MediaCancelled):
        _run(SLOW_CMD, cancel=cancel)
    elapsed = time.monotonic() - start
    assert elapsed < KILL_DEADLINE_S, f"kill took {elapsed:.3f}s"


def test_run_cancel_none_uses_plain_subprocess_run(monkeypatch):
    """Default path is byte-identical: subprocess.run, Popen never touched."""
    import monteur.media as media

    sentinel = subprocess.CompletedProcess(["x"], 0, b"out", b"err")
    seen = {}

    def fake_run(cmd, capture_output=False):
        seen["cmd"] = cmd
        seen["capture_output"] = capture_output
        return sentinel

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(
        media.subprocess, "Popen",
        _raises(AssertionError("Popen must not run on the default path")),
    )

    result = media._run(["ffmpeg", "-i", "clip.mp4"])
    assert result is sentinel
    assert seen["cmd"] == ["ffmpeg", "-i", "clip.mp4"]
    assert seen["capture_output"] is True


def test_run_cancel_none_still_honours_custom_runner(monkeypatch):
    """A custom test runner with cancel=None keeps working, no Popen."""
    import monteur.media as media

    monkeypatch.setattr(
        media.subprocess, "Popen",
        _raises(AssertionError("Popen must not run with a custom runner")),
    )
    seen = {}

    def runner(cmd, capture_output=False):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    media._run(["a", "b"], runner=runner)
    assert seen["cmd"] == ["a", "b"]


def test_run_returns_completed_process_shape_on_normal_finish():
    """The cancellable path returns the same CompletedProcess parsers expect."""
    cancel = threading.Event()  # never set
    result = _run(
        [sys.executable, "-c", "import sys; sys.stdout.write('ok')"],
        cancel=cancel,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == b"ok"


def test_media_cancelled_is_not_a_media_error():
    """MediaCancelled must NOT be swallowed by ``except MonteurMediaError``."""
    from monteur.media import MonteurMediaError

    assert not issubclass(MediaCancelled, MonteurMediaError)


# --------------------------------------------------------------------------
# sift.sift_directory — cancel tears down without draining every clip
# --------------------------------------------------------------------------

def test_sift_directory_cancel_raises_without_draining(tmp_path, monkeypatch):
    """A cancel mid-run raises SiftCancelled; not every clip is analysed."""
    import monteur.sift as sift

    total = 8
    for i in range(total):
        (tmp_path / f"clip_{i:02d}.mp4").write_bytes(b"x")

    cancel = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def fake_analyze(path, cancel=None):
        # analyze_clip threads the cancel into every ffmpeg call; a running
        # ffmpeg killed by a set cancel surfaces as MediaCancelled.
        with lock:
            started.append(path)
            first = len(started) == 1
        if cancel is not None and cancel.is_set():
            raise MediaCancelled("in-flight ffmpeg killed")
        if first:
            cancel.set()  # the user hits Cancel during this clip's decode
        raise MediaCancelled("ffmpeg killed mid-decode")

    monkeypatch.setattr(sift, "analyze_clip", fake_analyze)

    start = time.monotonic()
    with pytest.raises(sift.SiftCancelled):
        sift.sift_directory(str(tmp_path), cancel=cancel)
    elapsed = time.monotonic() - start

    # Cancelled clips do not crash the pool, and queued clips are skipped:
    # the run never drained all eight.
    assert len(started) < total
    # And it tore down promptly (no per-clip real work).
    assert elapsed < KILL_DEADLINE_S, f"sift teardown took {elapsed:.3f}s"


def test_sift_directory_no_cancel_object_still_works(tmp_path, monkeypatch):
    """cancel=None keeps the original behaviour (no cancellation)."""
    import monteur.sift as sift
    from monteur.sift import ClipReport

    for i in range(3):
        (tmp_path / f"clip_{i}.mp4").write_bytes(b"x")

    def fake_analyze(path, cancel=None):
        assert cancel is None
        return ClipReport(path=path, duration=1.0)

    monkeypatch.setattr(sift, "analyze_clip", fake_analyze)
    reports = sift.sift_directory(str(tmp_path))
    assert len(reports) == 3


# --------------------------------------------------------------------------
# preview._run_ffmpeg / _subprocess_run_cancellable — render path
# --------------------------------------------------------------------------

def test_preview_subprocess_cancellable_kills_midway():
    """The preview render's ffmpeg runner kills a running process too."""
    from monteur.preview import _subprocess_run_cancellable

    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(MediaCancelled):
            _subprocess_run_cancellable(SLOW_CMD, cancel)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - start
    assert elapsed < KILL_DEADLINE_S, f"kill took {elapsed:.3f}s"


def test_preview_default_path_uses_subprocess_run(monkeypatch):
    """cancel=None on the render path is byte-identical (no Popen)."""
    import monteur.preview as preview

    sentinel = subprocess.CompletedProcess(["x"], 0, b"", b"")
    monkeypatch.setattr(
        preview.subprocess, "run",
        lambda cmd, capture_output=False: sentinel,
    )
    monkeypatch.setattr(
        preview.subprocess, "Popen",
        _raises(AssertionError("Popen must not run on the default path")),
    )
    assert preview._subprocess_run_cancellable(["a"], None) is sentinel


def test_run_ffmpeg_forwards_cancel(monkeypatch):
    """_run_ffmpeg threads its cancel down into the cancellable runner."""
    import monteur.preview as preview

    monkeypatch.setattr(preview, "find_ffmpeg", lambda: "ffmpeg")
    seen = {}

    def fake(cmd, cancel):
        seen["cancel"] = cancel
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(preview, "_subprocess_run_cancellable", fake)
    cancel = threading.Event()
    preview._run_ffmpeg(["-i", "x", "out.mp4"], "encoding", cancel)
    assert seen["cancel"] is cancel


# --------------------------------------------------------------------------
# proxies.ensure_proxies — stops mid-batch on a killed transcode
# --------------------------------------------------------------------------

def test_ensure_proxies_stops_mid_batch_on_media_cancelled(tmp_path, monkeypatch):
    """A killed transcode stops the batch — it does not drain the rest."""
    from monteur import proxies

    cancel = threading.Event()
    calls: list[str] = []

    def fake_ensure(path, *, progress=None, cancel=None):
        calls.append(path)
        cancel.set()
        raise MediaCancelled("transcode killed mid-flight")

    monkeypatch.setattr(proxies, "ensure_proxy", fake_ensure)
    paths = [str(tmp_path / f"c{i}.mp4") for i in range(5)]
    made, errors = proxies.ensure_proxies(paths, cancel=cancel)

    assert calls == paths[:1]  # stopped after the first, not drained
    assert made == {}
    assert errors == {}


def test_ensure_proxies_threads_cancel_into_each_transcode(tmp_path, monkeypatch):
    """The batch passes its cancel object down into ensure_proxy."""
    from monteur import proxies

    cancel = threading.Event()
    seen = {}

    def fake_ensure(path, *, progress=None, cancel=None):
        seen["cancel"] = cancel
        return path

    monkeypatch.setattr(proxies, "ensure_proxy", fake_ensure)
    proxies.ensure_proxies([str(tmp_path / "c0.mp4")], cancel=cancel)
    assert seen["cancel"] is cancel
