"""Monteur MCP server — lets Claude drive DaVinci Resolve and Monteur's engines.

Run it with ``monteur mcp`` (stdio transport). Claude Desktop config:

    {"mcpServers": {"monteur": {"command": "monteur", "args": ["mcp"]}}}

Every tool returns a compact JSON-serializable dict. Tools that need a
running DaVinci Resolve never raise on connection problems — they return
``{"error": ..., "hint": ...}`` so Claude can tell the user what to fix.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from monteur import analysis, io, references
from monteur.model import Marker, Timeline, seconds_to_frames
from monteur.resolve import MonteurResolveError, connect

RESOLVE_HINT = (
    "Is DaVinci Resolve running with scripting enabled "
    "(Preferences > System > General)?"
)

mcp_instance = FastMCP("monteur")


# --- helpers ------------------------------------------------------------------


def _resolve_error(exc: Exception) -> dict:
    return {"error": str(exc), "hint": RESOLVE_HINT}


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _load_file_timeline(file: str, fps: float) -> tuple[Timeline | None, dict | None]:
    """Load a local EDL/FCPXML file; (timeline, None) or (None, error dict)."""
    try:
        return io.load_timeline(file, fps=fps or None), None
    except FileNotFoundError:
        return None, {"error": f"file not found: {file}"}
    except (ValueError, OSError) as exc:
        return None, {"error": str(exc)}


def _obtain_timeline(
    timeline: str, file: str, fps: float
) -> tuple[Timeline | None, dict | None]:
    """A Timeline from a local file (preferred when given) or from Resolve."""
    if file:
        return _load_file_timeline(file, fps)
    try:
        bridge = connect()
        return bridge.read_timeline(timeline or None), None
    except MonteurResolveError as exc:
        return None, _resolve_error(exc)


def _characterize(stats: analysis.PacingStats) -> str:
    if stats.shot_count == 0:
        return "Empty timeline — no shots to analyze."
    asl = stats.avg_shot_seconds
    tempo = "fast" if asl < 2.5 else ("moderate" if asl <= 6.0 else "slow")
    rhythm = "even" if stats.std_shot_seconds < 0.5 * asl else "varied"
    return (
        f"{stats.shot_count} shots in {stats.duration_seconds:.0f}s — "
        f"{tempo} cutting (ASL {asl:.1f}s) with a {rhythm} rhythm."
    )


def _stats_dict(stats: analysis.PacingStats) -> dict:
    return {
        "timeline": stats.timeline_name,
        "fps": stats.fps,
        "duration_seconds": _round(stats.duration_seconds),
        "shot_count": stats.shot_count,
        "cut_count": stats.cut_count,
        "avg_shot_seconds": _round(stats.avg_shot_seconds),
        "median_shot_seconds": _round(stats.median_shot_seconds),
        "min_shot_seconds": _round(stats.min_shot_seconds),
        "max_shot_seconds": _round(stats.max_shot_seconds),
        "std_shot_seconds": _round(stats.std_shot_seconds),
        "histogram": {label: count for label, count in stats.histogram},
        "sections": [
            {
                "start": _round(s.start),
                "end": _round(s.end),
                "label": s.label,
                "avg_shot_seconds": _round(s.avg_shot_length),
            }
            for s in stats.sections
        ],
        "longest_shots": [
            {
                "name": shot.name,
                "start_seconds": _round(shot.start),
                "length_seconds": _round(shot.length),
            }
            for shot in stats.longest_shots
        ],
        "characterization": _characterize(stats),
    }


def _one_cut(
    file: str, timeline: str, fps: float, side: str
) -> tuple[analysis.PacingStats | None, dict | None]:
    if not file and not timeline:
        return None, {
            "error": f"cut {side}: pass either file_{side} (absolute path to an "
            f"EDL/FCPXML) or timeline_{side} (a Resolve timeline name)."
        }
    tl, err = _obtain_timeline(timeline, file, fps)
    if err is not None:
        return None, err
    return analysis.analyze_timeline(tl), None


# --- Resolve status -----------------------------------------------------------


@mcp_instance.tool()
def resolve_status() -> dict:
    """Check the connection to DaVinci Resolve and see what's open.

    Use this first when the user wants to work with Resolve: it reports
    whether Monteur can reach a running Resolve instance, the open project's
    name, all timeline names in that project, and which timeline is current.
    Takes no arguments. If it returns an error, Resolve is not running or
    scripting is disabled — relay the hint to the user.
    """
    try:
        bridge = connect()
        project = bridge.project_name()
        timelines = bridge.list_timelines()
        try:
            current = bridge.current_timeline_name()
        except MonteurResolveError:
            current = ""
        return {
            "connected": True,
            "project": project,
            "timelines": timelines,
            "current_timeline": current,
        }
    except MonteurResolveError as exc:
        return _resolve_error(exc)


# --- Pacing analysis ----------------------------------------------------------


@mcp_instance.tool()
def analyze_timeline(timeline: str = "", file: str = "", fps: float = 0) -> dict:
    """Pacing and rhythm statistics for a cut.

    Analyzes either a timeline in the running DaVinci Resolve (pass
    ``timeline`` with its name, or leave everything empty for the current
    timeline) or a local EDL/FCPXML file (pass ``file`` as an absolute path;
    ``fps`` is required for .edl files, e.g. 25, because EDLs carry no frame
    rate). All durations are seconds. Returns scalar stats (ASL = average
    shot length; lower means faster cutting), a shot-length histogram,
    fast/medium/slow sections with start/end times, the five longest shots,
    and a one-line characterization of the cut.
    """
    tl, err = _obtain_timeline(timeline, file, fps)
    if err is not None:
        return err
    return _stats_dict(analysis.analyze_timeline(tl))


@mcp_instance.tool()
def analyze_scenes(timeline: str = "", file: str = "", fps: float = 0) -> dict:
    """Per-scene pacing, using timeline markers as scene boundaries.

    Same source options as analyze_timeline: a Resolve timeline by name
    (empty = current timeline) or a local EDL/FCPXML ``file`` (absolute
    path; ``fps`` required for EDL). Every marker starts a new scene named
    after the marker; material before the first marker is "Opening"; a cut
    without markers comes back as one scene. Use this to see which scenes
    drag or race relative to the rest of the film. Times are seconds.
    """
    tl, err = _obtain_timeline(timeline, file, fps)
    if err is not None:
        return err
    scenes = analysis.analyze_scenes(tl)
    return {
        "timeline": tl.name,
        "scene_count": len(scenes),
        "scenes": [
            {
                "heading": scene.heading,
                "start": _round(scene.start),
                "end": _round(scene.end),
                "shot_count": scene.stats.shot_count,
                "avg_shot_seconds": _round(scene.stats.avg_shot_seconds),
                "characterization": _characterize(scene.stats),
            }
            for scene in scenes
        ],
    }


@mcp_instance.tool()
def compare_cuts(
    file_a: str = "",
    file_b: str = "",
    timeline_a: str = "",
    timeline_b: str = "",
    fps: float = 0,
) -> dict:
    """Compare two versions of a cut (A vs B) metric by metric.

    Each side is either a local EDL/FCPXML file (``file_a``/``file_b``,
    absolute paths; ``fps`` required for EDL, e.g. 25) or a timeline in the
    running DaVinci Resolve (``timeline_a``/``timeline_b`` by name) — you
    can mix, e.g. an exported v4 file against the current Resolve timeline.
    Returns, per metric, the value in A, in B, and delta = B - A (a negative
    delta on avg_shot_seconds means B is cut faster), plus a plain-English
    verdict of how B differs from A. Durations are seconds.
    """
    stats_a, err = _one_cut(file_a, timeline_a, fps, "a")
    if err is not None:
        return err
    stats_b, err = _one_cut(file_b, timeline_b, fps, "b")
    if err is not None:
        return err
    result = analysis.compare(stats_a, stats_b)
    result["a"] = stats_a.timeline_name
    result["b"] = stats_b.timeline_name
    return result


@mcp_instance.tool()
def check_genre(
    genre: str, timeline: str = "", file: str = "", fps: float = 0
) -> dict:
    """Judge a cut's tempo against a genre's typical shot-length band.

    ``genre`` is one of: action, thriller, horror, comedy, drama, arthouse,
    documentary, musicvideo. Source options as in analyze_timeline: a
    Resolve timeline by name (empty = current) or a local EDL/FCPXML
    ``file`` (``fps`` required for EDL). Returns the genre's typical ASL
    band in seconds, whether the cut sits below/inside/above it, and a
    verdict sentence. These bands are orientation, not rules.
    """
    tl, err = _obtain_timeline(timeline, file, fps)
    if err is not None:
        return err
    stats = analysis.analyze_timeline(tl)
    try:
        result = references.compare_to_reference(stats, genre)
    except ValueError as exc:
        return {"error": str(exc), "genres": sorted(references.PROFILES)}
    result["timeline"] = tl.name
    return result


@mcp_instance.tool()
def mark_slow_sections(timeline: str = "", threshold: float = 1.5) -> dict:
    """Drop markers in DaVinci Resolve where the cut drags.

    Analyzes a Resolve timeline (``timeline`` by name, empty = current
    timeline), finds sections whose average shot length exceeds
    ``threshold`` times the cut's overall average (1.5 = the standard
    "slow" definition; raise it to flag only the worst offenders), and adds
    a red marker named "Monteur: slow section" at the start of each, with
    the section's ASL in the note. Returns how many markers were set and
    where. Requires Resolve running with scripting enabled.
    """
    try:
        bridge = connect()
        tl = bridge.read_timeline(timeline or None)
        stats = analysis.analyze_timeline(tl)
        slow = [
            s
            for s in stats.sections
            if stats.avg_shot_seconds > 0
            and s.avg_shot_length >= threshold * stats.avg_shot_seconds
        ]
        if not slow:
            return {
                "timeline": tl.name,
                "markers_added": 0,
                "message": (
                    f"no sections slower than {threshold:g}x the cut's average "
                    f"shot length ({stats.avg_shot_seconds:.1f}s) — nothing to mark"
                ),
            }
        markers = [
            Marker(
                frame=seconds_to_frames(s.start, tl.fps),
                name="Monteur: slow section",
                note=(
                    f"ASL {s.avg_shot_length:.1f}s vs cut average "
                    f"{stats.avg_shot_seconds:.1f}s"
                ),
                color="Red",
            )
            for s in slow
        ]
        added = bridge.add_markers(markers, timeline_name=timeline or None)
        return {
            "timeline": tl.name,
            "markers_added": added,
            "cut_avg_shot_seconds": _round(stats.avg_shot_seconds),
            "slow_sections": [
                {
                    "start": _round(s.start),
                    "end": _round(s.end),
                    "avg_shot_seconds": _round(s.avg_shot_length),
                }
                for s in slow
            ],
        }
    except MonteurResolveError as exc:
        return _resolve_error(exc)


# --- Footage & music ------------------------------------------------------------


@mcp_instance.tool()
def sift_footage(folder: str) -> dict:
    """Scan a folder of video clips: what's usable, what's not, and why.

    ``folder`` is an absolute path to a directory of video files on the
    user's machine. Monteur decodes each clip and classifies stretches as
    usable or problematic (too dark / blurry / shaky), then ranks the best
    moments for cutting. Returns per clip: usable percentage, the main
    problems, and how many good moments were found. Needs ffmpeg installed.
    Run this before create_montage to preview the material's quality.
    """
    try:
        from monteur.media import MonteurMediaError
        from monteur.sift import sift_directory
    except ImportError as exc:
        return {"error": f"media features unavailable: {exc}"}
    try:
        reports = sift_directory(folder)
    except MonteurMediaError as exc:
        return {"error": str(exc)}
    if not reports:
        return {"error": f"no video files found in {folder}"}
    return {
        "folder": folder,
        "clip_count": len(reports),
        "total_moments": sum(len(r.moments) for r in reports),
        "clips": [
            {
                "file": Path(r.path).name,
                "duration_seconds": _round(r.duration),
                "usable_percent": round(r.usable_ratio * 100),
                "good_moments": len(r.moments),
                "problems": r.notes,
            }
            for r in reports
        ],
    }


@mcp_instance.tool()
def analyze_song(file: str) -> dict:
    """Analyze a music track: tempo, beats, and energy sections.

    ``file`` is an absolute path to an audio file (mp3/wav/m4a/...).
    Returns the tempo estimate in BPM, the number of detected beats, the
    duration in seconds, and low/mid/high energy sections with start/end
    times — the map create_montage cuts against. Works best on music with a
    clear pulse; ambient or beatless tracks yield tempo 0. Needs ffmpeg.
    """
    try:
        from monteur.media import MonteurMediaError
        from monteur.music import analyze_music
    except ImportError as exc:
        return {"error": f"music analysis unavailable: {exc}"}
    try:
        music = analyze_music(file)
    except MonteurMediaError as exc:
        return {"error": str(exc)}
    return {
        "file": file,
        "duration_seconds": _round(music.duration),
        "tempo_bpm": _round(music.tempo, 1),
        "beat_count": len(music.beats),
        "sections": [
            {
                "start": _round(s.start),
                "end": _round(s.end),
                "label": s.label,
                "energy": _round(s.energy),
            }
            for s in music.sections
        ],
    }


@mcp_instance.tool()
def create_montage(
    folder: str,
    music: str,
    output: str = "",
    order: str = "chronological",
    fps: float = 25,
    max_duration: float = 0,
    style: str = "auto",
    into_resolve: bool = False,
    brief: str = "",
) -> dict:
    """Build a music-cut montage from a folder of footage — a first cut.

    Sifts every clip in ``folder`` (absolute path), analyzes the song at
    ``music`` (absolute path), and lays the best moments on the beat grid:
    calm sections cut slower, high-energy sections faster. ``order`` is
    "chronological" (keep footage order — travel/event films) or
    "best_first" (strongest material on the loudest music). ``style`` is
    one of "auto", "travel", "wedding", "music_video", "trailer".
    ``max_duration`` caps the montage length in seconds (0 = full song).
    ``brief`` is an optional natural-language brief (German or English,
    e.g. "90 Sekunden, energiegeladen"); it is interpreted with a rough
    offline keyword matcher and applied only where style/order/max_duration
    are still at their defaults. Prefer passing explicit style/order/
    max_duration values yourself — you can interpret the user's wish far
    better than the keyword matcher; ``brief`` is a convenience fallback.
    Destination — pick exactly one: ``into_resolve=True`` builds the
    timeline directly in the running DaVinci Resolve, or ``output`` saves
    it to an absolute .fcpxml/.edl path for import later. ``fps`` is the
    timeline frame rate. Returns the plan summary: number of cuts,
    duration, tempo, planner notes, and (when a brief was used) the
    interpretation rationale.
    """
    if not into_resolve and not output:
        return {
            "error": "no destination: pass into_resolve=true to build the "
            "timeline in DaVinci Resolve, or output=<absolute .fcpxml/.edl "
            "path> to save it to a file."
        }
    brief_rationale = ""
    if brief and style == "auto" and order == "chronological" and not max_duration:
        # Inside MCP the caller IS Claude — never spend an API round trip
        # here; the offline keyword interpreter is the only sensible option.
        from monteur.brief import resolve_brief

        settings = resolve_brief(brief, use_ai=False)
        style, order = settings.style, settings.order
        if settings.max_duration:
            max_duration = settings.max_duration
        brief_rationale = settings.rationale
    if order not in ("chronological", "best_first"):
        return {
            "error": f"unknown order {order!r} — use 'chronological' or 'best_first'"
        }
    try:
        from monteur.media import MonteurMediaError
        from monteur.montage import montage_to_timeline, plan_montage
        from monteur.music import analyze_music
        from monteur.sift import sift_directory
    except ImportError as exc:
        return {"error": f"montage features unavailable: {exc}"}
    try:
        reports = sift_directory(folder)
    except MonteurMediaError as exc:
        return {"error": str(exc)}
    if not reports:
        return {"error": f"no video files found in {folder}"}
    try:
        song = analyze_music(music)
    except MonteurMediaError as exc:
        return {"error": str(exc)}
    try:
        plan = plan_montage(
            reports, song, order=order, max_duration=max_duration or None,
            style=style,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    if not plan.entries:
        return {
            "error": "no usable material found in the footage — use "
            "sift_footage to see per-clip problems",
            "notes": plan.notes,
        }
    result = {
        "cuts": len(plan.entries),
        "duration_seconds": _round(plan.duration),
        "tempo_bpm": _round(song.tempo, 1),
        "clips_used": len({e.clip_path for e in plan.entries}),
        "order": order,
        "style": style,
        "notes": plan.notes,
    }
    if brief_rationale:
        result["brief_rationale"] = brief_rationale
    if into_resolve:
        try:
            bridge = connect()
            name = bridge.build_timeline_from_plan(plan, fps=fps)
        except MonteurResolveError as exc:
            return _resolve_error(exc)
        result["resolve_timeline"] = name
    else:
        timeline = montage_to_timeline(plan, fps=fps)
        try:
            io.save_timeline(timeline, output)
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}
        result["output"] = output
    return result


# --- Assembly -------------------------------------------------------------------


@mcp_instance.tool()
def build_assembly(
    script: str,
    takes_dir: str,
    output: str = "",
    fps: float = 25,
    max_takes: int = 1,
) -> dict:
    """Build a first cut of a scripted film from screenplay + take transcripts.

    ``script`` is an absolute path to a Fountain (or plain-text) screenplay;
    ``takes_dir`` is an absolute path to a directory of take transcripts
    (.srt or Whisper .json), named like the clips — names like S12_T03
    route a take to scene 12. Monteur matches each take's speech against
    the script's dialogue, scores takes (coverage, accuracy, fluffs), picks
    the best material per scene (up to ``max_takes`` takes per scene) and
    saves the cut to ``output`` (required; absolute .edl/.fcpxml path).
    Returns dialogue coverage and, per scene, the chosen takes and notes.
    The plan is a proposal — the editor reviews it in the NLE.
    """
    if not output:
        return {
            "error": "output is required: an absolute .edl/.fcpxml path to "
            "save the assembly to."
        }
    from monteur.assembly import TakeSource, assembly_to_timeline, plan_assembly
    from monteur.screenplay import parse_fountain
    from monteur.transcribe import scene_take_from_name

    try:
        screenplay = parse_fountain(Path(script).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"could not read screenplay {script}: {exc}"}

    takes_path = Path(takes_dir)
    if not takes_path.is_dir():
        return {"error": f"not a directory: {takes_dir}"}
    takes: list[TakeSource] = []
    skipped: list[str] = []
    for path in sorted(takes_path.glob("*")):
        if path.suffix.lower() not in (".srt", ".json"):
            continue
        try:
            transcript = io.load_transcript(path)
        except ValueError as exc:
            skipped.append(f"{path.name}: {exc}")
            continue
        scene_hint, take_hint = scene_take_from_name(path.name)
        takes.append(
            TakeSource(
                name=path.stem,
                transcript=transcript,
                scene_hint=scene_hint,
                take_hint=take_hint,
            )
        )
    if not takes:
        return {
            "error": f"no .srt/.json transcripts found in {takes_dir} — "
            "transcribe the takes first (e.g. 'monteur transcribe')."
        }

    plan = plan_assembly(screenplay, takes, max_takes_per_scene=max_takes)
    timeline = assembly_to_timeline(plan, takes, fps=fps)
    if not timeline.clips:
        return {
            "error": "nothing matched — check the scene numbers in the "
            "script and the take file names (e.g. S12_T03).",
            "scenes": [
                {"heading": s.heading, "notes": s.notes} for s in plan.scenes
            ],
        }
    try:
        io.save_timeline(timeline, output)
    except (ValueError, OSError) as exc:
        return {"error": str(exc)}
    return {
        "output": output,
        "coverage_percent": round(plan.coverage() * 100),
        "duration_seconds": _round(timeline.duration_seconds),
        "segments": len(timeline.track_clips("V1")),
        "takes_available": len(takes),
        "skipped_transcripts": skipped,
        "scenes": [
            {
                "heading": s.heading,
                "chosen_takes": list(
                    dict.fromkeys(seg.take for seg in s.segments)
                ),
                "notes": s.notes,
            }
            for s in plan.scenes
        ],
    }


# --- Version store --------------------------------------------------------------


@mcp_instance.tool()
def save_version(
    label: str = "", file: str = "", fps: float = 0, project_dir: str = "."
) -> dict:
    """Snapshot a cut's pacing stats into the project's version history.

    Analyzes either a local EDL/FCPXML ``file`` (absolute path; ``fps``
    required for EDL) or, when ``file`` is empty, the current timeline in
    the running DaVinci Resolve. ``label`` names the snapshot (e.g. "v5
    tighter act two"; defaults to the timeline name). ``project_dir`` is
    the project directory holding the .monteur/ store (absolute path
    recommended). Only derived statistics are stored, never media. Use
    list_versions to review history, and compare_cuts to A/B two files.
    """
    from datetime import datetime, timezone

    from monteur.project import Project

    tl, err = _obtain_timeline("", file, fps)
    if err is not None:
        return err
    stats = analysis.analyze_timeline(tl)
    try:
        entry = Project(project_dir).add_version(
            stats,
            label=label,
            source_file=file,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except (OSError, ValueError) as exc:
        return {"error": f"could not write version store in {project_dir}: {exc}"}
    return {
        "id": entry["id"],
        "label": entry["label"],
        "saved_at": entry["saved_at"],
        "timeline": stats.timeline_name,
        "duration_seconds": _round(stats.duration_seconds),
        "shot_count": stats.shot_count,
        "avg_shot_seconds": _round(stats.avg_shot_seconds),
    }


@mcp_instance.tool()
def list_versions(project_dir: str = ".") -> dict:
    """List the saved versions of a project's cut, oldest first.

    ``project_dir`` is the project directory containing the .monteur/
    version store (absolute path recommended). Each entry has an id, label,
    when it was saved, and its headline pacing numbers (duration in
    seconds, shot count, average shot length) — enough to see how the
    film's rhythm evolved across versions. Save new snapshots with
    save_version.
    """
    from monteur.project import Project

    try:
        versions = Project(project_dir).versions()
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "project_dir": project_dir,
        "count": len(versions),
        "versions": [
            {
                "id": v["id"],
                "label": v["label"],
                "saved_at": v["saved_at"],
                "source_file": v["source_file"],
                "duration_seconds": _round(v["duration_seconds"]),
                "shot_count": v["shot_count"],
                "avg_shot_seconds": _round(v["avg_shot_seconds"]),
            }
            for v in versions
        ],
    }


# --- entry point -----------------------------------------------------------------


def main() -> None:
    """Run the Monteur MCP server on stdio (for Claude Desktop / Claude Code)."""
    mcp_instance.run()


if __name__ == "__main__":
    main()
