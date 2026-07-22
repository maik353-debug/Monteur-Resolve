"""Entry point for the packaged Monteur desktop app (PyInstaller).

Double-clicking the built executable opens Monteur Studio in its own native
window (WebView2 on Windows). A staged update, if any, installs before the
window opens — that logic lives inside ``serve_app``.

Kept deliberately tiny: all real behaviour is in the ``monteur`` package, so
the packaged app and ``monteur ui --window`` run exactly the same code.
"""

from __future__ import annotations

import multiprocessing


def main() -> None:
    # Monteur isolates a few risky operations (e.g. the Resolve bridge) in
    # child processes; a frozen build must call this before spawning any, or
    # each child would re-launch the whole app.
    multiprocessing.freeze_support()

    from monteur.web import serve_app

    serve_app()


if __name__ == "__main__":
    main()
