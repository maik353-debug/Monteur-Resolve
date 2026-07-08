"""Monteur Studio — local web UI server.

A zero-dependency local server (stdlib only): serves the single-page app in
``app.html`` and a small JSON API on top of Monteur's analysis engine. Started
via ``monteur ui``. Binds to 127.0.0.1 — this is a local tool, not a network
service.

API (all JSON):

* ``POST /api/analyze``   {"filename", "content", "fps"?}      -> {"stats"}
* ``POST /api/compare``   {"a": <analyze payload>, "b": ...}   -> {"a", "b", "compare"}
* ``GET  /api/versions``                                        -> {"versions": [...]}
* ``POST /api/versions``  {analyze payload + "label"?}         -> {"version", "stats"}
* ``GET  /api/versions/<id>``                                   -> {"stats"}
* ``DELETE /api/versions/<id>``                                 -> {"ok": true}
* ``GET  /api/resolve/status``                                  -> {"connected", ...}
* ``POST /api/resolve/analyze`` {"timeline"?, "save"?}          -> {"stats", "version"?}

Timeline content is passed as text (EDL/FCPXML are text formats); ``fps`` is
required for EDL files.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from monteur import __version__
from monteur.analysis import analyze_timeline, compare
from monteur.project import Project

_APP_HTML = Path(__file__).with_name("app.html")

# Writing a response to a socket the browser already closed raises one of these
# — very common on Windows (WinError 10053 ConnectionAbortedError / 10054
# ConnectionResetError). The client simply went away; it is not worth crashing a
# worker thread over. (ConnectionReset/Aborted/BrokenPipe are all subclasses of
# ConnectionError, but we spell them out for clarity / defensiveness.)
_CLIENT_GONE = (
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


def _install_diagnostic_hooks():
    """Install excepthooks that make an otherwise-silent crash VISIBLE.

    A crash inside a ThreadingHTTPServer worker thread (i.e. while handling a
    request) never reaches serve()'s main-thread ``except`` — it dies in the
    worker. Without a ``threading.excepthook`` that would print a traceback and
    vanish (or, worse, be swallowed). We install one here that always flushes,
    and chain to whatever was installed before.

    Returns ``(prev_threading_hook, prev_sys_hook)`` so serve() can restore the
    originals in its ``finally`` block — importing this module must not globally
    mutate the hooks (keeps the test suite clean).
    """
    prev_thread_hook = threading.excepthook
    prev_sys_hook = sys.excepthook

    def worker_hook(args):
        import traceback

        name = getattr(args.thread, "name", "?")
        print(
            f"Monteur Studio: uncaught error in worker thread {name}:",
            flush=True,
        )
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback
        )
        sys.stderr.flush()
        if prev_thread_hook is not None:
            try:
                prev_thread_hook(args)
            except Exception:  # noqa: BLE001 - a chained hook must not re-crash
                pass

    def main_hook(exc_type, exc_value, exc_tb):
        try:
            if prev_sys_hook is not None:
                prev_sys_hook(exc_type, exc_value, exc_tb)
        finally:
            sys.stderr.flush()
            sys.stdout.flush()

    threading.excepthook = worker_hook
    sys.excepthook = main_hook
    return prev_thread_hook, prev_sys_hook


def _restore_diagnostic_hooks(prev_thread_hook, prev_sys_hook) -> None:
    """Undo :func:`_install_diagnostic_hooks` (used by serve()'s finally)."""
    threading.excepthook = prev_thread_hook
    sys.excepthook = prev_sys_hook


class MonteurServer(ThreadingHTTPServer):
    """Hardened server class used by serve().

    * ``daemon_threads`` — worker threads don't keep the process alive on exit.
    * ``allow_reuse_address`` — restart-friendly (no TIME_WAIT bind failures).
    * ``handle_error`` — one concise line per bad request instead of
      socketserver's default multi-line stderr dump, so a single dropped
      connection never *looks* catastrophic.
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_GONE):
            return  # the client simply disconnected — not worth a line
        print(
            f"Monteur Studio: error serving {client_address} — "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        import traceback

        traceback.print_exc()  # a real error here deserves its full stack
        sys.stderr.flush()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _analyze_payload(payload: dict):
    from monteur.io import edl, fcpxml

    filename = payload.get("filename", "")
    content = payload.get("content")
    if not content:
        raise ApiError(400, "missing 'content'")
    suffix = Path(filename).suffix.lower()
    if suffix == ".edl":
        fps = payload.get("fps")
        if not fps:
            raise ApiError(400, "EDL files need 'fps'")
        timeline = edl.read_edl(content, fps=float(fps), name=Path(filename).stem)
    elif suffix in (".xml", ".fcpxml"):
        timeline = fcpxml.read_fcpxml(content)
        if not timeline.name:
            timeline.name = Path(filename).stem
    else:
        raise ApiError(400, f"unsupported file type: {filename!r} (use .edl or .fcpxml)")
    return analyze_timeline(timeline)


def _scan_progress():
    """Per-clip sift progress printed to the server terminal.

    The browser gets no streaming feedback during a scan, so the terminal that
    launched ``monteur ui`` is where a long scan shows life. Reuses the CLI's
    ``_sift_progress`` line format ("[i/N] name ..." then "[i/N] name — X%
    usable"); imported lazily (monteur.cli only imports monteur.web inside its
    ``ui`` command, so there is no import cycle), with a minimal local copy as
    a fallback if the import ever fails.
    """
    try:
        from monteur.cli import _sift_progress

        return _sift_progress
    except ImportError:
        pass

    def fallback(index, total, name, stage, report):
        if stage == "start":
            print(f"[{index}/{total}] {name} ...", flush=True)
        elif stage == "done":
            print(
                f"[{index}/{total}] {name} — "
                f"{report.usable_ratio * 100:.0f}% usable",
                flush=True,
            )

    return fallback


class MonteurHandler(BaseHTTPRequestHandler):
    server_version = f"MonteurStudio/{__version__}"
    project: Project  # set by serve()

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # keep the terminal quiet

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            pass  # client closed the socket mid-response — nothing to do

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "empty request body")
        if length > 64 * 1024 * 1024:
            raise ApiError(413, "request too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"invalid JSON body: {exc}")

    def _dispatch(self, method: str) -> None:
        try:
            handler = self._route(method)
            handler()
        except ApiError as exc:
            self._send_json({"error": exc.message}, status=exc.status)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # a genuine handler bug — surface it, don't hide it
            import traceback

            print(
                f"Monteur Studio: unhandled error in {method} {self.path}:",
                flush=True,
            )
            traceback.print_exc()
            sys.stderr.flush()
            self._send_json({"error": f"internal error: {exc}"}, status=500)

    def _route(self, method: str):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        routes = {
            ("GET", "/"): self._app,
            ("GET", "/favicon.ico"): self._favicon,
            ("GET", "/api/versions"): self._versions_list,
            ("POST", "/api/versions"): self._versions_add,
            ("POST", "/api/analyze"): self._analyze,
            ("POST", "/api/compare"): self._compare,
            ("GET", "/api/resolve/status"): self._resolve_status,
            ("POST", "/api/resolve/analyze"): self._resolve_analyze,
            ("POST", "/api/assembly/plan"): self._assembly_plan,
            ("POST", "/api/assembly/export"): self._assembly_export,
            ("POST", "/api/create/scan"): self._create_scan,
            ("POST", "/api/create/build"): self._create_build,
        }
        if (method, path) in routes:
            return routes[(method, path)]
        if path.startswith("/api/versions/"):
            tail = path.rsplit("/", 1)[1]
            if tail.isdigit():
                vid = int(tail)
                if method == "GET":
                    return lambda: self._versions_get(vid)
                if method == "DELETE":
                    return lambda: self._versions_delete(vid)
        raise ApiError(404, f"no route for {method} {path}")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # -- endpoints --------------------------------------------------------

    def _app(self) -> None:
        body = _APP_HTML.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            pass  # browser closed the tab mid-load — not an error

    def _favicon(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            '<rect width="16" height="16" rx="3" fill="#2a78d6"/>'
            '<path d="M3 11 6.5 6l2.5 3 2-2.5L13 11z" fill="#fff"/></svg>'
        ).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
        except _CLIENT_GONE:
            pass  # client went away — nothing to send to

    def _analyze(self) -> None:
        stats = _analyze_payload(self._read_json())
        self._send_json({"stats": asdict(stats)})

    def _compare(self) -> None:
        payload = self._read_json()
        if "a" not in payload or "b" not in payload:
            raise ApiError(400, "compare needs 'a' and 'b'")
        stats_a = _analyze_payload(payload["a"])
        stats_b = _analyze_payload(payload["b"])
        self._send_json(
            {
                "a": asdict(stats_a),
                "b": asdict(stats_b),
                "compare": compare(stats_a, stats_b),
            }
        )

    def _versions_list(self) -> None:
        self._send_json({"versions": self.project.versions()})

    def _versions_add(self) -> None:
        payload = self._read_json()
        stats = _analyze_payload(payload)
        entry = self.project.add_version(
            stats,
            label=payload.get("label", ""),
            source_file=payload.get("filename", ""),
            saved_at=time.strftime("%Y-%m-%d %H:%M"),
        )
        entry = {k: v for k, v in entry.items() if k != "stats"}
        self._send_json({"version": entry, "stats": asdict(stats)})

    def _versions_get(self, version_id: int) -> None:
        try:
            stats = self.project.get_stats(version_id)
        except KeyError as exc:
            raise ApiError(404, exc.args[0])
        self._send_json({"stats": asdict(stats)})

    def _versions_delete(self, version_id: int) -> None:
        self.project.delete_version(version_id)
        self._send_json({"ok": True})

    def _assembly_inputs(self, payload: dict):
        from monteur.io import read_srt, read_whisper_json
        from monteur.assembly import TakeSource
        from monteur.screenplay import parse_fountain
        from monteur.transcribe import scene_take_from_name

        script = payload.get("script") or {}
        if not script.get("content"):
            raise ApiError(400, "missing script content")
        screenplay = parse_fountain(script["content"])
        takes = []
        for item in payload.get("takes") or []:
            filename = item.get("filename", "")
            content = item.get("content", "")
            if not content:
                continue
            stem = Path(filename).stem
            if filename.lower().endswith(".json"):
                transcript = read_whisper_json(content, source_name=stem)
            else:
                transcript = read_srt(content, source_name=stem)
            scene_hint, take_hint = scene_take_from_name(filename)
            takes.append(
                TakeSource(
                    name=stem, transcript=transcript,
                    scene_hint=scene_hint, take_hint=take_hint,
                )
            )
        if not takes:
            raise ApiError(400, "no readable take transcripts (.srt/.json) provided")
        forced = {
            int(k): v for k, v in (payload.get("forced") or {}).items() if str(v)
        }
        return screenplay, takes, forced

    def _assembly_plan(self) -> None:
        from monteur.assembly import plan_assembly
        from monteur.screenplay import DIALOGUE

        payload = self._read_json()
        screenplay, takes, forced = self._assembly_inputs(payload)
        plan = plan_assembly(
            screenplay, takes,
            max_takes_per_scene=int(payload.get("max_takes") or 1),
            forced=forced,
        )
        scenes = [
            {
                "heading": s.heading,
                "number": s.number,
                "dialogue": [
                    {"index": i, "character": e.character, "text": e.text}
                    for i, e in enumerate(s.elements)
                    if e.kind == DIALOGUE
                ],
            }
            for s in screenplay.scenes
        ]
        self._send_json(
            {
                "screenplay": {"title": screenplay.title, "scenes": scenes},
                "plan": asdict(plan),
                "coverage": plan.coverage(),
                "takes": [t.name for t in takes],
            }
        )

    def _assembly_export(self) -> None:
        from monteur.assembly import assembly_to_timeline, plan_assembly
        from monteur.io import write_edl, write_fcpxml

        payload = self._read_json()
        screenplay, takes, forced = self._assembly_inputs(payload)
        fps = float(payload.get("fps") or 25)
        plan = plan_assembly(
            screenplay, takes,
            max_takes_per_scene=int(payload.get("max_takes") or 1),
            forced=forced,
        )
        handles_raw = payload.get("handles")
        timeline = assembly_to_timeline(
            plan, takes, fps=fps,
            handles=0.5 if handles_raw is None else float(handles_raw),
        )
        if not timeline.clips:
            raise ApiError(422, "nothing matched — no segments to export")
        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, filename = write_edl(timeline), "monteur_assembly.edl"
        elif fmt == "fcpxml":
            content, filename = write_fcpxml(timeline), "monteur_assembly.fcpxml"
        else:
            raise ApiError(400, f"unknown format {fmt!r} (use 'edl' or 'fcpxml')")
        self._send_json({"filename": filename, "content": content})

    def _create_scan(self) -> None:
        from monteur.media import MonteurMediaError
        from monteur.sift import sift_directory

        payload = self._read_json()
        folder = payload.get("folder", "")
        if not folder:
            raise ApiError(400, "missing 'folder' (path to your footage)")
        try:
            reports = sift_directory(folder, progress=_scan_progress())
        except MonteurMediaError as exc:
            raise ApiError(422, str(exc))
        if not reports:
            raise ApiError(422, f"no video files found in {folder}")
        self._send_json({"clips": [asdict(r) for r in reports]})

    def _create_build(self) -> None:
        from monteur.media import MonteurMediaError
        from monteur.montage import CHRONOLOGICAL, montage_to_timeline, plan_montage
        from monteur.music import analyze_music
        from monteur.sift import sift_directory
        from monteur.io import write_edl, write_fcpxml

        payload = self._read_json()
        folder = payload.get("folder", "")
        music_path = payload.get("music", "")
        if not folder or not music_path:
            raise ApiError(400, "need 'folder' (footage) and 'music' (song file)")
        try:
            reports = sift_directory(folder, progress=_scan_progress())
            music = analyze_music(music_path)
        except MonteurMediaError as exc:
            raise ApiError(422, str(exc))
        max_duration = payload.get("max_duration")
        plan = plan_montage(
            reports,
            music,
            order=payload.get("order") or CHRONOLOGICAL,
            max_duration=float(max_duration) if max_duration else None,
            style=payload.get("style") or "auto",
        )
        if not plan.entries:
            raise ApiError(422, "no usable material found — check the scan results")
        fps = float(payload.get("fps") or 25)
        timeline = montage_to_timeline(plan, fps=fps)
        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, filename = write_edl(timeline), "monteur_montage.edl"
        else:
            content, filename = write_fcpxml(timeline), "monteur_montage.fcpxml"
        self._send_json(
            {
                "filename": filename,
                "content": content,
                "plan": {
                    "duration": plan.duration,
                    "cuts": len(plan.entries),
                    "tempo": music.tempo,
                    "notes": plan.notes,
                },
            }
        )

    def _resolve_status(self) -> None:
        # Isolated in a child process: Resolve's native module can hard-crash
        # (access violation) under an incompatible Python, and that would take
        # the whole server down. resolve_status_isolated never raises.
        from monteur.resolve import resolve_status_isolated

        self._send_json(resolve_status_isolated())

    def _resolve_analyze(self) -> None:
        from monteur.resolve import MonteurResolveError, read_timeline_isolated

        payload = self._read_json()
        try:
            timeline = read_timeline_isolated(payload.get("timeline"))
        except MonteurResolveError as exc:
            raise ApiError(502, str(exc))
        stats = analyze_timeline(timeline)
        response: dict = {"stats": asdict(stats)}
        if payload.get("save"):
            entry = self.project.add_version(
                stats,
                label=payload.get("label", ""),
                source_file="DaVinci Resolve",
                saved_at=time.strftime("%Y-%m-%d %H:%M"),
            )
            response["version"] = {k: v for k, v in entry.items() if k != "stats"}
        self._send_json(response)


def serve(
    port: int = 8765,
    project_root: str = ".",
    open_browser: bool = True,
    ready: threading.Event | None = None,
    on_bind=None,
) -> None:
    """Run Monteur Studio until interrupted.

    ``on_bind`` (optional) is called with the bound server object right before
    the serve loop starts — a small seam so callers/tests can shut the server
    down cleanly from another thread.
    """
    # faulthandler turns a C-level access violation (the prime suspect for
    # "process just vanishes with no Python exception" while serving) into a
    # printed native traceback instead of a silent death. Idempotent + guarded
    # so enabling it can never itself take the server down.
    try:
        import faulthandler

        if not faulthandler.is_enabled():
            # enable() captures stderr's fileno and raises if there isn't one
            # (e.g. under pythonw.exe). Fall back to the original stderr, then
            # to a log file, so a native crash is never silent just because the
            # console stream is unusual.
            try:
                faulthandler.enable(all_threads=True)
            except (ValueError, AttributeError, OSError):
                target = getattr(sys, "__stderr__", None)
                if target is not None:
                    faulthandler.enable(file=target, all_threads=True)
                else:
                    log = open(Path(project_root) / "monteur-crash.log", "a")
                    faulthandler.enable(file=log, all_threads=True)
                    print(
                        f"(Native-crash logging goes to {log.name})", flush=True
                    )
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break startup
        print(
            f"(Note: could not enable native crash reporting: {exc})", flush=True
        )

    # Make worker-thread and main-thread crashes visible; restore on the way out
    # so importing/embedding this module does not permanently mutate the hooks.
    prev_hooks = _install_diagnostic_hooks()

    handler = type("BoundHandler", (MonteurHandler,), {"project": Project(project_root)})
    server = None
    for candidate in range(port, port + 10):
        try:
            server = MonteurServer(("127.0.0.1", candidate), handler)
            break
        except OSError as exc:
            bind_error = exc
    if server is None:
        _restore_diagnostic_hooks(*prev_hooks)
        raise OSError(
            f"ports {port}-{port + 9} are all in use ({bind_error}) — "
            f"is another Monteur Studio still running?"
        )
    if server.server_address[1] != port:
        print(f"Port {port} is busy — using {server.server_address[1]} instead.", flush=True)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Monteur Studio running at {url}", flush=True)
    print("Leave this window open. Press Ctrl+C here to stop.", flush=True)
    if ready is not None:
        ready.set()
    if on_bind is not None:
        on_bind(server)
    if open_browser:
        _open_browser_safely(url)
    try:
        server.serve_forever()
        print("\nMonteur Studio exited unexpectedly (the serve loop returned "
              "on its own).", flush=True)
    except KeyboardInterrupt:
        print("\nMonteur Studio stopped (Ctrl+C).", flush=True)
    except BaseException as exc:  # noqa: BLE001 - surface EVERY exit reason
        import traceback

        print(f"\nMonteur Studio stopped via {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        raise
    finally:
        server.server_close()
        _restore_diagnostic_hooks(*prev_hooks)


def _open_browser_safely(url: str) -> None:
    """Open the browser without ever taking the server down with it."""
    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a browser failure must not matter
            print(f"(Could not open a browser automatically — visit {url} yourself.)",
                  flush=True)

    threading.Thread(target=_open, daemon=True).start()
