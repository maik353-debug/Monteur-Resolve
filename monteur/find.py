"""Search the footage by what Claude saw — offline, against the vision cache.

Once :func:`monteur.vision.analyze_reports` has annotated a shoot ("monteur
see <folder>" or the ``see_footage`` MCP tool), every analyzed moment lives in
the ``.monteur-vision.json`` sidecar next to the footage. This module answers
"zeig mir alle Kurven-Shots" from that sidecar alone: no API call, no re-sift,
no cost — just parsing the cache and matching words.

How honest the matching is (and is not):

* the query is tokenized into lowercase words; a shot's haystack is its
  label + tags + group + role, also as lowercase words;
* a query token matches a haystack word when either is a prefix of the other
  and the shorter of the two is at least 3 characters — so "kurve" finds
  "kurven" and "kurven" finds "kurve" (German plurals), while "in" does not
  light up every "innsbruck";
* relevance is the fraction of query tokens that matched, plus a small
  ``0.2 * hero`` bonus (capped at 1.0) so hero shots rank first among equals;
  shots without a single token hit are not returned at all;
* the special queries "hero" / "held" return every shot with hero >= 0.5,
  ranked by hero — "show me the money shots".

Staleness: each cache key embeds the clip's absolute path and mtime (see
:func:`monteur.vision._moment_key`). An entry whose clip no longer exists, or
whose recorded mtime no longer equals the file's current mtime (renamed,
re-exported, re-copied — the pixels may differ), is skipped so the search
never returns ghost hits.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from monteur.vision import CACHE_FILENAME, _clamped, _load_cache

#: A prefix match only counts when the shorter word has at least this many
#: characters — long enough for German stems ("kurve"/"kurven"), short enough
#: that "see" still finds "seealpen", but "in" never matches "innsbruck".
_MIN_PREFIX = 3

#: Hero threshold for the special "hero"/"held" query.
_HERO_FLOOR = 0.5

#: Hero bonus weight on top of the token-match fraction.
_HERO_BONUS = 0.2

# Words (including German umlauts/ß) — used for both query and haystack.
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass
class FoundShot:
    """One cached vision annotation, reconstructed as a searchable shot.

    ``start``/``end`` are file-relative seconds inside ``clip_path`` — ready
    for Resolve markers or a papercut. ``relevance`` is 0..1: how well the
    shot matched the query (1.0 for :func:`load_annotations`, which does not
    filter).
    """

    clip_path: str
    start: float
    end: float
    label: str
    tags: list[str]
    role: str
    hero: float
    group: str
    relevance: float
    #: True when this "shot" is a spoken line from a transcript sidecar
    #: (``<clip>.json``) rather than a vision annotation.
    spoken: bool = False


def _parse_key(key: str) -> tuple[str, float, float, float] | None:
    """Split a vision cache key into (abspath, mtime, start, end).

    The key format is ``abspath|mtime|start-end|model`` (2 decimals on the
    window, see :func:`monteur.vision._moment_key`). ``rsplit`` keeps a ``|``
    inside the path harmless. Returns None for anything unparseable — an
    unknown key must never abort the search.
    """
    parts = key.rsplit("|", 3)
    if len(parts) != 4:
        return None
    abspath, mtime_text, window, _model = parts
    start_text, sep, end_text = window.partition("-")
    if not sep:
        return None
    try:
        return abspath, float(mtime_text), float(start_text), float(end_text)
    except ValueError:
        return None


def _is_stale(abspath: str, mtime: float) -> bool:
    """True when the clip is gone or its mtime changed since annotation.

    The recorded mtime round-trips exactly through the key (Python float
    repr), so an unchanged file compares equal; a rename, re-export or
    re-copy does not — those annotations may describe different pixels.
    """
    try:
        return os.path.getmtime(abspath) != mtime
    except OSError:
        return True  # missing (or unreadable) clip


def load_annotations(folder: str | Path) -> list[FoundShot]:
    """Every cached vision annotation in ``folder``, relevance 1.0.

    Reads only the ``.monteur-vision.json`` sidecar — no API call, no sift.
    Entries for clips that no longer exist (or whose mtime changed) are
    skipped as stale. The list is ordered by clip path, then start time.

    Raises FileNotFoundError with a "run ``monteur see <folder>`` first"
    message when the cache sidecar does not exist yet.
    """
    folder = Path(folder)
    cache_path = folder / CACHE_FILENAME
    if not cache_path.is_file():
        raise FileNotFoundError(
            f"no vision cache at {cache_path} — run 'monteur see {folder}' "
            "first so Claude can look at the footage (analyzed once, "
            "searchable forever)."
        )
    shots: list[FoundShot] = []
    for key, raw in _load_cache(cache_path).items():
        parsed = _parse_key(str(key))
        if parsed is None:
            continue
        abspath, mtime, start, end = parsed
        if _is_stale(abspath, mtime):
            continue
        clean = _clamped(raw)  # same validation the annotator applies
        if clean is None:
            continue
        shots.append(
            FoundShot(
                clip_path=abspath,
                start=start,
                end=end,
                label=clean["label"],
                tags=clean["tags"],
                role=clean["role"],
                hero=clean["hero"],
                group=clean["group"],
                relevance=1.0,
            )
        )
    shots.sort(key=lambda s: (s.clip_path, s.start))
    return shots


def load_spoken(folder: str | Path) -> list[FoundShot]:
    """Every spoken line from ``<clip>.json`` transcript sidecars in ``folder``.

    ``monteur transcribe`` writes one ``<clip>.json`` (``{"segments": [...],
    "language": ...}``) next to each media file; each segment becomes a
    searchable spoken "shot" (label = the words said, ``spoken=True``). A folder
    with no transcripts returns ``[]`` — speech is an optional signal, never a
    gate. Segments of clips that no longer exist are skipped.
    """
    from monteur.media import MEDIA_EXTENSIONS

    folder = Path(folder)
    shots: list[FoundShot] = []
    try:
        media = [p for p in folder.iterdir() if p.suffix.lower() in MEDIA_EXTENSIONS]
    except OSError:
        return shots
    for clip in sorted(media):
        sidecar = clip.with_suffix(".json")
        if not sidecar.is_file():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        segments = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(segments, list):
            continue  # not a transcript sidecar
        abspath = str(clip.resolve())
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(seg.get("start") or 0.0)
                end = float(seg.get("end") or start)
            except (TypeError, ValueError):
                continue
            shots.append(FoundShot(
                clip_path=abspath, start=start, end=end, label=text,
                tags=[], role="", hero=0.0, group="", relevance=1.0, spoken=True,
            ))
    return shots


def _token_matches(token: str, word: str) -> bool:
    """Bidirectional prefix match; the shorter side needs >= _MIN_PREFIX chars."""
    if token == word:
        return True
    if len(token) >= _MIN_PREFIX and word.startswith(token):
        return True  # "kurve" matches "kurven"
    if len(word) >= _MIN_PREFIX and token.startswith(word):
        return True  # "kurven" matches "kurve"
    return False


def _haystack_words(shot: FoundShot) -> list[str]:
    """The shot's searchable vocabulary: label + tags + group + role."""
    text = " ".join([shot.label, *shot.tags, shot.group, shot.role])
    return _WORDS.findall(text.lower())


def search_footage(folder: str | Path, query: str, limit: int = 20) -> list[FoundShot]:
    """Find shots in ``folder``'s vision cache matching ``query`` — offline.

    Tokenizes the query (lowercase) and matches each token against the
    shots' label/tags/group/role words with bidirectional prefix matching
    (min length 3), so German plurals work: "kurve" finds "kurven".
    Relevance = fraction of query tokens found, plus ``0.2 * hero`` (capped
    at 1.0) so hero shots rank first among equals; only shots with at least
    one token hit are returned, best first (relevance desc, then hero desc),
    at most ``limit``.

    Searches BOTH signals: the vision cache AND spoken lines from ``<clip>.json``
    transcript sidecars (``monteur transcribe``) — so a word that was *said*
    ("die Kurve") is found even without a vision label, and spoken hits are
    flagged ``spoken=True``. Either signal alone is enough.

    The special queries "hero" / "held" (alone) return all shots with
    hero >= 0.5, ranked by hero (vision only — spoken lines carry no hero).
    An empty query raises ValueError; a folder with neither a vision cache nor
    transcripts raises FileNotFoundError telling the user to run ``monteur see``.
    """
    tokens = _WORDS.findall(query.lower())
    if not tokens:
        raise ValueError("give me something to look for")
    # vision annotations (may be absent) + spoken lines from transcripts (also
    # optional) — either signal alone is enough to search.
    try:
        vision = load_annotations(folder)
    except FileNotFoundError as exc:
        vision = None
        vision_error = exc
    spoken = load_spoken(folder)
    if vision is None and not spoken:
        raise vision_error  # nothing indexed at all -> the "run see/transcribe" hint
    shots = (vision or []) + spoken

    if query.strip().lower() in ("hero", "held"):
        heroes = [s for s in shots if s.hero >= _HERO_FLOOR]
        for shot in heroes:
            shot.relevance = shot.hero
        heroes.sort(key=lambda s: (-s.hero, s.clip_path, s.start))
        return heroes[:limit]

    found: list[FoundShot] = []
    for shot in shots:
        words = _haystack_words(shot)
        hits = sum(
            1 for token in tokens if any(_token_matches(token, w) for w in words)
        )
        if hits == 0:
            continue
        shot.relevance = min(1.0, hits / len(tokens) + _HERO_BONUS * shot.hero)
        found.append(shot)
    found.sort(key=lambda s: (-s.relevance, -s.hero, s.clip_path, s.start))
    return found[:limit]
