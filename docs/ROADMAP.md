# Monteur Roadmap

Vision: the editorial intelligence layer for DaVinci Resolve — Monteur
understands the material (which moments are good), the music (where the
pulse is) and the cut (where it drags), and acts on that judgment. The two
fields research showed are genuinely open: beat-driven auto-cutting for
real filmmakers, and editorial intelligence as an AI-accessible layer
(existing Resolve MCPs offer raw control only). Distribution via open
MCP/CLI, product via Monteur Studio.

## v0.1 — Foundation (this release)

- Core data model: timecode (incl. drop-frame), timelines, clips, transcripts
- I/O: CMX3600 EDL, FCPXML 1.x, SRT, Whisper JSON
- Pacing analytics: ASL, pacing curve, sections, histogram, A/B compare
- Monteur Studio: local web app — drag & drop analysis, version history with
  tempo trend, Resolve panel
- Project version store (`.monteur/versions.json` per film project)
- Self-contained HTML pacing report (light/dark)
- DaVinci Resolve bridge (read timelines, import cuts)
- Papercut: transcript → checklist → EDL/FCPXML rough cut (for dialogue/doc)
- Optional Claude-powered selects, pacing notes, footage logs
- CLI: `monteur ui | analyze | papercut | convert | resolve | ai`

## v0.2 — Deeper senses (specialist moat, part 1)

Musical intelligence:
- [x] Downbeat & bar detection; cut on musical phrases (4/8 bars), not
      just beats
- [x] Drop/chorus detection — put the strongest shot on the drop
- [x] Named song sections (intro/verse/chorus) in the montage plan

Visual intelligence:
- [x] Motion-direction matching at cut points (exit motion ~ entry motion)
- [ ] Shot-size estimate (wide/medium/close via face size) and alternation
      rules — no two near-identical framings back to back
- [x] Audio-based sift signals: wind noise, clipping, on-set silence
- [x] Highlight detection from audio (laughter, cheers, action peaks)

Craft templates:
- [x] Montage structures per use case: travel film, wedding,
      music video, trailer — each a pacing arc (opening/build/climax/outro),
      not just a grid
- [x] Editorial controls on every cut: pace (seconds per shot,
      beat-rounded), transitions (cuts/dissolves/smash to black with title
      slots), canvas (16:9 / 9:16 / 2.39:1 in HD & 4K), audio modes
      (music / mix / original), black fade-in/out in the export

## v0.3 — Semantic understanding (specialist moat, part 2)

- [x] Claude watches the footage (`monteur see`): one frame per good
      moment, labeled with what it shows, hero-shot score, dramaturgical
      role (opener/build/climax/closer) and a scene-similarity group;
      cached next to the footage; feeds semantic casting in the montage
      (hero on the drop, opener up front, no same-scene adjacency)
- [x] Footage search (`monteur find`, MCP `find_shots`): full-text over
      the cached vision annotations — instant, offline, free; German
      plural tolerance, hero ranking, stale-entry skipping
- [x] Trailer distillation (`monteur distill`): a finished long cut
      becomes the 30/60s trailer — the cut's own shots are the material,
      screen time is the editor's prior
- [x] Revision loop (`monteur create --save-plan` + `monteur revise`):
      iterate in plain German/English ("zweite Hälfte ruhiger"), pinned
      shots survive verbatim, untouched regions stay bit-identical,
      cuts stay on the beat grid
- [ ] Marry Whisper to the montage: speech-aware cutting for travel
      films with voice — don't cut mid-sentence, duck-the-music cues in
      mix mode, spoken moments as protected slots; transcript words as
      a search signal in `monteur find`; a transcribe button in Studio
- [x] Movie creator stage 1 (`monteur movie new`): idea + constraints ->
      Claude drafts the blueprint — Fountain screenplay (assembly-ready),
      scene list with concrete shooting tips, sound notes, cut intents,
      printable shotlist with take checklists
- [x] Movie creator stage 2: Studio movie view — open/create a project
      in the browser, scene slots with shooting tips, assign footage per
      scene, shoot progress, footage checks (technical + vision content
      match, INT/EXT and DAY/NIGHT consistency, dark-fits-night)
- [x] Movie creator stage 3: assemble the film along the screenplay —
      scene lengths estimated from the script, pacing and transitions
      from each scene's cut intent, S03_T02 take files auto-restricted,
      original sound on A1, a scene marker per act, CLI + Studio button
      (line-accurate dialogue matching via transcripts = follow-up)
- [ ] Learn from the editor: diff the editor's corrections against
      Monteur's plan and adapt scoring preferences per project
- [ ] Watch mode: new footage auto-sifted overnight, report ready in the
      morning
- [ ] Change list between versions for sound/VFX handoffs
- [x] Claude composes the cut (`--ai-cut`, Studio toggle): the engine
      builds the beat grid, phases and dips; Claude casts every slot
      from the vision-labeled moment inventory following a per-style
      craft brief, writes the act titles and a story line; the engine
      validates every pick and falls back per slot
- [x] Director's Notes (`monteur direct`, Studio block): Claude reviews
      the planned cut against editing craft — verdict, score, issues
      with concrete replacements from the unused bench, one-click apply
      that leaves the beat grid untouched
- [x] Claude Code as an AI backend: with no ANTHROPIC_API_KEY but an
      installed `claude` CLI, every writing feature (movie blueprint,
      brief, publish kit, selects/notes) runs on the user's Claude
      subscription at no extra cost; footage vision stays API-only
      (the CLI takes no images)
- [x] Song matcher (`monteur pick-music`, Artlist integration stage 1):
      rank a folder of candidate songs against the footage — beat clarity,
      length vs unique material, tempo vs motion, drop, dynamic arc —
      with human-readable reasons; works on any downloaded files, no API
- [ ] Artlist integration stage 2: deep-linked searches built from the
      brief (music) and from title-slot/SFX markers (sound effects)
- [ ] Artlist integration stage 3: Enterprise API partnership — in-app
      search, preview against the timeline, direct download
      (enterprise-api-support@artlist.io once the product is public)

## Project persistence (REQUIRED — never lose a project)

- [ ] **First-class Monteur project format.** Today: settings/drafts/preferences
      persist as JSON in `~/.monteur/`, and the Movie mode has real project
      folders (`movie.save_project`), but the Cut/Create workflow only
      autosaves to a shared `drafts.json` and `project.py` stores only version
      history. Elevate a CUT to a durable, first-class project:
      - Each project = a folder (a projects root under `~/.monteur/projects/<slug>/`
        or a user-chosen location) with a manifest — a custom `.monteur` bundle
        (project.json inside): name, footage folder PATH (media is never copied —
        "your files are never moved"), the chosen options, the current
        MontagePlan (already JSON-serializable via `plan_to_dict`), version
        history, export history, timestamps.
      - Subfolders for exports / versions / proxies. Global config stays in
        `~/.monteur/settings.json`; preferences global.
      - A `monteur/projects.py` (or extend `project.py`) with save/load/list;
        MIGRATE existing `drafts.json` into projects so nothing is lost.
      - The new Project-Manager home lists these real projects (currently it
        lists drafts as a bridge). — Maik, explicit: "auf keinen Fall Projekte
        verlieren." Backend track right after the UI pages land.
- [ ] **Unified model (Maik):** everything is ONE always-saved project. A
      project = a MEDIA POOL (files/folders referenced from disk, Resolve-style
      — never imported/moved) + the DERIVED INFORMATION (sift reports, AI
      labels, daylight/spatial caches, the plan, version stats) + a
      regenerable proxy cache. Consequence: the project stores only knowledge,
      not video, so the timeline/preview is a data view over small proxies →
      performant; the one heavy cost is the one-time analysis of the pool.
      DRAFTS DISAPPEAR (always in a saved project; migrate them). The top
      app-tabs (Create/Movie/Series) COLLAPSE — things you do inside a project,
      not separate apps; Home opens/creates a project and inside you move
      between PAGES (Media Pool · Storyboard · Cut · Color · Analysis) like
      Resolve's page bar. Build order: (1) project/media-pool backend +
      migration, (2) Media-Pool page UX, (3) collapse the top nav, (4) the
      multi-lane timeline on top.

## v1.0 — Product

- [~] **Native app shell (REQUIRED, not a browser tab).** Monteur must feel
      like real software: its OWN window, modern Windows (Fluent) design, no
      browser chrome, no address bar. DONE (shell): the Studio runs in a
      native pywebview window via `serve_app` — `monteur ui --window` (the
      `[app]` extra, `pip install 'monteur[app]'`). WebView2 on Windows =
      modern Edge Chromium in a native window; bundles with the Python we
      already ship. The HTTP server runs on a daemon thread; the window owns
      the main thread; without pywebview it falls back to the browser. NEXT:
      restyle app.html toward a Fluent look (title bar, mica/acrylic
      surfaces, native controls), and package it (below). Tauri stays the
      richer-native alternative if we ever want fully native chrome. — Maik,
      explicit.
- [~] **Package Studio as a desktop app (no Python required).** DONE: a
      PyInstaller build (`python scripts/build_exe.py`, spec in
      `packaging/`, `[build]` extra) produces one self-contained executable
      that wraps `serve_app` — `app.html` is bundled next to `server.py`, the
      window opens via WebView2 on Windows. Verified end to end (the frozen
      binary serves the Studio + API). NEXT: code-signing + a real installer
      (MSI/DMG), and an app icon. See `docs/PACKAGING.md`.
- [x] **In-app updates (payload split, Electron-style).** The app is split
      into a rarely-changing **shell** (the ~70 MB executable) and a small
      **payload** (`monteur` + `app.html`, ~650 KB). Help → Check for updates…
      (and `monteur update`) checks the GitHub Releases API, shows the notes,
      downloads only the payload zip, verifies its `.sha256`, and unpacks it
      into `~/.monteur/payloads/<version>/`. The launcher runs the newest
      payload on disk at the next start — no executable swap, KB not MB
      (`monteur/payload.py` + `monteur/update.py`, stdlib, network injected +
      fully tested; the shell/payload override was verified on a real frozen
      build). A full-executable update is the rare fallback when deps change;
      a source checkout degrades to a "git pull / pip install -U" advisory.
- [x] **Continuous delivery + channels.** Every push to `main` auto-publishes a
      payload to the **dev** channel via GitHub Actions (`0.1.<commit-count>`,
      a prerelease, payload builds on plain Linux — no Windows runner). **stable**
      reads GitHub's `/releases/latest`, so deliberate tags reach real users while
      dev stays yours. The channel is a Settings → Updates toggle (default
      stable); updates stay opt-in (a manual check, no silent auto-apply).
      NEXT: sign the payload (the checksum guards integrity, not authenticity);
      optional background auto-check.
- [ ] Resolve plugin panel (Workflow Integration)
- [ ] Licensing/pricing model (indie editors first)
- [ ] Team features: shared version history, review links, feedback threads

## Design principles

1. **Text is the interface.** Transcripts, papercuts and reports are plain
   files — versionable, diffable, portable.
2. **Never lock in.** Everything exports to open formats (EDL/FCPXML/OTIO).
3. **Editor stays in charge.** AI suggests; the editor decides. Suggestions
   arrive as reviewable diffs (ticked checkboxes), never silent changes.
4. **Stdlib core.** The core has zero dependencies; AI is opt-in.
