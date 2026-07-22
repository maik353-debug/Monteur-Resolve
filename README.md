# Monteur 🎬

**The editing assistant that understands your footage**

Generic Resolve remote controls exist. Monteur is the editorial
intelligence layer: it knows *which* moments in your material are worth
using, *where* the beat falls in your music, and *why* your cut drags in
act two — and it hands finished timelines to DaVinci Resolve.

- **Create a first cut** — point Monteur at a folder of clips and a song.
  It scans every clip (flags what's too dark, blurry or shaky), ranks the
  best moments, detects the music's tempo and beats, and builds a rough cut
  on the beat grid — faster cutting where the song gets loud. Straight into
  Resolve, or as an EDL/FCPXML file.
- **Claude integration (MCP)** — `monteur mcp` gives your Claude account
  editorial senses inside Resolve: "analyze my timeline and mark where it
  drags", "build a 60-second montage from this folder to this track". Not
  raw API remote control — judgment.
- **Footage sifting** — `monteur sift` tells you what's usable in a shoot
  before you watch a single clip.
- **Pacing analytics** — shot-length stats, pacing curve, fast/slow
  sections, genre reference bands, per-scene view, and version comparison
  with a plain-language verdict ("v5 is cut faster, with a more even
  rhythm, and runs 40s shorter").
- **Monteur Studio** — the app: a suite of three tools reached from one
  hub — **Create** (a social cut from your footage, Series of shorts
  included), **Movie** (a film from a screenplay), and **Analysis** (read a
  timeline's rhythm). Runs in its own native desktop window (Windows/macOS/
  Linux) or your browser. A Resolve-style **Media Pool** (your files stay on
  disk, never copied), a real-time player that plays the cut as you shape it,
  a deterministic **Color** page (looks + basic grade, baked into the export),
  and a **page bar** you move along like an NLE.
- **Never lose a project** — every cut is a durable, always-saved project
  (its own `.monteur` bundle), and every distinct version is a restorable
  snapshot. Installing, updating and even uninstalling never touch your work.
- **Assembly & papercut (dialogue/doc material)** — match take transcripts
  against a screenplay, or tick takes in a transcript checklist, and render
  the selects as a timeline.

Works with the free DaVinci Resolve via timeline files; live control
(markers, building timelines in place, MCP) uses the scripting API and
needs Resolve Studio.

> **Python version note for live Resolve features.** DaVinci Resolve's
> scripting module is a native library built for **Python 3.6–3.11**;
> loading it under a newer Python (3.12+) crashes it. Monteur isolates
> every Resolve call in a child process, so this never takes Monteur down
> — but the live features won't *work* until the child runs a compatible
> Python. If your main Python is 3.12+, install 3.11 alongside it and set
> `MONTEUR_RESOLVE_PYTHON` to its path (e.g.
> `set MONTEUR_RESOLVE_PYTHON=C:\Python311\python.exe` on Windows). All
> file-based features (create, sift, analyze → EDL/FCPXML) work on any
> Python 3.10+.

Pure Python 3.10+, zero required dependencies (AI features optionally use
the `anthropic` package).

## Install & launch

```bash
pip install -e .          # core
pip install -e '.[ai]'    # with AI features (needs ANTHROPIC_API_KEY or Claude Code)

cd ~/my-film-project
monteur ui                  # launches Monteur Studio in your browser
```

### Desktop app

Run Monteur in its own native window instead of a browser tab:

```bash
pip install -e '.[app]'     # adds pywebview (WebView2 on Windows)
monteur ui --window
```

To ship it as an installed app (no Python on the target), build a
self-contained executable and a real Windows installer — see
[`docs/PACKAGING.md`](docs/PACKAGING.md). The packaged app **updates itself**:
**Help → Check for updates…** (or `monteur update`) downloads a small,
checksummed app payload and applies it on the next launch — no reinstall. A
stable and a dev channel are selectable in Settings → Updates.

Everything below is also available from the command line.

## Quick start (CLI)

### 0. Automatic first cut: footage + music

```bash
pip install -e '.[media]'      # brings numpy + a bundled ffmpeg
monteur sift  ~/footage/day01    # what's usable? what's too dark/blurry/shaky?
monteur create ~/footage/day01 ~/music/track.mp3 -o first_cut.fcpxml
# -> import in Resolve: your best moments, cut to the beat
```

`--order best_first` puts the strongest material on the loudest sections;
`--max-duration 60` caps the cut. Works best with music that has a clear
pulse.

The cut is yours to shape without leaving the command line:

```bash
monteur create footage/ track.mp3 -o cut.fcpxml \
    --style trailer         # auto | travel | wedding | music_video | trailer
    --pace 2                # ~seconds per shot in the fastest phase (beat-rounded)
    --transitions smash     # auto | cuts | dissolves | smash (black title slots)
    --canvas cine-uhd       # hd/uhd 16:9, vertical[-uhd] 9:16, cine[-uhd] 2.39:1
    --audio mix             # music | mix (song + camera mic) | original (no song)
```

No song at all — a ride-POV cut that keeps the engine sound?

```bash
monteur create footage/ --audio original --max-duration 90 -o ride.fcpxml
```

Not sure which song? Drop your candidates (e.g. Artlist downloads) in a
folder and let Monteur rank them against the footage — beat clarity,
length vs your unique material, tempo vs your motion, drop, dynamics:

```bash
monteur pick-music footage/ ~/Music/candidates/
# 1. skyline.mp3 — 87/100 (124 BPM, 95s)
#    - clear steady pulse (124 BPM)
#    - 95s fits your 78s of material — nothing has to repeat
#    - drop at 42s — a natural climax anchor
```

Every export opens and closes on black, a trailer smashes to black
between acts (each gap carries a "Title slot" marker), and cuts land a
frame before the beat so the incoming shot hits ON it.

One heads-up for the `cine[-uhd]` canvases: a 2.39:1 timeline fits your
16:9 footage with bars on the SIDES at first. For the classic cinema
look, set Resolve's Project Settings > Image Scaling > "Scale full frame
with crop" — the picture fills the width and the top/bottom bars appear
when you view or export in 16:9 (the plan reminds you, too).

**Iterate in plain language.** Save the plan, then revise it — pinned
shots stay exactly where they are, untouched regions stay bit-identical,
and every cut stays on the beat grid:

```bash
monteur create footage/ track.mp3 -o v1.fcpxml --save-plan plan.json
monteur revise plan.json footage/ -o v2.fcpxml \
    --brief "zweite Hälfte ruhiger" --pin 0:12
```

**Distill the trailer from the finished film.** Your final cut is the
best curation there is — every shot in it was hand-picked:

```bash
monteur distill final_cut.fcpxml teaser_song.mp3 -o trailer.fcpxml --target 45
monteur distill final_cut.fcpxml -o short.fcpxml --target 30 --canvas vertical-uhd
```

**Search footage by content** (after `monteur see`): `monteur find
footage/ "kurve"` lists every shot Claude labeled as a curve — instant,
offline, free; `"hero"` lists your hero shots.

### 0b. Auto-assembly: screenplay + takes → first cut (dialogue)

```bash
monteur transcribe footage/scene12/          # whisper, writes .json per clip
monteur assembly script.fountain footage/scene12/ -o scene12.fcpxml --fps 25
# -> import scene12.fcpxml in Resolve: best takes, in script order
```

Clips named like `S12_T03.mov` are routed to scene 12 automatically. Try it
on the included demo: `monteur assembly examples/demo/script.fountain
examples/demo/takes -o assembly.edl`

### 1. Analyze the pacing of a cut

```bash
monteur analyze my_cut.edl --fps 25
monteur analyze my_cut_v4.edl --compare my_cut_v3.edl --fps 25
monteur analyze my_cut.fcpxml --report pacing.html   # shareable HTML report
monteur analyze my_cut.edl --fps 25 --scenes --reference thriller
```

### 2. Rough cut from a transcript (dialogue/doc scenes)

```bash
# Transcribe your footage (e.g. with Whisper), then:
monteur papercut create interview.srt -o cut.md --fps 25

# Open cut.md, tick the takes you want, reorder lines freely:
#   - [x] [00:00:12.400 --> 00:00:19.800] ANNA: The night it happened ...
#   - [ ] [00:00:20.100 --> 00:00:24.000] (um, false start)
#   - [x] [00:01:02.000 --> 00:01:09.500] ANNA: Nobody believed me.

monteur papercut render cut.md -o rough_cut.fcpxml --handles 0.5
# -> import rough_cut.fcpxml in DaVinci Resolve
```

### 3. Talk to DaVinci Resolve

```bash
monteur resolve status            # list timelines in the open project
monteur resolve analyze           # pacing stats for the current timeline
monteur resolve import rough.edl  # import a rendered papercut
```

Requires Resolve running with scripting enabled
(Preferences → System → General → External scripting: Local).

### 4. AI assistance (optional)

```bash
export ANTHROPIC_API_KEY=...
monteur ai selects cut.md --brief "90-second teaser, lead with the conflict"
monteur ai notes my_cut.edl --fps 25    # editorial notes on your pacing
monteur ai log interview.srt           # footage log: topics, quotes, timestamps
```

No API key? If [Claude Code](https://claude.com/claude-code) is installed
(the `claude` command), Monteur's writing features — selects, notes, logs,
briefs, publish copy, movie blueprints — use it automatically with its
subscription, at no extra cost. Set `MONTEUR_AI_BACKEND=api` or
`MONTEUR_AI_BACKEND=claude-cli` to force one backend. Only footage vision
(`monteur see`, below) sends images and therefore always needs the API key.

**Claude watches your footage.** `monteur see` sends one frame per good
moment to Claude, which labels what it shows ("overtake in a left-hand
curve"), scores hero shots, and assigns each moment a dramaturgical role
(opener / build / climax / closer):

```bash
monteur see footage/day01              # what does Claude see in your clips?
monteur create footage/ track.mp3 --see --style trailer -o cut.fcpxml
# -> the real hero shot lands on the drop, the establishing shot opens
#    the film, and no two takes of the same scene sit back to back
```

Results are cached next to your footage (`.monteur-vision.json`), so a
re-run only pays for new material — a scan costs on the order of a cent.

**Movie creator.** Monteur can stand beside you BEFORE the shoot: give
it your idea and your real-world constraints, and it drafts the whole
pre-production package —

```bash
monteur movie new projekt/ --genre thriller \
    --brief "5 Minuten, 2 Personen, Wald und Auto, nachts, kein Budget"
# projekt/script.fountain  — the screenplay (assembly-ready)
# projekt/shotlist.md      — scene-by-scene shooting tips, sound notes,
#                            take checklists, file-naming plan
# projekt/movie.json       — the machine-readable project
```

Shoot the scenes (files named `S03_T02` route themselves), then
`monteur assembly projekt/script.fountain takes/` builds the first cut.

**Publish kit.** The upload needs more than a timeline — `--kit` writes
it in one go, straight from the cut:

```bash
monteur create footage/ track.mp3 --see -o cut.fcpxml --kit publish/
# publish/publish.md    — title ideas, description draft, tags,
#                         YouTube chapters (from the cut's scene changes)
# publish/thumbs/*.jpg  — thumbnail candidates, hero shots first,
#                         no two from the same scene
```

Title/description/tags are drafted by Claude when a key is set, from an
honest offline template otherwise — the kit never blocks the export.

## Claude integration (MCP)

Monteur ships an MCP server, so you can talk to DaVinci Resolve through
Claude: ask Claude to analyze your current timeline's pacing, mark the slow
sections with timeline markers, sift a footage folder, build a music-cut
montage straight into Resolve, or compare two versions of a cut — all
conversationally. Install the extra and register the server:

```bash
pip install 'monteur[mcp]'
```

**Claude Desktop / claude.ai**: add this to your MCP configuration
(on macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{"mcpServers": {"monteur": {"command": "monteur", "args": ["mcp"]}}}
```

**Claude Code**: one line —

```bash
claude mcp add monteur -- monteur mcp
```

Then just ask Claude, for example:

- "Analyze my current timeline and mark the slow sections"
- "Build a 60-second montage from /footage/day01 to /music/track.mp3 straight into Resolve"
- "Compare v4 and v5 of my cut"

Resolve-dependent tools need DaVinci Resolve running with scripting enabled
(Preferences → System → General → External scripting: Local).

## Python API

```python
from monteur import io
from monteur.analysis import analyze_timeline, compare
from monteur.papercut import create_papercut, parse_papercut, papercut_to_timeline

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
