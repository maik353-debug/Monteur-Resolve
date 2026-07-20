"""Isolated worker subprocess for DaVinci Resolve scripting access.

Run as ``python -m monteur._resolve_worker <command>``. The worker reads a
JSON request object from stdin, writes a single JSON response object to stdout
and exits 0.

Why a separate process? Resolve's native scripting module (``fusionscript``,
loaded by ``DaVinciResolveScript``) can hard-crash the interpreter with a
C-level access violation when imported under an incompatible Python version
(Resolve supports roughly 3.6–3.11). That crash cannot be caught with
``try``/``except`` — it kills the process. By performing every Resolve access
here, in a disposable child, a native crash only takes down this worker; the
parent detects the nonzero exit code and reports "Resolve unavailable" instead
of dying. See ``monteur.resolve._run_worker`` for the parent side.

Protocol
--------
Command is the first CLI argument; extra arguments arrive as a JSON object on
stdin.

``info``
    stdin: ignored. Crash-free diagnostics (never imports the native
    module): interpreter version/bits/executable, ``module_dir`` (where
    DaVinciResolveScript.py was found, or null), ``searched``, ``env``
    (RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB / PYTHONPATH /
    MONTEUR_RESOLVE_PYTHON, each with raw ``value``, ``quoted`` and
    ``exists`` flags — PYTHONPATH gets per-entry ``missing`` instead of
    ``exists``) and ``resolve_install`` (``library``: the actual
    fusionscript.dll/.so found, or null; ``searched``: where it looked).

``load_test``
    stdin: ignored. The ONE exception to the single-JSON-response protocol:
    streams one JSON line per completed stage (``locate`` → ``dll-load`` →
    ``import`` → ``connect``), flushed after each, so a native crash in a
    later stage still leaves the completed stages on stdout. See
    :func:`load_test` for the exact stage payloads.

``status``
    stdin: ignored. Response on success::

        {"connected": true, "project": <str>, "timelines": [<str>, ...],
         "current": <str|null>}

    On a handled MonteurResolveError (e.g. Resolve not running)::

        {"connected": false, "error": <str>}

``read_timeline``
    stdin: ``{"name": <str|null>}`` (null / omitted => current timeline).
    Response on success::

        {"ok": true, "timeline": {<_timeline_to_dict payload>}}

    On a handled MonteurResolveError::

        {"ok": false, "error": <str>}

``build_plan``
    stdin (plans are large, so everything travels on stdin, never argv)::

        {"plan": {<monteur.montage.plan_to_dict payload>},
         "fps": <float>,
         "name": <str>,                                   # timeline name
         "titles": [{"start": <s>, "duration": <s>, "text": <str>}, ...]
                   | null,                                # optional Fusion titles
         "canvas": <str> | null}                          # optional CANVASES key
                                                          # (e.g. "uhd", "cine-uhd")

    Rebuilds the MontagePlan (``plan_from_dict``) and runs
    ``connect().build_timeline_from_plan(plan, fps=fps, name=name,
    titles=titles, canvas=canvas)`` — a canvas sets the timeline
    resolution, and the cinemascope presets also put "scale full frame
    with crop" on the footage. Response on success::

        {"ok": true, "timeline": <created timeline name>,
         "warnings": [<str>, ...]}      # add_titles' + canvas messages

    On a handled failure (malformed plan, unknown canvas,
    MonteurResolveError)::

        {"ok": false, "error": <str>}

A handled ``MonteurResolveError`` is a *clean, catchable* failure: the worker
still exits 0 with a ``connected``/``ok`` ``false`` payload. Likewise any
ordinary Python exception is caught at the top level and reported as
``{"error": <str>}`` with exit 0 — a normal error must never masquerade as a
crash. ONLY an uncatchable native crash makes this process exit nonzero, which
is exactly the signal the parent keys on.
"""

from __future__ import annotations

import json
import os.path
import sys

# The parent launches this file BY PATH (not ``-m``), so it runs fine under an
# interpreter that does not have Monteur pip-installed — e.g. a bare Python 3.11
# pointed at via MONTEUR_RESOLVE_PYTHON while Monteur itself runs on 3.14. Put
# the directory that contains the ``monteur`` package on sys.path so
# ``import monteur.resolve`` (pure-Python, stdlib-only) works regardless.
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)


def _read_request() -> dict:
    """Read and parse the JSON request object from stdin (``{}`` if absent)."""
    try:
        data = sys.stdin.read()
    except Exception:
        return {}
    if not data or not data.strip():
        return {}
    try:
        parsed = json.loads(data)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def handle(command: str, request: dict) -> dict:
    """Perform one worker command and return a JSON-serializable response.

    Resolve is imported and connected to lazily (inside this function) so that
    any native crash happens here, in the child, rather than at module import.
    ``MonteurResolveError`` is converted to a clean ``false`` payload.
    """
    if command == "info":
        # Safe diagnostic: report this interpreter, whether Resolve's module
        # FILE and native library exist, and the Resolve-relevant environment
        # variables (with quoted/stale flags) — WITHOUT importing the native
        # part (so this never crashes).
        import os.path
        import struct

        from monteur.resolve import (
            _MODULE_NAME,
            _candidate_module_dirs,
            _env_report,
            _fusionscript_candidates,
            _locate_fusionscript,
        )

        dirs = _candidate_module_dirs()
        found = next(
            (
                d
                for d in dirs
                if os.path.isfile(os.path.join(d, _MODULE_NAME + ".py"))
            ),
            None,
        )
        return {
            "python_version": "%d.%d.%d" % sys.version_info[:3],
            "bits": struct.calcsize("P") * 8,
            "executable": sys.executable,
            "module_dir": found,
            "searched": dirs,
            "env": _env_report(),
            "resolve_install": {
                "library": _locate_fusionscript(),
                "searched": _fusionscript_candidates(),
            },
        }

    from monteur.resolve import (
        MonteurResolveError,
        _timeline_to_dict,
        connect,
    )

    if command == "status":
        try:
            bridge = connect()
            project = bridge.project_name()
            timelines = bridge.list_timelines()
            try:
                current = bridge.current_timeline_name()
            except MonteurResolveError:
                current = None
            return {
                "connected": True,
                "project": project,
                "timelines": timelines,
                "current": current,
            }
        except MonteurResolveError as exc:
            return {"connected": False, "error": str(exc)}

    if command == "read_timeline":
        try:
            bridge = connect()
            timeline = bridge.read_timeline(request.get("name"))
            return {"ok": True, "timeline": _timeline_to_dict(timeline)}
        except MonteurResolveError as exc:
            return {"ok": False, "error": str(exc)}

    if command == "build_plan":
        # monteur.montage is imported HERE, not at module top: it pulls in
        # numpy (via monteur.music), and every other command must keep working
        # under a bare, stdlib-only MONTEUR_RESOLVE_PYTHON interpreter.
        try:
            from monteur.montage import plan_from_dict
        except ImportError as exc:
            return {
                "ok": False,
                "error": (
                    "The Resolve worker interpreter cannot rebuild the "
                    f"montage plan: {exc}. Install Monteur's dependencies "
                    "for that interpreter (the one MONTEUR_RESOLVE_PYTHON "
                    "points at), e.g. 'python -m pip install numpy'."
                ),
            }
        try:
            plan = plan_from_dict(request.get("plan") or {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            fps = float(request["fps"])
        except (KeyError, TypeError, ValueError):
            return {
                "ok": False,
                "error": "build_plan request is missing a numeric 'fps'.",
            }
        name = str(request.get("name") or "Monteur Montage")
        titles = request.get("titles") or None
        canvas = request.get("canvas") or None
        warnings: list[str] = []
        try:
            bridge = connect()
            timeline_name = bridge.build_timeline_from_plan(
                plan, fps=fps, name=name, titles=titles, canvas=canvas,
                warnings=warnings,
            )
            return {"ok": True, "timeline": timeline_name, "warnings": warnings}
        except (MonteurResolveError, ValueError) as exc:
            # ValueError: an unknown canvas preset — a clean, handled failure.
            return {"ok": False, "error": str(exc)}

    return {"error": f"Unknown worker command: {command!r}"}


def load_test() -> None:
    """The ``load_test`` command: staged native load, one JSON line per stage.

    Unlike every other command (single JSON response), this streams a line
    per COMPLETED stage and flushes after each — so when a later stage
    hard-crashes this process, the parent still knows the last stage that
    succeeded from the partial stdout. Stages, in order:

    1. ``locate`` — find the fusionscript library file (no native code).
       Success: ``{"stage": "locate", "ok": true, "path": <lib>}``. When no
       library exists anywhere Monteur looks, a clean terminal
       ``{"stage": "locate", "ok": false, "error": ...}`` is emitted and
       nothing native is ever attempted.
    2. ``dll-load`` — ``ctypes.CDLL`` of the located library (after
       ``_register_resolve_dll_dir``, exactly like the real load path).
    3. ``import`` — ``import DaVinciResolveScript``.
    4. ``connect`` — ``scriptapp("Resolve")``; ``ok: false`` with the
       message when it returns None or raises cleanly (module fine, Resolve
       not reachable).

    Any stage failing CLEANLY (a catchable exception) emits
    ``{"stage": ..., "ok": false, "error": ...}`` and stops; the process
    still exits 0. Only an uncatchable native crash exits nonzero — and the
    parent (:func:`monteur.resolve.load_test_isolated`) pinpoints the crash
    site from the completed-stage trail.
    """
    import ctypes

    from monteur.resolve import (
        _locate_fusionscript,
        _register_resolve_dll_dir,
        find_scripting_module,
    )

    def emit(payload: dict) -> None:
        print(json.dumps(payload), flush=True)

    library = _locate_fusionscript()
    if library is None:
        emit(
            {
                "stage": "locate",
                "ok": False,
                "error": (
                    "No fusionscript library was found — DaVinci Resolve "
                    "does not appear to be installed in its standard "
                    "location (and RESOLVE_SCRIPT_LIB, if set, does not "
                    "point at one)."
                ),
            }
        )
        return
    emit({"stage": "locate", "ok": True, "path": library})
    _register_resolve_dll_dir()
    try:
        ctypes.CDLL(library)
    except Exception as exc:  # noqa: BLE001 - a clean load failure is data
        emit(
            {
                "stage": "dll-load",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return
    emit({"stage": "dll-load", "ok": True})
    try:
        module = find_scripting_module()
    except Exception as exc:  # noqa: BLE001 - a clean import failure is data
        emit({"stage": "import", "ok": False, "error": str(exc)})
        return
    emit({"stage": "import", "ok": True})
    try:
        app = module.scriptapp("Resolve")
    except Exception as exc:  # noqa: BLE001 - a clean connect failure is data
        emit(
            {
                "stage": "connect",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return
    if app is None:
        emit(
            {
                "stage": "connect",
                "ok": False,
                "error": (
                    "scriptapp('Resolve') returned nothing — the module is "
                    "fine, but no running Resolve was reached (is Resolve "
                    "running with external scripting set to Local?)."
                ),
            }
        )
        return
    emit({"stage": "connect", "ok": True})


def main(argv: list[str] | None = None) -> int:
    """Entry point. Always exits 0 unless the interpreter natively crashes."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""
    if command == "load_test":
        # Streams its own line-per-stage protocol; never reads stdin.
        try:
            load_test()
        except Exception as exc:  # noqa: BLE001 - must not look like a crash
            print(
                json.dumps(
                    {
                        "stage": "internal",
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                ),
                flush=True,
            )
        return 0
    request = _read_request()
    try:
        response = handle(command, request)
    except Exception as exc:  # noqa: BLE001 - a normal error must not look like a crash
        response = {"error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
