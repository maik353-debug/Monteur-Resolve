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
        # Safe diagnostic: report this interpreter and whether Resolve's module
        # FILE exists — WITHOUT importing the native part (so this never crashes).
        import os.path
        import struct

        from monteur.resolve import _MODULE_NAME, _candidate_module_dirs

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

    return {"error": f"Unknown worker command: {command!r}"}


def main(argv: list[str] | None = None) -> int:
    """Entry point. Always exits 0 unless the interpreter natively crashes."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""
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
