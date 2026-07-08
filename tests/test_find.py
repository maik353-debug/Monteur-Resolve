"""Tests for the offline footage search (monteur/find.py).

The search reads only the vision cache sidecar (.monteur-vision.json), so
these tests build synthetic caches in vision.py's exact key/value format:
key = ``abspath|mtime|start-end (2dp)|model``, value = a
label/tags/role/hero/group dict. Dummy clip files back the keys so the
staleness check (missing file, changed mtime) can be exercised for real.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from monteur import find
from monteur.find import FoundShot, load_annotations, search_footage
from monteur.vision import CACHE_FILENAME, _moment_key

MODEL = "claude-test-model"


def _entry(label="", tags=(), role="", hero=0.0, group=""):
    return {
        "label": label,
        "tags": list(tags),
        "role": role,
        "hero": hero,
        "group": group,
    }


def _put(folder: Path, cache: dict, name: str, start: float, end: float, value: dict,
         mtime: float | None = None) -> str:
    """Add one cache entry in vision.py's exact key format; returns the key."""
    clip = folder / name
    if not clip.exists():
        clip.write_bytes(b"not really a video")
    key = (
        f"{os.path.abspath(clip)}|{mtime if mtime is not None else os.path.getmtime(clip)}"
        f"|{start:.2f}-{end:.2f}|{MODEL}"
    )
    cache[key] = value
    return key


def _write(folder: Path, cache: dict) -> None:
    (folder / CACHE_FILENAME).write_text(json.dumps(cache), encoding="utf-8")


# --- cache key format ---------------------------------------------------------


def test_key_format_matches_vision_exactly(tmp_path):
    """The synthetic keys these tests build ARE vision._moment_key's keys."""
    from monteur.sift import Moment

    clip = tmp_path / "ride.mp4"
    clip.write_bytes(b"clip")
    moment = Moment(start=1.234, end=5.678, score=0.9)
    real_key = _moment_key(str(clip), MODEL, moment)
    assert real_key == (
        f"{os.path.abspath(clip)}|{os.path.getmtime(clip)}|1.23-5.68|{MODEL}"
    )
    # ...and find.py's parser inverts it exactly.
    assert find._parse_key(real_key) == (
        os.path.abspath(clip), os.path.getmtime(clip), 1.23, 5.68,
    )


def test_unparseable_keys_are_skipped_not_fatal(tmp_path):
    cache = {}
    _put(tmp_path, cache, "a.mp4", 0.0, 2.0, _entry(label="curve", tags=["kurven"]))
    cache["garbage-without-pipes"] = _entry(label="ghost")
    cache["a|b|c|d"] = _entry(label="ghost")  # right shape, unparseable numbers
    cache["also|1.0|nodash|m"] = _entry(label="ghost")
    _write(tmp_path, cache)
    shots = load_annotations(tmp_path)
    assert [s.label for s in shots] == ["curve"]


# --- load_annotations -----------------------------------------------------------


def test_load_annotations_returns_everything_relevance_one(tmp_path):
    cache = {}
    _put(tmp_path, cache, "b.mp4", 4.0, 6.0, _entry(label="tunnel", hero=0.2))
    _put(tmp_path, cache, "a.mp4", 10.0, 12.5,
         _entry(label="sunset ridge", tags=["berge"], role="closer", hero=0.9,
                group="ridge"))
    _put(tmp_path, cache, "a.mp4", 1.0, 3.0, _entry(label="parking lot"))
    _write(tmp_path, cache)

    shots = load_annotations(tmp_path)
    assert len(shots) == 3
    assert all(isinstance(s, FoundShot) for s in shots)
    assert all(s.relevance == 1.0 for s in shots)
    # ordered by clip path, then start
    assert [(Path(s.clip_path).name, s.start) for s in shots] == [
        ("a.mp4", 1.0), ("a.mp4", 10.0), ("b.mp4", 4.0),
    ]
    ridge = shots[1]
    assert (ridge.end, ridge.label, ridge.tags, ridge.role, ridge.hero, ridge.group) \
        == (12.5, "sunset ridge", ["berge"], "closer", 0.9, "ridge")


def test_missing_cache_raises_with_monteur_see_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="monteur see"):
        load_annotations(tmp_path)
    with pytest.raises(FileNotFoundError, match="monteur see"):
        search_footage(tmp_path, "kurve")


# --- staleness ------------------------------------------------------------------


def test_stale_entries_are_skipped(tmp_path):
    cache = {}
    _put(tmp_path, cache, "fresh.mp4", 0.0, 2.0, _entry(label="fresh kurven"))
    # Clip that vanished since annotation (renamed/deleted).
    gone = _put(tmp_path, cache, "gone.mp4", 0.0, 2.0, _entry(label="ghost kurven"))
    (tmp_path / "gone.mp4").unlink()
    # Clip re-exported since annotation: same path, different mtime.
    retouched = tmp_path / "retouched.mp4"
    _put(tmp_path, cache, "retouched.mp4", 0.0, 2.0, _entry(label="stale kurven"))
    old = os.path.getmtime(retouched)
    os.utime(retouched, (old + 100, old + 100))
    _write(tmp_path, cache)

    labels = [s.label for s in load_annotations(tmp_path)]
    assert labels == ["fresh kurven"]
    hits = search_footage(tmp_path, "kurve")
    assert [s.label for s in hits] == ["fresh kurven"]
    assert gone in cache and retouched.exists()  # the entries were really there


# --- matching -------------------------------------------------------------------


def test_german_plural_prefix_matching_both_directions(tmp_path):
    cache = {}
    _put(tmp_path, cache, "a.mp4", 0.0, 2.0,
         _entry(label="overtake in a left-hand curve", tags=["kurven", "berge"]))
    _put(tmp_path, cache, "b.mp4", 0.0, 2.0, _entry(tags=["kurve"]))
    _put(tmp_path, cache, "c.mp4", 0.0, 2.0, _entry(tags=["tunnel"]))
    _write(tmp_path, cache)

    # singular query finds the plural tag...
    names = {Path(s.clip_path).name for s in search_footage(tmp_path, "kurve")}
    assert names == {"a.mp4", "b.mp4"}
    # ...and the plural query finds the singular tag.
    names = {Path(s.clip_path).name for s in search_footage(tmp_path, "kurven")}
    assert names == {"a.mp4", "b.mp4"}


def test_short_tokens_need_exact_match(tmp_path):
    cache = {}
    _put(tmp_path, cache, "a.mp4", 0.0, 2.0, _entry(label="ride in innsbruck"))
    _put(tmp_path, cache, "b.mp4", 0.0, 2.0, _entry(tags=["inn"]))
    _write(tmp_path, cache)
    # "in" (2 chars) must not prefix-match "innsbruck" or "inn"...
    names = {Path(s.clip_path).name for s in search_footage(tmp_path, "in")}
    assert names == {"a.mp4"}  # ...but its exact occurrence in the label counts
    # 3 chars is enough for prefixing again.
    names = {Path(s.clip_path).name for s in search_footage(tmp_path, "inn")}
    assert names == {"a.mp4", "b.mp4"}


def test_haystack_covers_label_tags_group_and_role(tmp_path):
    cache = {}
    _put(tmp_path, cache, "lab.mp4", 0.0, 2.0, _entry(label="waterfall in a gorge"))
    _put(tmp_path, cache, "tag.mp4", 0.0, 2.0, _entry(tags=["wasserfall"]))
    _put(tmp_path, cache, "grp.mp4", 0.0, 2.0, _entry(group="waterfall canyon"))
    _put(tmp_path, cache, "rol.mp4", 0.0, 2.0, _entry(role="opener"))
    _write(tmp_path, cache)
    assert len(search_footage(tmp_path, "waterfall")) == 2  # label + group
    assert len(search_footage(tmp_path, "wasserfall")) == 1  # tag
    assert [Path(s.clip_path).name for s in search_footage(tmp_path, "opener")] \
        == ["rol.mp4"]  # role


def test_relevance_fraction_and_no_hit_exclusion(tmp_path):
    cache = {}
    _put(tmp_path, cache, "both.mp4", 0.0, 2.0, _entry(tags=["kurven", "berge"]))
    _put(tmp_path, cache, "one.mp4", 0.0, 2.0, _entry(tags=["kurven", "tunnel"]))
    _put(tmp_path, cache, "none.mp4", 0.0, 2.0, _entry(tags=["strand"]))
    _write(tmp_path, cache)

    hits = search_footage(tmp_path, "Kurven Berge")  # query is lowercased
    assert [Path(s.clip_path).name for s in hits] == ["both.mp4", "one.mp4"]
    assert hits[0].relevance == pytest.approx(1.0)
    assert hits[1].relevance == pytest.approx(0.5)  # 1 of 2 tokens, hero 0


def test_hero_bonus_ranks_heroes_first_among_equals_capped_at_one(tmp_path):
    cache = {}
    _put(tmp_path, cache, "plain.mp4", 0.0, 2.0, _entry(tags=["kurven"], hero=0.0))
    _put(tmp_path, cache, "hero.mp4", 0.0, 2.0, _entry(tags=["kurven"], hero=1.0))
    _put(tmp_path, cache, "mid.mp4", 0.0, 2.0, _entry(tags=["kurven"], hero=0.5))
    _write(tmp_path, cache)

    hits = search_footage(tmp_path, "kurve")
    assert [Path(s.clip_path).name for s in hits] == [
        "hero.mp4", "mid.mp4", "plain.mp4",
    ]
    assert hits[0].relevance == pytest.approx(1.0)  # 1.0 + 0.2 capped at 1.0
    assert hits[1].relevance == pytest.approx(1.0)  # full match + 0.5 bonus, capped
    assert hits[2].relevance == pytest.approx(1.0)  # full match, no bonus
    # the cap is why hero (desc) is the tiebreaker — verify it on a partial match
    cache = {}
    _put(tmp_path, cache, "a.mp4", 0.0, 2.0, _entry(tags=["kurven"], hero=0.5))
    _write(tmp_path, cache)
    (hit,) = search_footage(tmp_path, "kurven tunnel")
    assert hit.relevance == pytest.approx(0.5 + 0.2 * 0.5)


def test_limit_caps_the_results(tmp_path):
    cache = {}
    for i in range(6):
        _put(tmp_path, cache, f"clip{i}.mp4", 0.0, 2.0,
             _entry(tags=["kurven"], hero=i / 10))
    _write(tmp_path, cache)
    hits = search_footage(tmp_path, "kurve", limit=3)
    assert len(hits) == 3
    # the best (highest hero) survive the cut
    assert [Path(s.clip_path).name for s in hits] == [
        "clip5.mp4", "clip4.mp4", "clip3.mp4",
    ]
    assert len(search_footage(tmp_path, "kurve")) == 6  # default limit is 20


# --- special queries --------------------------------------------------------------


@pytest.mark.parametrize("query", ["hero", "held", " Hero ", "HELD"])
def test_hero_query_returns_heroes_ranked_by_hero(tmp_path, query):
    cache = {}
    _put(tmp_path, cache, "meh.mp4", 0.0, 2.0, _entry(label="parking", hero=0.4))
    _put(tmp_path, cache, "good.mp4", 0.0, 2.0, _entry(label="ridge", hero=0.7))
    _put(tmp_path, cache, "best.mp4", 0.0, 2.0, _entry(label="drone dive", hero=1.0))
    _write(tmp_path, cache)
    hits = search_footage(tmp_path, query)
    assert [Path(s.clip_path).name for s in hits] == ["best.mp4", "good.mp4"]
    assert [s.relevance for s in hits] == [1.0, 0.7]  # ranked by hero


def test_hero_query_respects_limit(tmp_path):
    cache = {}
    for i in range(5):
        _put(tmp_path, cache, f"h{i}.mp4", 0.0, 2.0, _entry(hero=0.5 + i / 10))
    _write(tmp_path, cache)
    hits = search_footage(tmp_path, "hero", limit=2)
    assert [s.hero for s in hits] == [pytest.approx(0.9), pytest.approx(0.8)]


def test_empty_query_raises_value_error(tmp_path):
    _write(tmp_path, {})
    with pytest.raises(ValueError, match="give me something to look for"):
        search_footage(tmp_path, "")
    with pytest.raises(ValueError, match="give me something to look for"):
        search_footage(tmp_path, "   ")


def test_empty_cache_searches_to_nothing(tmp_path):
    _write(tmp_path, {})
    assert load_annotations(tmp_path) == []
    assert search_footage(tmp_path, "kurve") == []
