# Packaging Monteur as a desktop app

Monteur ships as a single self-contained executable so users don't need Python.
It's split like Electron — a **shell** and an **app payload** — so updates are
small and don't touch the executable:

- **Shell** (`Monteur.exe`, ~70 MB): the PyInstaller bootloader + Python + all
  third-party deps + a tiny launcher. Changes rarely (only when deps change).
- **Payload** (`monteur-app-<version>.zip`, ~650 KB): the `monteur` package +
  `app.html`. Changes every release. This is what the in-app updater downloads.

The launcher puts the **newest payload on disk** — the baseline baked into the
shell, or a newer one the updater dropped in `~/.monteur/payloads/` — on
`sys.path` before importing `monteur`, so an update takes effect on the next
launch with no executable swap. (Verified: the same shell reports the baseline
version, then a newer version once a payload is installed beside it.)

## Build it

On the OS you want to target (PyInstaller does **not** cross-compile — a
Windows `.exe` must be built on Windows):

```bash
pip install -e '.[app,build]'
python scripts/build_exe.py
```

`dist/` then holds three things:

| File | Upload to the Release? | What it is |
|------|------------------------|------------|
| `Monteur-<version>-<platform>.exe` | yes (per platform) | the shell — first install / a deps change |
| `monteur-app-<version>.zip` | **yes** | the payload — every release |
| `monteur-app-<version>.zip.sha256` | **yes** | the payload checksum |

Under the hood:

- `scripts/build_payload.py` makes the payload zip + checksum.
- `packaging/monteur.spec` builds the shell: it analyses `monteur` (so every
  stdlib + third-party dependency is frozen in), then **drops `monteur`'s own
  modules from the frozen code** and ships them as the baseline *payload* data,
  so the on-disk payload always wins at import time.
- Entry point: `packaging/monteur_app.py` (picks the newest payload, opens the
  native window). `console=False`; drop a `packaging/monteur.ico` for an icon.

## Publishing an update

1. Bump `__version__` in `monteur/__init__.py`.
2. `python scripts/build_exe.py`.
3. Create a GitHub Release tagged with the new version (e.g. `v0.2.0`) under
   `maik353-debug/Monteur-Resolve` (override with `MONTEUR_UPDATE_REPO`).
4. Attach `monteur-app-<version>.zip` **and** its `.sha256`. Attach a fresh
   `Monteur-<version>-<platform>.exe` too whenever the shell/deps changed.

Then **Help → Check for updates…** (or `monteur update`) finds it: it reads the
release, downloads the payload, verifies the checksum, unpacks it into
`~/.monteur/payloads/<version>/`, and the shell runs it next launch. A source
checkout never installs anything — it points you at `git pull` / `pip install
-U monteur`.

## Notes

- The `[app]` extra (pywebview) must be present in the build environment.
- On Windows, pywebview uses WebView2 (bundled with modern Edge — nothing extra
  on the target). macOS/Linux use WebKit / GTK·Qt; the same spec covers them.
- The checksum guards integrity, not authenticity. Code-signing the shell (and
  eventually signing the payload) is the next hardening step.
