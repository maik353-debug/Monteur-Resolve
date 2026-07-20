"""Isolated worker subprocess for DaVinci Resolve scripting access.

Run as ``python -m monteur._resolve_worker <command>``. The worker reads a
JSON request object from stdin, writes a single JSON response object to stdout
and exits 0.

Why a separate process? Resolve's native scripting module (``fusionscript``,
loaded by ``DaVinciResolveScript``) can hard-crash the interpreter with a
C-level access violation when imported under an incompatible Python version
(current Resolve releases support roughly 3.10–3.12; older Resolve versions
accepted 3.6+). That crash cannot be caught with
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
    module): interpreter version/bits/executable/``platform``,
    ``module_dir`` (where DaVinciResolveScript.py was found, or null),
    ``searched``, ``env`` (RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB /
    PYTHONPATH / MONTEUR_RESOLVE_PYTHON, each with raw ``value``,
    ``quoted`` and ``exists`` flags — PYTHONPATH gets per-entry
    ``missing`` instead of ``exists``), ``resolve_install`` (``library``:
    the actual fusionscript.dll/.so found, or null; ``searched``: where it
    looked), ``registered_pythons`` (Windows only: the
    SOFTWARE\\Python\\PythonCore registry census, ``[{"version", "hive":
    "HKLM"|"HKCU", "path"}]``, ``[]`` elsewhere — fusionscript.dll loads
    the HIGHEST HKLM-registered Python, not the importing interpreter) and
    ``registry_highest`` (that highest HKLM version, or null).

``load_test``
    stdin: ignored. An exception to the single-JSON-response protocol:
    streams one JSON line per completed stage (``locate`` → ``dll-load`` →
    ``import`` → ``connect``), flushed after each, so a native crash in a
    later stage still leaves the completed stages on stdout. See
    :func:`load_test` for the exact stage payloads.

``render``
    stdin::

        {"timeline": <str|null>,     # null = the current timeline
         "target_dir": <str>,        # created (with parents) if missing
         "name": <str>,              # output file name (no extension)
         "preset": "2160p" | "1080p" | null}   # null = "2160p"

    The other STREAMED command (line-per-event, flushed after each, like
    ``load_test``): drives Resolve's Deliver page end-to-end — load a
    render preset (matched loosely against ``GetRenderPresetList()``, e.g.
    "YouTube - 2160p"; falling back to ``SetCurrentRenderFormatAndCodec``
    with mp4 + an H.264-ish codec when no preset matches), point
    ``SetRenderSettings`` at ``target_dir``/``name``, ``AddRenderJob()``,
    ``StartRendering([job_id])``, then poll ``IsRenderingInProgress()`` /
    ``GetRenderJobStatus(job_id)`` about every 2 seconds. Emitted lines::

        {"stage": "prepare", "ok": true, "preset": <what was actually
                    chosen: the preset name, or "ext/codec" for the
                    format/codec fallback>}
        {"stage": "progress", "percent": <int>}     # only when it changed
        {"stage": "done", "ok": true, "path": <TargetDir/CustomName, plus
                    the extension when knowable (fallback mode)>,
         "seconds": <wall-clock render time>}

    Any CLEAN failure (bad timeline name, no usable preset AND no workable
    format/codec, AddRenderJob/StartRendering refusing, a failed job
    status) emits a terminal ``{"stage": ..., "ok": false, "error": ...}``
    line and still exits 0.

    IMPORTANT — a render is never left unmonitored lightly: this worker
    only WATCHES the render; Resolve itself does the work. If the parent
    times out or kills this process (or the machine's Python crashes), the
    render keeps going inside Resolve — check Resolve's Deliver page. The
    parent's timeout message says exactly that.

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
         "canvas": <str> | null,                          # optional CANVASES key
                                                          # (e.g. "uhd", "cine-uhd")
         "audio": <str> | null}                           # montage audio mode:
                                                          # picks the SFX track for
                                                          # placed sound elements
                                                          # (default "music")

    Rebuilds the MontagePlan (``plan_from_dict``) and runs
    ``connect().build_timeline_from_plan(plan, fps=fps, name=name,
    titles=titles, canvas=canvas, audio=audio)`` — a canvas sets the
    timeline resolution, and the cinemascope presets also put "scale full
    frame with crop" on the footage. Response on success::

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
            _registered_pythons,
            _registry_highest,
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
        registered = _registered_pythons()  # guarded; [] off Windows
        return {
            "python_version": "%d.%d.%d" % sys.version_info[:3],
            "bits": struct.calcsize("P") * 8,
            "executable": sys.executable,
            "platform": sys.platform,
            "module_dir": found,
            "searched": dirs,
            "env": _env_report(),
            "resolve_install": {
                "library": _locate_fusionscript(),
                "searched": _fusionscript_candidates(),
            },
            # Windows: what fusionscript.dll actually loads is the highest
            # HKLM-registered Python — NOT this interpreter. See
            # monteur.resolve._registry_conflict_verdict.
            "registered_pythons": registered,
            "registry_highest": _registry_highest(registered),
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
        audio = str(request.get("audio") or "music")
        warnings: list[str] = []
        try:
            bridge = connect()
            timeline_name = bridge.build_timeline_from_plan(
                plan, fps=fps, name=name, titles=titles, canvas=canvas,
                warnings=warnings, audio=audio,
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


# How long the render command sleeps between IsRenderingInProgress polls.
# Module-level so tests (and desperate users) can shrink it.
_RENDER_POLL_SECONDS = 2.0

# The two qualities Monteur offers. Deliberately small: this is the "one
# more click and the video exists" button, not a Deliver-page replacement.
_RENDER_PRESETS = ("2160p", "1080p")


def _emit(payload: dict) -> None:
    """One flushed JSON line — the streamed commands' shared emitter."""
    print(json.dumps(payload), flush=True)


def _preset_names(project) -> list[str]:
    """GetRenderPresetList(), normalized to a list of names, defensively.

    Depending on the Resolve version the list holds plain strings or dicts
    (keyed ``RenderPresetName``); anything unreadable yields fewer names,
    never an error.
    """
    try:
        raw = project.GetRenderPresetList() or []
    except Exception:  # noqa: BLE001 - a refusing API means no presets
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("RenderPresetName") or entry.get("Name") or ""
            if name:
                names.append(str(name))
    return names


def _looks_h264(text: str) -> bool:
    """True when a codec key/label looks like H.264 (H264, H.264, h264...)."""
    return "h264" in (text or "").lower().replace(".", "").replace(" ", "")


def _select_render_quality(project, preset: str) -> tuple[str | None, str | None, str]:
    """Pick the render quality: a shipped preset, else the mp4/H.264 fallback.

    Returns ``(chosen_label, extension, error)`` — exactly one of
    ``chosen_label`` / ``error`` is set. Preset matching is loose and
    case-insensitive against the live ``GetRenderPresetList()``: a name
    containing both "youtube" and the wanted token ("2160p"/"1080p") wins
    (Resolve ships "YouTube - 2160p"-style presets), else any name
    containing the token. A matched name must also survive
    ``LoadRenderPreset``. When no preset works, the manual fallback walks
    ``GetRenderFormats()`` for an mp4 format and ``GetRenderCodecs`` for an
    H.264-ish codec and applies ``SetCurrentRenderFormatAndCodec``; its
    label is ``"<ext>/<codec>"`` and the extension makes the output path
    knowable. Every Resolve call is guarded — versions differ.
    """
    token = preset.lower()
    names = _preset_names(project)
    best = next(
        (n for n in names if "youtube" in n.lower() and token in n.lower()), None
    )
    if best is None:
        best = next((n for n in names if token in n.lower()), None)
    if best is not None:
        try:
            loaded = bool(project.LoadRenderPreset(best))
        except Exception:  # noqa: BLE001 - fall through to the manual setup
            loaded = False
        if loaded:
            return best, None, ""
    # Manual fallback: mp4 + an H.264-ish codec.
    try:
        formats = dict(project.GetRenderFormats() or {})
    except Exception:  # noqa: BLE001
        formats = {}
    ext = None
    for desc, extension in formats.items():
        candidate = str(extension or "").lower().lstrip(".")
        if candidate == "mp4" or str(desc).lower() == "mp4":
            ext = candidate or "mp4"
            break
    if ext is None:
        return None, None, (
            "Resolve offered no matching render preset (looked for "
            f"{preset!r} in {names or 'an empty preset list'}) and no mp4 "
            "render format — render from Resolve's Deliver page instead."
        )
    try:
        codecs = dict(project.GetRenderCodecs(ext) or {})
    except Exception:  # noqa: BLE001
        codecs = {}
    codec = None
    for desc, key in codecs.items():
        if _looks_h264(str(key)) or _looks_h264(str(desc)):
            codec = str(key or desc)
            break
    if codec is None:
        return None, None, (
            "Resolve offered no H.264 codec for its mp4 format — render "
            "from Resolve's Deliver page instead."
        )
    try:
        applied = bool(project.SetCurrentRenderFormatAndCodec(ext, codec))
    except Exception as exc:  # noqa: BLE001
        return None, None, (
            f"Resolve refused the mp4/{codec} render setup: "
            f"{type(exc).__name__}: {exc}"
        )
    if not applied:
        return None, None, (
            f"Resolve refused the mp4/{codec} render setup "
            "(SetCurrentRenderFormatAndCodec returned false)."
        )
    return f"{ext}/{codec}", ext, ""


def _render_percent(status: dict) -> int | None:
    """CompletionPercentage from a job-status dict, defensively, or None."""
    if not isinstance(status, dict):
        return None
    value = status.get("CompletionPercentage")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _job_status(project, job_id) -> dict:
    """GetRenderJobStatus(job_id) as a dict, defensively ({} on refusal)."""
    try:
        status = project.GetRenderJobStatus(job_id)
    except Exception:  # noqa: BLE001 - keys and behavior differ across versions
        return {}
    return status if isinstance(status, dict) else {}


def render(request: dict) -> None:
    """The ``render`` command: drive Resolve's Deliver engine, streamed.

    See the module docstring for the wire format. Structure: everything up
    to (and including) ``AddRenderJob`` is the *prepare* phase — any clean
    failure there emits ``{"stage": "prepare", "ok": false, ...}``; from
    ``StartRendering`` on, failures are ``{"stage": "render", ...}``. This
    process only monitors: killing it does NOT stop the render in Resolve.
    """
    import time

    from monteur.resolve import MonteurResolveError, connect

    target_dir = str(request.get("target_dir") or "").strip()
    name = str(request.get("name") or "").strip() or "monteur_render"
    preset = request.get("preset") or "2160p"
    if preset not in _RENDER_PRESETS:
        _emit(
            {
                "stage": "prepare",
                "ok": False,
                "error": (
                    f"unknown render preset {preset!r} — use "
                    + " or ".join(repr(p) for p in _RENDER_PRESETS)
                ),
            }
        )
        return
    if not target_dir:
        _emit(
            {
                "stage": "prepare",
                "ok": False,
                "error": "the render request is missing a 'target_dir'.",
            }
        )
        return
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        _emit(
            {
                "stage": "prepare",
                "ok": False,
                "error": (
                    f"could not create the render folder {target_dir!r}: {exc}"
                ),
            }
        )
        return

    try:
        bridge = connect()
        project = bridge._project()
        timeline_name = request.get("timeline")
        if timeline_name:
            timeline = bridge._timeline_by_name(str(timeline_name))
            project.SetCurrentTimeline(timeline)
        else:
            bridge._current_timeline()  # no timeline at all -> clean error
    except MonteurResolveError as exc:
        _emit({"stage": "prepare", "ok": False, "error": str(exc)})
        return

    # Cosmetic but faithful to the Deliver workflow; older hosts may lack it.
    open_page = getattr(bridge.app, "OpenPage", None)
    if callable(open_page):
        try:
            open_page("deliver")
        except Exception:  # noqa: BLE001 - purely cosmetic
            pass

    chosen, ext, error = _select_render_quality(project, preset)
    if chosen is None:
        _emit({"stage": "prepare", "ok": False, "error": error})
        return

    settings = {
        "SelectAllFrames": True,
        "TargetDir": target_dir,
        "CustomName": name,
    }
    try:
        accepted = project.SetRenderSettings(settings)
    except Exception as exc:  # noqa: BLE001 - a refusing API is a clean failure
        _emit(
            {
                "stage": "prepare",
                "ok": False,
                "error": f"Resolve refused the render settings: {exc}",
            }
        )
        return
    if accepted is False:
        _emit(
            {
                "stage": "prepare",
                "ok": False,
                "error": (
                    "Resolve refused the render settings "
                    "(SetRenderSettings returned false)."
                ),
            }
        )
        return
    try:
        job_id = project.AddRenderJob()
    except Exception as exc:  # noqa: BLE001
        job_id = None
        add_error = f": {type(exc).__name__}: {exc}"
    else:
        add_error = "."
    if not job_id:
        _emit(
            {
                "stage": "prepare",
                "ok": False,
                "error": (
                    "Resolve did not create a render job "
                    "(AddRenderJob returned nothing)" + add_error
                ),
            }
        )
        return
    _emit({"stage": "prepare", "ok": True, "preset": chosen})

    started_at = time.monotonic()
    try:
        started = project.StartRendering([job_id])
    except Exception as exc:  # noqa: BLE001
        started = False
        start_error = f": {type(exc).__name__}: {exc}"
    else:
        start_error = "."
    if not started:
        _emit(
            {
                "stage": "render",
                "ok": False,
                "error": (
                    "Resolve did not start the render "
                    "(StartRendering returned false)" + start_error
                ),
            }
        )
        return

    last_percent: int | None = None
    while True:
        try:
            in_progress = bool(project.IsRenderingInProgress())
        except Exception:  # noqa: BLE001 - treat a refusing poll as finished
            in_progress = False
        percent = _render_percent(_job_status(project, job_id))
        if percent is not None and percent != last_percent:
            _emit({"stage": "progress", "percent": percent})
            last_percent = percent
        if not in_progress:
            break
        if _RENDER_POLL_SECONDS > 0:
            time.sleep(_RENDER_POLL_SECONDS)

    final = _job_status(project, job_id)
    job_state = str(final.get("JobStatus") or "")
    if "fail" in job_state.lower() or "cancel" in job_state.lower():
        detail = str(final.get("Error") or "").strip()
        _emit(
            {
                "stage": "render",
                "ok": False,
                "error": (
                    f"Resolve reported the render job as {job_state!r}"
                    + (f": {detail}" if detail else ".")
                ),
            }
        )
        return
    path = os.path.join(target_dir, name + (f".{ext}" if ext else ""))
    _emit(
        {
            "stage": "done",
            "ok": True,
            "path": path,
            "seconds": round(time.monotonic() - started_at, 2),
        }
    )


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
    if command == "render":
        # Streamed like load_test, but driven by a stdin request.
        try:
            render(request)
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
    try:
        response = handle(command, request)
    except Exception as exc:  # noqa: BLE001 - a normal error must not look like a crash
        response = {"error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
