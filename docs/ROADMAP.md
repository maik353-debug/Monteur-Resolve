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
- [ ] Learn from the editor: diff the editor's corrections against
      Monteur's plan and adapt scoring preferences per project
- [ ] Watch mode: new footage auto-sifted overnight, report ready in the
      morning
- [ ] Change list between versions for sound/VFX handoffs
- [x] Song matcher (`monteur pick-music`, Artlist integration stage 1):
      rank a folder of candidate songs against the footage — beat clarity,
      length vs unique material, tempo vs motion, drop, dynamic arc —
      with human-readable reasons; works on any downloaded files, no API
- [ ] Artlist integration stage 2: deep-linked searches built from the
      brief (music) and from title-slot/SFX markers (sound effects)
- [ ] Artlist integration stage 3: Enterprise API partnership — in-app
      search, preview against the timeline, direct download
      (enterprise-api-support@artlist.io once the product is public)

## v1.0 — Product

- [ ] Package Studio as a desktop app (installers, no Python required)
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
