"""Keep child processes from flashing a console window on Windows.

A windowless GUI parent — Monteur launched via ``pythonw`` or a frozen
``--windowed`` build — makes Windows pop a black console window for EVERY
console child it spawns: ffmpeg for a proxy or a preview, ffprobe, whisper,
git for an update, the ``claude`` CLI. Dozens of them, flickering up and
vanishing, look for all the world like something is installing itself on the
machine. :data:`NO_WINDOW` carries the ``CREATE_NO_WINDOW`` creation flag so
every spawn stays invisible; off Windows it is an empty dict, so passing
``**NO_WINDOW`` is a no-op everywhere else (and in the test suite).
"""

from __future__ import annotations

import subprocess
import sys

#: subprocess ``run`` / ``Popen`` kwargs that suppress a child's console window
#: on Windows; an empty dict on every other platform (so ``**NO_WINDOW`` is a
#: no-op, byte-identical to the call without it).
NO_WINDOW: dict = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if sys.platform == "win32"
    else {}
)
