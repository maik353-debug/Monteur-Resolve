# Fable 🎬

**Your footage. A song. A first cut — automatically.**

Fable takes the tedious parts off a filmmaker's plate: it sifts your
footage, finds the good moments, and cuts them to your music — then hands
the result to DaVinci Resolve where you make it yours. Built for filmmakers
who'd rather tell stories than scrub through clips.

- **Create a first cut** — point Fable at a folder of clips and a song.
  It scans every clip (flags what's too dark, blurry or shaky), ranks the
  best moments, detects the music's tempo and beats, and builds a rough cut
  on the beat grid — faster cutting where the song gets loud. Out comes a
  timeline for Resolve.
- **Footage sifting** — `fable sift` alone tells you what's usable in a
  shoot before you watch a single clip.
- **Auto-assembly (dialogue scenes)** — from screenplay to first cut:
  Fable matches take transcripts against your script, scores takes
  (coverage, accuracy, restarts), picks the best material per scene.
- **Fable Studio** — a local app in your browser with guided, step-by-step
  workflows. No timeline jargon required to get started.
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

### 0. Automatic first cut: footage + music

```bash
pip install -e '.[media]'      # brings numpy + a bundled ffmpeg
fable sift  ~/footage/day01    # what's usable? what's too dark/blurry/shaky?
fable create ~/footage/day01 ~/music/track.mp3 -o first_cut.fcpxml
# -> import in Resolve: your best moments, cut to the beat
```

`--order best_first` puts the strongest material on the loudest sections;
`--max-duration 60` caps the cut. Works best with music that has a clear
pulse.

### 0b. Auto-assembly: screenplay + takes → first cut (dialogue)

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
