# Monteur Roadmap

Vision: the pacing assistant for narrative film editors — objective feedback
on the rhythm of a cut, version by version, integrated with DaVinci Resolve.
Personally useful first, marketable second. The GUI (Monteur Studio) is the
product; the CLI is the workbench underneath.

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

## v0.2 — In the editor's daily loop (Studio-first)

- [ ] Scene-level view: pacing per scene (from Resolve markers / clip names)
- [ ] Reference pacing profiles: compare your cut against genre baselines
      and reference films
- [ ] One-click "pull & save" from Resolve on every export (watch mode)
- [ ] Notes on versions ("director saw this one", "tightened act 2")
- [ ] AI pacing notes inside Studio (opt-in, per version)
- [ ] Windows/macOS install docs + Resolve scripts-menu integration

## v0.3 — Deeper dramaturgy

- [ ] Act/sequence structure overlay on the pacing curve
- [ ] Audio-aware analysis: dialogue density, music vs. dialogue balance
- [ ] Marker round-trip with Resolve (notes ↔ timeline markers)
- [ ] Whisper integration for the papercut workflow

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
