# Fable 🎬

**The pacing assistant for film editors.**

Fable gives editors objective feedback on the rhythm of a cut — and tracks
how it evolves, version by version. Built for narrative film, works with
DaVinci Resolve.

- **Auto-assembly** — from screenplay to first cut. Fable transcribes your
  takes, matches every dialogue line of your script against them, scores
  each take (coverage, accuracy, restarts/fluffs), picks the best material
  and builds the scene assembly as a timeline for Resolve. You start from
  a cut, not from zero.
- **Fable Studio** — a local app in your browser. Drop a timeline export
  (EDL/FCPXML) or pull the current timeline from a running Resolve, and see
  your cut's pacing: shot-length stats, a pacing curve, fast/medium/slow
  sections, histogram.
- **Version history** — save every cut as a version and watch your film's
  tempo evolve across weeks of editing. Compare any two versions and get a
  plain-language verdict ("v5 is cut faster, with a more even rhythm, and
  runs 40s shorter").
- **Resolve bridge** — talks directly to a running DaVinci Resolve via its
  scripting API: read timelines, import cuts.
- **Papercut** — bonus for dialogue scenes and documentary work: turn
  transcripts into a tickable checklist and render your selects as a real
  timeline.
- **AI assistance (optional)** — Claude writes editorial pacing notes on
  your cut and suggests selects from transcripts.

Pure Python 3.10+, zero required dependencies (AI features optionally use
the `anthropic` package).

## Install & launch

```bash
pip install -e .          # core
pip install -e '.[ai]'    # with AI features (needs ANTHROPIC_API_KEY)

cd ~/my-film-project
fable ui                  # launches Fable Studio in your browser
```

Everything below is also available from the command line.

## Quick start (CLI)

### 0. Auto-assembly: screenplay + takes → first cut

```bash
fable transcribe footage/scene12/          # whisper, writes .json per clip
fable assembly script.fountain footage/scene12/ -o scene12.fcpxml --fps 25
# -> import scene12.fcpxml in Resolve: best takes, in script order
```

Clips named like `S12_T03.mov` are routed to scene 12 automatically. Try it
on the included demo: `fable assembly examples/demo/script.fountain
examples/demo/takes -o assembly.edl`

### 1. Analyze the pacing of a cut

```bash
fable analyze my_cut.edl --fps 25
fable analyze my_cut_v4.edl --compare my_cut_v3.edl --fps 25
fable analyze my_cut.fcpxml --report pacing.html   # shareable HTML report
fable analyze my_cut.edl --fps 25 --scenes --reference thriller
```

### 2. Rough cut from a transcript (dialogue/doc scenes)

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
