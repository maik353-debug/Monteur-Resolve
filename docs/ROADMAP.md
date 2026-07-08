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
- [ ] Downbeat & bar detection; cut on musical phrases (4/8 bars), not
      just beats
- [ ] Drop/chorus detection — put the strongest shot on the drop
- [ ] Named song sections (intro/verse/chorus) in the montage plan

Visual intelligence:
- [ ] Motion-direction matching at cut points (exit motion ~ entry motion)
- [ ] Shot-size estimate (wide/medium/close via face size) and alternation
      rules — no two near-identical framings back to back
- [ ] Audio-based sift signals: wind noise, clipping, on-set silence
- [ ] Highlight detection from audio (laughter, cheers, action peaks)

Craft templates:
- [ ] Montage structures per use case: travel film, wedding, event,
      music video, trailer — each a pacing arc (opening/build/climax/outro),
      not just a grid

## v0.3 — Semantic understanding (specialist moat, part 2)

- [ ] Claude-vision footage search: sample frames -> "find shots of the
      bride laughing", "every shot of the red car" — local index,
      Resolve-native, feeds the montage builder and MCP
- [ ] Trailer mode: distill a finished long cut into 30/60s (scene
      detection + pacing analysis + AI shot selection)
- [ ] Learn from the editor: diff the editor's corrections against
      Monteur's plan and adapt scoring preferences per project
- [ ] Watch mode: new footage auto-sifted overnight, report ready in the
      morning
- [ ] Change list between versions for sound/VFX handoffs

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
