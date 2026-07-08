"""Monteur publish kit — everything the upload needs, straight from the cut.

The cut is not the end of a production: before a video goes online you
still need a thumbnail, a title, a description with chapters and a tag
list. All of that is already latent in the montage plan — the hero
scores know which frames sell the video, the vision labels know what
each act shows, the record times know where the chapters sit. The kit
makes it explicit::

    monteur create footage/ song.mp3 --see -o cut.fcpxml --kit publish/

    publish/
      publish.md            # title ideas, description draft, chapters, tags
      thumbs/thumb_01_overtake-in-a-left-hand-curve.jpg
      thumbs/thumb_02_....jpg

Thumbnails
----------
Up to ``max_thumbs`` (default 6) candidates, ranked by the underlying
moment's hero score, then its sift score — but never two from the same
scene-similarity ``group`` while other scenes are available. Extracted
with ffmpeg from the middle of the entry's source window at full frame
height (JPEG q=2), so they are real stills from the material, not
timeline renders.

Chapters
--------
YouTube chapters from the entries themselves: a new chapter wherever the
scene group (or, without vision data, the source clip) changes, at least
``_CHAPTER_MIN_SECONDS`` apart so YouTube accepts them, always starting
at 00:00. Labels come from the vision labels; without them, from the
clip's filename.

Text
----
With the ``anthropic`` package and ``ANTHROPIC_API_KEY``, one Claude call
drafts title ideas, a description and tags from the labels, tags and the
brief (in the brief's language). Without them the kit falls back to an
honest offline template built from the same data and says so in a note —
the kit never fails the export.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePath

from monteur.montage import MontagePlan
from monteur.sift import ClipReport

# YouTube requires chapters >= 10s apart (and at least three of them).
_CHAPTER_MIN_SECONDS = 10.0
# Thumbnail candidates: enough to choose from, few enough to stay quick.
_DEFAULT_MAX_THUMBS = 6
# Full-HD frame height for thumbnail stills (JPEG quality 2 = visually lossless).
_THUMB_HEIGHT = 1080

_EPS = 1e-6


@dataclass
class Chapter:
    start: float  # seconds in the cut
    title: str


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _slug(text: str, max_len: int = 40) -> str:
    """Filename-safe slug of a label ("Overtake, left!" -> "overtake-left")."""
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len].rstrip("-") or "shot"


def _moment_for(report: ClipReport | None, source_start: float, source_end: float):
    """The report moment overlapping an entry's source window (best overlap)."""
    if report is None:
        return None
    best, best_overlap = None, 0.0
    for moment in report.moments:
        overlap = min(moment.end, source_end) - max(moment.start, source_start)
        if overlap > best_overlap + _EPS:
            best, best_overlap = moment, overlap
    return best


def plan_chapters(plan: MontagePlan, reports: list[ClipReport] | None = None) -> list[Chapter]:
    """YouTube chapters from the plan's entries (see the module docstring)."""
    by_path = {r.path: r for r in (reports or [])}
    chapters: list[Chapter] = []
    prev_key: str | None = None
    for entry in sorted(plan.entries, key=lambda e: e.record_start):
        moment = _moment_for(by_path.get(entry.clip_path), entry.source_start, entry.source_end)
        group = getattr(moment, "group", "") if moment else ""
        key = group or entry.clip_path
        title = entry.label or (moment.label if moment else "") or PurePath(entry.clip_path).stem
        if prev_key is None:
            chapters.append(Chapter(0.0, title))
        elif key != prev_key and entry.record_start - chapters[-1].start >= _CHAPTER_MIN_SECONDS:
            chapters.append(Chapter(entry.record_start, title))
        prev_key = key
    return chapters


def _thumbnail_candidates(
    plan: MontagePlan, reports: list[ClipReport] | None, max_thumbs: int
) -> list[tuple]:
    """(entry, moment) picks ranked hero-first, scene groups deduplicated."""
    by_path = {r.path: r for r in (reports or [])}
    ranked = []
    for entry in plan.entries:
        moment = _moment_for(by_path.get(entry.clip_path), entry.source_start, entry.source_end)
        hero = getattr(moment, "hero", 0.0) if moment else 0.0
        ranked.append((-(hero), -entry.score, entry.record_start, entry, moment))
    ranked.sort(key=lambda item: item[:3])
    picks: list[tuple] = []
    seen_groups: set[str] = set()
    deferred: list[tuple] = []
    for _, _, _, entry, moment in ranked:
        group = getattr(moment, "group", "") if moment else ""
        if group and group in seen_groups:
            deferred.append((entry, moment))
            continue
        seen_groups.add(group)
        picks.append((entry, moment))
        if len(picks) >= max_thumbs:
            return picks
    for item in deferred:  # fewer scenes than slots: fill up with repeats
        picks.append(item)
        if len(picks) >= max_thumbs:
            break
    return picks


def extract_thumbnails(
    plan: MontagePlan,
    reports: list[ClipReport] | None,
    out_dir: str | Path,
    max_thumbs: int = _DEFAULT_MAX_THUMBS,
) -> list[str]:
    """Write thumbnail JPEGs; returns kit-relative paths ("thumbs/...")."""
    from monteur.media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    thumb_dir = Path(out_dir) / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for i, (entry, moment) in enumerate(
        _thumbnail_candidates(plan, reports, max_thumbs), start=1
    ):
        middle = (entry.source_start + entry.source_end) / 2.0
        name = f"thumb_{i:02d}_{_slug(entry.label or PurePath(entry.clip_path).stem)}.jpg"
        target = thumb_dir / name
        cmd = [
            ffmpeg, "-v", "error", "-ss", f"{middle:.3f}", "-i", entry.clip_path,
            "-frames:v", "1", "-vf", f"scale=-2:{_THUMB_HEIGHT}", "-q:v", "2",
            "-y", str(target),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            continue  # a broken source loses its thumbnail, not the kit
        if target.exists() and target.stat().st_size > 0:
            hero = getattr(moment, "hero", 0.0) if moment else 0.0
            written.append(f"thumbs/{name}|{entry.label or ''}|{hero:.2f}")
    return written


def _collect_tags(plan: MontagePlan, reports: list[ClipReport] | None) -> list[str]:
    """Frequency-ranked vision tags across the moments the cut actually used."""
    by_path = {r.path: r for r in (reports or [])}
    counts: dict[str, int] = {}
    for entry in plan.entries:
        moment = _moment_for(by_path.get(entry.clip_path), entry.source_start, entry.source_end)
        for tag in getattr(moment, "tags", []) if moment else []:
            counts[tag] = counts.get(tag, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:12]


def _ai_copy(
    chapters: list[Chapter], tags: list[str], brief: str, duration: float
) -> str:
    """One Claude call drafting titles/description/tags; raises on failure."""
    from monteur.ai import _run

    chapter_lines = "\n".join(f"{_mmss(c.start)} {c.title}" for c in chapters)
    prompt = (
        "Draft the YouTube publish copy for a finished video cut.\n"
        f"Length: {duration:.0f} seconds.\n"
        f"Editor's brief: {brief or '(none given)'}\n"
        f"What the acts show (chapters):\n{chapter_lines or '(no chapter data)'}\n"
        f"Content tags: {', '.join(tags) or '(none)'}\n\n"
        "Write, in the language of the brief (English if none):\n"
        "## Title ideas\n(5 bullet points, each under 70 characters, no clickbait)\n"
        "## Description\n(2-4 sentences; do NOT include the chapter list — "
        "it is appended separately)\n"
        "## Tags\n(one comma-separated line, max 15 tags)\n"
        "Answer with exactly those three markdown sections and nothing else."
    )
    return _run(prompt, effort="medium")


def _template_copy(
    chapters: list[Chapter], tags: list[str], brief: str, duration: float
) -> str:
    """Offline fallback copy: honest, data-driven, no pretend creativity."""
    unique_titles: list[str] = []
    for chapter in chapters:
        if chapter.title not in unique_titles:
            unique_titles.append(chapter.title)
    subjects = tags[:3] or unique_titles[:2]
    headline = " · ".join(s.title() for s in subjects if s) or "A First Cut"
    lines = [
        "## Title ideas",
        f"- {headline} ({duration:.0f}s)",
        f"- {headline} — POV",
    ]
    if brief:
        lines.append(f"- {brief[:70]}")
    lines += [
        "",
        "## Description",
        brief or "A cut assembled with Monteur.",
        "",
        "## Tags",
        ", ".join(tags) if tags else "(run --see so Claude can tag the content)",
    ]
    return "\n".join(lines)


def publish_kit(
    plan: MontagePlan,
    reports: list[ClipReport] | None,
    out_dir: str | Path,
    brief: str = "",
    max_thumbs: int = _DEFAULT_MAX_THUMBS,
) -> list[str]:
    """Write the publish kit; returns notes about what was produced."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    chapters = plan_chapters(plan, reports)
    tags = _collect_tags(plan, reports)
    thumbs = extract_thumbnails(plan, reports, out, max_thumbs=max_thumbs)

    copy_source = "Claude"
    try:
        copy = _ai_copy(chapters, tags, brief, plan.duration)
    except Exception as exc:  # noqa: BLE001 - the kit must not fail the export
        copy = _template_copy(chapters, tags, brief, plan.duration)
        copy_source = "offline template"
        notes.append(
            f"copy drafted from the offline template ({exc}); with "
            "ANTHROPIC_API_KEY set, Claude drafts titles and description"
        )

    doc = ["# Publish kit", ""]
    doc.append(copy.strip())
    doc += ["", "## Chapters (paste below your description)", ""]
    if len(chapters) >= 3:
        doc += [f"{_mmss(c.start)} {c.title}" for c in chapters]
    elif chapters:
        doc += [f"{_mmss(c.start)} {c.title}" for c in chapters]
        doc.append("")
        doc.append(
            "_(YouTube shows chapters from 3 entries of 10s+ — this cut has "
            "fewer scene changes.)_"
        )
    else:
        doc.append("_(no entries — nothing to chapter)_")
    doc += ["", "## Thumbnail candidates", ""]
    if thumbs:
        for item in thumbs:
            path, label, hero = item.split("|")
            extra = f" — {label}" if label else ""
            extra += f" (hero {float(hero):.1f})" if float(hero) > 0 else ""
            doc.append(f"- `{path}`{extra}")
    else:
        doc.append("_(no thumbnails could be extracted)_")
    doc.append("")
    (out / "publish.md").write_text("\n".join(doc), encoding="utf-8")

    notes.insert(0, f"publish kit -> {out / 'publish.md'}")
    notes.insert(
        1,
        f"{len(thumbs)} thumbnail candidates, {len(chapters)} chapters, "
        f"copy by {copy_source}",
    )
    if not tags and not any(e.label for e in plan.entries):
        notes.append("tip: add --see so chapters, tags and thumbnails know the content")
    return notes
