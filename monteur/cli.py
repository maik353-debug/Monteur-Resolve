"""Monteur command line interface.

Workflow overview::

    monteur analyze cut_v3.edl --fps 25 --report pacing.html
    monteur analyze cut_v3.edl --compare cut_v2.edl --fps 25
    monteur papercut create interview.srt -o cut.md --fps 25
    # ... tick the takes you want in cut.md ...
    monteur papercut render cut.md -o rough_cut.fcpxml
    monteur convert cut.edl cut.fcpxml --fps 25
    monteur create clips song.mp3 -o cut.fcpxml --save-plan plan.json
    monteur create clips song.mp3 -o cut.fcpxml --style trailer --see --ai-cut
    monteur elements sfx_library
    monteur create clips song.mp3 -o cut.fcpxml --elements sfx_library
    monteur revise plan.json clips -o cut_v2.fcpxml --brief "zweite Hälfte ruhiger"
    monteur preview plan.json -o preview.mp4
    monteur export plan.json -o video.mp4 --canvas uhd --quality high
    monteur upload video.mp4 --title "Alpen im Herbst" --tags travel,alps
    monteur missing clips --style trailer --target 60
    monteur direct plan.json clips --apply -o cut_v3.fcpxml
    monteur resolve status
    monteur resolve render --out renders --preset 2160p
    monteur ai selects cut.md --brief "90s teaser, keep it fast"
"""

from __future__ import annotations

import argparse
import json
import re
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

    if args.action == "render":
        # The finished-video step: Resolve's own Deliver engine does the
        # work; Monteur watches from an isolated child (crash-safe, never
        # raises). Percent updates redraw one \r line, like a download.
        from monteur.resolve import render_isolated

        if not args.out:
            _fail("resolve render needs --out DIR (folder for the finished video)")

        def progress(percent: int) -> None:
            print(f"\rRendering… {percent}%", end="", flush=True)

        print("Rendering through Resolve's Deliver engine …", flush=True)
        result = render_isolated(
            args.timeline,
            args.out,
            args.name or "monteur_render",
            preset=args.preset,
            progress=progress,
        )
        print(flush=True)  # end the \r progress line
        if not result.get("ok"):
            _fail(result.get("error", "DaVinci Resolve could not render the video."))
        seconds = result.get("seconds")
        timing = f" in {seconds:.0f}s" if isinstance(seconds, (int, float)) else ""
        print(f"Your video is ready: {result.get('path')}{timing}")
        print(f"  (rendered with {result.get('preset')})")
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


def cmd_movie_new(args: argparse.Namespace) -> None:
    from monteur.ai import MonteurAIError
    from monteur.movie import generate_movie, save_project

    print("Drafting your blueprint — Claude is writing the screenplay ...", flush=True)
    try:
        project = generate_movie(args.brief, genre=args.genre)
    except MonteurAIError as exc:
        _fail(str(exc))
    paths = save_project(project, args.project_dir)
    print(f"\n{project.title!r} — {len(project.scenes)} scenes")
    if project.logline:
        print(f"  {project.logline}")
    for note in project.notes:
        print(f"  {note}")
    print("\nWritten:")
    for path in paths:
        print(f"  {path}")
    print(
        "\nNext: print shotlist.md, shoot the scenes (2-3 takes, name files "
        "S03_T02), then come back for the assembly."
    )


def _movie_progress(index: int, total: int, name: str, stage: str) -> None:
    """Per-scene and per-clip feedback for a movie assembly, in the same
    sequential-line style as :func:`_sift_progress` (robust across
    terminals, scrollable). Scene lines are flush left; the sift's own
    per-clip lines are indented under their scene.
    """
    if stage == "scene":
        print(f"[scene {index}/{total}] {name}", flush=True)
    elif stage == "start":
        print(f"  [{index}/{total}] {name} ...", flush=True)
    elif stage == "done":
        print(f"  [{index}/{total}] {name} — sifted", flush=True)


def cmd_movie_assemble(args: argparse.Namespace) -> None:
    from monteur import io
    from monteur.media import MonteurMediaError
    from monteur.movie import assemble_movie, load_project

    try:
        project = load_project(args.project_dir)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
    print(
        f"Assembling {project.title!r} — sifting each scene's footage "
        "(this decodes the clips, so it can take a while) ...",
        flush=True,
    )
    try:
        timeline, notes, _plan = assemble_movie(
            project, fps=args.fps, canvas=args.canvas, progress=_movie_progress
        )
    except (ValueError, FileNotFoundError, MonteurMediaError) as exc:
        _fail(str(exc))
    io.save_timeline(timeline, args.output)
    print(
        f"\nFilm -> {args.output} "
        f"({timeline.duration_seconds:.1f}s at {args.fps:g} fps)"
    )
    for note in notes:
        print(f"  {note}")


def cmd_movie_status(args: argparse.Namespace) -> None:
    from monteur.movie import load_project, project_progress, shoot_plan

    try:
        project = load_project(args.project_dir)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
    progress = project_progress(project)
    print(
        f"{project.title} — {progress['assigned']}/{progress['scenes']} "
        f"scenes assigned ({progress['percent']}%)"
    )
    plan = shoot_plan(project)
    status_marks = {"checked-ok": "✓", "checked-weak": "!"}
    by_number = {s["number"]: s for s in plan["scenes"]}
    for scene in project.scenes:
        state = by_number[scene.number]
        mark = status_marks.get(
            state["status"], "x" if scene.status == "assigned" else " "
        )
        line = f"  [{mark}] {scene.number:>2}  {scene.heading}"
        if scene.folder:
            line += f"  -> {scene.folder}"
        print(line)

    # The shoot plan: what still has to be filmed (deterministic, no AI).
    if plan["unshot"] or plan["reshoot"] or plan["thin"]:
        print("\nShoot plan:")
        for item in plan["unshot"]:
            print(f"  unshot   scene {item['scene']:>2}  {item['heading']}")
            for tip in item["tips"][:2]:
                print(f"           tip: {tip}")
        for item in plan["reshoot"]:
            print(f"  reshoot  scene {item['scene']:>2}  {item['heading']}")
            print(f"           why: {item['why']}")
        for item in plan["thin"]:
            print(f"  thin     scene {item['scene']:>2}  {item['heading']}")
            print(f"           why: {item['why']}")
    else:
        print("\nShoot plan: nothing left to shoot — assemble the film.")

    if getattr(args, "advice", False):
        from monteur.ai import MonteurAIError
        from monteur.movie import shoot_plan_advice

        try:
            advice = shoot_plan_advice(project, plan)
        except MonteurAIError as exc:
            # Graceful by contract: the deterministic plan above is the
            # answer; the AI layer is an upgrade, not a gate.
            print(f"\n(no AI advice: {exc})")
            return
        print("\nClaude's shoot-day advice:")
        for item in advice["first"]:
            print(f"  first: scene {item['scene']} — {item['why']}")
        for step in advice["day_plan"]:
            print(f"  - {step}")
        if advice["summary"]:
            print(f"  {advice['summary']}")
        for note in advice.get("notes", []):
            print(f"  ({note})")


def cmd_find(args: argparse.Namespace) -> None:
    from monteur.find import search_footage

    try:
        shots = search_footage(args.folder, args.query, limit=args.limit)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
    if not shots:
        print(f'no shots match "{args.query}" — try broader words, or re-run '
              f"'monteur see {args.folder}' after adding footage")
        return
    print(f'{len(shots)} shots for "{args.query}":')
    for shot in shots:
        name = Path(shot.clip_path).name
        mm_in, ss_in = divmod(int(shot.start), 60)
        mm_out, ss_out = divmod(int(shot.end), 60)
        line = (f"  {name}  {mm_in}:{ss_in:02d}-{mm_out}:{ss_out:02d}  "
                f"{'“' + shot.label + '”' if shot.spoken else (shot.label or '(no label)')}")
        if shot.spoken:
            line += "  [spoken]"
        if shot.hero >= 0.5:
            line += f"  [hero {shot.hero:.1f}]"
        print(line)
        if shot.tags:
            print(f"      tags: {', '.join(shot.tags)}")


def cmd_missing(args: argparse.Namespace) -> None:
    """Pre-cut coverage check: which shots are still missing?

    Mirrors cmd_sift's plumbing (plain sift of the folder — the vision
    annotations ride along when 'monteur see' cached them and the reports
    carry labels), then asks :func:`monteur.coverage.missing_shots` for
    the gap list and prints it: score, what the material already has,
    and the shots to film grouped by priority, each with a filming tip.
    """
    from monteur.ai import MonteurAIError
    from monteur.coverage import missing_shots
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

    print("Asking Claude what's still missing ...", flush=True)
    try:
        result = missing_shots(
            reports, style=args.style, brief=args.brief,
            target_seconds=args.target,
        )
    except MonteurAIError as exc:
        _fail(str(exc))

    basics = result.get("basics") or {}
    print(f"\nCoverage: {result['coverage_score']}/100"
          + (f" — {result['verdict']}" if result["verdict"] else ""))
    line = f"Material: {basics.get('usable_seconds', 0):.0f}s usable"
    if basics.get("target_seconds"):
        line += f" for a {basics['target_seconds']:.0f}s target"
    print(line)
    for finding in basics.get("findings") or []:
        print(f"  ! {finding}")
    if result["have"]:
        print("You have:")
        for item in result["have"]:
            print(f"  + {item}")
    missing = result["missing"]
    if missing:
        musts = [m for m in missing if m["priority"] == "must"]
        nices = [m for m in missing if m["priority"] != "must"]
        print(f"Still missing ({len(musts)} must, {len(nices)} nice):")
        for entry in musts + nices:
            print(f"  {entry['priority'].upper():<4}  {entry['shot']}")
            if entry["why"]:
                print(f"        why: {entry['why']}")
            if entry["tip"]:
                print(f"        tip: {entry['tip']}")
    else:
        print("Still missing: nothing — Claude would start cutting.")
    if result["summary"]:
        print(f"\n{result['summary']}")
    for note in result.get("notes") or []:
        print(f"  {note}")


def cmd_distill(args: argparse.Namespace) -> None:
    from monteur import io
    from monteur.distill import distill
    from monteur.media import MonteurMediaError
    from monteur.montage import montage_to_timeline
    from monteur.music import analyze_music

    timeline = _load_timeline(args.timeline, args.fps)
    music = None
    if args.music:
        try:
            print("Analyzing music ...")
            music = analyze_music(args.music)
            print(f"  {music.tempo:.0f} BPM, {music.duration:.0f}s")
        except MonteurMediaError as exc:
            _fail(str(exc))
    audio = args.audio if args.music else "original"
    try:
        plan = distill(
            timeline, music, target=args.target, style=args.style, sfx=args.sfx
        )
    except ValueError as exc:
        _fail(str(exc))
    out_timeline = montage_to_timeline(
        plan, fps=args.fps, audio=audio, canvas=args.canvas
    )
    io.save_timeline(out_timeline, args.output)
    print(f"\n{len(plan.entries)} cuts -> {args.output} "
          f"({plan.duration:.1f}s at {args.fps:g} fps)")
    for note in plan.notes:
        print(f"  {note}")


def cmd_elements(args: argparse.Namespace) -> None:
    """Rate a sound-elements folder: classify every snippet, print the library.

    The "rate my snippets" view of :func:`monteur.elements.scan_elements` —
    offline, cached next to the folder, no AI. Kinds impact/whoosh/riser/
    braam are placeable by 'monteur create --elements'; "other" is listed
    but never placed automatically.
    """
    from monteur.elements import scan_elements
    from monteur.media import MonteurMediaError

    try:
        elements = scan_elements(args.folder)
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not elements:
        _fail(f"no audio files found in {args.folder}")
    print(f"{len(elements)} sound elements in {args.folder}:")
    order = {"impact": 0, "whoosh": 1, "riser": 2, "braam": 3, "other": 4}
    for e in sorted(
        elements, key=lambda e: (order.get(e.kind, 9), -e.confidence, e.path)
    ):
        name = Path(e.path).name
        line = (
            f"  {e.kind:<6}  {e.confidence:>4.0%}  {e.duration:>6.2f}s  {name}"
        )
        if e.kind == "other":
            line += "  (unclassified — never placed automatically)"
        print(line)
    placeable = sum(1 for e in elements if e.kind != "other")
    print(
        f"{placeable} placeable — use them with "
        "'monteur create ... --elements " + args.folder + "'"
    )


def cmd_pick_music(args: argparse.Namespace) -> None:
    from monteur.media import MonteurMediaError
    from monteur.pick import list_songs, rank_songs
    from monteur.sift import list_media, sift_directory

    songs = list_songs(args.music_dir)
    if not songs:
        _fail(f"no audio files found in {args.music_dir}")
    try:
        count = len(list_media(args.folder))
        print(
            f"Scanning {count} clips in {args.folder} — this decodes each clip, "
            f"so it can take a few seconds per clip.",
            flush=True,
        )
        reports = sift_directory(args.folder, progress=_sift_progress)
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not reports:
        _fail(f"no video files found in {args.folder}")

    def _song_progress(index: int, total: int, name: str) -> None:
        print(f"  [{index}/{total}] listening to {name} ...", flush=True)

    ratings = rank_songs(
        reports, args.music_dir, target_duration=args.max_duration,
        progress=_song_progress,
    )
    print(f"\nBest match first ({len(ratings)} songs):")
    for rank, rating in enumerate(ratings, start=1):
        name = Path(rating.path).name
        header = f"{rank}. {name}  —  {rating.score * 100:.0f}/100"
        if rating.tempo:
            header += f"  ({rating.tempo:.0f} BPM, {rating.duration:.0f}s)"
        print(header)
        for reason in rating.reasons:
            print(f"     - {reason}")


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


def _clock(seconds: float) -> str:
    """Format a clip position as MM:SS (e.g. 75.4 -> "01:15")."""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _vision_progress(index: int, total: int, name: str, stage: str) -> None:
    """Per-clip feedback for a vision pass, in the same sequential-line
    style as :func:`_sift_progress` (robust across terminals, scrollable).
    """
    wording = {
        "frames": "grabbing frames ...",
        "vision": "asking the vision model ...",
        "cache": "from cache",
    }
    print(f"[{index}/{total}] {name} — {wording.get(stage, stage)}", flush=True)


def _run_vision(reports, *, model=None, max_moments: int = 48) -> None:
    """Annotate sifted reports with the vision pass; print its notes.

    Imports :mod:`monteur.vision` lazily (it needs the anthropic package)
    and fails cleanly when the vision pass can't run.
    """
    from monteur.vision import MonteurVisionError, analyze_reports

    print("Looking at the footage ...", flush=True)
    try:
        notes = analyze_reports(
            reports, model=model, max_moments=max_moments, progress=_vision_progress
        )
    except MonteurVisionError as exc:
        _fail(str(exc))
    for note in notes:
        print(f"  {note}")


def cmd_see(args: argparse.Namespace) -> None:
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
    _run_vision(reports, model=args.model, max_moments=args.max_moments)
    for report in reports:
        seen = [m for m in report.moments if m.label or m.role or m.tags or m.hero]
        if not seen:
            continue
        print(f"\n{report.path}")
        for m in seen:
            line = f"  {_clock(m.start)}-{_clock(m.end)}  [{m.role or '-'}] {m.label}"
            if m.tags:
                line += f" ({', '.join(m.tags)})"
            print(f"{line} hero={m.hero:.1f}")


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
    # Platform preset ("--platform tiktok"): resolved here at the caller
    # layer — plan_montage never takes a platform. One shared precedence
    # rule set (monteur.montage.resolve_platform, same as the Studio):
    # the platform sets the canvas and caps the length; an explicit
    # --style (or a brief-derived one) wins over the preset's "short".
    # Runs AFTER the brief (so a brief style counts as explicit) and
    # BEFORE the no-music check (the cap can supply the required length).
    platform_notes: list[str] = []
    if args.platform:
        from monteur.montage import resolve_platform

        resolved = resolve_platform(
            args.platform, style=args.style, canvas=args.canvas,
            max_duration=args.max_duration,
        )
        if resolved["style"]:
            args.style = resolved["style"]
        args.canvas = resolved["canvas"]
        args.max_duration = resolved["max_duration"]
        platform_notes = resolved["notes"]
        print(
            f"Platform {args.platform}: canvas {args.canvas}, style "
            f"{args.style}, max duration "
            f"{args.max_duration if args.max_duration else 'full song'}"
        )
        for note in platform_notes:
            print(f"  {note}")
    # A sound-elements folder rides on the SFX layer: the cues are the
    # places the elements go, so --elements implies --sfx.
    if args.elements and not args.sfx:
        args.sfx = True
        print("--elements implies --sfx: planning the SFX cue layer")
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
    if args.see:
        # Vision pass: annotate the moments (labels, roles, hero shots,
        # scene groups); plan_montage picks the annotations up by itself.
        _run_vision(reports, max_moments=args.max_moments)
    arrangement = None
    if args.arrangement:
        # The editor's own scene order (see --arrangement's help for the
        # format). Structure and clip names are validated by plan_montage
        # itself — here only the file and JSON shape are checked.
        try:
            arrangement = json.loads(
                Path(args.arrangement).read_text(encoding="utf-8")
            )
        except OSError as exc:
            _fail(f"could not read --arrangement {args.arrangement}: {exc}")
        except ValueError as exc:
            _fail(f"--arrangement {args.arrangement} is not valid JSON: {exc}")
        if not isinstance(arrangement, list) or not arrangement:
            _fail(
                "--arrangement JSON must be a non-empty list of scenes, "
                'e.g. [{"clip": "b.mp4", "start": 12.0}]'
            )
    try:
        if args.ai_cut:
            # Claude composes the cut (monteur.compose): the engine still
            # builds the exact grid plan_montage would, then one Claude
            # completion casts the slots and titles the act breaks. The CLI
            # keeps the graceful fallback (strict=False): an unreachable
            # backend degrades to the heuristic cut with a printed note.
            from monteur.compose import compose_montage

            plan = compose_montage(
                reports, music, style=args.style, brief=args.brief,
                order=args.order, max_duration=args.max_duration,
                allow_repeats=args.allow_repeats, cut_lead=args.cut_lead,
                pace=args.pace, transitions=args.transitions, sfx=args.sfx,
                arrangement=arrangement,
            )
        elif getattr(args, "refine", False):
            # Render -> watch -> refine (blueprint 4.2): plan, self-critique
            # against the Waves 1-3 acceptance metrics, and turn the right
            # knob until they pass (or the budget runs out) — opt-in, the
            # one-shot plan_montage stays the default. Deterministic; offline.
            from monteur.refine import refine_plan

            plan, history = refine_plan(
                reports, music, order=args.order, max_duration=args.max_duration,
                style=args.style, allow_repeats=args.allow_repeats,
                cut_lead=args.cut_lead, pace=args.pace,
                transitions=args.transitions, sfx=args.sfx,
                arrangement=arrangement,
            )
            print(f"Refine: {len(history)} pass(es) watched")
            for note in plan.notes:
                if note.startswith("refine:"):
                    print(f"  {note}")
        else:
            plan = plan_montage(
                reports, music, order=args.order, max_duration=args.max_duration,
                style=args.style, allow_repeats=args.allow_repeats,
                cut_lead=args.cut_lead, pace=args.pace,
                transitions=args.transitions, sfx=args.sfx,
                arrangement=arrangement,
            )
    except ValueError as exc:
        _fail(str(exc))
    if not plan.entries:
        _fail("no usable material found — run 'monteur sift' to see why")
    if platform_notes:
        plan.notes.extend(platform_notes)
    if args.elements:
        # Rate the user's sound library offline and place the snippets as
        # real clips on the plan's SFX layer (riser into the drop, impact
        # on the smash cuts); the notes say what landed where.
        from monteur.elements import assign_elements, scan_elements

        print(f"Rating sound elements in {args.elements} ...")
        try:
            elements = scan_elements(args.elements)
        except MonteurMediaError as exc:
            _fail(str(exc))
        for note in assign_elements(plan, music, elements):
            print(f"  {note}")
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
        # Built in an ISOLATED child process (build_plan_isolated): Resolve's
        # native module can hard-crash an incompatible interpreter, and the
        # subprocess honors MONTEUR_RESOLVE_PYTHON.
        from monteur.resolve import build_plan_isolated, titles_from_plan

        titles = titles_from_plan(plan) if plan.dips else None
        result = build_plan_isolated(
            plan, fps=args.fps, titles=titles, canvas=args.canvas,
            audio=args.audio,
        )
        if not result.get("ok"):
            _fail(result.get("error", "Resolve build failed."))
        name = result["timeline"]
        print(f"\n{len(plan.entries)} cuts -> Resolve timeline {name!r} "
              f"({plan.duration:.1f}s at {args.fps:g} fps)")
        for warning in result.get("warnings") or []:
            print(f"  Resolve: {warning}")
    if args.save_plan:
        _save_plan(plan, args.save_plan)
    if args.kit:
        from monteur.publish import publish_kit

        for note in publish_kit(plan, reports, args.kit, brief=args.brief):
            print(f"  {note}")
    for note in plan.notes:
        print(f"  {note}")
    if plan.sfx:
        # The planned sound-design layer, one line per cue: where it goes,
        # what it is, what to paste into the SFX library, why it is there.
        print(f"  SFX layer ({len(plan.sfx)} cues):")
        kind_width = max(len(cue.kind) for cue in plan.sfx)
        query_width = max(len(cue.query) for cue in plan.sfx)
        for cue in plan.sfx:
            total = int(cue.time)
            line = (
                f"    {total // 60}:{total % 60:02d}  "
                f"{cue.kind:<{kind_width}}  {cue.query:<{query_width}}  "
                f"({cue.note})"
            )
            if cue.file:
                # A placed sound element: this cue is a REAL clip, not a
                # search query.
                line += f"  -> {Path(cue.file).name}"
            print(line)


def cmd_series(args: argparse.Namespace) -> None:
    """Serien-Modus: one tour folder -> N different vertical Shorts.

    A thin caller over :func:`monteur.series.plan_series` (Phase 1: the
    engine + this hook; the Studio UI is a separate follow-up). Scans the
    folder once, analyzes the song once, plans up to N shorts — each built
    around a different strong moment, zero moment repeated across the whole
    series — and writes one plan JSON (and, with --render, one MP4) per
    short into the output directory.
    """
    from monteur.media import MonteurMediaError
    from monteur.music import analyze_music
    from monteur.series import DEFAULT_SHORT_SECONDS, plan_series
    from monteur.sift import sift_directory

    max_seconds = (
        args.max_seconds if args.max_seconds is not None else DEFAULT_SHORT_SECONDS
    )

    if args.canvas not in ("vertical", "vertical-uhd"):
        print(
            f"Note: --canvas {args.canvas} is not vertical — shorts are a "
            "9:16 format; Auto-Reframe applies on a vertical canvas."
        )
    if not args.music and args.audio != "original":
        _fail(
            "no music given — pass a song file, or add --audio original to "
            "cut each short to the clips' own sound"
        )
    try:
        from monteur.sift import list_media

        count = len(list_media(args.folder))
        print(
            f"Scanning {count} clips in {args.folder} — this decodes each "
            "clip, so it can take a few seconds per clip.",
            flush=True,
        )
        reports = sift_directory(args.folder, progress=_sift_progress)
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not reports:
        _fail(f"no video files found in {args.folder}")
    print(
        f"  {len(reports)} clips, "
        f"{sum(len(r.moments) for r in reports)} good moments found"
    )
    if args.see:
        _run_vision(reports, max_moments=args.max_moments)
    music = None
    if args.music:
        print("Analyzing music ...")
        try:
            music = analyze_music(args.music)
        except MonteurMediaError as exc:
            _fail(str(exc))
        print(
            f"  {music.tempo:.0f} BPM, {len(music.beats)} beats, "
            f"{music.duration:.0f}s"
        )
    try:
        shorts = plan_series(
            reports, music, count=args.count, canvas=args.canvas,
            max_seconds=max_seconds, order=args.order,
            allow_repeats=args.allow_repeats, transitions=args.transitions,
        )
    except ValueError as exc:
        _fail(str(exc))
    if not shorts:
        _fail("no usable material found — run 'monteur sift' to see why")
    if len(shorts) < args.count:
        print(
            f"\nSeries: {len(shorts)} of {args.count} requested shorts — the "
            "footage did not yield more distinct strong moments."
        )
    else:
        print(f"\nSeries: {len(shorts)} shorts")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = args.audio or ("music" if music else "original")
    for i, short in enumerate(shorts, start=1):
        print(f"\nShort {i}/{len(shorts)}: {short.note}")
        stem = out_dir / f"short_{i:02d}"
        _save_plan(short.plan, str(stem.with_suffix(".plan.json")))
        if args.render:
            # A real render of N videos reuses render_export per plan; the
            # loop stays thin — one call per short's own plan.
            from monteur.preview import render_export

            def progress(done: int, total: int, label: str, _i: int = i) -> None:
                print(f"  [short {_i}] [{done}/{total}] {label}", flush=True)

            try:
                result = render_export(
                    short.plan, str(stem.with_suffix(".mp4")),
                    canvas=short.canvas, fps=args.fps, audio=audio,
                    quality=args.quality, progress=progress,
                )
            except (MonteurMediaError, ValueError) as exc:
                _fail(str(exc))
            print(
                f"  -> {stem.with_suffix('.mp4')} "
                f"({result['duration']:.1f}s, {result['width']}x{result['height']})"
            )


def _save_plan(plan, path: str) -> None:
    """Write a plan as JSON — the input for the next 'monteur revise'."""
    from monteur.montage import plan_to_dict

    Path(path).write_text(
        json.dumps(plan_to_dict(plan), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Plan saved to {path} — iterate on it with 'monteur revise'.")


_PIN_MMSS_RE = re.compile(r"(\d+):([0-5]\d)")


def _parse_pin(raw: str) -> float:
    """A --pin value in seconds: accepts M:SS ("0:04") or plain seconds."""
    match = _PIN_MMSS_RE.fullmatch(raw.strip())
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    try:
        value = float(raw)
    except ValueError:
        _fail(f"--pin {raw!r} is neither M:SS nor seconds")
    if value < 0:
        _fail(f"--pin {raw!r} is negative — pins are record times in the cut")
    return value


def cmd_revise(args: argparse.Namespace) -> None:
    from monteur import io
    from monteur.media import MonteurMediaError
    from monteur.montage import montage_to_timeline
    from monteur.music import analyze_music
    from monteur.revise import parse_revision, revise_plan, style_from_plan
    from monteur.sift import list_media, sift_directory

    plan = _load_plan(args.plan)
    if not plan.entries:
        _fail("the plan has no entries — nothing to revise")

    pins = [_parse_pin(raw) for raw in args.pin]
    audio = args.audio or ("music" if plan.music_path else "original")
    if not plan.music_path and audio != "original":
        _fail(f"the plan has no music; audio mode {audio!r} needs a song")

    revision = parse_revision(args.brief)
    print(f"Revision: {revision.rationale}")

    # The material has to be re-sifted: the plan file stores the cut, not
    # the footage analysis.
    try:
        count = len(list_media(args.folder))
        print(
            f"Scanning {count} clips in {args.folder} — this decodes each clip, "
            f"so it can take a few seconds per clip.",
            flush=True,
        )
        reports = sift_directory(args.folder, progress=_sift_progress)
        music = None
        if plan.music_path:
            print("Analyzing music ...")
            music = analyze_music(plan.music_path)
            print(f"  {music.tempo:.0f} BPM, {len(music.beats)} beats, "
                  f"{music.duration:.0f}s")
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not reports:
        _fail(f"no video files found in {args.folder}")

    # The plan file stores no run flags, so the re-plan recovers what it
    # can: the style from the plan's own notes, the length from the plan
    # itself (authoritative — allow_repeats keeps the guard from re-capping
    # it), the SFX layer from whether cues were planned. Order and the
    # other knobs use the create defaults.
    try:
        revised = revise_plan(
            plan, reports, music, revision, pinned=pins,
            style=style_from_plan(plan),
            max_duration=plan.duration,
            allow_repeats=True,
            sfx=bool(plan.sfx),
        )
    except ValueError as exc:
        _fail(str(exc))
    if not revised.entries:
        _fail("the revision left no entries — run 'monteur sift' to see why")

    if any(cue.file for cue in plan.sfx):
        # Placed sound elements survive the revision in untouched regions:
        # same-kind cues at the same times get their files back. Cues in
        # genuinely replanned regions stay search-query markers — re-run
        # 'monteur create --elements' to re-place from the library.
        from monteur.elements import carry_element_files

        carried = carry_element_files(plan, revised)
        if carried:
            revised.notes.append(
                f"{carried} sound element{'s' if carried != 1 else ''} "
                "carried over from the previous cut"
            )

    timeline = montage_to_timeline(
        revised, fps=args.fps, audio=audio, canvas=args.canvas
    )
    io.save_timeline(timeline, args.output)
    print(f"\n{len(revised.entries)} cuts -> {args.output} "
          f"({revised.duration:.1f}s at {args.fps:g} fps)")
    if args.save_plan:
        _save_plan(revised, args.save_plan)
    for note in revised.notes:
        print(f"  {note}")


def cmd_preview(args: argparse.Namespace) -> None:
    """Render a saved plan to a small real MP4 — Monteur's own engine, no
    Resolve.

    A thin wrapper over :func:`monteur.preview.render_preview`: the same
    source ranges, record gaps (black dips) and music offset the Resolve
    timeline would get, encoded small and fast. Per-segment progress prints
    in the same sequential-line style as the other long-running commands.
    """
    from monteur.media import MonteurMediaError
    from monteur.preview import render_preview

    plan = _load_plan(args.plan)
    if not plan.entries:
        _fail("the plan has no entries — nothing to preview")
    audio = args.audio or ("music" if plan.music_path else "original")
    if not plan.music_path and audio != "original":
        _fail(f"the plan has no music; audio mode {audio!r} needs a song")

    def progress(done: int, total: int, label: str) -> None:
        print(f"[{done}/{total}] {label}", flush=True)

    print(
        f"Rendering the preview with Monteur's own engine ({audio} audio) ...",
        flush=True,
    )
    try:
        result = render_preview(
            plan, args.output, width=args.width, fps=args.fps, audio=audio,
            progress=progress,
        )
    except (MonteurMediaError, ValueError) as exc:
        _fail(str(exc))
    print(
        f"\nPreview -> {args.output} ({result['duration']:.1f}s, "
        f"{result['width']}px wide, {result['segments']} segments)"
    )
    print(
        "  Rough pixels, real cut — dissolves show as hard cuts here; "
        "the Resolve build stays the reference."
    )


def cmd_export(args: argparse.Namespace) -> None:
    """Direct Export: render a saved plan to the finished MP4 — no Resolve.

    A thin wrapper over :func:`monteur.preview.render_export`: the full
    delivery pipeline (canvas resolution, real dissolves, act titles on
    the dips, placed sound effects, YouTube-target loudness, +faststart)
    with the same progress-line style as ``monteur preview``.
    """
    from monteur.media import MonteurMediaError
    from monteur.preview import render_export

    plan = _load_plan(args.plan)
    if not plan.entries:
        _fail("the plan has no entries — nothing to export")
    audio = args.audio or ("music" if plan.music_path else "original")
    if not plan.music_path and audio != "original":
        _fail(f"the plan has no music; audio mode {audio!r} needs a song")
    size = None
    if args.size:
        try:
            w, h = args.size.lower().split("x", 1)
            size = (int(w), int(h))
        except ValueError:
            _fail(f"--size must look like 1920x1080, not {args.size!r}")

    def progress(done: int, total: int, label: str) -> None:
        print(f"[{done}/{total}] {label}", flush=True)

    print(
        f"Exporting the video with Monteur's own engine "
        f"({args.quality} quality, {audio} audio) ...",
        flush=True,
    )
    try:
        result = render_export(
            plan, args.output, canvas=args.canvas, fps=args.fps, audio=audio,
            quality=args.quality, progress=progress, size=size,
        )
    except (MonteurMediaError, ValueError) as exc:
        _fail(str(exc))
    print(
        f"\nExport -> {args.output} ({result['duration']:.1f}s, "
        f"{result['width']}x{result['height']}, "
        f"rendered in {result['seconds']:.1f}s)"
    )
    for note in result["notes"]:
        print(f"  {note}")
    print(
        "  Upload-ready from Monteur's own engine — Resolve stays the "
        "place for grading and fine-tuning."
    )


def cmd_upload(args: argparse.Namespace) -> None:
    """Upload a finished video to YouTube as a private draft.

    Uses the connection stored by Monteur Studio (Settings → YouTube:
    the user's own Google Cloud Desktop-app client + one "Connect
    YouTube" click); :mod:`monteur.youtube` does the OAuth refresh and
    the resumable upload. Byte progress redraws one ``\\r`` line like the
    Resolve render does; the typed errors (quota/daily cap, expired
    connection) exit 1 with their friendly messages.
    """
    from monteur import youtube
    from monteur.settings import (
        youtube_client_id,
        youtube_client_secret,
        youtube_refresh_token,
    )

    if not Path(args.video).is_file():
        _fail(f"there is no video file at {args.video!r}")
    title = args.title.strip()
    if not title:
        _fail("--title must not be empty")
    description = ""
    if args.description_file:
        try:
            description = Path(args.description_file).read_text(encoding="utf-8")
        except OSError as exc:
            _fail(f"could not read --description-file: {exc}")
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    client_id = youtube_client_id()
    client_secret = youtube_client_secret()
    refresh_token = youtube_refresh_token()
    if not (client_id and client_secret and refresh_token):
        _fail(
            "YouTube is not connected — run 'monteur ui', open Settings → "
            "YouTube and do the one-time setup (your own Google Cloud "
            "project + Connect YouTube)"
        )

    def progress(sent: int, total: int) -> None:
        percent = sent / total * 100 if total else 100.0
        print(
            f"\rUploading… {sent / 1_000_000:.1f} / {total / 1_000_000:.1f} MB "
            f"({percent:.0f}%)",
            end="",
            flush=True,
        )

    try:
        token = youtube.refresh_access_token(client_id, client_secret, refresh_token)
        print(f"Uploading {args.video} to YouTube ({args.privacy}) …", flush=True)
        try:
            uploaded = youtube.upload_video(
                token, args.video, title=title, description=description,
                tags=tags, privacy=args.privacy, progress=progress,
            )
        except youtube.TokenExpired:
            # One refresh + retry; a second failure exits with the message.
            token = youtube.refresh_access_token(
                client_id, client_secret, refresh_token
            )
            uploaded = youtube.upload_video(
                token, args.video, title=title, description=description,
                tags=tags, privacy=args.privacy, progress=progress,
            )
    except youtube.TokenExpired:
        print(flush=True)
        _fail("your YouTube connection expired — reconnect in Monteur's settings")
    except youtube.MonteurYouTubeError as exc:  # QuotaExceeded included
        print(flush=True)
        _fail(str(exc))
    print(flush=True)  # end the \r progress line
    if args.thumbnail:
        note = youtube.set_thumbnail(token, uploaded["video_id"], args.thumbnail)
        if note:
            print(f"  {note}")
    video_id = uploaded["video_id"]
    channel = f" on {uploaded['channel']}" if uploaded.get("channel") else ""
    if args.privacy == "private":
        print(
            f"Uploaded as a private draft{channel} — review and publish "
            "in YouTube Studio:"
        )
    else:
        print(f"Uploaded as unlisted{channel}:")
    print(f"  Studio: https://studio.youtube.com/video/{video_id}/edit")
    print(f"  Watch:  https://www.youtube.com/watch?v={video_id}")


def _load_plan(path: str):
    """Read a saved plan JSON (create/revise --save-plan) or fail cleanly."""
    from monteur.montage import plan_from_dict

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"plan file not found: {path}")
    except json.JSONDecodeError as exc:
        _fail(f"{path} is not valid JSON: {exc}")
    try:
        return plan_from_dict(data)
    except ValueError as exc:
        _fail(str(exc))


def cmd_direct(args: argparse.Namespace) -> None:
    """Director's notes: Claude reviews the planned cut against craft.

    Mirrors cmd_revise's plumbing (plan file + re-sift + the plan's own
    music), then prints the review; --apply additionally swaps in the
    suggested replacements and writes the timeline through the same
    output pathway revise uses.
    """
    from monteur.ai import MonteurAIError
    from monteur.director import apply_review, direct_cut
    from monteur.media import MonteurMediaError
    from monteur.music import analyze_music
    from monteur.sift import list_media, sift_directory

    if args.apply and not args.output:
        _fail("direct --apply needs -o/--output for the improved timeline")

    plan = _load_plan(args.plan)
    if not plan.entries:
        _fail("the plan has no entries — nothing to review")

    # The material has to be re-sifted: the plan file stores the cut, not
    # the footage analysis (same as revise). The cached vision sidecar is
    # picked up by a prior 'monteur see' run, not here.
    try:
        count = len(list_media(args.folder))
        print(
            f"Scanning {count} clips in {args.folder} — this decodes each clip, "
            f"so it can take a few seconds per clip.",
            flush=True,
        )
        reports = sift_directory(args.folder, progress=_sift_progress)
        music = None
        music_path = args.music or plan.music_path
        if music_path:
            print("Analyzing music ...")
            music = analyze_music(music_path)
            print(f"  {music.tempo:.0f} BPM, {len(music.beats)} beats, "
                  f"{music.duration:.0f}s")
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not reports:
        _fail(f"no video files found in {args.folder}")

    print("Asking Claude for director's notes ...", flush=True)
    try:
        review = direct_cut(plan, reports, music, notes=args.notes)
    except MonteurAIError as exc:
        _fail(str(exc))

    print(f"\nVerdict: {review['verdict'] or '(none)'}")
    print(f"Score  : {review['score']}/100")
    if review["praise"]:
        print("Works well:")
        for line in review["praise"]:
            print(f"  + {line}")
    if review["issues"]:
        print(f"Issues ({len(review['issues'])}):")
        for n, issue in enumerate(review["issues"], start=1):
            slots = "+".join(str(s + 1) for s in issue["slots"])
            kind = issue["kind"].replace("_", " ") or "issue"
            print(f"  {n}. slot {slots} — {kind}: {issue['problem']}")
            if issue["suggestion"]:
                print(f"     -> {issue['suggestion']}")
            if issue["replacement"]:
                rep = issue["replacement"]
                print(
                    f"     -> swap in {rep['clip']} "
                    f"{rep['start']:.1f}-{rep['end']:.1f}s"
                )
    else:
        print("Issues : none — Claude would ship this cut.")
    if review["summary"]:
        print(f"\n{review['summary']}")

    if not args.apply:
        return

    from monteur import io
    from monteur.montage import montage_to_timeline

    audio = args.audio or ("music" if plan.music_path else "original")
    if not plan.music_path and audio != "original":
        _fail(f"the plan has no music; audio mode {audio!r} needs a song")
    improved, applied_notes = apply_review(plan, review, reports)
    timeline = montage_to_timeline(
        improved, fps=args.fps, audio=audio, canvas=args.canvas
    )
    io.save_timeline(timeline, args.output)
    print(f"\n{len(improved.entries)} cuts -> {args.output} "
          f"({improved.duration:.1f}s at {args.fps:g} fps)")
    for note in applied_notes:
        print(f"  {note}")
    if args.save_plan:
        _save_plan(improved, args.save_plan)


def cmd_proxies(args: argparse.Namespace) -> None:
    """Prepare the playback proxies Studio's instant players run on.

    A thin wrapper over :func:`monteur.proxies.ensure_proxies` — one small
    seek-friendly H.264 per clip in ``~/.monteur/proxies`` (or
    ``MONTEUR_PROXIES_PATH``), skip-when-fresh, then a cache prune. The
    Studio kicks the same transcodes automatically after every scan; this
    command warms the cache ahead of time (or from scripts).
    """
    from monteur import proxies as proxies_mod
    from monteur.media import MonteurMediaError, list_media

    try:
        paths = [str(p) for p in list_media(args.folder)]
    except MonteurMediaError as exc:
        _fail(str(exc))
    if not paths:
        _fail(f"no video files found in {args.folder}")

    def progress(done: int, total: int, name: str) -> None:
        print(f"[{done}/{total}] {name}", flush=True)

    print(f"Preparing playback proxies in {proxies_mod.proxies_dir()} ...", flush=True)
    made, errors = proxies_mod.ensure_proxies(paths, progress=progress)
    removed = proxies_mod.prune_proxies(max_gb=args.max_gb)
    for path, message in errors.items():
        print(f"  ! {Path(path).name}: {message}")
    print(
        f"\nProxies ready for {len(made)}/{len(paths)} clips"
        + (f" ({len(removed)} old proxies pruned)" if removed else "")
        + " — Studio plays these instantly."
    )
    if errors and not made:
        _fail("no proxy could be prepared — see the messages above")


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
    from monteur.web import serve, serve_app

    try:
        if getattr(args, "window", False):
            # native desktop window (pywebview); falls back to the browser
            # when pywebview isn't installed
            serve_app(port=args.port, project_root=args.project)
        else:
            serve(port=args.port, project_root=args.project, open_browser=not args.no_browser)
    except OSError as exc:
        _fail(f"could not start Monteur Studio on port {args.port}: {exc}")


def cmd_changes(args: argparse.Namespace) -> None:
    import json

    from monteur import changelist

    try:
        old = json.loads(Path(args.old).read_text(encoding="utf-8"))
        new = json.loads(Path(args.new).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"could not read a plan file: {exc}")
    cl = changelist.diff_plans(old, new)
    print(changelist.format_change_list(
        cl, old_label=Path(args.old).stem, new_label=Path(args.new).stem))


def cmd_update(args: argparse.Namespace) -> None:
    from monteur import update as update_mod
    from monteur.settings import update_channel

    channel = getattr(args, "channel", None) or update_channel()
    info = update_mod.check(channel=channel)
    if info.error and not info.latest:
        _fail(info.error)
    if not info.available:
        print(f"Monteur {info.current} is the latest version.")
        return
    print(f"A newer version is available: {info.latest} (you have {info.current}).")
    if info.url:
        print(f"  {info.url}")
    if info.notes:
        print("\n" + info.notes.strip() + "\n")
    if getattr(args, "check", False):
        return
    if info.mode != "frozen":
        print(
            "This is a source install — update with 'git pull' or "
            "'pip install -U monteur'."
        )
        return
    if info.kind == "payload":
        print(f"Downloading {info.payload_name}…")
        version = update_mod.install_payload(info)
        print(f"Installed Monteur {version}. Restart Monteur to use it.")
        return
    if info.kind == "exe" and info.download_url:
        print(f"Downloading {info.asset_name}…")
        update_mod.download(info)
        print(f"Downloaded Monteur {info.latest}. Restart Monteur to finish installing.")
        return
    print("This release has no installable build for your platform yet.")


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
        choices=["status", "doctor", "import", "analyze", "render", "install-scripts"],
    )
    p.add_argument("file", nargs="?", help="file for 'import'")
    p.add_argument(
        "--timeline", default=None,
        help="timeline to render (default: the current one)",
    )
    p.add_argument(
        "--out", default="",
        help="folder for the finished video (render; created if missing)",
    )
    p.add_argument(
        "--name", default="",
        help="output file name for 'render' (default: monteur_render)",
    )
    p.add_argument(
        "--preset", choices=["2160p", "1080p"], default=None,
        help="render quality (default: 2160p)",
    )
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
        help="montage style: auto, travel, wedding, music_video, trailer, "
             "short (the hook-first vertical style)",
    )
    p.add_argument(
        "--platform", choices=["youtube", "short", "reel", "tiktok"],
        default=None,
        help="publish-target preset: sets the canvas (youtube 16:9 4K; "
             "short/reel/tiktok 9:16 4K) and CAPS the length (short/tiktok "
             "60s, reel 90s — min of cap and --max-duration, never longer). "
             "Vertical platforms also pick the hook-first 'short' style — "
             "an explicit --style wins over that, the platform then only "
             "sets canvas and cap. The platform always sets the canvas "
             "(--canvas is ignored with it)",
    )
    p.add_argument(
        "--audio", choices=["music", "mix", "original"], default="music",
        help="what plays under the pictures: the song (music), song + the "
             "clips' own sound (mix), or only the clips' own sound (original)",
    )
    p.add_argument(
        "--sfx", action="store_true",
        help="plan a sound-design layer: timed cues (ambience, risers, "
             "impacts, sub-drops, whooshes) as green timeline markers with "
             "ready-to-paste SFX search queries — the film mode, best with "
             "--audio original",
    )
    p.add_argument(
        "--elements", default="", metavar="DIR",
        help="your own sound library folder (impacts, whooshes, risers): "
             "Monteur classifies the snippets offline and places them as "
             "REAL audio clips on their own track — riser into the drop, "
             "impact on the smash cuts (implies --sfx)",
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
        help="OVERRIDE the cut pace: approximate seconds per clip in the "
             "fastest phase, e.g. 1 for snappy cuts or 4 for long calm "
             "shots; slower phases scale with it. Default: Auto "
             "(recommended) — the engine follows the music, the footage "
             "(calm material cuts slower) and the local tempo, and varies "
             "shot length on its own",
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
        help="how clips hand over: auto (recommended) decides per cut — "
             "hard cuts in the climax and where the same shot continues, "
             "dissolves at scene and daylight changes in calm passages, "
             "and the trailer smashes to black at act changes; cuts = hard "
             "cuts only, dissolves = dissolve on every cut, smash = black "
             "title-slot gaps at act changes",
    )
    p.add_argument(
        "--brief", default="",
        help='natural-language brief, e.g. "90 Sekunden, energiegeladen" — '
             "sets style/order/max-duration; explicit flags win over the "
             "brief. With --ai-cut the same text also briefs the composer",
    )
    p.add_argument(
        "--ai-cut", action="store_true",
        help="let Claude compose the cut: the engine locks the beat grid, "
             "dips and durations, Claude casts every slot, writes the act "
             "titles and a story arc (runs over your Claude connection — "
             "Claude Code costs nothing extra; falls back to the heuristic "
             "cut with a note when Claude is unreachable; sharpest with "
             "--see)",
    )
    p.add_argument(
        "--refine", action="store_true",
        help="render -> watch -> refine (blueprint 4.2): plan the cut, "
             "self-critique it against the acceptance metrics (peak-on-beat "
             "coincidence, silence honesty, no slivers, shot grammar) and "
             "turn the right knob until they pass — opt-in, deterministic "
             "and offline; the plain one-shot cut is the default",
    )
    p.add_argument(
        "--arrangement", default="", metavar="FILE.json",
        help="arrange the story yourself: a JSON list of scenes in YOUR "
             'order, e.g. [{"clip": "b.mp4", "start": 12.0, "after": '
             '{"transition": "smash"}, "sfx": "impact"}, ...] — each scene '
             "claims the next slot on the beat grid; \"after\" (cut/"
             "dissolve/smash) sets the boundary into the next scene, "
             '"sfx" (impact/whoosh/riser) drops a cue there; remaining '
             "slots fill automatically and the notes carry a consistency "
             "report",
    )
    p.add_argument(
        "--save-plan", default="", metavar="PATH.json",
        help="also save the plan as JSON — the input for 'monteur revise' "
             "(iterate on the cut without starting over)",
    )
    p.add_argument(
        "--kit", default="",
        help="also write a publish kit into this folder: thumbnail "
             "candidates from your hero shots, YouTube chapters, title/"
             "description/tag drafts (best with --see)",
    )
    p.add_argument(
        "--see", action="store_true",
        help="look at the footage with a vision model first: openers open, "
             "hero shots land on the drop, same-scene takes stay apart "
             "(needs the anthropic package and ANTHROPIC_API_KEY)",
    )
    p.add_argument(
        "--max-moments", type=int, default=48,
        help="how many of the strongest moments the vision pass looks at "
             "(default 48; only with --see)",
    )
    p.set_defaults(func=cmd_create)

    p = sub.add_parser(
        "series",
        help="Serien-Modus: one tour folder -> N different vertical Shorts, "
             "each built around a different strong moment, zero moment "
             "repeated across the series",
    )
    p.add_argument("folder", help="directory with your video clips (one long tour)")
    p.add_argument(
        "music", nargs="?", default=None,
        help="song file reused for every short; omit for a no-music cut "
             "(needs --audio original)",
    )
    p.add_argument(
        "-n", "--count", type=int, required=True,
        help="how many shorts to try to build (fewer come back when the "
             "footage lacks that many distinct strong moments)",
    )
    p.add_argument(
        "-o", "--output-dir", default="shorts", metavar="DIR",
        help="directory for the per-short plans (and MP4s with --render); "
             "default 'shorts'",
    )
    p.add_argument(
        "--max-seconds", type=float, default=None,
        help="cap each short's length in seconds (default: the short style's "
             "own ~30 s; required without music)",
    )
    p.add_argument(
        "--canvas", choices=["vertical", "vertical-uhd"], default="vertical-uhd",
        help="the 9:16 delivery frame (default vertical-uhd 2160x3840); "
             "Auto-Reframe centres each cut's attention point on it",
    )
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--order", choices=["chronological", "best_first"], default="chronological")
    p.add_argument(
        "--audio", choices=["music", "mix", "original"], default=None,
        help="what plays under each short (default: music when a song is "
             "given, else the clips' own sound)",
    )
    p.add_argument(
        "--transitions", choices=["auto", "cuts", "dissolves", "smash"],
        default="auto",
    )
    p.add_argument(
        "--allow-repeats", action="store_true",
        help="let footage repeat WITHIN a short (zero-repeat ACROSS the "
             "series always holds via the disjoint groups)",
    )
    p.add_argument(
        "--render", action="store_true",
        help="also render each short to an MP4 with Monteur's own engine "
             "(reuses the export pipeline per plan)",
    )
    p.add_argument(
        "--quality", choices=["draft", "high"], default="high",
        help="render quality when --render is set (default high)",
    )
    p.add_argument(
        "--see", action="store_true",
        help="look at the footage with a vision model first (sharpens the "
             "seed strength and the look-variety partition)",
    )
    p.add_argument("--max-moments", type=int, default=48)
    p.set_defaults(func=cmd_series)

    p = sub.add_parser(
        "revise",
        help='iterate on a saved cut: "zweite Hälfte ruhiger" without losing the rest',
    )
    p.add_argument("plan", help="plan JSON from 'monteur create --save-plan' (or a previous revise)")
    p.add_argument("folder", help="the same footage folder the plan was cut from")
    p.add_argument("-o", "--output", required=True, help="output .fcpxml/.edl")
    p.add_argument(
        "--brief", default="",
        help='what to change, e.g. "zweite Hälfte ruhiger" or "second half '
             'calmer, harte Schnitte" (offline German/English keywords)',
    )
    p.add_argument(
        "--pin", action="append", default=[], metavar="TIME",
        help="keep the shot at this record time exactly as it is (M:SS or "
             "seconds); repeatable",
    )
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument(
        "--audio", choices=["music", "mix", "original"], default=None,
        help="what plays under the pictures (default: music when the plan "
             "has a song, original otherwise)",
    )
    p.add_argument(
        "--canvas",
        choices=["hd", "uhd", "vertical", "vertical-uhd", "cine", "cine-uhd"],
        default="uhd",
        help="timeline shape and resolution (same presets as 'create')",
    )
    p.add_argument(
        "--save-plan", default="", metavar="PATH.json",
        help="save the revised plan as JSON for the next iteration",
    )
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser(
        "preview",
        help="watch a saved plan as a small real MP4 — rendered by Monteur's "
             "own engine in seconds, no Resolve needed",
    )
    p.add_argument("plan", help="plan JSON from 'monteur create --save-plan' (or a revise)")
    p.add_argument("-o", "--output", required=True, help="output .mp4")
    p.add_argument(
        "--width", type=int, default=640,
        help="preview width in pixels (default 640; height follows the footage)",
    )
    p.add_argument(
        "--audio", choices=["music", "mix", "original"], default=None,
        help="what plays under the pictures (default: music when the plan "
             "has a song, original otherwise)",
    )
    p.add_argument("--fps", type=float, default=25.0)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser(
        "export",
        help="Direct Export: render a saved plan to the finished, "
             "upload-ready MP4 with Monteur's own engine — dissolves, act "
             "titles, sound effects and YouTube loudness, no Resolve needed",
    )
    p.add_argument("plan", help="plan JSON from 'monteur create --save-plan' (or a revise)")
    p.add_argument("-o", "--output", required=True, help="output .mp4")
    p.add_argument(
        "--canvas",
        choices=["hd", "uhd", "vertical", "vertical-uhd", "cine", "cine-uhd"],
        default="uhd",
        help="canvas shape and resolution (same presets as 'create'; the "
             "export renders at the preset's exact size)",
    )
    p.add_argument(
        "--audio", choices=["music", "mix", "original"], default=None,
        help="what plays under the pictures (default: music when the plan "
             "has a song, original otherwise)",
    )
    p.add_argument(
        "--quality", choices=["high", "medium"], default="high",
        help="encode profile: high = crf 18 preset slow + AAC 320k (the "
             "upload master), medium = crf 21 preset medium + AAC 192k",
    )
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument(
        "--size", default="", metavar="WxH",
        help="advanced/testing: render at an explicit resolution instead "
             "of the canvas preset (e.g. 480x270)",
    )
    p.set_defaults(func=cmd_export)

    p = sub.add_parser(
        "upload",
        help="upload a finished video to YouTube as a private draft "
             "(one-time connection via Monteur Studio's settings)",
    )
    p.add_argument("video", help="the finished video file (e.g. the export's .mp4)")
    p.add_argument("--title", required=True, help="video title on YouTube")
    p.add_argument(
        "--description-file", default="", metavar="FILE",
        help="text file with the description (e.g. drafted from the publish kit)",
    )
    p.add_argument("--tags", default="", help="comma-separated tags")
    p.add_argument(
        "--privacy", choices=["private", "unlisted"], default="private",
        help="private (default) is also all an unverified personal Google "
             "Cloud project may upload — exactly the review-then-publish "
             "draft workflow",
    )
    p.add_argument(
        "--thumbnail", default="",
        help="thumbnail image (jpg/png) — set best-effort after the upload",
    )
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser(
        "direct",
        help="director's notes: Claude reviews the planned cut against "
             "editing craft (free over Claude Code; sharpest after "
             "'monteur see')",
    )
    p.add_argument("plan", help="plan JSON from 'monteur create --save-plan' (or a revise)")
    p.add_argument("folder", help="the same footage folder the plan was cut from")
    p.add_argument(
        "--music", default="",
        help="song file for musical context (default: the plan's own song)",
    )
    p.add_argument(
        "--notes", default="",
        help='context for the review, e.g. "Instagram teaser for our travel blog"',
    )
    p.add_argument(
        "--apply", action="store_true",
        help="apply the review's replacement suggestions and write the "
             "improved timeline (needs -o)",
    )
    p.add_argument("-o", "--output", help="output .fcpxml/.edl (with --apply)")
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument(
        "--audio", choices=["music", "mix", "original"], default=None,
        help="what plays under the pictures (default: music when the plan "
             "has a song, original otherwise)",
    )
    p.add_argument(
        "--canvas",
        choices=["hd", "uhd", "vertical", "vertical-uhd", "cine", "cine-uhd"],
        default="uhd",
        help="timeline shape and resolution (same presets as 'create')",
    )
    p.add_argument(
        "--save-plan", default="", metavar="PATH.json",
        help="save the improved plan as JSON for the next iteration",
    )
    p.set_defaults(func=cmd_direct)

    p = sub.add_parser(
        "movie",
        help="movie creator: from an idea to a production blueprint "
             "(screenplay, scene list, shooting tips)",
    )
    movie_sub = p.add_subparsers(dest="movie_action", required=True)
    p_new = movie_sub.add_parser(
        "new", help="draft a blueprint from your idea (needs ANTHROPIC_API_KEY)"
    )
    p_new.add_argument("project_dir", help="project folder to create/fill")
    p_new.add_argument(
        "--brief", required=True,
        help='idea + constraints, e.g. "5 Minuten Thriller, 2 Personen, '
             'Wald und Auto, nachts, kein Budget"',
    )
    p_new.add_argument("--genre", default="", help="genre (optional)")
    p_new.set_defaults(func=cmd_movie_new)
    p_asm = movie_sub.add_parser(
        "assemble",
        help="assemble the film along the screenplay: scenes in order, each "
             "filled from its assigned footage folder, paced by its cut "
             "intent, with the clips' own sound",
    )
    p_asm.add_argument("project_dir", help="project folder with movie.json")
    p_asm.add_argument("-o", "--output", required=True, help="output .fcpxml/.edl")
    p_asm.add_argument("--fps", type=float, default=25.0)
    p_asm.add_argument(
        "--canvas",
        choices=["hd", "uhd", "vertical", "vertical-uhd", "cine", "cine-uhd"],
        default="uhd",
        help="timeline shape and resolution (same presets as 'create')",
    )
    p_asm.set_defaults(func=cmd_movie_assemble)
    p_status = movie_sub.add_parser(
        "status", help="shooting progress: per-scene status and folders"
    )
    p_status.add_argument("project_dir", help="project folder with movie.json")
    p_status.add_argument(
        "--advice",
        action="store_true",
        help="ask Claude to prioritize the shoot (falls back to the "
        "deterministic plan when no AI backend is reachable)",
    )
    p_status.set_defaults(func=cmd_movie_status)

    p = sub.add_parser(
        "proxies",
        help="prepare small playback proxies so Studio's players scrub "
             "instantly (the UI also does this automatically after a scan)",
    )
    p.add_argument("folder", help="directory with your video clips")
    p.add_argument(
        "--max-gb", type=float, default=5.0,
        help="proxy cache budget in GB — oldest proxies beyond it are "
             "pruned (default 5)",
    )
    p.set_defaults(func=cmd_proxies)

    p = sub.add_parser(
        "find",
        help='search footage by what Claude saw ("kurven", "hero") — instant, '
             "uses the cache from 'monteur see'",
    )
    p.add_argument("folder", help="footage folder (must have been seen)")
    p.add_argument("query", help='what to look for, e.g. "kurve überholen"')
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser(
        "missing",
        help="pre-cut coverage check: which shots are still missing — a "
             "concrete list to film BEFORE you cut (free over Claude Code; "
             "sharpest after 'monteur see')",
    )
    p.add_argument("folder", help="directory with your video clips")
    p.add_argument(
        "--style", default="auto",
        help="what the video should become: auto, travel, wedding, "
             "music_video, trailer (the craft brief the coverage is judged "
             "against)",
    )
    p.add_argument(
        "--brief", default="",
        help='what the video should become, in your own words — e.g. '
             '"epic alps trailer, end on the summit"',
    )
    p.add_argument(
        "--target", type=float, default=None,
        help="planned cut length in seconds — coverage is judged against it",
    )
    p.set_defaults(func=cmd_missing)

    p = sub.add_parser(
        "distill",
        help="distill a finished cut into a short trailer (30/60s) — the "
             "cut's own shots are the material",
    )
    p.add_argument("timeline", help="the finished cut (.edl/.fcpxml)")
    p.add_argument("music", nargs="?", default="", help="song for the trailer (optional)")
    p.add_argument("-o", "--output", required=True, help="output .fcpxml/.edl")
    p.add_argument("--target", type=float, default=60.0, help="trailer length in seconds")
    p.add_argument("--style", default="trailer",
                   help="montage style (default: trailer)")
    p.add_argument("--fps", type=float, default=25.0,
                   help="frame rate (needed to read .edl inputs)")
    p.add_argument("--audio", choices=["music", "mix", "original"], default="music",
                   help="soundtrack mode; no music given -> original automatically")
    p.add_argument("--canvas",
                   choices=["hd", "uhd", "vertical", "vertical-uhd", "cine", "cine-uhd"],
                   default="uhd")
    p.add_argument("--sfx", action="store_true", help="plan an SFX cue layer too")
    p.set_defaults(func=cmd_distill)

    p = sub.add_parser(
        "elements",
        help="rate a folder of sound snippets: impact / whoosh / riser / "
             "braam, classified offline (use with 'create --elements')",
    )
    p.add_argument("folder", help="folder with your sound effect files")
    p.set_defaults(func=cmd_elements)

    p = sub.add_parser(
        "pick-music",
        help="rank candidate songs (e.g. Artlist downloads) against your footage",
    )
    p.add_argument("folder", help="footage folder")
    p.add_argument("music_dir", help="folder with candidate audio files")
    p.add_argument(
        "--max-duration", type=float, default=None,
        help="planned cut length in seconds — length fit is scored against "
             "this instead of the footage's unique material",
    )
    p.set_defaults(func=cmd_pick_music)

    p = sub.add_parser("sift", help="scan footage: what's usable, what's not")
    p.add_argument("folder", help="directory with your video clips")
    p.set_defaults(func=cmd_sift)

    p = sub.add_parser(
        "see", help="look at footage with a vision model: what each moment shows"
    )
    p.add_argument("folder", help="directory with your video clips")
    p.add_argument(
        "--model", default=None,
        help="vision model to ask (default: monteur's own choice)",
    )
    p.add_argument(
        "--max-moments", type=int, default=48,
        help="how many of the strongest moments to look at (default 48)",
    )
    p.set_defaults(func=cmd_see)

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
    p.add_argument("--window", action="store_true",
                   help="open in a native desktop window instead of a browser "
                        "(needs the [app] extra: pip install 'monteur[app]')")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("changes", help="change list between two saved plans (for sound/VFX handoff)")
    p.add_argument("old", help="the earlier plan JSON (e.g. --save-plan output)")
    p.add_argument("new", help="the later plan JSON")
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("update", help="check for and install a newer Monteur build")
    p.add_argument("--check", action="store_true",
                   help="only check — don't download anything")
    p.add_argument("--channel", choices=["stable", "dev"], default=None,
                   help="release channel (default: your saved setting, else stable)")
    p.set_defaults(func=cmd_update)

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
