"""Monteur command line interface.

Workflow overview::

    monteur analyze cut_v3.edl --fps 25 --report pacing.html
    monteur analyze cut_v3.edl --compare cut_v2.edl --fps 25
    monteur papercut create interview.srt -o cut.md --fps 25
    # ... tick the takes you want in cut.md ...
    monteur papercut render cut.md -o rough_cut.fcpxml
    monteur convert cut.edl cut.fcpxml --fps 25
    monteur create clips song.mp3 -o cut.fcpxml --save-plan plan.json
    monteur revise plan.json clips -o cut_v2.fcpxml --brief "zweite Hälfte ruhiger"
    monteur resolve status
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
        timeline, notes = assemble_movie(
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
    from monteur.movie import load_project, project_progress

    try:
        project = load_project(args.project_dir)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
    progress = project_progress(project)
    print(
        f"{project.title} — {progress['assigned']}/{progress['scenes']} "
        f"scenes assigned ({progress['percent']}%)"
    )
    for scene in project.scenes:
        mark = "x" if scene.status == "assigned" else " "
        line = f"  [{mark}] {scene.number:>2}  {scene.heading}"
        if scene.folder:
            line += f"  -> {scene.folder}"
        print(line)


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
                f"{shot.label or '(no label)'}")
        if shot.hero >= 0.5:
            line += f"  [hero {shot.hero:.1f}]"
        print(line)
        if shot.tags:
            print(f"      tags: {', '.join(shot.tags)}")


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
    try:
        plan = plan_montage(
            reports, music, order=args.order, max_duration=args.max_duration,
            style=args.style, allow_repeats=args.allow_repeats,
            cut_lead=args.cut_lead, pace=args.pace,
            transitions=args.transitions, sfx=args.sfx,
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
        # Built in an ISOLATED child process (build_plan_isolated): Resolve's
        # native module can hard-crash an incompatible interpreter, and the
        # subprocess honors MONTEUR_RESOLVE_PYTHON.
        from monteur.resolve import build_plan_isolated, titles_from_plan

        titles = titles_from_plan(plan) if plan.dips else None
        result = build_plan_isolated(
            plan, fps=args.fps, titles=titles, canvas=args.canvas
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
            print(
                f"    {total // 60}:{total % 60:02d}  "
                f"{cue.kind:<{kind_width}}  {cue.query:<{query_width}}  "
                f"({cue.note})"
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
    from monteur.montage import montage_to_timeline, plan_from_dict
    from monteur.music import analyze_music
    from monteur.revise import parse_revision, revise_plan, style_from_plan
    from monteur.sift import list_media, sift_directory

    try:
        data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"plan file not found: {args.plan}")
    except json.JSONDecodeError as exc:
        _fail(f"{args.plan} is not valid JSON: {exc}")
    try:
        plan = plan_from_dict(data)
    except ValueError as exc:
        _fail(str(exc))
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
        "--sfx", action="store_true",
        help="plan a sound-design layer: timed cues (ambience, risers, "
             "impacts, sub-drops, whooshes) as green timeline markers with "
             "ready-to-paste SFX search queries — the film mode, best with "
             "--audio original",
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
    p_status.set_defaults(func=cmd_movie_status)

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
