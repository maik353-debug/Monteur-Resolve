# PyInstaller spec for the Monteur desktop app.
#
#   pyinstaller packaging/monteur.spec        (from the repo root)
#   # or, with the version baked into the filename:
#   python scripts/build_exe.py
#
# Produces a single self-contained executable (no Python needed on the target).
# On Windows the WebView2 runtime ships with modern Edge, so the native window
# just works; the [app] extra (pywebview) must be installed in the build env.

import sys
from pathlib import Path

# `pyinstaller <spec>` runs this file with SPECPATH set to its directory.
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH injected by PyInstaller

datas = [
    # the Studio UI is loaded via Path(__file__).with_name("app.html"), so it
    # must sit next to server.py inside the bundle
    (str(ROOT / "monteur" / "web" / "app.html"), "monteur/web"),
]

# pywebview pulls its GUI backend lazily; help PyInstaller find the ones that
# exist in the build environment (missing ones are simply skipped).
hiddenimports = []
for mod in ("webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
            "webview.platforms.cocoa", "webview.platforms.gtk", "webview.platforms.qt",
            "clr"):
    hiddenimports.append(mod)

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "monteur_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],  # keep tkinter — the native file/folder pickers use it
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Monteur",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,          # a GUI app — no console window on Windows
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "monteur.ico") if (ROOT / "packaging" / "monteur.ico").exists() else None,
)
