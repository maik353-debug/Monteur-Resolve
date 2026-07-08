"""Reference pacing profiles.

Rough genre baselines for average shot length (ASL), so an editor can ask
"is my thriller cut like a thriller?". Values are approximations distilled
from film-scholarship shot-length data (Barry Salt's statistical style
analysis and the Cinemetrics corpus): contemporary mainstream features
average roughly 2.5–6 s ASL depending on genre, with a broad drift toward
faster cutting since the 1980s. Treat these as orientation, not rules —
plenty of great films sit far outside their genre's band.
"""

from __future__ import annotations

from dataclasses import dataclass

from monteur.analysis import PacingStats


@dataclass(frozen=True)
class ReferenceProfile:
    key: str
    name: str
    asl_low: float  # seconds, typical band for contemporary features
    asl_high: float
    rhythm: str  # editor-facing description of the typical rhythm


PROFILES: dict[str, ReferenceProfile] = {
    p.key: p
    for p in (
        ReferenceProfile(
            "action", "Action / Blockbuster", 2.0, 3.5,
            "very fast, high shot-length variance: rapid set pieces against "
            "short breathers",
        ),
        ReferenceProfile(
            "thriller", "Thriller / Crime", 3.0, 5.0,
            "controlled tension: mid-length coverage that tightens sharply "
            "in suspense peaks",
        ),
        ReferenceProfile(
            "horror", "Horror", 3.0, 5.5,
            "long unsettling holds broken by fast shock clusters — variance "
            "is the point",
        ),
        ReferenceProfile(
            "comedy", "Comedy", 3.5, 5.5,
            "steady conversational tempo; cuts land on the joke, not before",
        ),
        ReferenceProfile(
            "drama", "Drama", 4.0, 7.0,
            "room to breathe: performance-led scenes with longer takes and "
            "an even pulse",
        ),
        ReferenceProfile(
            "arthouse", "Arthouse / Slow Cinema", 8.0, 30.0,
            "long-take language; rhythm comes from movement inside the "
            "frame, not from cutting",
        ),
        ReferenceProfile(
            "documentary", "Documentary", 4.0, 8.0,
            "interview-and-b-roll cadence; tempo follows speech rhythm",
        ),
        ReferenceProfile(
            "musicvideo", "Music Video / Commercial", 1.0, 2.5,
            "beat-driven cutting, often under 2 seconds per shot",
        ),
    )
}


def compare_to_reference(stats: PacingStats, genre: str) -> dict:
    """Judge a cut against a genre band.

    Returns {"profile", "asl", "position" ("below"|"inside"|"above"),
    "verdict"} where verdict is a plain-language sentence.
    """
    profile = PROFILES.get(genre.lower())
    if profile is None:
        options = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown reference profile {genre!r} — pick one of: {options}")
    asl = stats.avg_shot_seconds
    if asl < profile.asl_low:
        position = "below"
        verdict = (
            f"Your ASL of {asl:.1f}s cuts faster than the typical "
            f"{profile.name} band ({profile.asl_low:g}–{profile.asl_high:g}s). "
            f"That can read as urgency — or as not letting moments land."
        )
    elif asl > profile.asl_high:
        position = "above"
        verdict = (
            f"Your ASL of {asl:.1f}s is slower than the typical "
            f"{profile.name} band ({profile.asl_low:g}–{profile.asl_high:g}s). "
            f"Deliberate patience, or scenes overstaying — worth a pass."
        )
    else:
        position = "inside"
        verdict = (
            f"Your ASL of {asl:.1f}s sits inside the typical {profile.name} "
            f"band ({profile.asl_low:g}–{profile.asl_high:g}s)."
        )
    return {
        "profile": profile.name,
        "rhythm": profile.rhythm,
        "asl": asl,
        "band": [profile.asl_low, profile.asl_high],
        "position": position,
        "verdict": verdict,
    }
