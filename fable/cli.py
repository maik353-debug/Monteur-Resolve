"""Fable command line interface.

Workflow overview::

    fable analyze cut_v3.edl --fps 25 --report pacing.html
    fable analyze cut_v3.edl --compare cut_v2.edl --fps 25
    fable papercut create interview.srt -o cut.md --fps 25
    # ... tick the takes you want in cut.md ...
    fable papercut render cut.md -o rough_cut.fcpxml
    fable convert cut.edl cut.fcpxml --fps 25
    fable resolve status
    fable ai selects cut.md --brief "90s teaser, keep it fast"
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from fable import __version__


def _fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"fable: {message}", file=sys.stderr)
    raise SystemExit(1)


def _load_timeline(path: str, fps: float | None):
    from fable import io

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
    from fable.analysis import analyze_timeline, compare

    stats = analyze_timeline(_load_timeline(args.timeline, args.fps), track=args.track)
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
    if args.report:
        from fable.report import save_report

        save_report(stats, args.report, compare_to=other)
        print(f"\nReport written to {args.report}")


def cmd_papercut_create(args: argparse.Namespace) -> None:
    from fable import io, papercut

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
    from fable import io, papercut

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
    from fable import io

    timeline = _load_timeline(args.input, args.fps)
    io.save_timeline(timeline, args.output)
    print(f"{args.input} -> {args.output}")


def cmd_resolve(args: argparse.Namespace) -> None:
    from fable.resolve import FableResolveError, connect

    try:
        bridge = connect()
        if args.action == "status":
            print(f"Connected to project: {bridge.project_name()}")
            for name in bridge.list_timelines():
                marker = "*" if name == bridge.current_timeline_name() else " "
                print(f" {marker} {name}")
        elif args.action == "import":
            if not args.file:
                _fail("resolve import needs a file argument")
            bridge.import_timeline_file(args.file)
            print(f"Imported {args.file} into {bridge.project_name()}")
        elif args.action == "analyze":
            from fable.analysis import analyze_timeline

            timeline = bridge.read_timeline()
            _print_stats(analyze_timeline(timeline))
    except FableResolveError as exc:
        _fail(str(exc))


def cmd_ui(args: argparse.Namespace) -> None:
    from fable.web import serve

    try:
        serve(port=args.port, project_root=args.project, open_browser=not args.no_browser)
    except OSError as exc:
        _fail(f"could not start Fable Studio on port {args.port}: {exc}")


def cmd_ai(args: argparse.Namespace) -> None:
    from fable.ai import FableAIError, pacing_notes, suggest_selects, summarize_footage

    try:
        if args.action == "selects":
            text = Path(args.file).read_text(encoding="utf-8")
            print(suggest_selects(text, brief=args.brief or ""))
        elif args.action == "notes":
            from fable.analysis import analyze_timeline

            stats = analyze_timeline(_load_timeline(args.file, args.fps))
            print(pacing_notes(stats))
        elif args.action == "log":
            from fable import io

            print(summarize_footage(io.load_transcript(args.file)))
    except (FableAIError, ValueError, FileNotFoundError) as exc:
        _fail(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fable",
        description="Fable — AI-assisted editing room toolkit for DaVinci Resolve.",
    )
    parser.add_argument("--version", action="version", version=f"fable {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="pacing & rhythm analysis of a timeline")
    p.add_argument("timeline", help="EDL or FCPXML file")
    p.add_argument("--fps", type=float, default=None, help="frame rate (required for EDL)")
    p.add_argument("--track", default=None, help="video track to analyze (default: V1)")
    p.add_argument("--compare", help="second timeline to compare against")
    p.add_argument("--report", help="write an HTML pacing report to this path")
    p.add_argument("--json", action="store_true", help="print stats as JSON")
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
    p.add_argument("action", choices=["status", "import", "analyze"])
    p.add_argument("file", nargs="?", help="file for 'import'")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("ui", help="launch Fable Studio (local web app)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--project", default=".", help="project directory for version history")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser")
    p.set_defaults(func=cmd_ui)

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
