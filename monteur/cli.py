"""Monteur command line interface.

Workflow overview::

    monteur analyze cut_v3.edl --fps 25 --report pacing.html
    monteur analyze cut_v3.edl --compare cut_v2.edl --fps 25
    monteur papercut create interview.srt -o cut.md --fps 25
    # ... tick the takes you want in cut.md ...
    monteur papercut render cut.md -o rough_cut.fcpxml
    monteur convert cut.edl cut.fcpxml --fps 25
    monteur resolve status
    monteur ai selects cut.md --brief "90s teaser, keep it fast"
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from monteur import __version__


def _fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"monteur: {message}", file=sys.stderr)
    raise SystemExit(1)


def _load_timeline(path: str, fps: float | None):
    from monteur import io

    try:
        return io.load_timeline(path, fps=fps)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))


def _print_stats(stats) -> None:
    print(f"Timeline : {stats.timeline_name or '-'}")
    minutes, seconds = divmod(int(stats.duration_seconds), 60)
    print(f"Duration : {minutes}:{seconds:02d}  ({stats.fps:g} fps)")
    print(f"Shots    : {stats.shot_count}   Cuts: {stats.cut_count}")
    print(
        f"Shot len : avg {stats.avg_shot_seconds:.2f}s  "
        f"median {stats.median_shot_seconds:.2f}s  "
        f"min {stats.min_shot_seconds:.2f}s  max {stats.max_shot_seconds:.2f}s  "
        f"std {stats.std_shot_seconds:.2f}s"
    )
    width = max((count for _, count in stats.histogram), default=0)
    if width:
        print("Histogram:")
        for label, count in stats.histogram:
            bar = "#" * round(count / width * 40)
            print(f"  {label:>7} | {bar} {count}")
    if stats.sections:
        print("Sections :")
        for section in stats.sections:
            print(
                f"  {int(section.start // 60)}:{int(section.start % 60):02d}"
                f"-{int(section.end // 60)}:{int(section.end % 60):02d}"
                f"  {section.label:<6} (avg {section.avg_shot_length:.1f}s)"
            )


def cmd_analyze(args: argparse.Namespace) -> None:
    from monteur.analysis import analyze_scenes, analyze_timeline, compare

    timeline = _load_timeline(args.timeline, args.fps)
    stats = analyze_timeline(timeline, track=args.track)
    other = None
    if args.compare:
        other = analyze_timeline(
            _load_timeline(args.compare, args.fps), track=args.track
        )
    if args.json:
        payload = asdict(stats)
        if other is not None:
            payload["compare"] = compare(other, stats)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_stats(stats)
        if other is not None:
            result = compare(other, stats)
            print(f"\nvs {args.compare}: {result['verdict']}")
        if args.scenes:
            print("\nScenes:")
            for scene in analyze_scenes(timeline, track=args.track):
                s = scene.stats
                print(
                    f"  {int(scene.start // 60)}:{int(scene.start % 60):02d}"
                    f"  {scene.heading[:36]:<36} {s.shot_count:>3} shots"
                    f"  ASL {s.avg_shot_seconds:5.2f}s"
                )
        if args.reference:
            from monteur.references import compare_to_reference

            result = compare_to_reference(stats, args.reference)
            print(f"\n{result['profile']}: {result['verdict']}")
    if args.report:
        from monteur.report import save_report

        save_report(stats, args.report, compare_to=other)
        print(f"\nReport written to {args.report}")


def cmd_papercut_create(args: argparse.Namespace) -> None:
    from monteur import io, papercut

    transcripts = []
    for path in args.transcripts:
        try:
            transcripts.append(io.load_transcript(path))
        except (ValueError, FileNotFoundError) as exc:
            _fail(str(exc))
    title = args.title or Path(args.transcripts[0]).stem
    if len(transcripts) == 1:
        text = papercut.create_papercut(transcripts[0], fps=args.fps, title=title)
    else:
        text = papercut.create_papercut_multi(transcripts, fps=args.fps, title=title)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Papercut written to {args.output} — tick the takes you want.")
    else:
        print(text)


def cmd_papercut_render(args: argparse.Namespace) -> None:
    from monteur import io, papercut

    try:
        cut = papercut.parse_papercut(Path(args.papercut).read_text(encoding="utf-8"))
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
    timeline = papercut.papercut_to_timeline(cut, handles=args.handles)
    if not timeline.clips:
        _fail("no takes are ticked in the papercut — nothing to render")
    io.save_timeline(timeline, args.output)
    selected = len(timeline.track_clips("V1"))
    print(
        f"{selected} takes -> {args.output} "
        f"({timeline.duration_seconds:.1f}s at {timeline.fps:g} fps)"
    )


def cmd_convert(args: argparse.Namespace) -> None:
    from monteur import io

    timeline = _load_timeline(args.input, args.fps)
    io.save_timeline(timeline, args.output)
    print(f"{args.input} -> {args.output}")


def cmd_resolve(args: argparse.Namespace) -> None:
    from monteur.resolve import (
        MonteurResolveError,
        connect,
        install_scripts,
        read_timeline_isolated,
        resolve_status_isolated,
    )

    if args.action == "install-scripts":
        for path in install_scripts():
            print(f"Installed {path}")
        print(
            "Restart DaVinci Resolve, then find the scripts under "
            "Workspace > Scripts > Utility."
        )
        return

    if args.action == "doctor":
        from monteur.resolve import diagnose

        d = diagnose()
        env = d["monteur_resolve_python"]
        print("MONTEUR_RESOLVE_PYTHON: " + (env or "(not set — using Monteur's own Python)"))
        print(f"Resolve worker interpreter: {d['worker_interpreter']}")
        info = d.get("info")
        if info:
            print(
                f"  -> Python {info['python_version']} ({info['bits']}-bit)"
            )
            print(
                "  -> Resolve module: "
                + (info["module_dir"] or "NOT FOUND on the standard paths")
            )
        print(f"\nVerdict: {d['verdict']}")
        return

    # Read-only inspection runs in a child process so an incompatible-Python
    # native crash in Resolve's module can't take the CLI down.
    if args.action == "status":
        status = resolve_status_isolated()
        if not status.get("connected"):
            _fail(status.get("error", "DaVinci Resolve not available"))
        print(f"Connected to project: {status['project']}")
        for name in status.get("timelines", []):
            marker = "*" if name == status.get("current") else " "
            print(f" {marker} {name}")
        return
    if args.action == "analyze":
        from monteur.analysis import analyze_timeline

        try:
            timeline = read_timeline_isolated()
        except MonteurResolveError as exc:
            _fail(str(exc))
        _print_stats(analyze_timeline(timeline))
        return

    # Mutating ops still use the in-process bridge (they need a compatible
    # Python to work at all; a crash here prints via faulthandler).
    try:
        bridge = connect()
        if args.action == "import":
            if not args.file:
                _fail("resolve import needs a file argument")
            bridge.import_timeline_file(args.file)
            print(f"Imported {args.file} into {bridge.project_name()}")
    except MonteurResolveError as exc:
        _fail(str(exc))


def _sift_progress(index: int, total: int, name: str, stage: str, report) -> None:
    """Per-clip feedback for a sift run: one line as a clip starts, then the
    result appended when it finishes.

    Sequential lines (not a \\r progress bar) are used deliberately: they are
    robust across terminals and give an honest, scrollable log of what was
    analysed. ``flush=True`` on every print so Windows consoles (which buffer
    without a newline flush) show progress live.
    """
    if stage == "start":
        print(f"[{index}/{total}] {name} ...", flush=True)
    elif stage == "done":
        print(
            f"[{index}/{total}] {name} — {report.usable_ratio * 100:.0f}% usable, "
            f"{len(report.moments)} good moments",
            flush=True,
        )


def cmd_sift(args: argparse.Namespace) -> None:
    from monteur.media import MonteurMediaError
    from monteur.sift import list_media, sift_directory

    try:
        count = len(list_media(args.folder))
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not count:
        _fail(f"no video files found in {args.folder}")
    print(
        f"Scanning {count} clips in {args.folder} — this decodes each clip, "
        f"so it can take a few seconds per clip.",
        flush=True,
    )
    try:
        reports = sift_directory(args.folder, progress=_sift_progress)
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not reports:
        _fail(f"no video files found in {args.folder}")
    for report in reports:
        for note in report.notes:
            print(f"  {note}")


def cmd_create(args: argparse.Namespace) -> None:
    from monteur import io
    from monteur.media import MonteurMediaError
    from monteur.montage import montage_to_timeline, plan_montage
    from monteur.music import analyze_music
    from monteur.sift import sift_directory

    if not args.output and not args.into_resolve:
        _fail("create needs -o/--output and/or --into-resolve")
    if args.brief:
        from monteur.brief import merge_brief, resolve_brief

        settings = resolve_brief(args.brief)
        # Explicit flags win over the brief: only values still at their
        # argparse defaults are overridden (see monteur.brief.merge_brief).
        args.style, args.order, args.max_duration = merge_brief(
            args.style, args.order, args.max_duration, settings
        )
        print(f"Brief: {settings.rationale}")
        print(
            f"  -> style {args.style}, order {args.order}, "
            f"max duration {args.max_duration if args.max_duration else 'full song'}"
        )
    # No-music mode (ride-POV: the clips' own sound IS the soundtrack).
    # Validated after the brief so a brief-set max duration counts.
    if not args.music:
        if args.audio != "original":
            _fail(
                "no music given — pass a song file, or add --audio original "
                "to cut to the clips' own sound"
            )
        if not args.max_duration:
            _fail("no music given — pass --max-duration to set the cut length")
    try:
        from monteur.sift import list_media

        count = len(list_media(args.folder))
        print(
            f"Scanning {count} clips in {args.folder} — this decodes each clip, "
            f"so it can take a few seconds per clip.",
            flush=True,
        )
        reports = sift_directory(args.folder, progress=_sift_progress)
        print(f"  {len(reports)} clips, "
              f"{sum(len(r.moments) for r in reports)} good moments found")
        music = None
        if args.music:
            print("Analyzing music ...")
            music = analyze_music(args.music)
            print(f"  {music.tempo:.0f} BPM, {len(music.beats)} beats, "
                  f"{music.duration:.0f}s")
    except MonteurMediaError as exc:
        _fail(str(exc))
    try:
        plan = plan_montage(
            reports, music, order=args.order, max_duration=args.max_duration,
            style=args.style, allow_repeats=args.allow_repeats,
            cut_lead=args.cut_lead, pace=args.pace,
            transitions=args.transitions,
        )
    except ValueError as exc:
        _fail(str(exc))
    if not plan.entries:
        _fail("no usable material found — run 'monteur sift' to see why")
    audio_wording = {
        "music": "song only",
        "mix": "song + the clips' own sound",
        "original": "the clips' own sound, no song",
    }
    print(f"Audio: {args.audio} ({audio_wording[args.audio]})")
    if args.output:
        timeline = montage_to_timeline(
            plan, fps=args.fps, audio=args.audio, canvas=args.canvas
        )
        io.save_timeline(timeline, args.output)
        print(f"\n{len(plan.entries)} cuts -> {args.output} "
              f"({plan.duration:.1f}s at {args.fps:g} fps)")
    if args.into_resolve:
        from monteur.resolve import MonteurResolveError, connect

        try:
            name = connect().build_timeline_from_plan(plan, fps=args.fps)
        except MonteurResolveError as exc:
            _fail(str(exc))
        print(f"\n{len(plan.entries)} cuts -> Resolve timeline {name!r} "
              f"({plan.duration:.1f}s at {args.fps:g} fps)")
    for note in plan.notes:
        print(f"  {note}")


def cmd_transcribe(args: argparse.Namespace) -> None:
    import json as json_module
    from dataclasses import asdict

    from monteur.transcribe import MonteurTranscribeError, transcribe_directory, transcribe_file

    target = Path(args.path)
    try:
        if target.is_dir():
            results = transcribe_directory(target, model=args.model, language=args.language)
        else:
            results = {str(target): transcribe_file(target, model=args.model, language=args.language)}
    except (MonteurTranscribeError, FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
    for media, transcript in results.items():
        out = Path(media).with_suffix(".json")
        if not out.exists():
            payload = {
                "segments": [asdict(s) | {"start": s.start, "end": s.end} for s in transcript.segments],
                "language": transcript.language,
            }
            out.write_text(json_module.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"{media} -> {out.name} ({len(transcript.segments)} segments)")


def cmd_assembly(args: argparse.Namespace) -> None:
    from monteur import io
    from monteur.assembly import TakeSource, assembly_to_timeline, plan_assembly
    from monteur.screenplay import parse_fountain
    from monteur.transcribe import scene_take_from_name

    try:
        screenplay = parse_fountain(Path(args.script).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
    takes = []
    takes_dir = Path(args.takes)
    for path in sorted(takes_dir.glob("*")):
        if path.suffix.lower() not in (".srt", ".json"):
            continue
        try:
            transcript = io.load_transcript(path)
        except ValueError as exc:
            print(f"monteur: skipping {path.name}: {exc}", file=sys.stderr)
            continue
        scene_hint, take_hint = scene_take_from_name(path.name)
        takes.append(
            TakeSource(name=path.stem, transcript=transcript, scene_hint=scene_hint, take_hint=take_hint)
        )
    if not takes:
        _fail(f"no .srt/.json transcripts found in {takes_dir} — run 'monteur transcribe' first")

    plan = plan_assembly(screenplay, takes, max_takes_per_scene=args.max_takes)
    print(f"Assembly plan — {plan.coverage() * 100:.0f}% of dialogue covered\n")
    for scene in plan.scenes:
        print(f"  {scene.heading or '(untitled scene)'}")
        for score in sorted(scene.take_scores, key=lambda s: s.total, reverse=True)[:3]:
            print(
                f"    {score.take}: coverage {score.coverage * 100:.0f}%, "
                f"accuracy {score.accuracy * 100:.0f}%, fluffs {score.fluffs}"
            )
        for note in scene.notes:
            print(f"    note: {note}")

    timeline = assembly_to_timeline(plan, takes, fps=args.fps, handles=args.handles)
    if not timeline.clips:
        _fail("nothing matched — check scene numbers in the script and file names")
    io.save_timeline(timeline, args.output)
    print(
        f"\n{len(timeline.track_clips('V1'))} segments -> {args.output} "
        f"({timeline.duration_seconds:.1f}s at {args.fps:g} fps)"
    )


def cmd_ui(args: argparse.Namespace) -> None:
    from monteur.web import serve

    try:
        serve(port=args.port, project_root=args.project, open_browser=not args.no_browser)
    except OSError as exc:
        _fail(f"could not start Monteur Studio on port {args.port}: {exc}")


def cmd_mcp(args: argparse.Namespace) -> None:
    try:
        from monteur import mcp_server
    except ImportError:
        _fail(
            "the MCP server needs the 'mcp' package — install it with: "
            "pip install 'monteur[mcp]'"
        )
    mcp_server.main()


def cmd_ai(args: argparse.Namespace) -> None:
    from monteur.ai import MonteurAIError, pacing_notes, suggest_selects, summarize_footage

    try:
        if args.action == "selects":
            text = Path(args.file).read_text(encoding="utf-8")
            print(suggest_selects(text, brief=args.brief or ""))
        elif args.action == "notes":
            from monteur.analysis import analyze_timeline

            stats = analyze_timeline(_load_timeline(args.file, args.fps))
            print(pacing_notes(stats))
        elif args.action == "log":
            from monteur import io

            print(summarize_footage(io.load_transcript(args.file)))
    except (MonteurAIError, ValueError, FileNotFoundError) as exc:
        _fail(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monteur",
        description="Monteur — AI-assisted editing room toolkit for DaVinci Resolve.",
    )
    parser.add_argument("--version", action="version", version=f"monteur {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="pacing & rhythm analysis of a timeline")
    p.add_argument("timeline", help="EDL or FCPXML file")
    p.add_argument("--fps", type=float, default=None, help="frame rate (required for EDL)")
    p.add_argument("--track", default=None, help="video track to analyze (default: V1)")
    p.add_argument("--compare", help="second timeline to compare against")
    p.add_argument("--report", help="write an HTML pacing report to this path")
    p.add_argument("--json", action="store_true", help="print stats as JSON")
    p.add_argument("--scenes", action="store_true", help="per-scene pacing (uses timeline markers)")
    p.add_argument("--reference", help="compare against a genre profile (e.g. thriller, drama)")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("papercut", help="text-based rough cutting from transcripts")
    pc_sub = p.add_subparsers(dest="papercut_command", required=True)
    c = pc_sub.add_parser("create", help="turn transcripts into a papercut checklist")
    c.add_argument("transcripts", nargs="+", help="SRT or Whisper JSON files")
    c.add_argument("-o", "--output", help="output markdown file (default: stdout)")
    c.add_argument("--fps", type=float, default=25.0)
    c.add_argument("--title", default="")
    c.set_defaults(func=cmd_papercut_create)
    r = pc_sub.add_parser("render", help="turn a ticked papercut into a timeline")
    r.add_argument("papercut", help="papercut markdown file")
    r.add_argument("-o", "--output", required=True, help="output .edl/.fcpxml/.xml")
    r.add_argument("--handles", type=float, default=0.0, help="seconds of handle on each side")
    r.set_defaults(func=cmd_papercut_render)

    p = sub.add_parser("convert", help="convert between EDL and FCPXML")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--fps", type=float, default=None, help="frame rate (required for EDL input)")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("resolve", help="talk to a running DaVinci Resolve")
    p.add_argument(
        "action",
        choices=["status", "doctor", "import", "analyze", "install-scripts"],
    )
    p.add_argument("file", nargs="?", help="file for 'import'")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("create", help="automatic first cut: footage folder + song")
    p.add_argument("folder", help="directory with your video clips")
    p.add_argument(
        "music", nargs="?", default=None,
        help="song file (mp3/wav/m4a/...); omit for a no-music cut "
             "(needs --audio original and --max-duration)",
    )
    p.add_argument("-o", "--output", help="output .fcpxml/.edl")
    p.add_argument(
        "--into-resolve",
        action="store_true",
        help="build the timeline directly in a running DaVinci Resolve",
    )
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--order", choices=["chronological", "best_first"], default="chronological")
    p.add_argument("--max-duration", type=float, default=None, help="cap the cut length (seconds)")
    p.add_argument(
        "--style", default="auto",
        help="montage style: auto, travel, wedding, music_video, trailer",
    )
    p.add_argument(
        "--audio", choices=["music", "mix", "original"], default="music",
        help="what plays under the pictures: the song (music), song + the "
             "clips' own sound (mix), or only the clips' own sound (original)",
    )
    p.add_argument(
        "--allow-repeats", action="store_true",
        help="let footage repeat instead of capping the cut length at "
             "1.5x the unique material",
    )
    p.add_argument(
        "--cut-lead", type=float, default=0.04,
        help="place each cut this many seconds BEFORE the beat so the "
             "incoming shot lands on it (default 0.04, 0 disables)",
    )
    p.add_argument(
        "--pace", type=float, default=None,
        help="approximate seconds per clip in the fastest phase, e.g. 1 for "
             "snappy cuts or 4 for long calm shots; slower phases scale with "
             "it (default: the style's own pacing)",
    )
    p.add_argument(
        "--canvas",
        choices=["hd", "uhd", "vertical", "vertical-uhd", "cine", "cine-uhd"],
        default="uhd",
        help="timeline shape and resolution: uhd 3840x2160 (default), "
             "hd 1920x1080, vertical[-uhd] 9:16 for Shorts/Reels, "
             "cine[-uhd] 2.39:1 cinemascope",
    )
    p.add_argument(
        "--transitions", choices=["auto", "cuts", "dissolves", "smash"],
        default="auto",
        help="how clips hand over: auto = the style's own habits (the "
             "trailer smashes to black), cuts = hard cuts only, dissolves = "
             "dissolve on every cut, smash = black title-slot gaps at act "
             "changes",
    )
    p.add_argument(
        "--brief", default="",
        help='natural-language brief, e.g. "90 Sekunden, energiegeladen" — '
             "sets style/order/max-duration; explicit flags win over the brief",
    )
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("sift", help="scan footage: what's usable, what's not")
    p.add_argument("folder", help="directory with your video clips")
    p.set_defaults(func=cmd_sift)

    p = sub.add_parser("transcribe", help="transcribe media files (whisper)")
    p.add_argument("path", help="media file or directory")
    p.add_argument("--model", default="small", help="whisper model (default: small)")
    p.add_argument("--language", default=None)
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("assembly", help="build a first cut from screenplay + take transcripts")
    p.add_argument("script", help="screenplay (.fountain or plain text)")
    p.add_argument("takes", help="directory with take transcripts (.srt/.json), named like the clips")
    p.add_argument("-o", "--output", required=True, help="output .edl/.fcpxml")
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--handles", type=float, default=0.5, help="seconds of handle per side")
    p.add_argument("--max-takes", type=int, default=1, help="takes per scene to draw from")
    p.set_defaults(func=cmd_assembly)

    p = sub.add_parser("ui", help="launch Monteur Studio (local web app)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--project", default=".", help="project directory for version history")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("mcp", help="run the MCP server for Claude Desktop/claude.ai")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ai", help="Claude-powered editorial assistance")
    p.add_argument("action", choices=["selects", "notes", "log"])
    p.add_argument("file", help="papercut (selects), timeline (notes) or transcript (log)")
    p.add_argument("--brief", help="editorial brief for 'selects'")
    p.add_argument("--fps", type=float, default=None)
    p.set_defaults(func=cmd_ai)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
