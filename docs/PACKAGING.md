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
pip install -e ".[app,build]"
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

## Channels: dev vs stable

There are two release streams, chosen in **Settings → Updates** (or
`monteur update --channel …`; default **stable**):

- **dev** — every push to `main`. A GitHub Actions workflow
  (`.github/workflows/dev-release.yml`) builds the payload with a monotone
  version `0.1.<commit-count>`, publishes it as a **prerelease**, and attaches
  the zip + `.sha256`. No Windows runner needed — the payload is pure Python +
  `app.html`. The dev channel reads the newest release incl. prereleases.
- **stable** — deliberate releases only. The stable channel reads GitHub's
  `/releases/latest`, which never returns a prerelease, so dev builds stay
  invisible to stable users.

The version scheme is monotone within a channel (commit-count patch), so an
update is offered whenever the newest release's tag is numerically higher than
the running `__version__`.

## Publishing a stable update

1. Bump `__version__` in `monteur/__init__.py`.
2. `python scripts/build_exe.py`.
3. Create a GitHub Release tagged with the version (e.g. `v0.2.0`) under
   `maik353-debug/Monteur-Resolve` (override with `MONTEUR_UPDATE_REPO`),
   **not** marked as a prerelease.
4. Attach `monteur-app-<version>.zip` **and** its `.sha256`. Attach a fresh
   `Monteur-<version>-<platform>.exe` too whenever the shell/deps changed.

Then **Help → Check for updates…** (or `monteur update`) finds it: it reads the
release for the active channel, downloads the payload, verifies the checksum,
unpacks it into `~/.monteur/payloads/<version>/`, and the shell runs it next
launch. A source checkout never installs anything — it points you at `git pull`
/ `pip install -U monteur`.

## A real Windows installer

`scripts/build_exe.py` gives you a portable `.exe` ("download and run"). To
ship an actual *installed* app — Start-menu + Desktop shortcuts, an
Add/Remove-Programs entry with an uninstaller — build the Inno Setup installer:

```bash
python scripts/build_exe.py          # 1) the shell + payload
python scripts/build_installer.py    # 2) the installer (Windows + Inno Setup 6)
# -> dist/Monteur-Setup-<version>.exe
```

- **Per-user install** (`PrivilegesRequired=lowest`) into
  `%LOCALAPPDATA%\Programs\Monteur` — no admin prompt, and it matches the
  self-update model (payloads land in `%USERPROFILE%\.monteur`, writable
  without elevation).
- Start-menu + optional Desktop shortcut; a proper uninstaller in
  Programs & Features.
- Needs Inno Setup 6 (`iscc` on PATH, https://jrsoftware.org/isdl.php); it's
  Windows-only, so build it on Windows. The `.iss` is `packaging/monteur.iss`.

macOS/Linux installers (`.dmg` / `.AppImage`) are future work; the portable
build already runs there.

## Your data is safe across install / update / uninstall

Everything persistent — **projects, settings, downloaded payloads, proxies,
version history** — lives under `%USERPROFILE%\.monteur` (`~/.monteur`),
entirely outside the install folder. Consequences:

- Installing or updating never touches your projects.
- **Uninstalling leaves `~/.monteur` in place** (the `.iss` deliberately has no
  `[UninstallDelete]` for it) — your work is never removed by removing the app.
- The windowed app writes its working files (analysis version store, crash log)
  to `~/.monteur/studio`, never into its read-only install folder — so a
  per-user or Program Files install both work.

## Icon

The app icon is generated from the brand mark:

```bash
pip install pillow            # in the [build] extra
python scripts/make_icon.py   # -> packaging/monteur.ico (+ monteur.png)
```

`monteur.ico` is committed, and the PyInstaller spec embeds it automatically
when present, so the exe and its shortcuts carry the icon. Re-run the script
only if the mark changes.

## Code-signing (SmartScreen trust)

Unsigned, Windows SmartScreen shows "Unknown publisher" on first run. Signing is
opt-in and wired into both build scripts (a clean no-op when no cert is set):

```bash
# either a PFX file…
set MONTEUR_SIGN_PFX=C:\path\to\cert.pfx
set MONTEUR_SIGN_PASS=…
# …or a thumbprint already in the Windows cert store
set MONTEUR_SIGN_SHA1=<thumbprint>

python scripts/build_exe.py         # signs the shell
python scripts/build_installer.py   # signs the installer
# or sign a file directly:  python scripts/sign.py dist\Monteur-Setup-0.1.0.exe
```

`scripts/sign.py` calls the Windows SDK's `signtool` (SHA-256 + an RFC-3161
timestamp so signatures outlive the cert). Get a certificate from a CA
(Sectigo/DigiCert): an **OV** cert works but earns SmartScreen reputation
slowly; an **EV** cert is trusted immediately. This is the one step that needs
your own secret — everything else builds unsigned without it.

## Notes

- The `[app]` extra (pywebview) must be present in the build environment.
- On Windows, pywebview uses WebView2 (bundled with modern Edge — nothing extra
  on the target). macOS/Linux use WebKit / GTK·Qt; the same spec covers them.
- The payload checksum guards integrity, not authenticity — code-signing (above)
  is what proves the publisher.
