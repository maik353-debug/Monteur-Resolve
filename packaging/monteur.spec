# PyInstaller spec for the Monteur desktop app — the SHELL half.
#
#   pyinstaller packaging/monteur.spec        (from the repo root)
#   # or, to also emit the distributable payload zip + checksum:
#   python scripts/build_exe.py
#
# The app is split shell/payload (see monteur/payload.py). This shell = the
# PyInstaller bootloader + Python + third-party deps + the launcher, and it
# carries a BASELINE payload (the monteur package + app.html) as data — not as
# frozen code — so the launcher can prefer a newer, downloaded payload at
# startup. `monteur` is therefore excluded from code analysis and its runtime
# deps are named as hiddenimports so they stay in the shell.

import json
import re
import shutil
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH injected by PyInstaller


def _version() -> str:
    text = (ROOT / "monteur" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0"


def _stage_baseline_payload() -> Path:
    """Copy monteur/ + a payload.json into a clean staging dir for bundling."""
    stage = ROOT / "build" / "payload_stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(
        ROOT / "monteur", stage / "monteur",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (stage / "payload.json").write_text(
        json.dumps({"version": _version()}, indent=2), encoding="utf-8"
    )
    return stage


STAGE = _stage_baseline_payload()

# pywebview pulls its GUI backend lazily; name the ones that might exist so the
# shell can open the native window (missing ones are simply skipped).
hiddenimports = [
    "webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
    "webview.platforms.cocoa", "webview.platforms.gtk", "webview.platforms.qt",
    "clr",
]

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "monteur_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(STAGE / "payload.json"), ".")],   # baseline version marker
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
# Analysis followed monteur's imports so every stdlib + third-party dependency
# is now frozen in the shell — but drop monteur's OWN modules from the frozen
# code, so at runtime `import monteur` comes from the on-disk payload (baseline
# or a downloaded update) instead of a copy baked into the executable.
def _not_ours(entry):
    name = entry[0]
    return not (name == "monteur" or name.startswith("monteur."))

a.pure = TOC([e for e in a.pure if _not_ours(e)])

# the baseline payload: the monteur package as data at monteur/ inside the bundle
a.datas += Tree(str(STAGE / "monteur"), prefix="monteur", excludes=["__pycache__", "*.pyc"])

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
