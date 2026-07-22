# Packaging Monteur as a desktop app

Monteur ships as a single self-contained executable so users don't need Python.
The build wraps the same code as `monteur ui --window` — a native window
(WebView2 on Windows) around the local Studio server.

## Build it

On the OS you want to target (PyInstaller does **not** cross-compile — a
Windows `.exe` must be built on Windows):

```bash
pip install -e '.[app,build]'
python scripts/build_exe.py
```

The result lands in `dist/`, named with the version and platform, e.g.
`dist/Monteur-0.1.0-windows.exe`. Ship that one file.

Under the hood it runs the PyInstaller spec directly:

```bash
pyinstaller --noconfirm --clean packaging/monteur.spec
```

- Entry point: `packaging/monteur_app.py` (opens the native window).
- Bundled data: `monteur/web/app.html` is placed next to `server.py` inside
  the bundle, exactly where `Path(__file__).with_name("app.html")` looks.
- `console=False`: no console window on Windows.
- Optional icon: drop `packaging/monteur.ico` and it's picked up automatically.

## Updates

The packaged build updates itself — see **Help → Check for updates…** in the
app, or `monteur update` on the CLI:

- **Check** hits the GitHub Releases API for `maik353-debug/Monteur-Resolve`
  (override with `MONTEUR_UPDATE_REPO`) and compares tags.
- **Install** downloads the matching asset (`*.exe` on Windows) into
  `~/.monteur/updates/` and writes a `pending.json` marker.
- The swap happens on the **next launch** (`update.apply_pending`, called from
  `serve_app`) — a process can't overwrite the executable it's running from, so
  the running exe is renamed aside and the new build takes its place.

For the updater to find anything, publish a GitHub Release whose tag is the new
version (e.g. `v0.2.0`) with the built executable attached as a release asset.
A source checkout (not frozen) never swaps files — it just points you at
`git pull` / `pip install -U monteur`.

## Notes

- The `[app]` extra (pywebview) must be present in the build environment.
- On Windows, pywebview uses WebView2, bundled with modern Edge; nothing extra
  to install on the target.
- macOS/Linux builds work too (WebKit / GTK/Qt); the same spec covers them.
