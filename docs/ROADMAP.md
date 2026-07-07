# Fable Roadmap

Vision: the AI editing-room assistant for DaVinci Resolve editors — from raw
footage to a strong cut, faster, with objective feedback on rhythm and
dramaturgy. Personally useful first, marketable second.

## v0.1 — Foundation (this release)

- Core data model: timecode (incl. drop-frame), timelines, clips, transcripts
- I/O: CMX3600 EDL, FCPXML 1.x, SRT, Whisper JSON
- Papercut: transcript → checklist → EDL/FCPXML rough cut
- Pacing analytics: ASL, pacing curve, sections, histogram, A/B compare
- Self-contained HTML pacing report (light/dark)
- DaVinci Resolve bridge (read timelines, import cuts)
- Optional Claude-powered selects, pacing notes, footage logs
- CLI: `fable analyze | papercut | convert | resolve | ai`

## v0.2 — In the editor's daily loop

- [ ] Whisper integration: `fable transcribe footage/` (local whisper.cpp or API)
- [ ] Multicam/multi-clip papercuts with speaker-based source mapping
- [ ] Marker round-trip with Resolve (notes ↔ timeline markers)
- [ ] Reference pacing profiles: compare your cut against genre baselines
- [ ] Watch mode: re-analyze on export, keep a version history of pacing stats
- [ ] Windows/macOS install docs + Resolve scripts-menu integration

## v0.3 — The assistant becomes proactive

- [ ] Scene/act detection from transcript + cut structure
- [ ] "Story sync": papercut sections mapped to a beat sheet / outline
- [ ] AI rough-cut proposals directly from footage transcripts (multi-source)
- [ ] Local web UI (FastAPI) for papercut editing with waveform preview

## v1.0 — Product

- [ ] Packaged installers, Resolve plugin panel
- [ ] Licensing/pricing model (indie editors first)
- [ ] Team features: shared papercuts, review links, feedback threads

## Design principles

1. **Text is the interface.** Transcripts, papercuts and reports are plain
   files — versionable, diffable, portable.
2. **Never lock in.** Everything exports to open formats (EDL/FCPXML/OTIO).
3. **Editor stays in charge.** AI suggests; the editor decides. Suggestions
   arrive as reviewable diffs (ticked checkboxes), never silent changes.
4. **Stdlib core.** The core has zero dependencies; AI is opt-in.
