"""DaVinci Resolve integration for Monteur.

Monteur talks to Resolve through Blackmagic's scripting API. First enable
scripting in Resolve: Preferences > System > General > "External scripting
using" and set it to "Local" (or "Network" for remote control). Then run
Monteur one of three ways:

1. Resolve Console (Workspace > Console, select Py3): ``import monteur.resolve``
   works directly because Resolve preloads its scripting module; call
   ``monteur.resolve.connect()``.

2. Workspace > Scripts: drop a script into the Resolve scripts folder
   (e.g. macOS ``~/Library/Application Support/Blackmagic Design/DaVinci
   Resolve/Fusion/Scripts/Utility``) and it runs with the API available.

3. Externally, from any Python 3 process while Resolve is running. Set the
   three environment variables (macOS values shown; adjust per platform):

   RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
   RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
   PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules"

   On Windows the API lives under ``%PROGRAMDATA%\\Blackmagic Design\\DaVinci
   Resolve\\Support\\Developer\\Scripting`` and the lib is ``fusionscript.dll``
   in the Resolve install folder; on Linux the API is under
   ``/opt/resolve/Developer/Scripting`` and the lib is
   ``/opt/resolve/libs/Fusion/fusionscript.so``.

Typical use::

    from monteur.resolve import connect

    bridge = connect()
    timeline = bridge.read_timeline()

Crash-safe (isolated) access
----------------------------
Resolve's native scripting module (``fusionscript.dll``/``.so``, loaded by
``DaVinciResolveScript``) is built for a specific range of Python versions
(roughly 3.10–3.12 for current Resolve releases; older Resolve versions
accepted 3.6+). Importing it under an *incompatible* interpreter (e.g.
Python 3.14) triggers a C-level access violation that **cannot** be caught
with ``try``/``except`` — it kills the whole process. That makes the direct
``connect()`` path unsafe to call speculatively (a Studio page load, an MCP
probe) on a mismatched interpreter.

The isolated layer solves this by running every scripting-module access in a
**separate child process** (``monteur._resolve_worker``). A native crash then
only kills that child; the parent detects the nonzero exit and returns a
graceful "Resolve unavailable" result instead of dying. Prefer these from any
long-lived process:

    from monteur.resolve import (
        build_plan_isolated,
        read_timeline_isolated,
        resolve_status_isolated,
    )

    status = resolve_status_isolated()        # never raises; dict result
    if status["connected"]:
        timeline = read_timeline_isolated()   # Timeline, or MonteurResolveError
        result = build_plan_isolated(plan, fps=25.0)  # build a montage; dict,
                                                      # never raises

The worker commands are ``status``, ``info`` (crash-free environment
forensics), ``load_test`` (staged, line-per-stage native load test that
pinpoints WHERE a crash happens — see :func:`load_test_isolated`),
``read_timeline``, ``build_plan`` (build a montage timeline from a
serialized MontagePlan, optionally with Fusion titles — by default the
hybrid FCPXML-import build that carries dissolves/fades/audio lanes,
falling back to the clip-by-clip append build) and ``render``
(streamed: drive Resolve's Deliver engine to a finished video file — see
:func:`render_isolated`; the child only MONITORS the render, so killing it
on a timeout leaves Resolve rendering on) — see
``monteur._resolve_worker`` for the wire protocol.

Choosing the worker interpreter
-------------------------------
The isolated worker runs under the first of (see ``_worker_python``):

1. the ``MONTEUR_RESOLVE_PYTHON`` environment variable (advanced override),
2. the ``resolve_python`` path saved in Monteur's settings
   (``~/.monteur/settings.json`` — written by Studio's "Find a compatible
   Python" button; silently ignored if the file no longer exists),
3. ``sys.executable`` (the interpreter running Monteur).

End users never touch environment variables: :func:`find_resolve_python`
walks the machine's Python installations (:func:`_candidate_pythons`) and
:func:`probe_resolve_python` safely checks each one — Studio's settings
panel drives both and remembers the winner.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from types import ModuleType
from typing import Any

from monteur.model import AUDIO, VIDEO, Clip, Marker, Timeline, format_timecode
from monteur.procio import NO_WINDOW

_MODULE_NAME = "DaVinciResolveScript"

_ENABLE_HINT = (
    'Make sure DaVinci Resolve is running and scripting is enabled: open '
    'Resolve Preferences > System > General and set "External scripting '
    'using" to "Local".'
)


class MonteurResolveError(RuntimeError):
    """Raised when the Resolve scripting API is unavailable or misbehaves."""


def _candidate_module_dirs() -> list[str]:
    dirs: list[str] = []
    api = os.environ.get("RESOLVE_SCRIPT_API")
    if api:
        dirs.append(os.path.join(api, "Modules"))
        dirs.append(api)
    lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if lib:
        dirs.append(os.path.dirname(lib))
    if sys.platform == "darwin":
        dirs.append(
            "/Library/Application Support/Blackmagic Design"
            "/DaVinci Resolve/Developer/Scripting/Modules"
        )
    elif sys.platform.startswith("win"):
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        dirs.append(
            os.path.join(
                program_data,
                "Blackmagic Design",
                "DaVinci Resolve",
                "Support",
                "Developer",
                "Scripting",
                "Modules",
            )
        )
    else:
        dirs.append("/opt/resolve/Developer/Scripting/Modules")
        dirs.append("/home/resolve/Developer/Scripting/Modules")
    return dirs


def _register_resolve_dll_dir() -> None:
    """Windows: add Resolve's program folder to the DLL search path.

    ``fusionscript.dll`` depends on sibling DLLs in Resolve's install folder.
    When it is loaded from an external Python (not from inside Resolve), the
    Windows loader may not find those siblings, and a missing/mismatched
    dependency can access-violate rather than fail cleanly. Registering the
    folder via ``os.add_dll_directory`` lets the loader resolve them. No-op off
    Windows and fully guarded.
    """
    if not sys.platform.startswith("win"):
        return
    candidates = []
    lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if lib:
        candidates.append(os.path.dirname(lib))
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    candidates.append(
        os.path.join(program_files, "Blackmagic Design", "DaVinci Resolve")
    )
    for directory in candidates:
        if directory and os.path.isdir(directory):
            try:
                os.add_dll_directory(directory)
            except (OSError, AttributeError):
                pass


# Environment variables that can break (or fix) Resolve's scripting-module
# load. Blackmagic's own docs show them QUOTED — and quotes around a Windows
# path break DLL loading — so the crash-free `info` worker command reports
# each one with `quoted` / `exists` flags for the diagnosis verdict.
_DIAG_ENV_VARS = (
    "RESOLVE_SCRIPT_API",
    "RESOLVE_SCRIPT_LIB",
    "PYTHONPATH",
    "MONTEUR_RESOLVE_PYTHON",
)


def _is_quoted(value: str) -> bool:
    """True when a value starts or ends with a quote character.

    Blackmagic's setup docs show the env-var examples in quotes; users paste
    them verbatim into the Windows environment-variable dialog, where the
    quotes become part of the value and silently break the DLL/module load.
    """
    v = (value or "").strip()
    return bool(v) and (v[0] in "'\"" or v[-1] in "'\"")


def _strip_env_quotes(value: str) -> str:
    """A path value with any surrounding whitespace and quote characters removed."""
    return (value or "").strip().strip("'\"")


def _env_report() -> dict:
    """Crash-free report on the Resolve-relevant environment variables.

    For each variable in :data:`_DIAG_ENV_VARS`: the raw ``value`` (or None
    when unset), ``quoted`` (starts/ends with a quote character — breaks
    loading on Windows) and ``exists`` (``os.path.exists`` after stripping
    quotes; None when unset). PYTHONPATH is a path LIST, so it gets
    ``exists: None`` plus ``missing`` — the entries that do not exist.
    """
    report: dict = {}
    for name in _DIAG_ENV_VARS:
        raw = os.environ.get(name)
        if raw is None:
            report[name] = {"value": None, "quoted": False, "exists": None}
            continue
        entry: dict = {"value": raw, "quoted": _is_quoted(raw)}
        if name == "PYTHONPATH":
            entries = [
                _strip_env_quotes(part)
                for part in raw.split(os.pathsep)
                if _strip_env_quotes(part)
            ]
            entry["exists"] = None
            entry["missing"] = [p for p in entries if not os.path.exists(p)]
        else:
            stripped = _strip_env_quotes(raw)
            entry["exists"] = os.path.exists(stripped) if stripped else False
        report[name] = entry
    return report


def _fusionscript_candidates() -> list[str]:
    """Where Resolve's native scripting library could be, best-first.

    The RESOLVE_SCRIPT_LIB environment variable (quotes stripped — quoted
    values are a known misconfiguration this diagnosis exists to catch),
    then the per-OS default install location. Pure and cheap — no native
    loading, safe for the crash-free ``info`` worker command.
    """
    paths: list[str] = []
    lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if lib:
        stripped = _strip_env_quotes(lib)
        if stripped:
            paths.append(stripped)
    if sys.platform == "darwin":
        paths.append(
            "/Applications/DaVinci Resolve/DaVinci Resolve.app"
            "/Contents/Libraries/Fusion/fusionscript.so"
        )
    elif sys.platform.startswith("win"):
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        paths.append(
            os.path.join(
                program_files,
                "Blackmagic Design",
                "DaVinci Resolve",
                "fusionscript.dll",
            )
        )
    else:
        paths.append("/opt/resolve/libs/Fusion/fusionscript.so")
    return paths


def _locate_fusionscript() -> str | None:
    """The actual fusionscript library file, or None when none exists."""
    return next((p for p in _fusionscript_candidates() if os.path.isfile(p)), None)


# --- Windows registry Python census (crash forensics) ---------------------------
#
# The confirmed field mechanism behind "crashes even under a compatible
# worker Python": fusionscript.dll does NOT bind to the interpreter that
# imports it. On Windows it walks HKEY_LOCAL_MACHINE\SOFTWARE\Python\
# PythonCore and loads the python DLL of the HIGHEST version registered
# there. A 3.13+ Python registered machine-wide therefore hard-crashes the
# load (typically at the DaVinciResolveScript import) even when the worker
# is a perfectly good 3.11. The census below is crash-free — pure winreg
# reads, never any native loading — and feeds the worker's ``info`` command
# so the diagnosis verdict can name that mechanism.


def _parse_py_tag(tag) -> tuple[int, int] | None:
    """(major, minor) from a PythonCore tag like "3.14" / "3.11-32", or None."""
    import re

    match = re.match(r"^(\d+)\.(\d+)", str(tag or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _pythoncore_census(winreg) -> list[dict]:
    """Enumerate SOFTWARE\\Python\\PythonCore in HKLM and HKCU.

    Takes the ``winreg`` module explicitly (injectable, so non-Windows
    tests can drive a shim). Returns ``[{"version": <tag>, "hive":
    "HKLM"|"HKCU", "path": <InstallPath default value or None>}, ...]``,
    deduped per hive+tag across the 64/32-bit registry views. Any per-key
    hiccup yields fewer entries, never an error; wholesale failures are the
    caller's (:func:`_registered_pythons`) problem.
    """
    census: list[dict] = []
    seen: set[tuple[str, str]] = set()
    views = [0]
    for name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        view = getattr(winreg, name, 0)
        if view and view not in views:
            views.append(view)
    for hive, root in (
        ("HKLM", winreg.HKEY_LOCAL_MACHINE),
        ("HKCU", winreg.HKEY_CURRENT_USER),
    ):
        for view in views:
            try:
                core = winreg.OpenKey(
                    root, r"SOFTWARE\Python\PythonCore", 0,
                    winreg.KEY_READ | view,
                )
            except OSError:
                continue
            with core:
                for tag in _registry_subkeys(winreg, core):
                    if (hive, tag) in seen:
                        continue
                    seen.add((hive, tag))
                    path = None
                    try:
                        install = winreg.OpenKey(
                            core, tag + r"\InstallPath", 0,
                            winreg.KEY_READ | view,
                        )
                    except OSError:
                        install = None
                    if install is not None:
                        with install:
                            try:
                                value = winreg.QueryValue(install, None)
                            except OSError:
                                value = None
                        path = str(value) if value else None
                    census.append(
                        {"version": str(tag), "hive": hive, "path": path}
                    )
    return census


def _registered_pythons(winreg_module=None) -> list[dict]:
    """The PythonCore registry census, guarded; [] off Windows or on failure.

    ``winreg_module`` is injectable for tests; by default the real
    ``winreg`` is imported (absent off Windows -> ``[]``). This never
    raises — it runs inside the crash-free ``info`` worker command.
    """
    if winreg_module is None:
        try:
            import winreg as winreg_module
        except ImportError:
            return []
    try:
        return _pythoncore_census(winreg_module)
    except Exception:  # noqa: BLE001 - forensics must never raise
        return []


def _registry_highest(census, hive: str = "HKLM") -> str | None:
    """Highest PythonCore version registered in ``hive``, as "major.minor".

    HKLM is what fusionscript.dll keys on (the confirmed mechanism); the
    HKCU answer is computed the same way so the verdict can mention a
    per-user Python registered even higher. Unparseable tags are skipped;
    None when the hive registers nothing.
    """
    best: tuple[int, int] | None = None
    for entry in census or []:
        if entry.get("hive") != hive:
            continue
        parsed = _parse_py_tag(entry.get("version"))
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    return "%d.%d" % best if best else None


def find_scripting_module() -> ModuleType:
    """Locate and import ``DaVinciResolveScript``.

    Tries PYTHONPATH / already-importable locations first, then
    RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB, then the standard per-platform
    install paths. Raises MonteurResolveError with setup guidance if missing.
    """
    _register_resolve_dll_dir()
    try:
        return importlib.import_module(_MODULE_NAME)
    except ImportError:
        pass
    searched = _candidate_module_dirs()
    for directory in searched:
        if not os.path.isfile(os.path.join(directory, _MODULE_NAME + ".py")):
            continue
        if directory not in sys.path:
            sys.path.append(directory)
        try:
            return importlib.import_module(_MODULE_NAME)
        except ImportError:
            continue
    raise MonteurResolveError(
        f"Could not locate the {_MODULE_NAME} module. Searched: "
        + ", ".join(searched)
        + ". Install DaVinci Resolve (free from blackmagicdesign.com), then "
        "point Monteur at its scripting API by setting RESOLVE_SCRIPT_API to "
        "the Developer/Scripting folder (and RESOLVE_SCRIPT_LIB to "
        "fusionscript.so/.dll), or add the Scripting/Modules folder to "
        "PYTHONPATH. " + _ENABLE_HINT
    )


def connect(app: Any | None = None) -> "ResolveBridge":
    """Connect to a running Resolve instance and return a ResolveBridge.

    Pass ``app`` to inject a pre-built Resolve app object (e.g. the ``resolve``
    global inside Resolve's Console, or a fake in tests).
    """
    if app is None:
        module = find_scripting_module()
        app = module.scriptapp("Resolve")
    if app is None:
        raise MonteurResolveError(
            "DaVinciResolveScript.scriptapp('Resolve') returned nothing — "
            "Resolve does not appear to be running. " + _ENABLE_HINT
        )
    return ResolveBridge(app)


# --- Crash-safe isolated layer -------------------------------------------------
#
# The functions below run all Resolve scripting-module access in a child
# process (``monteur._resolve_worker``). If Resolve's native module hard-crashes
# the interpreter (an access violation that Python cannot catch), only the child
# dies; the parent sees the nonzero exit and reports a graceful failure. This is
# what lets Monteur Studio / the MCP server / the CLI probe Resolve without any
# risk of taking down the host process.

import pathlib as _pathlib

# The worker is launched BY PATH (not ``-m monteur._resolve_worker``) so it runs
# under an interpreter that doesn't have Monteur pip-installed — a bare Python
# 3.11 pointed at via MONTEUR_RESOLVE_PYTHON. The worker bootstraps its own
# sys.path to find the package.
_WORKER_PATH = str(_pathlib.Path(__file__).resolve().with_name("_resolve_worker.py"))

# A true native crash exits with an access-violation-style code, NOT a normal
# Python exit code. POSIX: a signal gives a negative return code (SIGSEGV -> -11).
# Windows: 0xC0000005 (access violation) surfaces as the signed -1073741819 or the
# unsigned 3221225477; 0xC00000FD (stack overflow) as -1073741571 / 3221225725.
# A clean Python failure (bad import, argparse) exits 1 or 2 — that is NOT a crash.
_NATIVE_CRASH_CODES = {-1073741819, 3221225477, -1073741571, 3221225725}


def _looks_like_native_crash(code: int) -> bool:
    return code < 0 or code >= 2 ** 30 or code in _NATIVE_CRASH_CODES


# The in-app fix every crash/incompatibility message points at. End users
# never see a CLI or set environment variables — the settings panel does the
# work; the env var stays as a one-line advanced note.
_APP_FIX = (
    "Open Studio's settings (gear) > DaVinci Resolve and click "
    "“Find a compatible Python” — Monteur can locate or remember "
    "one for you. (Advanced: MONTEUR_RESOLVE_PYTHON overrides this.)"
)

_CRASH_MESSAGE = (
    "DaVinci Resolve's scripting module crashed while loading — this usually "
    "means the interpreter isn't compatible with your Resolve version (Resolve "
    "needs a 64-bit Python, roughly 3.10–3.12 for current Resolve releases; "
    "older Resolve versions accepted 3.6+). " + _APP_FIX
)


def _worker_python_source() -> tuple[str, str]:
    """The worker interpreter and where it came from.

    Returns ``(path, source)`` with ``source`` one of:

    * ``"env"`` — the ``MONTEUR_RESOLVE_PYTHON`` environment variable
      (advanced override, always wins);
    * ``"settings"`` — the ``resolve_python`` path saved in Monteur's
      settings (written by Studio's "Find a compatible Python" button).
      A saved path whose file no longer exists (Python uninstalled, drive
      renamed) is silently skipped — settings are a convenience, never a
      gate, and the panel still shows the stale value so it can be fixed;
    * ``"default"`` — ``sys.executable``, the interpreter running Monteur.
    """
    override = os.environ.get("MONTEUR_RESOLVE_PYTHON")
    if override:
        return override, "env"
    try:
        from monteur.settings import resolve_python

        saved = resolve_python()
    except Exception:  # noqa: BLE001 - settings must never break Resolve access
        saved = ""
    if saved and os.path.isfile(saved):
        return saved, "settings"
    return sys.executable, "default"


def _worker_python() -> str:
    """Interpreter used to run the isolated Resolve worker subprocess.

    Precedence: the ``MONTEUR_RESOLVE_PYTHON`` environment variable, then
    the ``resolve_python`` path saved in Monteur's settings (only while the
    file still exists), then ``sys.executable``. This lets Monteur itself
    run under any Python (e.g. 3.14) while every Resolve scripting-module
    call happens under a Resolve-compatible interpreter (roughly Python
    3.10–3.12 for current Resolve releases) — necessary because Resolve's
    native module hard-crashes
    under an incompatible Python. See :func:`_worker_python_source`.
    """
    return _worker_python_source()[0]


def _run_worker(
    command: str,
    timeout: float,
    request: dict | None = None,
    interpreter: str | None = None,
) -> tuple[bool, dict]:
    """Run one worker command in a child process.

    Returns ``(True, payload)`` when the worker exits 0 with parseable JSON on
    stdout, or ``(False, graceful)`` otherwise, where ``graceful`` carries a
    human-readable ``error`` and a ``reason`` of ``"crash"`` (nonzero exit —
    an uncatchable native crash), ``"timeout"``, ``"no-interpreter"`` (the
    worker interpreter could not be launched) or ``"bad-output"`` (unparseable
    stdout). This function NEVER raises for a child failure — that is the whole
    point of isolating Resolve here.

    ``interpreter`` overrides :func:`_worker_python` for this one call —
    used by :func:`probe_resolve_python` to try candidate interpreters
    without touching the configured one.
    """
    if interpreter is None:
        interpreter = _worker_python()
    cmd = [interpreter, _WORKER_PATH, command]
    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(request or {}),
            capture_output=True,
            text=True,
            timeout=timeout,
            **NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, {
            "error": (
                f"The Resolve worker did not respond within {timeout:g}s and was "
                "terminated. Resolve may be busy, mid-render or unresponsive — "
                "try again in a moment."
            ),
            "reason": "timeout",
        }
    except FileNotFoundError:
        return False, {
            "error": (
                f"Could not launch the Resolve worker interpreter {interpreter!r}. "
                "Open Studio's settings (gear) > DaVinci Resolve and click "
                "“Find a compatible Python”, or fix the saved path there. "
                "(Advanced: MONTEUR_RESOLVE_PYTHON overrides the saved choice.)"
            ),
            "reason": "no-interpreter",
        }
    if result.returncode != 0:
        if _looks_like_native_crash(result.returncode):
            return False, {"error": _CRASH_MESSAGE, "reason": "crash"}
        # A clean nonzero exit means the WORKER ITSELF failed (couldn't import
        # Monteur, missing dependency, argparse error) — NOT a Resolve native
        # crash. Surface the real reason instead of the misleading crash text.
        stderr = (result.stderr or "")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        detail = tail or f"exit code {result.returncode}"
        return False, {
            "error": (
                f"The Resolve helper failed to run under {interpreter!r}: "
                f"{detail}. Make sure that interpreter is a working Python 3 "
                "(the helper needs only the standard library)."
            ),
            "reason": "worker-error",
        }
    stdout = result.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return False, {
            "error": (
                "The Resolve worker returned output Monteur could not parse: "
                f"{(stdout or '')[:200]!r}"
            ),
            "reason": "bad-output",
        }
    if not isinstance(payload, dict):
        return False, {
            "error": (
                "The Resolve worker returned an unexpected (non-object) "
                f"response: {payload!r}"
            ),
            "reason": "bad-output",
        }
    return True, payload


def resolve_status_isolated(timeout: float = 25.0) -> dict:
    """Probe Resolve in a child process; return a status dict, never raising.

    On success returns the worker's payload, e.g.
    ``{"connected": True, "project": ..., "timelines": [...], "current": ...}``
    (or ``{"connected": False, "error": ...}`` for a clean, handled failure
    such as Resolve not running). On an uncatchable child crash, a timeout, a
    missing interpreter or garbage output it returns
    ``{"connected": False, "error": <message>, "reason":
    "crash"|"timeout"|"no-interpreter"|"bad-output"}``. Safe to call
    speculatively (page load, health check) — a native Resolve crash can never
    take down the caller.
    """
    ok, payload = _run_worker("status", timeout)
    if not ok:
        return {"connected": False, **payload}
    payload.setdefault("connected", False)
    return payload


# The staged load test's stage order (the worker's ``load_test`` command
# emits one JSON line per completed stage). When the child hard-crashes,
# the stage AFTER the last completed one is where it died.
_LOAD_STAGES = ("locate", "dll-load", "import", "connect")


def _parse_stage_lines(stdout: str) -> list[dict]:
    """Parse the load_test worker's line-per-stage stdout, tolerating garbage.

    A native crash can truncate the stream mid-line; unparseable or
    non-stage lines are simply skipped so the completed stages survive.
    """
    stages: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("stage"):
            stages.append(payload)
    return stages


def _stage_after(stages: list[dict]) -> str | None:
    """The load-test stage following the last completed one (= crash site)."""
    names = [s.get("stage") for s in stages]
    last = names[-1] if names else None
    if last is None:
        return _LOAD_STAGES[0]
    try:
        index = _LOAD_STAGES.index(last)
    except ValueError:
        return None
    return _LOAD_STAGES[index + 1] if index + 1 < len(_LOAD_STAGES) else None


def load_test_isolated(timeout: float = 25.0, interpreter: str | None = None) -> dict:
    """Staged load test of Resolve's scripting module; pinpoints crashes.

    Runs the worker's ``load_test`` command, which prints ONE JSON line per
    completed stage (``locate`` the fusionscript library, ``dll-load`` it via
    ctypes, ``import`` DaVinciResolveScript, ``connect`` via scriptapp) and
    flushes after each — so when a later stage hard-crashes the child, the
    parent still sees every stage that succeeded. Returns::

        {"stages": [<parsed stage lines, possibly partial>],
         "crashed_at": <stage name the child died in, when the exit was a
                        native crash; else None>,
         "reason": None | "crash" | "timeout" | "no-interpreter" |
                   "worker-error"}

    A stage may also fail CLEANLY (the worker emits ``ok: false`` with an
    error and exits 0) — that is data in ``stages``, not a ``reason``.
    Never raises.
    """
    if interpreter is None:
        interpreter = _worker_python()
    cmd = [interpreter, _WORKER_PATH, "load_test"]
    try:
        result = subprocess.run(
            cmd,
            input="{}",
            capture_output=True,
            text=True,
            timeout=timeout,
            **NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        return {
            "stages": _parse_stage_lines(stdout),
            "crashed_at": None,
            "reason": "timeout",
        }
    except FileNotFoundError:
        return {"stages": [], "crashed_at": None, "reason": "no-interpreter"}
    stdout = result.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    stages = _parse_stage_lines(stdout)
    report: dict = {"stages": stages, "crashed_at": None, "reason": None}
    if result.returncode != 0:
        if _looks_like_native_crash(result.returncode):
            report["reason"] = "crash"
            report["crashed_at"] = _stage_after(stages)
        else:
            report["reason"] = "worker-error"
    return report


def diagnose(timeout: float = 25.0) -> dict:
    """Full self-check of the Resolve bridge, for ``monteur resolve doctor``.

    Reports which interpreter the isolated worker uses and where it came from
    (``interpreter_source``: ``"env"`` — the MONTEUR_RESOLVE_PYTHON override,
    ``"settings"`` — saved by Studio's "Find a compatible Python" button, or
    ``"default"`` — Monteur's own Python), that interpreter's version and
    bitness, where Resolve's scripting module and native library were found,
    the Resolve-relevant environment variables (with quoted/stale flags),
    the live status probe, and a plain-language verdict. When the status
    probe reports a native crash, the staged :func:`load_test_isolated` runs
    too (``"load_test"`` in the report, else None) so the verdict can name
    the exact crash site. Never raises.
    """
    interpreter, source = _worker_python_source()
    report: dict = {
        "monteur_resolve_python": os.environ.get("MONTEUR_RESOLVE_PYTHON") or None,
        "worker_interpreter": interpreter,
        "interpreter_source": source,
    }
    info_ok, info = _run_worker("info", timeout)
    if info_ok:
        report["info"] = info
    else:
        report["info"] = None
        report["info_error"] = info
    status = resolve_status_isolated(timeout=timeout)
    report["status"] = status
    load_report = None
    if status.get("reason") == "crash":
        # The plain probe only says "crashed". Run the staged load test to
        # pinpoint WHERE (dll-load / import / connect) — the child streams a
        # JSON line per completed stage, so even a hard crash leaves a trail.
        load_report = load_test_isolated(timeout=timeout)
    report["load_test"] = load_report
    report["verdict"] = _diagnosis_verdict(
        source, info if info_ok else None, status, load_report
    )
    return report


# Human wording for _worker_python_source()'s source tags, used in verdicts.
_SOURCE_DESC = {
    "env": "the Python set by the MONTEUR_RESOLVE_PYTHON environment variable",
    "settings": "the Python saved in Monteur's settings",
    "default": "Monteur's own Python",
}


# How to remove a broken environment variable, appended to every env-var
# verdict. Plain Windows steps because that is where the quoting/staleness
# problems happen — no CLI, matching the app's fix-it-in-the-UI tone.
_ENV_REMOVE_HINT = " (Windows: search 'environment variables' in the Start menu.)"


def _looks_resolveish(path: str) -> bool:
    """Heuristic: does a path look like it points at Resolve's scripting files?"""
    lower = (path or "").lower()
    return "resolve" in lower or "blackmagic" in lower


def _env_issue_verdict(info) -> str:
    """The verdict for a broken Resolve env variable, or "" when none is.

    Checks the crash-free ``info`` report's ``env`` block for the two
    real-world misconfigurations that make fusionscript's load crash even
    under a perfectly compatible Python: values wrapped in quotation marks
    (Blackmagic's docs show quoted examples; pasted into the Windows dialog
    the quotes become part of the value and break loading) and values
    pointing at paths that no longer exist (stale after a Resolve update or
    uninstall). PYTHONPATH is only flagged when it is quoted or when a
    MISSING entry looks Resolve-related — unrelated stale entries are not
    our diagnosis to make.
    """
    env = (info or {}).get("env") or {}
    for name in ("RESOLVE_SCRIPT_LIB", "RESOLVE_SCRIPT_API"):
        entry = env.get(name) or {}
        if not entry.get("value"):
            continue
        if entry.get("quoted"):
            return (
                f"Your {name} environment variable has quotation marks "
                "around the path — Windows can't load it that way. Remove "
                "the variable (Monteur finds Resolve by itself) or remove "
                "the quotes." + _ENV_REMOVE_HINT
            )
        if entry.get("exists") is False:
            return (
                f"Your {name} environment variable points to a path that "
                f"doesn't exist ({_strip_env_quotes(str(entry['value']))}). "
                "Delete the stale variable — Monteur then finds Resolve's "
                "standard install by itself." + _ENV_REMOVE_HINT
            )
    pythonpath = env.get("PYTHONPATH") or {}
    if pythonpath.get("value"):
        if pythonpath.get("quoted"):
            return (
                "Your PYTHONPATH environment variable has quotation marks "
                "around it — Windows can't use it that way. Remove the "
                "quotes, or delete the Resolve entries from it (Monteur "
                "finds Resolve by itself)." + _ENV_REMOVE_HINT
            )
        stale = [
            p for p in pythonpath.get("missing") or [] if _looks_resolveish(p)
        ]
        if stale:
            return (
                "Your PYTHONPATH environment variable contains a Resolve "
                f"scripting path that doesn't exist ({stale[0]}). Remove "
                "that entry — Monteur finds Resolve by itself."
                + _ENV_REMOVE_HINT
            )
    return ""


def _registry_conflict_verdict(info, load_test) -> str:
    """The verdict for Windows' registered-Python conflict, or "" when N/A.

    The confirmed field mechanism: on Windows fusionscript.dll does not
    bind to the interpreter that imports it — it loads the python DLL of
    the HIGHEST version registered under HKEY_LOCAL_MACHINE\\SOFTWARE\\
    Python\\PythonCore. A registered 3.13+ (or anything newer than a
    compatible 3.10–3.12 worker) therefore hard-crashes the load at
    dll-load/import no matter which interpreter runs the worker. Fires
    only on Windows, only when the census found something, only for a
    crash pinpointed at dll-load or import — and never when the highest
    registered version IS the worker's version (that case falls through
    to the Resolve-release advice).
    """
    if not str((info or {}).get("platform") or "").startswith("win"):
        return ""
    census = (info or {}).get("registered_pythons") or []
    if not census:
        return ""
    if (load_test or {}).get("crashed_at") not in ("dll-load", "import"):
        return ""
    highest = (info or {}).get("registry_highest") or _registry_highest(census)
    top = _parse_py_tag(highest)
    if top is None:
        return ""
    worker = _parse_py_tag((info or {}).get("python_version"))
    if worker is not None and top == worker:
        return ""  # the registry picks the worker's own version — not this rule
    if not (
        top >= (3, 13)
        or (worker is not None and (3, 10) <= worker <= (3, 12) and top > worker)
    ):
        return ""
    path = next(
        (
            e.get("path")
            for e in census
            if e.get("hive") == "HKLM"
            and _parse_py_tag(e.get("version")) == top
            and e.get("path")
        ),
        None,
    )
    if top >= (3, 13):
        why = (
            "which this Resolve release cannot handle (it works with "
            "roughly 3.10–3.12)"
        )
    else:
        why = (
            "which does not match the worker and crashes when loaded into "
            "its process"
        )
    worker_text = (
        f"Python {(info or {}).get('python_version')}"
        if worker is not None
        else "a compatible version"
    )
    verdict = (
        "Resolve's scripting module picks the highest Python registered on "
        f"this machine — that is Python {highest}"
        + (f" ({path})" if path else "")
        + f", {why}. The worker being {worker_text} doesn't help: the "
        f"registry entry wins. Fix: uninstall Python {highest} (Windows "
        "Settings > Apps), or rename its registry key "
        f"HKEY_LOCAL_MACHINE\\SOFTWARE\\Python\\PythonCore\\{highest} to "
        "hide it from Resolve. Monteur itself runs fine on any Python 3.10+."
    )
    hkcu = _parse_py_tag(_registry_highest(census, hive="HKCU"))
    if hkcu is not None and hkcu > top:
        verdict += (
            " A per-user Python %d.%d is registered even higher "
            "(HKEY_CURRENT_USER) and may need the same treatment." % hkcu
        )
    return verdict


def _diagnosis_verdict(source, info, status, load_test=None) -> str:
    """One plain-language sentence (or three) summing up the Resolve bridge.

    ``source`` is the ``_worker_python_source()`` tag — every verdict says
    which interpreter was used, and every fixable problem points at ONE
    plain fix. For a native crash the causes are checked most-certain-first:
    an incompatible interpreter (too new / 32-bit), the Windows registry
    mechanism (a too-new Python registered in HKLM is what fusionscript
    actually loads — see :func:`_registry_conflict_verdict`), a broken
    environment variable (quoted or stale — see :func:`_env_issue_verdict`),
    then the
    staged ``load_test`` pinpoint (crashed at dll-load → the Resolve release
    doesn't support this Python, update Resolve; crashed at import → module
    files and library from different Resolve versions; library not found →
    Resolve isn't installed where Monteur looks). Only when the crash site
    says the library itself rejected a plausible Python does the verdict
    suggest trying another Python version. See :func:`diagnose`.
    """
    who = _SOURCE_DESC.get(source, _SOURCE_DESC["default"])
    if status.get("connected"):
        return (
            f"Connected to Resolve (project {status.get('project')!r}). "
            f"The live integration is working, using {who}."
        )
    reason = status.get("reason")
    version = (info or {}).get("python_version", "?")
    bits = (info or {}).get("bits")
    if reason == "crash":
        major_minor = version.rsplit(".", 1)[0] if version != "?" else "?"
        too_new = version != "?" and tuple(
            int(x) for x in version.split(".")[:2]
        ) >= (3, 13)
        base = (
            f"Resolve's module crashed loading under Python {version}"
            + (f" ({bits}-bit)" if bits else "")
            + f" — {who}. "
        )
        if too_new:
            return base + (
                f"Python {major_minor} is too new for Resolve "
                "(current Resolve releases work with roughly 3.10–3.12). "
                + _APP_FIX
            )
        if bits == 32:
            return base + (
                "That Python is 32-bit; Resolve needs a 64-bit Python. "
                + _APP_FIX
            )
        # The Windows registry mechanism outranks env-var and crash-site
        # guessing: when a too-new registered Python is what fusionscript
        # loads, no env cleanup or Resolve update fixes the crash.
        registry_issue = _registry_conflict_verdict(info, load_test)
        if registry_issue:
            return registry_issue
        env_issue = _env_issue_verdict(info)
        if env_issue:
            return base + env_issue
        crashed_at = (load_test or {}).get("crashed_at")
        install = (info or {}).get("resolve_install") or {}
        library = install.get("library")
        if crashed_at == "dll-load":
            return base + (
                "Resolve's own scripting library"
                + (f" ({library})" if library else "")
                + " crashed before Monteur could use it. This usually means "
                f"this Resolve release doesn't support Python {version} — "
                "update DaVinci Resolve (Studio updates are free), and if it "
                "still crashes, try Python 3.10."
            )
        if crashed_at == "import":
            return base + (
                "The library itself loaded, but Resolve's Python module "
                "(DaVinciResolveScript) crashed — its Scripting/Modules "
                "files may come from a different Resolve version than the "
                "library (this happens when RESOLVE_SCRIPT_API / "
                "RESOLVE_SCRIPT_LIB point across versions). Update DaVinci "
                "Resolve and remove those variables so the installed "
                "version's own files are used."
            )
        if crashed_at == "connect":
            return base + (
                "The module loaded, but crashed while connecting to "
                "Resolve. Restart DaVinci Resolve and try again; if it "
                "keeps happening, update Resolve."
            )
        if install and not library:
            return base + (
                "Monteur could not find Resolve's scripting library "
                "(fusionscript) in its standard install location. Install "
                "DaVinci Resolve in its default folder, or point the "
                "RESOLVE_SCRIPT_LIB environment variable at the full path "
                "of fusionscript.dll — without quotation marks."
            )
        return base + (
            "Even a compatible Python can crash if Resolve isn't the expected "
            "version — check that DaVinci Resolve is installed and up to date."
        )
    if reason == "worker-error":
        return (
            "The isolated helper could not run: "
            f"{status.get('error')}. " + _APP_FIX
        )
    if reason == "no-interpreter":
        if _is_quoted(os.environ.get("MONTEUR_RESOLVE_PYTHON") or ""):
            return (
                f"Monteur could not launch {who} — its value has quotation "
                "marks around the path. Remove the quotes, or delete the "
                "variable and use the settings panel instead."
                + _ENV_REMOVE_HINT
            )
        return f"Monteur could not launch {who}. " + _APP_FIX
    if reason == "timeout":
        return "Resolve did not respond in time — it may be busy or mid-render."
    # Clean 'not connected' — module loaded fine, Resolve just isn't reachable.
    return (
        f"The interpreter (Python {version}"
        + (f", {bits}-bit" if bits else "")
        + f", {who}) loaded Resolve's module fine, but no running Resolve "
        "was reached: "
        + str(status.get("error", "is Resolve running with scripting set to Local?"))
        + " Note: driving Resolve from outside (what Monteur does) needs "
        "DaVinci Resolve Studio — the free edition only runs scripts from "
        "inside Resolve's own menus."
    )


# --- Finding a compatible worker Python ----------------------------------------
#
# The product rule behind this block: end users never see a CLI or set
# environment variables. When Monteur runs under a Python that Resolve's
# native module can't survive (3.13+, or 32-bit), Studio's settings panel
# calls find_resolve_python() below, which walks the machine's Python
# installations and safely probes each one in a child process; the endpoint
# then SAVES the winner to settings so every later Resolve call uses it.

# Names tried on PATH, best-first: current Resolve releases work with roughly
# Python 3.10–3.12 (older Resolve versions accepted 3.6+). 3.11 is the safest
# single choice, then 3.12, then 3.10, then the legacy range.
_WHICH_NAMES = (
    "python3.11", "python3.12", "python3.10", "python3.9", "python3.8",
    "python3.7", "python3.6", "python3", "python",
)

# Probing runs one or two subprocesses per candidate — keep the total bounded.
_MAX_CANDIDATES = 15


def _parse_py_launcher_output(text: str) -> list[str]:
    """Interpreter paths from ``py -0p`` / ``py --list-paths`` output.

    The launcher prints one line per install, tag first, path last, e.g.::

         -V:3.11 *        C:\\Users\\me\\AppData\\...\\Python311\\python.exe
         -3.10-64         C:\\Python310\\python.exe

    Formats differ across launcher versions, so this just takes everything
    from the drive letter onward on lines that end in a python executable.
    Pure and Windows-free for testability.
    """
    import re

    paths: list[str] = []
    for line in (text or "").splitlines():
        match = re.search(r"[A-Za-z]:\\.*python[.\w]*\.exe", line, re.IGNORECASE)
        if match:
            paths.append(match.group(0).strip())
    return paths


def _windows_py_launcher_pythons() -> list[str]:
    """Interpreters known to the Windows ``py`` launcher; [] when absent.

    Every step is guarded — a missing launcher, a hung launcher (short
    timeout) or unparseable output just yields no candidates, never an
    error.
    """
    launcher = shutil.which("py")
    if not launcher:
        return []
    for flag in ("-0p", "--list-paths"):
        try:
            result = subprocess.run(
                [launcher, flag], capture_output=True, text=True, timeout=10, **NO_WINDOW
            )
        except Exception:  # noqa: BLE001 - discovery must never raise
            continue
        paths = _parse_py_launcher_output(
            (result.stdout or "") + "\n" + (result.stderr or "")
        )
        if paths:
            return paths
    return []


def _windows_registry_pythons() -> list[str]:
    """PEP 514 registry scan: Software\\Python\\<Company>\\<Tag>\\InstallPath.

    Checks HKCU and HKLM in both 64- and 32-bit registry views. Prefers the
    ``ExecutablePath`` value, falling back to ``<install dir>\\python.exe``.
    Fully guarded — any registry hiccup yields fewer candidates, never an
    error. Returns [] off Windows (no ``winreg``).
    """
    try:
        import winreg
    except ImportError:
        return []
    paths: list[str] = []
    views = {0, getattr(winreg, "KEY_WOW64_64KEY", 0),
             getattr(winreg, "KEY_WOW64_32KEY", 0)}
    try:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in views:
                try:
                    software = winreg.OpenKey(
                        root, r"Software\Python", 0, winreg.KEY_READ | view
                    )
                except OSError:
                    continue
                with software:
                    for company in _registry_subkeys(winreg, software):
                        try:
                            company_key = winreg.OpenKey(
                                software, company, 0, winreg.KEY_READ | view
                            )
                        except OSError:
                            continue
                        with company_key:
                            for tag in _registry_subkeys(winreg, company_key):
                                try:
                                    install = winreg.OpenKey(
                                        company_key,
                                        tag + r"\InstallPath",
                                        0,
                                        winreg.KEY_READ | view,
                                    )
                                except OSError:
                                    continue
                                with install:
                                    path = _registry_python_exe(winreg, install)
                                    if path:
                                        paths.append(path)
    except Exception:  # noqa: BLE001 - discovery must never raise
        pass
    return paths


def _registry_subkeys(winreg, key) -> list[str]:
    """All subkey names of an open registry key; [] on any failure."""
    names: list[str] = []
    try:
        count = winreg.QueryInfoKey(key)[0]
        for index in range(count):
            names.append(winreg.EnumKey(key, index))
    except OSError:
        pass
    return names


def _registry_python_exe(winreg, install_key) -> str:
    """python.exe path from an open PEP 514 InstallPath key, or ""."""
    try:
        exe = winreg.QueryValueEx(install_key, "ExecutablePath")[0]
        if exe:
            return str(exe)
    except OSError:
        pass
    try:
        install_dir = winreg.QueryValue(install_key, None)
        if install_dir:
            return os.path.join(str(install_dir), "python.exe")
    except OSError:
        pass
    return ""


def _windows_wellknown_pythons() -> list[str]:
    """Standard python.org install locations on Windows, best-first
    (3.11, then 3.12, then 3.10, then the legacy range down to 3.6):
    per-user %LOCALAPPDATA%\\Programs\\Python, the legacy C:\\Python3XX,
    and the Program Files variants."""
    paths: list[str] = []
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get(
        "PROGRAMFILES(X86)", r"C:\Program Files (x86)"
    )
    for minor in (11, 12, 10, 9, 8, 7, 6):
        folder = f"Python3{minor}"
        if local:
            paths.append(
                os.path.join(local, "Programs", "Python", folder, "python.exe")
            )
        paths.append(os.path.join(rf"C:\{folder}", "python.exe"))
        paths.append(os.path.join(program_files, folder, "python.exe"))
        paths.append(os.path.join(program_files_x86, folder, "python.exe"))
    return paths


def _candidate_pythons() -> list[str]:
    """Ordered, deduped, existing interpreter paths worth probing.

    Order (most explicit first): the MONTEUR_RESOLVE_PYTHON env override,
    the path saved in Monteur's settings, Windows discovery (py launcher,
    PEP 514 registry, well-known install folders), PATH lookups for
    python3.11 / python3.12 / python3.10 … python3.6 / python3 / python,
    and finally the interpreter
    running Monteur. Duplicates (after symlink resolution + case folding)
    and non-existent files are dropped; the list is capped at
    ``_MAX_CANDIDATES`` so probing stays bounded. Never raises.
    """
    raw: list[str] = []
    env = (os.environ.get("MONTEUR_RESOLVE_PYTHON") or "").strip()
    if env:
        raw.append(env)
    try:
        from monteur.settings import resolve_python

        saved = resolve_python()
    except Exception:  # noqa: BLE001 - settings must never break discovery
        saved = ""
    if saved:
        raw.append(saved)
    if sys.platform.startswith("win"):
        raw.extend(_windows_py_launcher_pythons())
        raw.extend(_windows_registry_pythons())
        raw.extend(_windows_wellknown_pythons())
    for name in _WHICH_NAMES:
        found = shutil.which(name)
        if found:
            raw.append(found)
    raw.append(sys.executable)

    seen: set[str] = set()
    candidates: list[str] = []
    for path in raw:
        if not path or not os.path.isfile(path):
            continue
        try:
            key = os.path.normcase(os.path.realpath(path))
        except OSError:
            key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)
        if len(candidates) >= _MAX_CANDIDATES:
            break
    return candidates


def probe_resolve_python(path: str, timeout: float = 12.0) -> dict:
    """Safely check whether ``path`` is a Resolve-compatible interpreter.

    Two stages, both in child processes, never raising:

    1. the worker's ``info`` command — crash-free by design (it never
       imports Resolve's native module). An interpreter that can't even run
       it fails with its ``_run_worker`` reason (``"no-interpreter"``,
       ``"worker-error"``, …). A version >= 3.13 or 32 bits is rejected as
       ``{"ok": False, "reason": "incompatible"}`` — carrying ``version``/
       ``bits`` so the UI list stays honest — WITHOUT ever attempting the
       native load (no point crashing a child we know can't work; 3.12 IS
       attempted, current Resolve releases support it);
    2. the worker's ``status`` command — the real native-module load and
       connect attempt. A native crash yields ``{"ok": False, "reason":
       "crash"}``. A clean result means the interpreter is compatible:
       ``{"ok": True, "connected": True, "project": ...}`` when Resolve was
       reached, ``{"ok": True, "connected": False}`` when Resolve is simply
       closed / not listening (still a perfectly good interpreter).

    Every result carries ``version``/``bits`` once stage 1 succeeded.
    """
    ok, info = _run_worker("info", timeout, interpreter=path)
    if not ok:
        return {
            "ok": False,
            "reason": info.get("reason", "no-interpreter"),
            "error": str(info.get("error") or ""),
        }
    version = str(info.get("python_version") or "")
    bits = info.get("bits")
    try:
        major_minor = tuple(int(part) for part in version.split(".")[:2])
    except ValueError:
        major_minor = ()
    if (major_minor and major_minor >= (3, 13)) or bits == 32:
        return {
            "ok": False,
            "reason": "incompatible",
            "version": version,
            "bits": bits,
        }
    ok, status = _run_worker("status", timeout, interpreter=path)
    if not ok:
        return {
            "ok": False,
            "reason": status.get("reason", "crash"),
            "version": version,
            "bits": bits,
            "error": str(status.get("error") or ""),
        }
    if status.get("connected"):
        return {
            "ok": True,
            "connected": True,
            "version": version,
            "bits": bits,
            "project": status.get("project"),
        }
    return {
        "ok": True,
        "connected": False,
        "version": version,
        "bits": bits,
        "error": str(status.get("error") or ""),
    }


def find_resolve_python(timeout_per: float = 10.0) -> dict:
    """Probe the machine's Pythons and return the first Resolve-compatible one.

    Walks :func:`_candidate_pythons` through :func:`probe_resolve_python`,
    stopping at the first compatible interpreter (``"ok": True`` — whether
    or not Resolve is currently running). Returns::

        {"found": <path or None>, "connected": <bool>,
         "probed": [{"path": ..., plus the probe result}, ...]}

    ``probed`` lists every candidate tried, in order, so the UI can show
    what happened. This function only LOOKS — persisting the find into
    settings is the caller's job (Studio's ``POST /api/resolve/detect``
    endpoint saves it). Never raises.
    """
    probed: list[dict] = []
    found: str | None = None
    connected = False
    for path in _candidate_pythons():
        result = probe_resolve_python(path, timeout=timeout_per)
        entry = {"path": path}
        entry.update(result)
        probed.append(entry)
        if result.get("ok"):
            found = path
            connected = bool(result.get("connected"))
            break
    return {"found": found, "connected": connected, "probed": probed}


def read_timeline_isolated(name: str | None = None, timeout: float = 40.0) -> Timeline:
    """Read a Resolve timeline in a child process and rebuild it as a Timeline.

    Runs the worker's ``read_timeline`` command (optionally for a named
    timeline) and reconstructs the :class:`~monteur.model.Timeline` from the
    JSON it emits. Raises :class:`MonteurResolveError` on any failure — a
    handled Resolve error, or an uncatchable native crash (whose message
    explains the Python-compatibility fix and MONTEUR_RESOLVE_PYTHON). Raising
    is appropriate here because callers of a read expect either a Timeline or
    an error.
    """
    ok, payload = _run_worker("read_timeline", timeout, request={"name": name})
    if not ok:
        raise MonteurResolveError(payload["error"])
    if not payload.get("ok"):
        raise MonteurResolveError(
            payload.get("error", "The Resolve worker could not read the timeline.")
        )
    timeline_data = payload.get("timeline")
    if not isinstance(timeline_data, dict):
        raise MonteurResolveError(
            "The Resolve worker did not return a timeline payload."
        )
    return _timeline_from_dict(timeline_data)


def build_plan_isolated(
    plan,
    fps: float,
    name: str = "Monteur Montage",
    titles: list[dict] | None = None,
    canvas: str | None = None,
    audio: str = "music",
    mode: str = "hybrid",
    timeout: float = 180.0,
) -> dict:
    """Build a montage timeline in Resolve from a child process; never raises.

    THE recommended path for any long-running host (Monteur Studio, the MCP
    server): the plan is serialized with :func:`monteur.montage.plan_to_dict`,
    handed to the isolated worker's ``build_plan`` command on stdin, and every
    Resolve scripting call — including the native module load that can
    hard-crash an incompatible interpreter — happens in a disposable child
    process (honoring ``MONTEUR_RESOLVE_PYTHON``, see :func:`_worker_python`).
    A native crash only kills the child; the caller gets a graceful failure
    dict instead of dying. The in-process
    :meth:`ResolveBridge.build_timeline_from_plan` remains for direct library
    use (e.g. scripts already running inside Resolve's own interpreter).

    ``titles`` (optional, ``[{"start": s, "duration": s, "text": ...}]`` in
    plan-time seconds, e.g. from :func:`titles_from_plan`) are inserted after
    the build, exactly as ``build_timeline_from_plan`` does. ``canvas``
    (optional, a :data:`monteur.montage.CANVASES` preset key) sizes the
    built timeline like the file exports do — cinemascope presets also set
    "scale full frame with crop" on the footage; see
    :meth:`ResolveBridge.build_timeline_from_plan`. ``audio`` (the montage
    audio mode, default ``"music"``) picks the sound layout of the built
    timeline (and, in the append fallback, the SFX track for placed sound
    elements — index 3 in "mix", index 2 otherwise). ``mode`` (default
    ``"hybrid"``) picks the build path: hybrid writes the plan as FCPXML
    and imports it (dissolves, fades, gaps and every audio lane arrive
    with the file), then finishes titles/canvas via the API, falling back
    to the clip-by-clip ``"append"`` build — with one warning — when this
    Resolve refuses the import; ``mode="append"`` forces the old
    clip-by-clip build outright. Workers without a ``"mode"`` key in the
    request behave as hybrid (backward-compatible wire format).

    Returns ``{"ok": True, "timeline": <created name>, "warnings": [...]}``
    — ``warnings`` are the non-fatal messages from
    :meth:`ResolveBridge.add_titles` (title placement) and the canvas
    application — or ``{"ok": False, "error": <message>}``. When the
    worker died of an uncatchable native crash the failure additionally
    carries ``"reason": "native-crash"`` and the error explains the fix: set
    MONTEUR_RESOLVE_PYTHON to a Resolve-compatible interpreter (roughly
    Python 3.10–3.12 for current Resolve releases). Other worker-launch
    failures pass their
    :func:`_run_worker` reason through ("timeout", "no-interpreter",
    "worker-error", "bad-output"); a clean, handled Resolve error carries the
    worker's own message and no reason.
    """
    from monteur.montage import plan_to_dict  # lazy: keeps this module
    # importable by the stdlib-only worker bootstrap (montage needs numpy)

    request = {
        "plan": plan_to_dict(plan),
        "fps": float(fps),
        "name": name,
        "titles": titles,
        "canvas": canvas,
        "audio": audio,
        "mode": mode,
    }
    ok, payload = _run_worker("build_plan", timeout, request=request)
    if not ok:
        result = {"ok": False, **payload}
        if result.get("reason") == "crash":
            result["reason"] = "native-crash"
        return result
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get(
                "error", "The Resolve worker could not build the timeline."
            ),
        }
    payload.setdefault("warnings", [])
    return payload


def _stream_worker(
    command: str,
    timeout: float,
    request: dict | None = None,
    interpreter: str | None = None,
    on_line=None,
) -> tuple[bool, list[dict], dict]:
    """Run one STREAMED worker command, reading its stdout while it runs.

    The streamed counterpart of :func:`_run_worker` (which collects output
    at the end): the child is launched with ``subprocess.Popen``, the
    request is written to its stdin, and stdout is consumed line by line AS
    THE CHILD RUNS — each parseable JSON-object line is appended to the
    returned list and (when given) passed to ``on_line(payload)``. That is
    what lets a caller show live render progress instead of a frozen bar.

    Returns ``(ok, lines, failure)``:

    * ``(True, lines, {})`` — the child exited 0; ``lines`` holds every
      parsed JSON line (unparseable lines are skipped, like
      :func:`_parse_stage_lines` does).
    * ``(False, lines, failure)`` — ``failure`` carries ``error`` and a
      ``reason`` of ``"crash"`` (native-crash exit code), ``"timeout"``
      (the deadline passed; the child was killed), ``"no-interpreter"`` or
      ``"worker-error"``, mirroring :func:`_run_worker` exactly. Even on
      failure, ``lines`` holds whatever the child managed to stream first.

    Timeout is enforced with a watchdog timer (the read loop itself blocks
    on the pipe). An exception raised BY ``on_line`` propagates to the
    caller — that is the cooperative-cancel seam — but the child process is
    always killed first, so no orphaned worker lingers. Existing callers of
    ``_run_worker`` are untouched.
    """
    import tempfile
    import threading

    if interpreter is None:
        interpreter = _worker_python()
    cmd = [interpreter, _WORKER_PATH, command]
    # stderr goes to a spool file, not a pipe: only stdout is read while the
    # child runs, and a long command (a render can take hours) with a chatty
    # native module could otherwise fill the stderr pipe and deadlock.
    stderr_spool = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_spool,
            text=True,
            **NO_WINDOW,
        )
    except FileNotFoundError:
        stderr_spool.close()
        return False, [], {
            "error": (
                f"Could not launch the Resolve worker interpreter {interpreter!r}. "
                "Open Studio's settings (gear) > DaVinci Resolve and click "
                "“Find a compatible Python”, or fix the saved path there. "
                "(Advanced: MONTEUR_RESOLVE_PYTHON overrides the saved choice.)"
            ),
            "reason": "no-interpreter",
        }
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()
    lines: list[dict] = []
    completed = False
    try:
        try:
            proc.stdin.write(json.dumps(request or {}))
            proc.stdin.close()
        except (OSError, ValueError):
            pass  # child died instantly — the exit code tells the story
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            lines.append(payload)
            if on_line is not None:
                on_line(payload)
        proc.wait()
        completed = True
    finally:
        watchdog.cancel()
        if not completed:  # on_line raised (cancel) — never orphan the child
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait()
                except OSError:
                    pass
            stderr_spool.close()
    if timed_out.is_set():
        stderr_spool.close()
        return False, lines, {
            "error": (
                f"The Resolve worker did not finish within {timeout:g}s and "
                "was terminated."
            ),
            "reason": "timeout",
        }
    if proc.returncode != 0:
        if _looks_like_native_crash(proc.returncode):
            stderr_spool.close()
            return False, lines, {"error": _CRASH_MESSAGE, "reason": "crash"}
        stderr = ""
        try:
            stderr_spool.seek(0)
            stderr = stderr_spool.read() or ""
        except (OSError, ValueError):
            pass
        finally:
            stderr_spool.close()
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        detail = tail or f"exit code {proc.returncode}"
        return False, lines, {
            "error": (
                f"The Resolve helper failed to run under {interpreter!r}: "
                f"{detail}. Make sure that interpreter is a working Python 3 "
                "(the helper needs only the standard library)."
            ),
            "reason": "worker-error",
        }
    stderr_spool.close()
    return True, lines, {}


def render_isolated(
    timeline: str | None,
    target_dir: str,
    name: str,
    preset: str | None = None,
    timeout: float = 7200.0,
    progress=None,
) -> dict:
    """Render a Resolve timeline to a finished video file; never raises.

    The last step of "media in, finished video out": runs the worker's
    streamed ``render`` command (see ``monteur._resolve_worker`` for the
    wire format) through :func:`_stream_worker`, so the child's per-event
    JSON lines are consumed WHILE the render runs. ``progress`` (optional)
    is called with each new integer percent as Resolve reports it.

    ``timeline`` (None = the current one) is rendered through Resolve's own
    Deliver engine into ``target_dir`` (created if missing) as ``name``:
    a shipped render preset matched loosely against ``preset`` ("2160p",
    the default, or "1080p") when one exists, else an mp4/H.264 fallback.

    Returns ``{"ok": True, "path": <file>, "seconds": <wall time>,
    "preset": <what was actually chosen>}`` or ``{"ok": False, "error":
    <message>}`` — with the same graceful ``reason`` classification as the
    other ``*_isolated`` functions on a native crash ("native-crash"),
    timeout, missing interpreter or garbage output. On a timeout the child
    monitor is killed but the render CONTINUES inside Resolve (the error
    message says so) — Monteur only ever watches, Resolve does the work.
    Like ``_stream_worker``, an exception raised by ``progress`` itself
    propagates (after the child is killed) — the cooperative-cancel seam.
    """
    request = {
        "timeline": timeline,
        "target_dir": target_dir,
        "name": name,
        "preset": preset,
    }
    prepare: dict = {}

    def on_line(payload: dict) -> None:
        stage = payload.get("stage")
        if stage == "prepare" and payload.get("ok"):
            prepare.update(payload)
        elif stage == "progress" and progress is not None:
            try:
                percent = int(payload.get("percent"))
            except (TypeError, ValueError):
                return
            progress(percent)

    ok, lines, failure = _stream_worker(
        "render", timeout, request=request, on_line=on_line
    )
    if not ok:
        result = {"ok": False, **failure}
        if result.get("reason") == "crash":
            result["reason"] = "native-crash"
        elif result.get("reason") == "timeout":
            result["error"] = str(result.get("error") or "") + (
                " The render itself continues inside Resolve — check the "
                "Deliver page there."
            )
        return result
    terminal = next(
        (
            line
            for line in reversed(lines)
            if line.get("stage") == "done" or line.get("ok") is False
        ),
        None,
    )
    if terminal is None:
        return {
            "ok": False,
            "error": (
                "The Resolve render worker ended without reporting a result."
            ),
            "reason": "bad-output",
        }
    if terminal.get("ok") and terminal.get("stage") == "done":
        return {
            "ok": True,
            "path": terminal.get("path"),
            "seconds": terminal.get("seconds"),
            "preset": prepare.get("preset"),
        }
    return {
        "ok": False,
        "error": str(
            terminal.get("error") or "DaVinci Resolve could not render the video."
        ),
    }


# --- Timeline (de)serialization ------------------------------------------------
#
# Plain-dict, JSON-safe representation of a Timeline so it can cross the
# worker/parent process boundary and be rebuilt exactly.


def _clip_to_dict(clip: Clip) -> dict:
    return {
        "name": clip.name,
        "track": clip.track,
        "kind": clip.kind,
        "source_in": clip.source_in,
        "source_out": clip.source_out,
        "record_in": clip.record_in,
        "record_out": clip.record_out,
        "source_name": clip.source_name,
        "metadata": dict(clip.metadata),
    }


def _clip_from_dict(data: dict) -> Clip:
    return Clip(
        name=data["name"],
        track=data.get("track", "V1"),
        kind=data.get("kind", VIDEO),
        source_in=int(data.get("source_in", 0)),
        source_out=int(data.get("source_out", 0)),
        record_in=int(data.get("record_in", 0)),
        record_out=int(data.get("record_out", 0)),
        source_name=data.get("source_name", ""),
        metadata=dict(data.get("metadata", {})),
    )


def _marker_to_dict(marker: Marker) -> dict:
    return {
        "frame": marker.frame,
        "name": marker.name,
        "note": marker.note,
        "color": marker.color,
    }


def _marker_from_dict(data: dict) -> Marker:
    return Marker(
        frame=int(data["frame"]),
        name=data.get("name", ""),
        note=data.get("note", ""),
        color=data.get("color", ""),
    )


def _timeline_to_dict(timeline: Timeline) -> dict:
    """Serialize a Timeline to a plain, JSON-safe dict (worker -> parent)."""
    return {
        "name": timeline.name,
        "fps": timeline.fps,
        "clips": [_clip_to_dict(c) for c in timeline.clips],
        "markers": [_marker_to_dict(m) for m in timeline.markers],
        "metadata": dict(timeline.metadata),
    }


def _timeline_from_dict(data: dict) -> Timeline:
    """Rebuild a Timeline from :func:`_timeline_to_dict`'s output."""
    return Timeline(
        name=data["name"],
        fps=float(data["fps"]),
        clips=[_clip_from_dict(c) for c in data.get("clips", [])],
        markers=[_marker_from_dict(m) for m in data.get("markers", [])],
        metadata=dict(data.get("metadata", {})),
    )


class ResolveBridge:
    """Thin wrapper around the Resolve scripting API's current project."""

    def __init__(self, app: Any) -> None:
        if app is None:
            raise MonteurResolveError(
                "ResolveBridge requires a Resolve app object. " + _ENABLE_HINT
            )
        self.app = app

    def _project(self) -> Any:
        manager = self.app.GetProjectManager()
        if manager is None:
            raise MonteurResolveError(
                "Resolve returned no project manager. " + _ENABLE_HINT
            )
        project = manager.GetCurrentProject()
        if project is None:
            raise MonteurResolveError(
                "No project is open in Resolve — open or create a project "
                "in the Project Manager first."
            )
        return project

    def _media_pool(self) -> Any:
        pool = self._project().GetMediaPool()
        if pool is None:
            raise MonteurResolveError(
                f"Project {self.project_name()!r} returned no media pool."
            )
        return pool

    def project_name(self) -> str:
        return self._project().GetName()

    def list_timelines(self) -> list[str]:
        project = self._project()
        names: list[str] = []
        for index in range(1, int(project.GetTimelineCount()) + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline is not None:
                names.append(timeline.GetName())
        return names

    def current_timeline_name(self) -> str:
        return self._current_timeline().GetName()

    def _current_timeline(self) -> Any:
        timeline = self._project().GetCurrentTimeline()
        if timeline is None:
            raise MonteurResolveError(
                f"Project {self.project_name()!r} has no current timeline — "
                "open a timeline in the Edit page first."
            )
        return timeline

    def _timeline_by_name(self, name: str) -> Any:
        project = self._project()
        for index in range(1, int(project.GetTimelineCount()) + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline is not None and timeline.GetName() == name:
                return timeline
        raise MonteurResolveError(
            f"Timeline {name!r} not found in project {self.project_name()!r}. "
            f"Available timelines: {self.list_timelines()}"
        )

    def read_timeline(self, name: str | None = None) -> Timeline:
        """Convert a Resolve timeline (current, or named) to a model Timeline.

        Record positions are normalized to start at frame 0; the original
        timeline start frame (typically 01:00:00:00) is stored in
        ``timeline.metadata["record_start"]``.
        """
        raw = self._current_timeline() if name is None else self._timeline_by_name(name)
        fps = _parse_fps(raw)
        record_start = _record_start(raw)
        clips: list[Clip] = []
        for kind, prefix in ((VIDEO, "V"), (AUDIO, "A")):
            for track_index in range(1, int(raw.GetTrackCount(kind)) + 1):
                items = raw.GetItemListInTrack(kind, track_index) or []
                for item in items:
                    clips.append(
                        _convert_item(item, f"{prefix}{track_index}", kind, record_start)
                    )
        markers = _convert_markers(raw)
        return Timeline(
            name=raw.GetName(),
            fps=fps,
            clips=clips,
            markers=markers,
            metadata={"record_start": record_start},
        )

    def import_timeline_file(self, path: str) -> bool:
        pool = self._media_pool()
        timeline = pool.ImportTimelineFromFile(path)
        if timeline is None:
            raise MonteurResolveError(
                f"Resolve failed to import a timeline from {path!r} into "
                f"project {self.project_name()!r} — check that the file is a "
                "valid EDL/FCPXML/AAF and its media is available."
            )
        return True

    def import_media(self, paths: list[str]) -> int:
        pool = self._media_pool()
        items = pool.ImportMedia(paths)
        if items is None:
            raise MonteurResolveError(
                f"Resolve failed to import media into project "
                f"{self.project_name()!r}: {paths}"
            )
        return len(items)

    def add_markers(
        self, markers: list[Marker], timeline_name: str | None = None
    ) -> int:
        """Add markers to the current (or named) timeline; return the count added.

        Resolve's ``Timeline.AddMarker(frameId, ...)`` takes frames RELATIVE
        to the timeline start (even when the timeline starts at e.g.
        01:00:00:00), which matches our 0-based ``Marker.frame`` — so frames
        are passed through unchanged. Marker colors are mapped to the nearest
        Resolve marker color name ("Blue" when unknown). Markers Resolve
        rejects (e.g. duplicate frame) are skipped, not fatal.
        """
        if timeline_name is None:
            timeline = self._current_timeline()
        else:
            timeline = self._timeline_by_name(timeline_name)
            self._project().SetCurrentTimeline(timeline)
        added = 0
        for marker in markers:
            ok = timeline.AddMarker(
                int(marker.frame),
                _marker_color(marker.color),
                marker.name,
                marker.note,
                1,
                "",
            )
            if ok:
                added += 1
        return added

    def add_titles(
        self, titles: list[dict], fps: float, warnings: list[str] | None = None
    ) -> int:
        """Insert Fusion Text+ titles into the current timeline; return the count.

        ``titles``: ``[{"start": seconds, "duration": seconds, "text": str},
        ...]`` with times relative to the timeline start. Typical use: after
        :meth:`build_timeline_from_plan` for a trailer plan, with specs from
        :func:`titles_from_plan`.

        Exact API sequence (every call may be missing or return None on some
        Resolve versions, so each step is checked):

        1. once: ``timeline.GetTrackCount("video")`` and, when only the
           footage track exists, ``timeline.AddTrack("video")`` so titles get
           a track above the picture;
        2. per title: snapshot every video track via ``GetItemListInTrack``,
           then ``timeline.InsertFusionTitleIntoTimeline("Text+")`` — Resolve
           inserts at the playhead; recent versions return the created
           TimelineItem, older ones only True, in which case the new item is
           found by re-scanning the video tracks (topmost first);
        3. ``item.SetStart(frame)`` / ``item.SetEnd(frame)`` to move it onto
           the requested spot — not scriptable on all versions; when
           unavailable the title stays at the playhead and a warning tells
           the user to drag it onto the black gap;
        4. ``item.GetFusionCompByIndex(1)``, the Text+ tool from
           ``comp.GetToolList``, then ``tool.SetInput("StyledText", text)``.

        A step failing softly (None/False) appends a human-readable message
        to ``warnings`` (pass a list to collect them) and the loop continues:
        a partial failure NEVER raises, and a title with the right text at
        the wrong spot still counts as inserted — that beats no title. Only
        an exception thrown by Resolve's native API raises, converted to
        :class:`MonteurResolveError`.
        """
        if warnings is None:
            warnings = []
        if not titles:
            return 0
        inserted = 0
        try:
            timeline = self._current_timeline()
            _ensure_title_track(timeline, warnings)
            record_start = _record_start(timeline)
            for index, spec in enumerate(titles):
                if not isinstance(spec, dict):
                    warnings.append(
                        f"title {index + 1}: expected a dict like "
                        f"{{'start': s, 'duration': s, 'text': ...}}, got "
                        f"{spec!r} — skipped."
                    )
                    continue
                text = str(spec.get("text") or "").strip() or "Title"
                label = f"title {index + 1} ({text!r})"
                try:
                    start = float(spec.get("start", 0.0))
                    duration = float(spec.get("duration", 0.0))
                except (TypeError, ValueError):
                    warnings.append(
                        f"{label}: invalid start/duration in {spec!r} — skipped."
                    )
                    continue
                # Resolve inserts Fusion titles AT THE PLAYHEAD, and several
                # builds (Resolve 21 among them) refuse to reposition
                # timeline items afterwards — so move the playhead to the
                # title's spot FIRST. The SetStart/SetEnd path below stays
                # as a correction for builds that support it.
                start_frame = int(record_start) + int(round(start * fps))
                moved = _move_playhead(timeline, start_frame, fps)
                before = _video_track_snapshot(timeline)
                result = timeline.InsertFusionTitleIntoTimeline("Text+")
                if not result:
                    warnings.append(
                        f"{label}: Resolve did not insert a Fusion Text+ title "
                        "(InsertFusionTitleIntoTimeline returned nothing) — "
                        "this Resolve version/page may not support scripted "
                        f"Fusion titles; add it by hand at {start:.1f}s."
                    )
                    continue
                inserted += 1
                # Recent Resolve versions return the created TimelineItem;
                # older ones return True and leave it at the playhead.
                item = result if hasattr(result, "GetFusionCompByIndex") else None
                if item is None:
                    item = _find_new_video_item(timeline, before)
                if item is None:
                    if moved:
                        warnings.append(
                            f"{label}: inserted at {start:.1f}s (the "
                            "playhead), but Monteur could not locate the new "
                            "timeline item — set its text by hand."
                        )
                    else:
                        warnings.append(
                            f"{label}: inserted, but Monteur could not locate "
                            "the new timeline item — Resolve placed the title "
                            "at the playhead; set its text and drag it onto "
                            f"the black gap at {start:.1f}s."
                        )
                    continue
                end_frame = start_frame + max(1, int(round(duration * fps)))
                set_start = getattr(item, "SetStart", None)
                set_end = getattr(item, "SetEnd", None)
                if callable(set_start) and callable(set_end):
                    ok_start = set_start(start_frame)
                    ok_end = set_end(end_frame)
                    if not (ok_start and ok_end) and not moved:
                        warnings.append(
                            f"{label}: Resolve placed the title at the "
                            "playhead — drag it onto the black gap at "
                            f"{start:.1f}s."
                        )
                elif not moved:
                    warnings.append(
                        f"{label}: this Resolve version cannot reposition "
                        "timeline items via scripting — the title sits at the "
                        f"playhead; drag it onto the black gap at {start:.1f}s."
                    )
                get_comp = getattr(item, "GetFusionCompByIndex", None)
                comp = get_comp(1) if callable(get_comp) else None
                if comp is None:
                    warnings.append(
                        f"{label}: could not open the title's Fusion "
                        "composition — double-click the title in Resolve and "
                        "set the text by hand."
                    )
                    continue
                tool = _find_text_plus_tool(comp)
                if tool is None:
                    warnings.append(
                        f"{label}: no Text+ tool found in the title's Fusion "
                        "composition — set the text by hand."
                    )
                    continue
                # Fusion's SetInput returns None even on success — only an
                # exception (handled below) means it failed.
                tool.SetInput("StyledText", text)
                # the picked title animation (blueprint 1.7) — best effort;
                # a host that can't animate keeps the static title + a note
                anim = str(spec.get("anim") or "none")
                if anim in ("fade", "slide", "type"):
                    if not _apply_title_anim(tool, comp, anim, duration, fps):
                        warnings.append(
                            f"{label}: this Resolve/Fusion build could not "
                            f"script the '{anim}' title animation — the title "
                            "is placed correctly but stays static; animate it "
                            "by hand if you want the motion."
                        )
        except MonteurResolveError:
            raise
        except Exception as exc:  # noqa: BLE001 - native API misbehaving
            raise MonteurResolveError(
                "Resolve's scripting API failed while inserting Fusion titles "
                f"({inserted} of {len(titles)} made it in before the failure): "
                f"{type(exc).__name__}: {exc}. The timeline itself is intact — "
                "add the remaining titles by hand at the 'Title slot' markers."
            ) from exc
        return inserted

    def build_timeline_from_plan(
        self,
        plan,
        fps: float,
        name: str = "Monteur Montage",
        titles: list[dict] | None = None,
        canvas: str | None = None,
        warnings: list[str] | None = None,
        audio: str = "music",
        mode: str = "hybrid",
    ) -> str:
        """Build a montage timeline in Resolve from a MontagePlan.

        ``mode`` picks how the timeline gets into Resolve:

        * ``"hybrid"`` (default) — write the plan as an FCPXML file
          (:func:`monteur.montage.montage_to_timeline` +
          :func:`monteur.io.save_timeline`, to a temp file that is always
          removed) and import it via
          ``MediaPool.ImportTimelineFromFile(path, {"timelineName": ...,
          "importSourceClips": True, "sourceClipsPath": <first clip's
          folder>})``, then finish the imported timeline through the API:
          Fusion Text+ titles (:meth:`add_titles`, plan-time positions —
          the imported timeline has the REAL black gaps), the canvas
          (resolution verified against what the file's format element
          already set; the per-clip Scaling crop/fill is ALWAYS applied —
          the one canvas piece the file cannot carry) and a frame-rate
          read-back (the file carries the rate; a differing imported rate
          is trusted, with one warning). Nothing is appended clip-by-clip
          and no music/SFX appends happen — the file already carries every
          audio lane, the dissolves, and the head/tail black fades.
        * ``"append"`` — the clip-by-clip API build described below. Also
          the automatic FALLBACK when this Resolve build refuses the file
          import (None return or an exception): one warning says so, and
          the append flow runs unchanged.

        The honest capability matrix behind the hybrid synthesis: the
        FCPXML file carries dissolves, head/tail black fades, real gaps
        and all audio lanes, but not Fusion Text+ (titles exist only as
        markers there); the append build places everything positionally
        and creates real Text+/crop/resolution, but Resolve's scripting
        API has no way to add transitions or fades. Hybrid = the file's
        transitions + the API's finishing, in one click.

        Append flow: import the distinct clip paths (plus the music) into
        the media pool, create an empty timeline (name uniquified with
        " 2", " 3", ... on a clash), append one video clip per plan entry
        at its record position (``recordFrame`` = timeline start + record
        seconds, so the plan's smash-to-black dips exist as REAL black
        gaps), then append the music as one audio clip at record 0. Source
        ranges are expressed in each media pool item's OWN frame space —
        the clip's native frame rate and its embedded start timecode (see
        :func:`_clip_native_fps` / :func:`_clip_source_offset`);
        timeline-fps frames would make Resolve clamp every cut to a
        sliver. When a Resolve build rejects positioned placement, the
        build falls back to the old gapless append (dips then only exist
        in the file exports; one warning). Returns the created timeline's
        name in every mode.

        ``titles`` (optional, ``[{"start": s, "duration": s, "text": ...}]``
        in plan-time seconds — e.g. from :func:`titles_from_plan`; the caller
        decides the texts, nothing is derived from the plan) are inserted via
        :meth:`add_titles` after the montage is built, at their plan-time
        positions (the black gaps are real — in hybrid because the file
        carries them, in append thanks to recordFrame placement). Only in
        the gapless append fallback are
        title starts shifted earlier by the summed lengths of the dips before
        them, so each title still lands exactly on its act change. Title
        placement problems are non-fatal (see add_titles); pass a
        ``warnings`` list to collect the human-readable messages (it is only
        ever appended to).

        ``canvas`` (optional, a :data:`monteur.montage.CANVASES` preset key
        such as ``"uhd"`` or ``"cine-uhd"``; an unknown key raises
        ValueError before any Resolve work) sizes the built timeline like
        the file exports do. Applied after the build (and titles): the
        timeline resolution is set via timeline-level custom settings
        (``SetSetting("useCustomSettings", "1")`` then the
        ``timelineResolution*`` keys, string values), falling back to the
        project-level ``SetSetting`` when the timeline refuses — in hybrid
        the resolution is VERIFIED first and only set when the imported
        format didn't already match; and every
        video-track-1 clip gets explicit scaling — "scale full frame with
        crop" for the cinemascope presets (16:9 footage fills the 2.39:1
        frame instead of showing side bars), "fill" for everything else
        (mismatched footage never sits small in the frame).
        Both steps are defensive: refusals are summarized into ``warnings``
        (one message per step, never per clip) and the build still succeeds.

        Limitation: dissolves and the head/tail black fades can only reach
        Resolve through the timeline FILE — the scripting API has no way
        to add transitions. The default hybrid mode carries them (that is
        its point); the append mode (or the automatic fallback when the
        import is refused) cannot, and the honest warning points at the
        downloaded/exported file instead.

        Imported media-pool items are mapped back to file paths by, in order:
        ``GetClipProperty("File Path")``; positional order when Resolve
        returned exactly one item per requested path; and finally basename
        matching via ``GetName()``. An unmapped path raises
        MonteurResolveError.

        SFX cues carrying a concrete ``file`` (placed sound elements,
        :mod:`monteur.elements`) are appended (append mode only — the
        hybrid file already carries them as connected clips) after the
        music as audio
        clips at ``recordFrame = timeline start + cue.time`` on the SFX
        track — audio track index 3 in ``audio="mix"`` (song on A1,
        camera sound on A2), index 2 otherwise, the same layout
        :func:`monteur.montage.montage_to_timeline` uses. Their files are
        imported with the same ImportMedia/mapping machinery (a separate
        call, so the entry/music import stays untouched). Element
        placement is best-effort by contract: import misses and per-cue
        append refusals are collected into ONE summarized warning and
        never fail the build; when this Resolve rejected positioned
        placement altogether the elements are skipped with the same
        summarized warning (gapless appends would land them at the wrong
        times).
        """
        if mode not in ("hybrid", "append"):
            raise ValueError(
                f"unknown build mode {mode!r}; valid modes: hybrid, append"
            )
        # A plan without music cannot carry a song bed: coerce the audio
        # mode so no-music builds work end to end (clip sound on A1, SFX
        # on A2) instead of failing on a missing song.
        if not getattr(plan, "music_path", "") and audio in ("music", "mix"):
            audio = "original"
        canvas_size: tuple[int, int] | None = None
        if canvas is not None:
            # Lazy import: monteur.montage pulls in numpy, and this module
            # must stay importable by the stdlib-only worker bootstrap.
            from monteur.montage import CANVASES

            if canvas not in CANVASES:
                valid = ", ".join(sorted(CANVASES))
                raise ValueError(
                    f"unknown canvas {canvas!r}; valid canvases: {valid}"
                )
            canvas_size = CANVASES[canvas]
        if mode == "hybrid":
            built = self._build_via_timeline_import(
                plan, fps, name, titles, canvas, canvas_size, warnings, audio
            )
            if built is not None:
                return built
            if warnings is not None:
                warnings.append(
                    "this Resolve build refused the timeline file import — "
                    "built clip-by-clip instead; dissolves and fades live "
                    "in the downloaded file."
                )
        entries = sorted(plan.entries, key=lambda e: e.record_start)
        paths: list[str] = []
        for entry in entries:
            if entry.clip_path not in paths:
                paths.append(entry.clip_path)
        # audio="original" carries no song bed (and a no-music plan has no
        # song at all) — only import the music when it will be appended.
        with_music = bool(plan.music_path) and audio != "original"
        if with_music and plan.music_path not in paths:
            paths.append(plan.music_path)

        pool = self._media_pool()
        items = pool.ImportMedia(paths)
        if not items:
            raise MonteurResolveError(
                f"Resolve imported no media into project "
                f"{self.project_name()!r} from: {paths}. Check that the files "
                "exist and are readable by Resolve."
            )
        by_path = _map_items_to_paths(paths, items)
        missing = [p for p in paths if by_path.get(p) is None]
        if missing:
            raise MonteurResolveError(
                f"Resolve did not return media pool items for: {missing}. "
                "The files may be missing, offline or unsupported."
            )

        existing = set(self.list_timelines())
        timeline_name = name
        suffix = 2
        while timeline_name in existing:
            timeline_name = f"{name} {suffix}"
            suffix += 1
        timeline = pool.CreateEmptyTimeline(timeline_name)
        if timeline is None:
            raise MonteurResolveError(
                f"Resolve failed to create timeline {timeline_name!r} in "
                f"project {self.project_name()!r}."
            )

        # A new timeline inherits the PROJECT's frame rate — in a 50 fps
        # project every 25 fps record frame would land at half its time:
        # cuts beside the beat, titles past the end of the video. Pin the
        # rate to the plan's fps and TRUST the read-back value for all
        # positioning (some Resolve builds refuse the setting).
        fps = _pin_timeline_fps(timeline, fps, warnings)

        # AppendToTimeline addresses the SOURCE in the media pool item's own
        # frame space: the clip's native frame rate (a 50 fps DJI file counts
        # 50 frames per second no matter what the timeline runs at), anchored
        # at the clip's embedded start timecode (action cams stamp
        # time-of-day, so frame 0 may not exist in the clip at all). Getting
        # either wrong makes Resolve clamp every cut to a sliver — the field
        # bug that turned a 31 s montage into 67 uniform 0.1 s slivers.
        # recordFrame places each entry at its true record position, so the
        # plan's black gaps (trailer dips) exist for real on the timeline.
        try:
            record_base = int(timeline.GetStartFrame())
        except Exception:  # noqa: BLE001 - older builds; positions then 0-based
            record_base = 0
        placed = True  # recordFrame placement accepted by this Resolve
        for index, entry in enumerate(entries):
            item = by_path[entry.clip_path]
            clip_fps = _clip_native_fps(item, fps)
            offset = _clip_source_offset(
                item, getattr(entry, "media_start", 0.0) or 0.0, clip_fps
            )
            start = offset + int(round(entry.source_start * clip_fps))
            # The source LENGTH is derived from the record window in
            # timeline frames (converted to clip frames): rounding source
            # and record independently leaves one-frame black slivers
            # between neighbouring cuts on beat-aligned decimals.
            rec_in = int(round(entry.record_start * fps))
            rec_out = int(round(entry.record_end * fps))
            src_frames = max(1, int(round((rec_out - rec_in) * clip_fps / fps)))
            clip_info = {
                "mediaPoolItem": item,
                "startFrame": start,
                "endFrame": start + src_frames - 1,
                "mediaType": 1,
            }
            if placed:
                clip_info["trackIndex"] = 1
                clip_info["recordFrame"] = record_base + rec_in
            if not pool.AppendToTimeline([clip_info]):
                if placed:
                    # This Resolve rejects positioned placement — retry the
                    # same clip gapless and stay gapless for the rest.
                    placed = False
                    clip_info.pop("recordFrame", None)
                    clip_info.pop("trackIndex", None)
                    if pool.AppendToTimeline([clip_info]):
                        continue
                raise MonteurResolveError(
                    f"Resolve failed to append {entry.clip_path!r} "
                    f"({entry.source_start:.2f}-{entry.source_end:.2f}s) to "
                    f"timeline {timeline_name!r}."
                )
        if with_music:
            music_item = by_path[plan.music_path]
            music_fps = _clip_native_fps(music_item, fps)
            music_offset = _clip_source_offset(music_item, 0.0, music_fps)
            # The plan's adaptive music window: the song enters at record
            # music_in and ends at music_out (0 = full length). The
            # record<->song mapping is unchanged (record t plays song time
            # music_start + t), so every cut stays on the beat.
            w_in = float(getattr(plan, "music_in", 0.0) or 0.0)
            w_out = float(getattr(plan, "music_out", 0.0) or 0.0)
            w_in = min(max(w_in, 0.0), plan.duration)
            w_end = min(w_out, plan.duration) if w_out > 0 else plan.duration
            if w_end <= w_in:
                w_in, w_end = 0.0, plan.duration
            # Deliberate silence (plan.music_gaps): the window minus the
            # gaps yields the AUDIBLE spans — one positioned music clip
            # per span. Each post-gap span reads the song from
            # music_start + its own record start (the gap's source span
            # is skipped too), so the beat grid holds. Without gaps this
            # is the single full-window append, exactly as before.
            gaps: list[tuple[float, float]] = []
            for g in getattr(plan, "music_gaps", []) or []:
                g_lo = max(float(g[0]), w_in)
                g_hi = min(float(g[1]), w_end)
                if g_hi - g_lo > 1e-6:
                    gaps.append((g_lo, g_hi))
            spans: list[tuple[float, float]] = []
            cursor = w_in
            for g_lo, g_hi in sorted(gaps):
                if g_lo - cursor > 1e-6:
                    spans.append((cursor, g_lo))
                cursor = max(cursor, g_hi)
            if w_end - cursor > 1e-6:
                spans.append((cursor, w_end))
            if not spans:
                spans.append((w_in, w_end))  # defensive: never no bed at all
            if not placed and warnings is not None:
                if w_in > 0:
                    warnings.append(
                        "this Resolve build ignored positioned placement, so "
                        f"the music entry at {w_in:.1f}s only exists in the "
                        "exported file — the appended song starts with the "
                        "first clip."
                    )
                if gaps:
                    warnings.append(
                        "this Resolve build ignored positioned placement, so "
                        f"the {len(gaps)} deliberate music silence"
                        f"{'s' if len(gaps) != 1 else ''} only exist in the "
                        "exported file — the appended song plays through."
                    )
            if not placed and len(spans) > 1:
                # Gapless appends would butt the spans together and shift
                # every beat after the first gap — append ONE continuous
                # bed instead (the warning above says why).
                spans = [(w_in, w_end)]
            for span_lo, span_hi in spans:
                # The plan cuts to the song's BEST window (plan.music_start),
                # not its intro — starting the audio at source 0 would put
                # every cut beside the beat.
                music_in = music_offset + int(
                    round(
                        (float(getattr(plan, "music_start", 0.0) or 0.0) + span_lo)
                        * music_fps
                    )
                )
                music_info = {
                    "mediaPoolItem": music_item,
                    "startFrame": music_in,
                    "endFrame": music_in
                    + int(round((span_hi - span_lo) * music_fps))
                    - 1,
                    "mediaType": 2,
                }
                if placed:
                    music_info["trackIndex"] = 1
                    music_info["recordFrame"] = record_base + int(
                        round(span_lo * fps)
                    )
                if not pool.AppendToTimeline([music_info]):
                    music_info.pop("recordFrame", None)
                    music_info.pop("trackIndex", None)
                    if not pool.AppendToTimeline([music_info]):
                        raise MonteurResolveError(
                            f"Resolve failed to append the music "
                            f"{plan.music_path!r} to timeline "
                            f"{timeline_name!r}."
                        )
        if audio in ("mix", "original"):
            self._append_entry_audio(
                pool, plan, entries, by_path, fps, record_base, placed, audio,
                warnings,
            )
        self._append_sfx_elements(
            pool, plan, fps, record_base, placed, audio, warnings
        )
        dips = list(getattr(plan, "dips", []) or [])
        if not placed and dips and warnings is not None:
            warnings.append(
                "this Resolve build ignored positioned placement, so the "
                "clips were appended back-to-back — the black title gaps "
                "only exist in the FCPXML export."
            )
        if titles:
            if placed:
                # Real black gaps exist on the timeline — titles land at
                # their plan-time positions unshifted.
                self.add_titles(titles, fps, warnings=warnings)
            else:
                shifted = _shift_titles_for_gapless_append(titles, dips)
                self.add_titles(shifted, fps, warnings=warnings)
        if canvas is not None:
            self._apply_canvas(
                timeline, canvas, canvas_size,
                warnings if warnings is not None else [],
            )
        return timeline_name

    def _build_via_timeline_import(
        self,
        plan,
        fps: float,
        name: str,
        titles: list[dict] | None,
        canvas: str | None,
        canvas_size: tuple[int, int] | None,
        warnings: list[str] | None,
        audio: str,
    ) -> str | None:
        """The hybrid build: write the plan as FCPXML, import it, finish it.

        Returns the imported timeline's REAL name (Resolve may rename), or
        ``None`` when this Resolve build (or the file generation itself)
        refused — the caller then emits the one honest warning and falls
        back to the append flow. Everything up to and including the import
        is guarded: any exception means "this path is unavailable", never a
        crash — the append fallback either succeeds or raises the real
        error. The temp file is removed in every outcome. Exceptions from
        the FINISHING steps (titles/canvas) propagate exactly like the
        append flow's do: at that point the timeline exists and rebuilding
        it clip-by-clip would duplicate it.
        """
        import tempfile

        try:
            # Lazy, worker-safe imports (montage pulls in numpy; this module
            # must stay importable by the stdlib-only worker bootstrap).
            from monteur.io import save_timeline
            from monteur.montage import montage_to_timeline

            pool = self._media_pool()
            existing = set(self.list_timelines())
            timeline_name = name
            suffix = 2
            while timeline_name in existing:
                timeline_name = f"{name} {suffix}"
                suffix += 1
            file_timeline = montage_to_timeline(
                plan,
                fps=fps,
                name=timeline_name,
                audio=audio,
                canvas=canvas if canvas is not None else "hd",
            )
            entries = sorted(plan.entries, key=lambda e: e.record_start)
            source_dir = os.path.dirname(
                entries[0].clip_path if entries else (plan.music_path or "")
            )
            options: dict = {
                "timelineName": timeline_name,
                "importSourceClips": True,
            }
            if source_dir:
                # Best-effort media linking: point Resolve at the folder of
                # the first clip (montage folders hold the footage together).
                options["sourceClipsPath"] = source_dir
            handle, tmp_path = tempfile.mkstemp(
                prefix="monteur-timeline-", suffix=".fcpxml"
            )
            os.close(handle)
            try:
                save_timeline(file_timeline, tmp_path)
                imported = pool.ImportTimelineFromFile(tmp_path, options)
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 - refusal, not failure: fall back
            return None
        if not imported:
            return None
        # Resolve the created timeline: recent builds return the timeline
        # object, older ones only a truthy flag — the import then made it
        # the current timeline.
        timeline = imported if hasattr(imported, "GetName") else None
        if timeline is None:
            try:
                timeline = self._project().GetCurrentTimeline()
            except Exception:  # noqa: BLE001 - unlocatable -> fall back
                timeline = None
        if timeline is None:
            return None
        try:
            actual_name = str(timeline.GetName() or "") or timeline_name
        except Exception:  # noqa: BLE001 - a nameless timeline keeps ours
            actual_name = timeline_name
        try:
            # add_titles works on the CURRENT timeline; most Resolve builds
            # already switched to the import, this makes sure of it.
            self._project().SetCurrentTimeline(timeline)
        except Exception:  # noqa: BLE001 - best-effort; import stays current
            pass
        # The FCPXML format element carries the rate: read back and CONFIRM
        # instead of fighting — a differing imported rate wins (titles are
        # placed in the timeline's own currency) with one warning.
        fps = _confirm_timeline_fps(timeline, fps, warnings)
        if titles:
            # The imported timeline HAS the real black gaps, so titles land
            # at their plan-time positions unshifted.
            self.add_titles(titles, fps, warnings=warnings)
        if canvas is not None and canvas_size is not None:
            # The file's format element usually set the resolution already —
            # verify instead of blindly setting. The per-clip Scaling
            # crop/fill is the part the file cannot carry: ALWAYS applied.
            self._apply_canvas(
                timeline, canvas, canvas_size,
                warnings if warnings is not None else [],
                verify_resolution=True,
            )
        return actual_name

    def _append_entry_audio(
        self,
        pool: Any,
        plan,
        entries,
        by_path: dict,
        fps: float,
        record_base: int,
        placed: bool,
        audio: str,
        warnings: list[str] | None,
    ) -> None:
        """Append each entry's own camera sound as positioned audio clips.

        The append-mode counterpart of :func:`monteur.montage.
        montage_to_timeline`'s own-audio tracks: ``audio="original"`` puts
        the clips' sound on audio track 1 (no song bed exists in that
        mode — the ride-POV/no-music layout), ``"mix"`` on track 2 under
        the song. Requires positioned placement (gapless appends would
        land the sound at the wrong times) — the fallback skips with one
        summarized warning, exactly like the SFX elements. Per-entry
        refusals are best-effort: ONE summarized warning, never a failed
        build (video-only sources have no audio stream to append).
        """
        if not entries:
            return

        def warn(message: str) -> None:
            if warnings is not None:
                warnings.append(message)

        if not placed:
            warn(
                "this Resolve build ignored positioned placement, so the "
                "clips' own sound was skipped — it is in the FCPXML export."
            )
            return
        track_index = 1 if audio == "original" else 2
        try:
            _ensure_audio_tracks(self._current_timeline(), track_index)
        except Exception:  # noqa: BLE001 - track creation is best-effort
            pass
        failures = 0
        for entry in entries:
            item = by_path.get(entry.clip_path)
            if item is None:
                failures += 1
                continue
            try:
                clip_fps = _clip_native_fps(item, fps)
                offset = _clip_source_offset(
                    item, getattr(entry, "media_start", 0.0) or 0.0, clip_fps
                )
                start = offset + int(round(entry.source_start * clip_fps))
                rec_in = int(round(entry.record_start * fps))
                rec_out = int(round(entry.record_end * fps))
                src_frames = max(1, int(round((rec_out - rec_in) * clip_fps / fps)))
                info = {
                    "mediaPoolItem": item,
                    "startFrame": start,
                    "endFrame": start + src_frames - 1,
                    "mediaType": 2,
                    "trackIndex": track_index,
                    "recordFrame": record_base + rec_in,
                }
                if not pool.AppendToTimeline([info]):
                    failures += 1
            except Exception:  # noqa: BLE001 - one silent clip must not kill the rest
                failures += 1
        if failures:
            warn(
                f"{failures} of {len(entries)} original-sound clips could "
                "not be placed — import the FCPXML export for the full "
                "audio layout."
            )

    def _append_sfx_elements(
        self,
        pool: Any,
        plan,
        fps: float,
        record_base: int,
        placed: bool,
        audio: str,
        warnings: list[str] | None,
    ) -> None:
        """Append the plan's filed SFX cues as positioned audio clips.

        Best-effort by contract (see :meth:`build_timeline_from_plan`):
        every problem — positioned placement rejected, files Resolve
        would not import, per-cue append refusals — is summarized into at
        most ONE ``warnings`` message; nothing here ever raises.
        """
        filed = [
            cue
            for cue in (getattr(plan, "sfx", []) or [])
            if getattr(cue, "file", "")
        ]
        if not filed:
            return

        def warn(message: str) -> None:
            if warnings is not None:
                warnings.append(message)

        if not placed:
            warn(
                f"this Resolve build ignored positioned placement, so the "
                f"{len(filed)} sound-element clips were skipped — they are "
                "in the FCPXML export."
            )
            return
        # The SFX track index mirrors montage_to_timeline's layout: A3 in
        # "mix" (song on A1 + camera sound on A2), A2 otherwise. The track
        # must EXIST first — an explicit trackIndex never creates it.
        track_index = 3 if audio == "mix" else 2
        try:
            _ensure_audio_tracks(self._current_timeline(), track_index)
        except Exception:  # noqa: BLE001 - track creation is best-effort
            pass
        element_paths: list[str] = []
        for cue in filed:
            if cue.file not in element_paths:
                element_paths.append(cue.file)
        failures: list[str] = []
        by_path: dict = {}
        try:
            items = pool.ImportMedia(element_paths) or []
            by_path = _map_items_to_paths(element_paths, items)
        except Exception:  # noqa: BLE001 - element import must not fail the build
            by_path = {}
        for cue in filed:
            item = by_path.get(cue.file)
            cue_name = os.path.basename(cue.file)
            if item is None:
                failures.append(cue_name)
                continue
            try:
                clip_fps = _clip_native_fps(item, fps)
                offset = _clip_source_offset(item, 0.0, clip_fps)
                length = min(
                    float(cue.duration), max(0.0, plan.duration - cue.time)
                )
                src_frames = max(1, int(round(length * clip_fps)))
                info = {
                    "mediaPoolItem": item,
                    "startFrame": offset,
                    "endFrame": offset + src_frames - 1,
                    "mediaType": 2,
                    "trackIndex": track_index,
                    "recordFrame": record_base + int(round(cue.time * fps)),
                }
                if not pool.AppendToTimeline([info]):
                    failures.append(cue_name)
            except Exception:  # noqa: BLE001 - one bad cue must not kill the rest
                failures.append(cue_name)
        if failures:
            shown = ", ".join(sorted(set(failures))[:4])
            warn(
                f"{len(failures)} of {len(filed)} sound-element clips could "
                f"not be placed ({shown}) — drop them in by hand or import "
                "the FCPXML export."
            )

    def _apply_canvas(
        self,
        timeline: Any,
        canvas: str,
        size: tuple[int, int],
        warnings: list[str],
        verify_resolution: bool = False,
    ) -> None:
        """Size a freshly built timeline to a canvas preset, defensively.

        Resolution first: timeline-level custom settings
        (``SetSetting("useCustomSettings", "1")`` then the
        ``timelineResolutionWidth``/``Height`` keys — Resolve's SetSetting
        takes STRING values), falling back to the project-level
        ``SetSetting`` when the timeline refuses (older Resolve versions
        return False there). With ``verify_resolution`` (the hybrid build's
        imported timelines) the resolution is READ first and only set when
        it doesn't already match — the FCPXML format element usually
        carried it. Then every clip on video track 1 gets explicit
        scaling: ``SetProperty("Scaling", 1)`` ("scale full frame with
        crop") for the cinemascope presets, ``("Scaling", 3)`` (fill) for
        everything else, so mismatched footage never sits small in the
        frame or behind bars — always applied, verified or not (the one
        canvas piece a timeline file cannot carry). Every refusal is
        non-fatal: each step
        contributes at most ONE summarized message to ``warnings``.
        """
        width, height = size
        already = (
            verify_resolution
            and _timeline_resolution(timeline) == (width, height)
        )
        if not already and not _set_timeline_resolution(timeline, width, height):
            if not _set_project_resolution(self._project(), width, height):
                warnings.append(
                    f"could not set the timeline resolution to "
                    f"{width}x{height} for the {canvas!r} canvas (Resolve "
                    "refused both the timeline-level custom settings and the "
                    "project setting) — set Project Settings > Timeline "
                    "resolution by hand."
                )
        # Every canvas gets explicit per-clip scaling: mismatched footage
        # (4:3 action cams, 16:9 in a 9:16 or 2.39:1 frame, HD files on a
        # UHD timeline) must FILL the frame, never sit small in the middle
        # or behind bars. Cinemascope keeps "scale full frame with crop";
        # everything else fills (aspect preserved, overflow cropped).
        # Auto-reframe 9:16 FOLLOW-UP (deferred): the ffmpeg export
        # (:func:`monteur.preview.render_export`) shifts this crop toward each
        # shot's attention point (:mod:`monteur.reframe`,
        # ``MontageEntry.reframe_focus``) so an off-centre subject survives the
        # 9:16 / cine crop. The Resolve equivalent is a per-clip Pan offset
        # alongside this Scaling call, but it needs a reliable timeline-item ->
        # plan-entry mapping and Resolve's Pan pixel/sign semantics verified
        # against a live DaVinci build — neither is safe to land blind, so
        # Resolve reframe is intentionally left as centre-crop for now.
        mode = 1 if canvas.startswith("cine") else 3
        failed, total = _set_clip_scaling(timeline, mode)
        if failed:
            if mode == 1:
                warnings.append(
                    f"could not set 'scale full frame with crop' on {failed} "
                    f"of {total} clips — for the cinemascope look, set "
                    "Project Settings > Image Scaling > 'Scale full frame "
                    "with crop' by hand."
                )
            else:
                warnings.append(
                    f"could not set fill-the-frame scaling on {failed} of "
                    f"{total} clips — if footage sits small or behind bars, "
                    "set the clips' Scaling to 'Fill' in the Inspector."
                )


# --- Canvas (resolution + cinemascope crop) -------------------------------------


def _move_playhead(timeline: Any, frame: int, fps: float) -> bool:
    """Move the playhead to an absolute timeline frame; True on success.

    ``SetCurrentTimecode`` takes a timecode string. Fusion titles insert
    at the playhead, and some Resolve builds cannot reposition items
    afterwards — positioning the playhead FIRST is the reliable path.
    """
    setter = getattr(timeline, "SetCurrentTimecode", None)
    if not callable(setter):
        return False
    try:
        return bool(setter(format_timecode(int(frame), fps)))
    except Exception:  # noqa: BLE001 - a refusing playhead is non-fatal
        return False


def _ensure_audio_tracks(timeline: Any, needed: int) -> None:
    """``AddTrack("audio")`` until the timeline has ``needed`` audio tracks.

    Appending with an explicit ``trackIndex`` does NOT create the track —
    Resolve just refuses the append (the field case: element clips
    silently missing because only A1 existed). Fully guarded; a refusing
    AddTrack simply leaves the per-cue appends to fail into the caller's
    summarized warning.
    """
    try:
        count = int(timeline.GetTrackCount("audio") or 0)
    except Exception:  # noqa: BLE001 - unreadable track count: nothing to do
        return
    add = getattr(timeline, "AddTrack", None)
    if not callable(add):
        return
    while count < needed:
        try:
            if not add("audio"):
                return
        except Exception:  # noqa: BLE001 - refusal handled downstream
            return
        count += 1


def _pin_timeline_fps(timeline: Any, fps: float, warnings) -> float:
    """Set the fresh timeline's frame rate to ``fps``; return the REAL rate.

    ``CreateEmptyTimeline`` inherits the project's frame rate, so the
    plan's fps and the timeline's fps can disagree — and every record
    frame, title frame and music frame would land at the wrong time.
    Tries ``SetSetting("useCustomSettings"/"timelineFrameRate")``, then
    reads the rate back and returns what the timeline ACTUALLY runs at
    (the caller does all positioning in that currency). A refusal is
    non-fatal: one warning explains that the cut was placed at the
    timeline's own rate to stay on the beat.
    """
    setter = getattr(timeline, "SetSetting", None)
    if callable(setter):
        try:
            setter("useCustomSettings", "1")
            setter("timelineFrameRate", f"{fps:g}")
        except Exception:  # noqa: BLE001 - refusal handled via read-back
            pass
    getter = getattr(timeline, "GetSetting", None)
    actual: float | None = None
    if callable(getter):
        try:
            raw = str(getter("timelineFrameRate") or "").split()
            actual = float(raw[0]) if raw else None
        except Exception:  # noqa: BLE001 - unreadable rate: trust the request
            actual = None
    if actual and actual > 0 and abs(actual - fps) > 1e-3:
        if warnings is not None:
            warnings.append(
                f"the timeline runs at {actual:g} fps (the project's "
                f"default won over the requested {fps:g}) — the cut was "
                f"placed at {actual:g} fps so it stays on the beat."
            )
        return actual
    return fps


def _confirm_timeline_fps(timeline: Any, fps: float, warnings) -> float:
    """Read back an IMPORTED timeline's frame rate; trust it, warn on drift.

    The hybrid build's counterpart of :func:`_pin_timeline_fps`: the FCPXML
    format element already carried the rate, so this only CONFIRMS — no
    SetSetting, no fighting the import. When the imported timeline reports
    a rate different from the plan's, the import wins: the returned rate is
    what the caller positions titles in, and one warning says so. An
    unreadable rate is trusted to be the requested one.
    """
    getter = getattr(timeline, "GetSetting", None)
    actual: float | None = None
    if callable(getter):
        try:
            raw = str(getter("timelineFrameRate") or "").split()
            actual = float(raw[0]) if raw else None
        except Exception:  # noqa: BLE001 - unreadable rate: trust the request
            actual = None
    if actual and actual > 0 and abs(actual - fps) > 1e-3:
        if warnings is not None:
            warnings.append(
                f"the imported timeline runs at {actual:g} fps, not the "
                f"plan's {fps:g} — trusting the import; titles were placed "
                f"at {actual:g} fps."
            )
        return actual
    return fps


def _timeline_resolution(timeline: Any) -> tuple[int, int] | None:
    """The timeline's current resolution via GetSetting, or None.

    Used by the hybrid build to VERIFY the resolution the FCPXML format
    element set instead of blindly re-setting it. Fully guarded — a missing
    GetSetting, an exception or unparseable values simply mean "unknown"
    (None), and the caller falls back to setting explicitly.
    """
    getter = getattr(timeline, "GetSetting", None)
    if not callable(getter):
        return None
    try:
        width = int(str(getter("timelineResolutionWidth") or "").strip())
        height = int(str(getter("timelineResolutionHeight") or "").strip())
    except Exception:  # noqa: BLE001 - unreadable resolution = unknown
        return None
    if width > 0 and height > 0:
        return (width, height)
    return None


def _set_timeline_resolution(timeline: Any, width: int, height: int) -> bool:
    """Try the timeline-level resolution; True only when Resolve took it all.

    Per the scripting API, a per-timeline resolution needs
    ``SetSetting("useCustomSettings", "1")`` first; values are strings.
    A missing SetSetting, an exception, or ANY False return yields False so
    the caller can fall back to the project-level setting.
    """
    setter = getattr(timeline, "SetSetting", None)
    if not callable(setter):
        return False
    try:
        ok_custom = setter("useCustomSettings", "1")
        ok_width = setter("timelineResolutionWidth", str(width))
        ok_height = setter("timelineResolutionHeight", str(height))
    except Exception:  # noqa: BLE001 - a refusing API is a fallback, not a crash
        return False
    return bool(ok_custom and ok_width and ok_height)


def _set_project_resolution(project: Any, width: int, height: int) -> bool:
    """Project-level resolution fallback; True when both keys were accepted."""
    setter = getattr(project, "SetSetting", None)
    if not callable(setter):
        return False
    try:
        ok_width = setter("timelineResolutionWidth", str(width))
        ok_height = setter("timelineResolutionHeight", str(height))
    except Exception:  # noqa: BLE001 - a refusing API becomes a warning upstream
        return False
    return bool(ok_width and ok_height)


def _set_clip_scaling(timeline: Any, mode: int) -> tuple[int, int]:
    """``SetProperty("Scaling", mode)`` on every video-track-1 item.

    Modes per the scripting API: 0 project default, 1 "scale full frame
    with crop" (the cinemascope look), 2 fit, 3 fill (aspect preserved,
    overflow cropped — the no-bars choice for mismatched footage), 4
    stretch. Returns ``(failed, total)`` — per-item failures are counted,
    never raised, so the caller can emit ONE summarized warning.
    """
    try:
        if int(timeline.GetTrackCount("video") or 0) < 1:
            return 0, 0
        items = list(timeline.GetItemListInTrack("video", 1) or [])
    except Exception:  # noqa: BLE001 - enumeration refused: nothing to scale
        return 0, 0
    failed = 0
    for item in items:
        setter = getattr(item, "SetProperty", None)
        ok = False
        if callable(setter):
            try:
                ok = bool(setter("Scaling", mode))
            except Exception:  # noqa: BLE001 - one bad item must not stop the rest
                ok = False
        if not ok:
            failed += 1
    return failed, len(items)


# --- Fusion Text+ titles --------------------------------------------------------

# A trailer's smash-to-black dip is ~0.4s — far too short to read a title, so
# titles_from_plan stretches every title to at least this long. The overlap
# with the incoming clip is deliberate: titles usually sit over picture.
MIN_TITLE_SECONDS = 2.0


def titles_from_plan(plan, texts: list[str] | None = None) -> list[dict]:
    """Title specs for :meth:`ResolveBridge.add_titles` from a plan's dips.

    Pure and Resolve-free (testable anywhere). One title per ``plan.dips``
    entry (the trailer's smash-to-black title slots): ``start`` is the dip's
    start and ``duration`` is ``max(dip length, MIN_TITLE_SECONDS)`` — the
    dip itself is too short for a readable title, so the title deliberately
    overlaps the incoming clip (titles usually sit over picture). The text is
    ``texts[i]`` when given (and non-empty); otherwise the plan's own
    ``title_texts[i]`` (the composed act titles from :mod:`monteur.compose`,
    when the plan carries any); otherwise the vision ``label`` of the entry
    that starts right after the dip; otherwise ``"Title"``. Plans without
    dips yield ``[]``.
    """
    dips = list(getattr(plan, "dips", []) or [])
    if not dips:
        return []
    if texts is None:
        # Plan-carried override: a Claude-composed plan brings its own act
        # titles along (older/duck-typed plans simply have no such field).
        carried = [str(t) for t in getattr(plan, "title_texts", None) or []]
        if any(t.strip() for t in carried):
            texts = carried
    anims = [str(a) for a in getattr(plan, "title_anims", None) or []]
    entries = sorted(plan.entries, key=lambda e: e.record_start)
    titles: list[dict] = []
    for index, (start, length) in enumerate(dips):
        text = ""
        if texts is not None and index < len(texts):
            text = str(texts[index] or "").strip()
        if not text:
            incoming = next(
                (e for e in entries if e.record_start >= start + length - 1e-6),
                None,
            )
            text = str(getattr(incoming, "label", "") or "").strip()
        anim = anims[index] if index < len(anims) else ""
        anim = anim if anim in ("fade", "slide", "type") else "none"
        titles.append(
            {
                "start": float(start),
                "duration": max(float(length), MIN_TITLE_SECONDS),
                "text": text or "Title",
                "anim": anim,
            }
        )
    return titles


def _clip_native_fps(item, fallback: float) -> float:
    """A media pool item's own frame rate, else ``fallback`` (timeline fps).

    ``GetClipProperty("FPS")`` returns a string like ``"50"``/``"59.94"``
    for video; audio-only items report 0 or nothing — then the timeline
    rate is the only sensible frame currency.
    """
    try:
        raw = item.GetClipProperty("FPS")
    except Exception:  # noqa: BLE001 - never let a property probe kill a build
        return fallback
    try:
        value = float(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _clip_source_offset(item, media_start: float, clip_fps: float) -> int:
    """First source frame of a pool item, in the clip's own frame numbering.

    Resolve anchors AppendToTimeline's startFrame/endFrame at the clip's
    embedded start timecode (``GetClipProperty("Start")``), not at zero —
    action cams stamp time-of-day, so a clip's first frame can be in the
    millions. Falls back to the plan's probed ``media_start`` seconds when
    the property is missing or unparseable.
    """
    try:
        raw = item.GetClipProperty("Start")
    except Exception:  # noqa: BLE001 - never let a property probe kill a build
        raw = None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(round(media_start * clip_fps))


def _shift_titles_for_gapless_append(
    titles: list[dict], dips: list[tuple[float, float]]
) -> list[dict]:
    """Map plan-time title starts onto the gapless appended Resolve timeline.

    ``build_timeline_from_plan`` appends entries back-to-back, so the plan's
    smash-to-black dips are squeezed out of the Resolve timeline — everything
    after a dip sits earlier by that dip's length. A title at plan time ``t``
    therefore belongs at ``t`` minus the total length of the dips that END at
    or before ``t`` (a title at its own dip's start is not shifted by that
    dip). Returns copies; with no dips the input is returned unchanged.
    """
    if not dips:
        return titles
    shifted: list[dict] = []
    for title in titles:
        copy = dict(title) if isinstance(title, dict) else title
        try:
            start = float(copy.get("start", 0.0))
        except (TypeError, ValueError, AttributeError):
            shifted.append(copy)
            continue
        shift = sum(
            length for dip_start, length in dips if dip_start + length <= start + 1e-6
        )
        copy["start"] = max(0.0, start - shift)
        shifted.append(copy)
    return shifted


def _ensure_title_track(timeline: Any, warnings: list[str]) -> None:
    """Make sure a video track exists above the footage for the titles.

    With fewer than two video tracks, tries ``timeline.AddTrack("video")`` so
    inserted Text+ items can sit over the picture instead of colliding with
    it. A missing or refusing AddTrack is only a warning — Resolve versions
    differ, and the insert itself still goes ahead.
    """
    try:
        count = int(timeline.GetTrackCount("video") or 0)
    except (TypeError, ValueError):
        count = 0
    if count >= 2:
        return
    add = getattr(timeline, "AddTrack", None)
    if callable(add) and add("video"):
        return
    warnings.append(
        "could not add a video track for the titles (Timeline.AddTrack "
        "unavailable or refused) — the titles will land wherever Resolve "
        "puts them; check the track above the footage."
    )


def _video_track_snapshot(timeline: Any) -> list[list[Any]]:
    """Items per video track (index 0 = track 1); tolerant of None returns."""
    try:
        count = int(timeline.GetTrackCount("video") or 0)
    except (TypeError, ValueError):
        count = 0
    return [
        list(timeline.GetItemListInTrack("video", index) or [])
        for index in range(1, count + 1)
    ]


def _find_new_video_item(timeline: Any, before: list[list[Any]]) -> Any | None:
    """The timeline item added since ``before`` (a _video_track_snapshot).

    Scans the topmost track first (titles land above the footage). In a
    track that gained items, the newest not-seen-before item wins; identity
    (``is``) comparison is used because Resolve's item wrappers do not
    support equality reliably. Returns None when nothing new is found —
    Resolve inserted somewhere unexpected (e.g. a brand-new track it created
    itself plus a re-read glitch), which callers report as a warning.
    """
    after = _video_track_snapshot(timeline)
    for track_index in range(len(after), 0, -1):
        old = before[track_index - 1] if track_index <= len(before) else []
        new = after[track_index - 1]
        if len(new) <= len(old):
            continue
        fresh = [item for item in new if all(item is not seen for seen in old)]
        return fresh[-1] if fresh else new[-1]
    return None


def _find_text_plus_tool(comp: Any) -> Any | None:
    """The Text+ ("TextPlus") tool of a Fusion composition, or None.

    Tries the type-filtered ``comp.GetToolList(False, "TextPlus")`` first
    (False = all tools, not just selected); older hosts without the filter
    argument raise TypeError, and the full ``GetToolList()`` is scanned
    instead. GetToolList returns a dict keyed by index in Fusion — lists are
    tolerated too. A candidate must have a callable ``SetInput``; when its
    ``GetAttrs()["TOOLS_RegID"]`` is readable it must be "TextPlus", and a
    filtered result without a readable registry id is trusted as a fallback.
    """
    getter = getattr(comp, "GetToolList", None)
    if not callable(getter):
        return None
    filtered = True
    try:
        candidates = getter(False, "TextPlus")
    except TypeError:  # host without the type-filter argument
        candidates = None
        filtered = False
    if not candidates:
        candidates = getter()
        filtered = False
    values = (
        list(candidates.values()) if isinstance(candidates, dict)
        else list(candidates or [])
    )
    fallback = None
    for tool in values:
        if tool is None or not callable(getattr(tool, "SetInput", None)):
            continue
        reg_id = ""
        get_attrs = getattr(tool, "GetAttrs", None)
        if callable(get_attrs):
            try:
                reg_id = (get_attrs() or {}).get("TOOLS_RegID", "")
            except Exception:  # identification only — never fatal
                reg_id = ""
        if reg_id == "TextPlus":
            return tool
        if filtered and not reg_id and fallback is None:
            fallback = tool  # the type filter already vouched for it
    return fallback


def _comp_frame_span(comp, duration: float, fps: float) -> tuple[float, float]:
    """The title comp's own [start, end] render frames.

    Fusion animation keyframes are placed in comp-frame time. Prefer the
    comp's declared render range (``COMPN_RenderStart``/``RenderEnd``); fall
    back to ``0 .. duration*fps`` when the host does not expose it.
    """
    start, end = 0.0, max(1.0, duration * fps)
    get_attrs = getattr(comp, "GetAttrs", None)
    if callable(get_attrs):
        try:
            attrs = get_attrs() or {}
            rs = attrs.get("COMPN_RenderStart")
            re_ = attrs.get("COMPN_RenderEnd")
            if rs is not None and re_ is not None and float(re_) > float(rs):
                start, end = float(rs), float(re_)
        except Exception:  # range probe only — never fatal
            pass
    return start, end


def _apply_title_anim(tool, comp, anim: str, duration: float, fps: float) -> bool:
    """Best-effort Fusion animation on a Text+ title (blueprint 1.7).

    Keyframes a standard Text+ input for each mode:

    * ``fade`` — ``Opacity`` 0 -> 1 over the head, 1 -> 0 over the tail.
    * ``slide`` — ``Center`` X slides in from off-screen left to centre.
    * ``type`` — ``WriteOnEnd`` 0 -> 1 (Fusion's native typewriter reveal).
    * ``none`` / anything else — nothing to do.

    Returns ``True`` when keyframes were set, ``False`` when the mode is
    static or the host could not animate (the caller then leaves a plain,
    correct title — a warning, never a raise, per this module's contract).
    Fusion's ``SetInput(name, value, frame)`` creates a spline on an
    un-animated input, so setting the same input at two frames animates it.
    """
    if anim not in ("fade", "slide", "type"):
        return False
    set_input = getattr(tool, "SetInput", None)
    if not callable(set_input):
        return False
    start, end = _comp_frame_span(comp, duration, fps)
    span = max(1.0, end - start)
    head = min(span * 0.35, max(1.0, 0.3 * fps))  # ~0.3 s, capped to a third
    try:
        if anim == "fade":
            set_input("Opacity", 0.0, start)
            set_input("Opacity", 1.0, start + head)
            set_input("Opacity", 1.0, end - head)
            set_input("Opacity", 0.0, end)
        elif anim == "slide":
            # Center is 0..1 in screen space; slide from off-left to centre
            set_input("Center", {1: -0.4, 2: 0.5}, start)
            set_input("Center", {1: 0.5, 2: 0.5}, start + head)
        elif anim == "type":
            # Text+ "Write On" End reveals characters as it goes 0 -> 1
            reveal = min(span * 0.6, 1.5 * fps)  # over up to ~1.5 s
            set_input("WriteOnEnd", 0.0, start)
            set_input("WriteOnEnd", 1.0, start + reveal)
        return True
    except Exception:  # noqa: BLE001 - host can't animate; leave a static title
        return False


# Resolve's fixed marker color palette.
RESOLVE_MARKER_COLORS = (
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
)


def _marker_color(color: str) -> str:
    """Nearest Resolve marker color name for a model marker color.

    A color that already is a Resolve color name passes through verbatim
    (case-insensitively normalized); anything else becomes "Blue".
    """
    wanted = (color or "").strip().lower()
    for name in RESOLVE_MARKER_COLORS:
        if name.lower() == wanted:
            return name
    return "Blue"


def _map_items_to_paths(paths: list[str], items: list[Any]) -> dict[str, Any]:
    """Map requested file paths to imported media pool items.

    Strategy (documented on build_timeline_from_plan): exact
    GetClipProperty("File Path") matches first, then positional order when
    the counts line up, then basename matching via GetName(). Unmatched
    paths map to None.
    """
    by_path: dict[str, Any] = {path: None for path in paths}
    for item in items:
        getter = getattr(item, "GetClipProperty", None)
        if not callable(getter):
            continue
        try:
            file_path = getter("File Path")
        except Exception:
            continue
        if file_path in by_path and by_path[file_path] is None:
            by_path[file_path] = item
    if any(v is None for v in by_path.values()) and len(items) == len(paths):
        for path, item in zip(paths, items):
            if by_path[path] is None:
                by_path[path] = item
    remaining = [p for p, v in by_path.items() if v is None]
    if remaining:
        by_name: dict[str, Any] = {}
        for item in items:
            try:
                by_name.setdefault(item.GetName(), item)
            except Exception:
                continue
        for path in remaining:
            by_path[path] = by_name.get(os.path.basename(path))
    return by_path


def _parse_fps(raw_timeline: Any) -> float:
    setting = raw_timeline.GetSetting("timelineFrameRate")
    try:
        return float(setting)
    except (TypeError, ValueError):
        raise MonteurResolveError(
            f"Timeline {raw_timeline.GetName()!r} reported an unreadable "
            f"frame rate setting: {setting!r}"
        ) from None


def _record_start(raw_timeline: Any) -> int:
    get_start = getattr(raw_timeline, "GetStartFrame", None)
    if callable(get_start):
        return int(get_start())
    starts: list[int] = []
    for kind in (VIDEO, AUDIO):
        for track_index in range(1, int(raw_timeline.GetTrackCount(kind)) + 1):
            for item in raw_timeline.GetItemListInTrack(kind, track_index) or []:
                starts.append(int(item.GetStart()))
    return min(starts, default=0)


def _convert_item(item: Any, track: str, kind: str, record_start: int) -> Clip:
    start = int(item.GetStart())
    end = int(item.GetEnd())
    left = int(item.GetLeftOffset())
    duration = int(item.GetDuration())
    source_name = ""
    get_pool_item = getattr(item, "GetMediaPoolItem", None)
    if callable(get_pool_item):
        pool_item = get_pool_item()
        if pool_item is not None:
            source_name = pool_item.GetName()
    return Clip(
        name=item.GetName(),
        track=track,
        kind=kind,
        source_in=left,
        source_out=left + duration,
        record_in=start - record_start,
        record_out=end - record_start,
        source_name=source_name,
    )


def _convert_markers(raw_timeline: Any) -> list[Marker]:
    raw_markers = raw_timeline.GetMarkers() or {}
    markers = [
        Marker(
            frame=int(frame),
            name=info.get("name", ""),
            note=info.get("note", ""),
            color=info.get("color", ""),
        )
        for frame, info in raw_markers.items()
    ]
    markers.sort(key=lambda m: m.frame)
    return markers


# --- Resolve menu scripts ------------------------------------------------------

_ANALYZE_TIMELINE_SCRIPT = '''\
"""Monteur - Analyze Timeline.

Runs inside DaVinci Resolve (Workspace > Scripts > Utility, installed by
`monteur resolve install-scripts`). Reads the current timeline, prints its
pacing stats to the console and adds a red "Monteur: slow section" marker
at the start of every slow section.
"""


def _get_resolve():
    """Return the Resolve app object from whatever host runs this script."""
    try:
        return resolve  # provided as a global by Resolve's script host
    except NameError:
        pass
    try:
        return app.GetResolve()  # Fusion-style host
    except (NameError, AttributeError):
        pass
    try:
        return bmd.scriptapp("Resolve")
    except (NameError, AttributeError):
        return None


def main():
    try:
        from monteur.analysis import analyze_timeline
        from monteur.model import Marker, seconds_to_frames
        from monteur.resolve import MonteurResolveError, ResolveBridge
    except ImportError:
        print(
            "Monteur is not installed in Resolve's Python interpreter.\\n"
            "Install it for the Python 3 that Resolve uses, e.g.:\\n"
            "    python3 -m pip install monteur\\n"
            "then re-run this script."
        )
        return

    resolve_app = _get_resolve()
    if resolve_app is None:
        print("Could not reach the Resolve scripting API from this host.")
        return

    bridge = ResolveBridge(resolve_app)
    try:
        timeline = bridge.read_timeline()
        stats = analyze_timeline(timeline)
        print("Timeline : " + (stats.timeline_name or "-"))
        print(
            "Duration : {0:.1f}s at {1:g} fps".format(
                stats.duration_seconds, stats.fps
            )
        )
        print(
            "Shots    : {0}   Cuts: {1}".format(stats.shot_count, stats.cut_count)
        )
        print(
            "Shot len : avg {0:.2f}s  median {1:.2f}s  "
            "min {2:.2f}s  max {3:.2f}s".format(
                stats.avg_shot_seconds,
                stats.median_shot_seconds,
                stats.min_shot_seconds,
                stats.max_shot_seconds,
            )
        )
        markers = [
            Marker(
                frame=seconds_to_frames(section.start, timeline.fps),
                name="Monteur: slow section",
                note="avg shot {0:.1f}s".format(section.avg_shot_length),
                color="Red",
            )
            for section in stats.sections
            if section.label == "slow"
        ]
        added = bridge.add_markers(markers)
        print("Added {0} 'Monteur: slow section' marker(s).".format(added))
    except MonteurResolveError as exc:
        print("Monteur: {0}".format(exc))


main()
'''

_OPEN_STUDIO_SCRIPT = '''\
"""Monteur - Open Studio.

Runs inside DaVinci Resolve (Workspace > Scripts > Utility, installed by
`monteur resolve install-scripts`). Launches the Monteur Studio web app
(`monteur ui`) detached in the background and prints its URL.
"""

import os
import subprocess
import sys

URL = "http://127.0.0.1:8765"


def main():
    try:
        import monteur  # noqa: F401
    except ImportError:
        print(
            "Monteur is not installed in Resolve's Python interpreter.\\n"
            "Install it for the Python 3 that Resolve uses, e.g.:\\n"
            "    python3 -m pip install monteur\\n"
            "then re-run this script."
        )
        return
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    subprocess.Popen([sys.executable, "-m", "monteur.cli", "ui"], **kwargs)
    print("Monteur Studio starting at " + URL)


main()
'''

_SCRIPT_FILES = {
    "Monteur - Analyze Timeline.py": _ANALYZE_TIMELINE_SCRIPT,
    "Monteur - Open Studio.py": _OPEN_STUDIO_SCRIPT,
}

_LINUX_SYSTEM_SCRIPT_DIR = "/opt/resolve/Fusion/Scripts/Utility"


def _script_install_dirs() -> list[str]:
    """Resolve's Fusion Scripts/Utility folder(s) for this platform."""
    if sys.platform == "darwin":
        return [
            os.path.expanduser(
                "~/Library/Application Support/Blackmagic Design"
                "/DaVinci Resolve/Fusion/Scripts/Utility"
            )
        ]
    if sys.platform.startswith("win"):
        appdata = os.environ.get(
            "APPDATA", os.path.expanduser(os.path.join("~", "AppData", "Roaming"))
        )
        return [
            os.path.join(
                appdata,
                "Blackmagic Design",
                "DaVinci Resolve",
                "Support",
                "Fusion",
                "Scripts",
                "Utility",
            )
        ]
    dirs = [os.path.expanduser("~/.local/share/DaVinciResolve/Fusion/Scripts/Utility")]
    if os.path.isdir(_LINUX_SYSTEM_SCRIPT_DIR) and os.access(
        _LINUX_SYSTEM_SCRIPT_DIR, os.W_OK
    ):
        dirs.append(_LINUX_SYSTEM_SCRIPT_DIR)
    return dirs


def install_scripts(dry_run: bool = False) -> list[str]:
    """Install Monteur's launcher scripts into Resolve's scripts menu.

    Writes "Monteur - Analyze Timeline.py" and "Monteur - Open Studio.py"
    into Resolve's Fusion ``Scripts/Utility`` folder (created if needed);
    they appear under Workspace > Scripts > Utility after a Resolve restart.
    With ``dry_run=True`` nothing is written and the target paths are
    returned as-is. Returns the list of written (or would-be-written) paths.
    """
    written: list[str] = []
    for directory in _script_install_dirs():
        for filename, content in _SCRIPT_FILES.items():
            target = os.path.join(directory, filename)
            written.append(target)
            if dry_run:
                continue
            os.makedirs(directory, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
    return written
