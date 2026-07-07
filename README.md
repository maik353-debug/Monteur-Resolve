# Fable 🎬

**AI-assisted editing room toolkit for DaVinci Resolve.**

Fable helps film editors get from raw footage to a strong cut, faster:

- **Papercut** — text-based rough cutting. Turn transcripts into a markdown
  checklist, tick and reorder the takes you want, and Fable renders a real
  timeline (EDL/FCPXML) you can import straight into DaVinci Resolve.
- **Pacing analytics** — objective rhythm data for a cut: average shot
  length, pacing curve, fast/medium/slow sections, shot-length histogram,
  and A/B comparison between two versions of a cut.
- **HTML pacing reports** — a self-contained, shareable report with charts,
  in light and dark mode.
- **Resolve bridge** — read timelines from a running DaVinci Resolve, import
  rendered rough cuts, all via Resolve's scripting API.
- **AI assistance (optional)** — Claude suggests selects from your
  transcript against an editorial brief, writes pacing notes on your cut,
  and logs footage summaries.

Pure Python 3.10+, standard library only (the AI features optionally use the
`anthropic` package).

## Install

```bash
pip install -e .          # core
pip install -e '.[ai]'    # with AI features (needs ANTHROPIC_API_KEY)
```

## Quick start

### 1. Rough cut from a transcript

```bash
# Transcribe your footage (e.g. with Whisper), then:
fable papercut create interview.srt -o cut.md --fps 25

# Open cut.md, tick the takes you want, reorder lines freely:
#   - [x] [00:00:12.400 --> 00:00:19.800] ANNA: The night it happened ...
#   - [ ] [00:00:20.100 --> 00:00:24.000] (um, false start)
#   - [x] [00:01:02.000 --> 00:01:09.500] ANNA: Nobody believed me.

fable papercut render cut.md -o rough_cut.fcpxml --handles 0.5
# -> import rough_cut.fcpxml in DaVinci Resolve
```

### 2. Analyze the pacing of a cut

```bash
fable analyze my_cut.edl --fps 25
fable analyze my_cut_v4.edl --compare my_cut_v3.edl --fps 25
fable analyze my_cut.fcpxml --report pacing.html   # charts, sections, histogram
```

### 3. Talk to DaVinci Resolve

```bash
fable resolve status            # list timelines in the open project
fable resolve analyze           # pacing stats for the current timeline
fable resolve import rough.edl  # import a rendered papercut
```

Requires Resolve running with scripting enabled
(Preferences → System → General → External scripting: Local).

### 4. AI assistance (optional)

```bash
export ANTHROPIC_API_KEY=...
fable ai selects cut.md --brief "90-second teaser, lead with the conflict"
fable ai notes my_cut.edl --fps 25    # editorial notes on your pacing
fable ai log interview.srt           # footage log: topics, quotes, timestamps
```

## Python API

```python
from fable import io
from fable.analysis import analyze_timeline, compare
from fable.papercut import create_papercut, parse_papercut, papercut_to_timeline

timeline = io.load_timeline("cut_v4.edl", fps=25)
stats = analyze_timeline(timeline)
print(stats.avg_shot_seconds, stats.sections)

transcript = io.load_transcript("interview.srt")
print(create_papercut(transcript, fps=25, title="Interview A"))
```

## Supported formats

| Format | Read | Write |
|---|---|---|
| CMX3600 EDL | ✅ | ✅ |
| FCPXML 1.x | ✅ | ✅ (1.9) |
| SRT subtitles/transcripts | ✅ | ✅ |
| Whisper JSON | ✅ | — |
| DaVinci Resolve (live, via scripting API) | ✅ | import |

## Development

```bash
pip install -e '.[dev]'
pytest
```

## Status & license

Alpha (v0.1). APIs may change. License: not yet decided — all rights
reserved for now.
