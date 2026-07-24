"""Tests for monteur.elements: offline SFX classification & plan placement."""

from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import monteur.elements as elements_mod
from monteur.elements import (
    CACHE_FILENAME,
    SoundElement,
    assign_elements,
    carry_element_files,
    classify_features,
    scan_elements,
)
from monteur.media import MonteurMediaError
from monteur.montage import MontagePlan, SfxCue
from monteur.music import MusicAnalysis

RATE = 22050


# --- synthetic WAV generation ------------------------------------------------------


def write_wav(path: Path, samples, rate: int = RATE) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _noise(duration: float, seed: int = 7):
    rng = np.random.default_rng(seed)
    return rng.uniform(-1.0, 1.0, int(duration * RATE))


def impact_samples(duration: float = 0.8, decay: float = 8.0, lead: float = 0.0):
    """A decaying noise burst — the classic hit (optional quiet lead-in)."""
    t = np.linspace(0.0, duration, int(duration * RATE), endpoint=False)
    burst = _noise(duration) * np.exp(-t * decay)
    if lead > 0:
        head = _noise(lead, seed=9) * 0.03
        burst = np.concatenate([head, burst])
    return burst * 0.9


def riser_samples(duration: float = 3.0):
    """Noise rising through the file, ending loud (peak at the very end)."""
    t = np.linspace(0.0, 1.0, int(duration * RATE), endpoint=False)
    return _noise(duration) * (t**2) * 0.9


def whoosh_samples(duration: float = 1.0):
    """An arched noise swell: quiet edges, peak mid-file."""
    n = int(duration * RATE)
    return _noise(duration) * np.hanning(n) * 0.9


def braam_samples(duration: float = 3.0):
    """A long low sine hit: early peak, slow decay, low-band dominant."""
    t = np.linspace(0.0, duration, int(duration * RATE), endpoint=False)
    return np.sin(2 * np.pi * 55.0 * t) * np.exp(-t * 1.2) * 0.9


def drone_samples(duration: float = 5.0):
    """A constant 440 Hz tone: too long, no shape — unclassifiable."""
    t = np.linspace(0.0, duration, int(duration * RATE), endpoint=False)
    return np.sin(2 * np.pi * 440.0 * t) * 0.5


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    folder = tmp_path / "sfx"
    folder.mkdir()
    write_wav(folder / "hit.wav", impact_samples())
    write_wav(folder / "rise.wav", riser_samples())
    write_wav(folder / "swoosh.wav", whoosh_samples())
    write_wav(folder / "boom.wav", braam_samples())
    write_wav(folder / "drone.wav", drone_samples())
    return folder


# --- scanning & classification -----------------------------------------------------


class TestScanAndClassify:
    def test_classifies_the_synthetic_library(self, library: Path) -> None:
        elements = scan_elements(library)
        by_name = {Path(e.path).name: e for e in elements}
        assert set(by_name) == {
            "hit.wav", "rise.wav", "swoosh.wav", "boom.wav", "drone.wav",
        }
        assert by_name["hit.wav"].kind == "impact"
        assert by_name["rise.wav"].kind == "riser"
        assert by_name["swoosh.wav"].kind == "whoosh"
        assert by_name["boom.wav"].kind == "braam"
        assert by_name["drone.wav"].kind == "other"
        # confidences: classified kinds carry a positive score, "other" none
        for name in ("hit.wav", "rise.wav", "swoosh.wav", "boom.wav"):
            assert 0.0 < by_name[name].confidence <= 1.0
        assert by_name["drone.wav"].confidence == 0.0
        # durations are honest (decoded length)
        assert by_name["hit.wav"].duration == pytest.approx(0.8, abs=0.05)
        assert by_name["rise.wav"].duration == pytest.approx(3.0, abs=0.05)
        # measured features ride along
        f = by_name["rise.wav"].features
        assert f["peak_pos"] >= 0.8
        assert f["rise_ratio"] >= 1.5
        assert by_name["hit.wav"].features["peak_pos"] <= 0.15
        assert by_name["boom.wav"].features["low_ratio"] >= 0.5

    def test_confidence_ordering_textbook_beats_marginal(self, tmp_path: Path) -> None:
        folder = tmp_path / "sfx"
        folder.mkdir()
        write_wav(folder / "clean.wav", impact_samples(duration=0.8, decay=10.0))
        # same hit with a quiet lead-in: the peak sits later (~11%), still
        # an impact but less textbook
        write_wav(folder / "late.wav", impact_samples(duration=0.8, lead=0.1))
        by_name = {Path(e.path).name: e for e in scan_elements(folder)}
        assert by_name["clean.wav"].kind == "impact"
        assert by_name["late.wav"].kind == "impact"
        assert by_name["clean.wav"].confidence > by_name["late.wav"].confidence

    def test_classify_features_is_pure_and_bounded(self) -> None:
        kind, conf = classify_features(
            0.8, {"peak_pos": 0.02, "decay_score": 0.95}
        )
        assert kind == "impact" and 0.0 < conf <= 1.0
        kind, conf = classify_features(
            2.5, {"peak_pos": 0.95, "rise_ratio": 5.0}
        )
        assert kind == "riser" and conf > 0.5
        kind, conf = classify_features(
            1.0, {"peak_pos": 0.5, "edge_ratio": 0.1}
        )
        assert kind == "whoosh" and conf > 0.5
        kind, conf = classify_features(
            3.0, {"peak_pos": 0.1, "low_ratio": 0.9}
        )
        assert kind == "braam" and conf > 0.5
        assert classify_features(10.0, {"peak_pos": 0.5}) == ("other", 0.0)

    def test_missing_folder_raises_clean_error(self, tmp_path: Path) -> None:
        with pytest.raises(MonteurMediaError, match="not a directory"):
            scan_elements(tmp_path / "nope")

    def test_empty_folder_returns_empty(self, tmp_path: Path) -> None:
        folder = tmp_path / "empty"
        folder.mkdir()
        assert scan_elements(folder) == []
        assert not (folder / CACHE_FILENAME).exists()

    def test_undecodable_file_is_listed_as_other(self, library: Path) -> None:
        (library / "broken.wav").write_bytes(b"RIFFnope")
        by_name = {Path(e.path).name: e for e in scan_elements(library)}
        broken = by_name["broken.wav"]
        assert broken.kind == "other"
        assert broken.confidence == 0.0
        assert broken.duration == 0.0


class TestCache:
    def test_cache_round_trip_skips_reanalysis(self, library: Path, monkeypatch) -> None:
        first = scan_elements(library)
        cache_file = library / CACHE_FILENAME
        assert cache_file.exists()

        calls: list[str] = []
        real_analyze = elements_mod._analyze

        def counting_analyze(path, rate=22050):
            calls.append(str(path))
            return real_analyze(path, rate)

        monkeypatch.setattr(elements_mod, "_analyze", counting_analyze)
        second = scan_elements(library)
        assert calls == []  # every file served from the cache
        assert [(e.path, e.kind, e.confidence, e.duration) for e in first] == [
            (e.path, e.kind, e.confidence, e.duration) for e in second
        ]

    def test_stale_mtime_entry_is_skipped(self, library: Path, monkeypatch) -> None:
        scan_elements(library)
        hit = library / "hit.wav"
        stat = hit.stat()
        os.utime(hit, (stat.st_atime, stat.st_mtime + 100))  # re-exported file

        calls: list[str] = []
        real_analyze = elements_mod._analyze

        def counting_analyze(path, rate=22050):
            calls.append(Path(path).name)
            return real_analyze(path, rate)

        monkeypatch.setattr(elements_mod, "_analyze", counting_analyze)
        result = scan_elements(library)
        assert calls == ["hit.wav"]  # ONLY the touched file re-analyzes
        assert {Path(e.path).name: e.kind for e in result}["hit.wav"] == "impact"

    def test_corrupt_cache_is_tolerated(self, library: Path) -> None:
        (library / CACHE_FILENAME).write_text("{not json", encoding="utf-8")
        elements = scan_elements(library)
        assert len(elements) == 5
        # and the cache heals itself into valid JSON
        data = json.loads((library / CACHE_FILENAME).read_text(encoding="utf-8"))
        assert len(data) == 5

    def test_malformed_cache_entry_reanalyzes(self, library: Path) -> None:
        scan_elements(library)
        cache_file = library / CACHE_FILENAME
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        key = next(k for k in data if k.split("|")[0].endswith("hit.wav"))
        data[key] = {"kind": "not-a-kind"}  # unusable entry
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        by_name = {Path(e.path).name: e for e in scan_elements(library)}
        assert by_name["hit.wav"].kind == "impact"


# --- assignment ---------------------------------------------------------------------


def element(path: str, kind: str, duration: float, confidence: float = 0.8):
    return SoundElement(
        path=path, duration=duration, kind=kind, confidence=confidence
    )


def make_music(duration: float = 40.0, drops=(20.0,)) -> MusicAnalysis:
    return MusicAnalysis(
        path="/music/song.wav", duration=duration, tempo=120.0,
        drops=list(drops),
    )


def make_plan(style: str = "trailer", duration: float = 40.0) -> MontagePlan:
    plan = MontagePlan(music_path="/music/song.wav", duration=duration)
    plan.notes.append(f'style "{style}": whatever')
    return plan


class TestAssignElements:
    def test_riser_ends_exactly_on_the_drop(self) -> None:
        plan = make_plan()
        pool = [element("/sfx/rise.wav", "riser", 3.0)]
        notes = assign_elements(plan, make_music(), pool)
        risers = [c for c in plan.sfx if c.kind == "riser" and c.file]
        assert len(risers) == 1
        cue = risers[0]
        assert cue.file == "/sfx/rise.wav"
        assert cue.time + cue.duration == pytest.approx(20.0)  # ends ON the drop
        assert cue.time == pytest.approx(17.0)  # start = drop - file length
        assert any("ends on the drop" in n for n in notes)

    def test_riser_prefers_the_length_fitting_the_run_up(self) -> None:
        plan = make_plan(duration=10.0)
        music = make_music(duration=10.0, drops=(6.0,))
        pool = [
            element("/sfx/rise_short.wav", "riser", 2.0),
            element("/sfx/rise_long.wav", "riser", 5.5),
            element("/sfx/rise_huge.wav", "riser", 30.0),
        ]
        assign_elements(plan, music, pool)
        cue = next(c for c in plan.sfx if c.kind == "riser" and c.file)
        # the 5.5s riser is closest to the 6s run-up to the drop
        assert cue.file == "/sfx/rise_long.wav"
        assert cue.time + cue.duration == pytest.approx(6.0)

    def test_riser_never_butchered_below_its_own_length(self) -> None:
        # The field bug: a 9.5s riser trimmed to 0.2s "kills the whole idea
        # of a riser". A riser must play >= max(2s, 70% of its file) or it
        # is NOT placed there — skipped with an honest note instead.
        plan = make_plan(duration=10.0)
        music = make_music(duration=10.0, drops=(2.0,))
        pool = [element("/sfx/rise.wav", "riser", 9.5)]
        notes = assign_elements(plan, music, pool)
        assert not any(c.kind == "riser" and c.file for c in plan.sfx)
        skip = next(n for n in notes if n.startswith("riser skipped"))
        assert "2.0s" in skip and "70%" in skip and "9.5s" in skip

    def test_riser_prefers_a_shorter_file_over_butchering(self) -> None:
        # With a short riser in the library the ramp still gets its riser —
        # the shorter file plays WHOLE instead of the long one fragmented.
        plan = make_plan(duration=10.0)
        music = make_music(duration=10.0, drops=(3.0,))
        pool = [
            element("/sfx/rise_long.wav", "riser", 9.5, confidence=0.99),
            element("/sfx/rise_short.wav", "riser", 2.5, confidence=0.4),
        ]
        assign_elements(plan, music, pool)
        cue = next(c for c in plan.sfx if c.kind == "riser" and c.file)
        assert cue.file == "/sfx/rise_short.wav"
        assert cue.duration == pytest.approx(2.5)  # one contiguous, full play
        assert cue.time + cue.duration == pytest.approx(3.0)  # ends ON the drop

    def test_riser_trim_within_thirty_percent_is_allowed(self) -> None:
        # A 6s riser into a 5s run-up plays 5s (83% of the file): a light
        # trim keeps the build; the honest trim note stays.
        plan = make_plan(duration=12.0)
        music = make_music(duration=12.0, drops=(5.0,))
        pool = [element("/sfx/rise.wav", "riser", 6.0)]
        notes = assign_elements(plan, music, pool)
        cue = next(c for c in plan.sfx if c.kind == "riser" and c.file)
        assert cue.time == pytest.approx(0.0)
        assert cue.duration == pytest.approx(5.0)  # still ends on the drop
        assert any("trimmed" in n for n in notes)

    def test_existing_drop_cues_get_the_files(self) -> None:
        plan = make_plan()
        plan.sfx = [
            SfxCue(18.0, 2.0, "riser", "riser build up", "build -> climax"),
            SfxCue(20.0, 1.0, "impact", "cinematic impact hit", "climax start"),
        ]
        pool = [
            element("/sfx/rise.wav", "riser", 2.5),
            element("/sfx/hit.wav", "impact", 0.8),
        ]
        assign_elements(plan, make_music(), pool)
        assert len(plan.sfx) == 2  # the existing cues were reused, none added
        riser = next(c for c in plan.sfx if c.kind == "riser")
        impact = next(c for c in plan.sfx if c.kind == "impact")
        assert riser.file == "/sfx/rise.wav"
        assert riser.time + riser.duration == pytest.approx(20.0)
        assert riser.duration == pytest.approx(2.5)  # the file's own length
        assert impact.file == "/sfx/hit.wav"
        assert impact.time == pytest.approx(20.0)  # ON the drop
        assert impact.duration == pytest.approx(0.8)

    def test_impact_added_on_the_drop_without_a_cue(self) -> None:
        plan = make_plan()
        pool = [element("/sfx/hit.wav", "impact", 1.0)]
        assign_elements(plan, make_music(), pool)
        impacts = [c for c in plan.sfx if c.kind == "impact" and c.file]
        assert [c.time for c in impacts] == pytest.approx([20.0])

    def test_impact_after_each_dip(self) -> None:
        plan = make_plan()
        plan.dips = [(10.0, 0.4), (30.0, 0.4)]
        pool = [
            element("/sfx/hit_a.wav", "impact", 0.8),
            element("/sfx/hit_b.wav", "impact", 0.8),
        ]
        notes = assign_elements(plan, MusicAnalysis("/m.wav", 40.0, 120.0), pool)
        impacts = sorted(c.time for c in plan.sfx if c.kind == "impact" and c.file)
        # right AFTER each dip: the hit lands when the picture returns
        assert impacts == pytest.approx([10.4, 30.4])
        assert sum("hit out of the black" in n for n in notes) == 2

    def test_drop_positions_respect_music_start(self) -> None:
        plan = make_plan(duration=30.0)
        plan.music_start = 15.0  # the cut uses the song's strongest window
        music = make_music(duration=60.0, drops=(25.0,))
        pool = [element("/sfx/hit.wav", "impact", 0.8)]
        assign_elements(plan, music, pool)
        impact = next(c for c in plan.sfx if c.kind == "impact" and c.file)
        assert impact.time == pytest.approx(10.0)  # 25 - 15 in montage time

    def test_sub_drop_cue_takes_a_braam(self) -> None:
        plan = make_plan()
        plan.sfx = [SfxCue(30.0, 0.4, "sub-drop", "sub drop boom", "title slot")]
        pool = [
            element("/sfx/boom.wav", "braam", 3.0),
            element("/sfx/hit.wav", "impact", 0.8),
        ]
        assign_elements(plan, MusicAnalysis("/m.wav", 40.0, 120.0), pool)
        sub = plan.sfx[0]
        assert sub.kind == "sub-drop"
        assert sub.file == "/sfx/boom.wav"  # kind match: sub-drop -> braam
        assert sub.duration == pytest.approx(3.0)

    def test_ambience_cue_stays_a_marker(self) -> None:
        plan = make_plan()
        plan.sfx = [SfxCue(0.0, 6.0, "ambience", "outdoor ambience", "opening")]
        pool = [element("/sfx/hit.wav", "impact", 0.8)]
        notes = assign_elements(plan, MusicAnalysis("/m.wav", 40.0, 120.0), pool)
        assert plan.sfx[0].file == ""
        assert any("stay search-query markers" in n for n in notes)

    def test_duration_fit_picks_the_closest_file(self) -> None:
        plan = make_plan()
        plan.sfx = [SfxCue(20.0, 1.0, "impact", "q", "climax start")]
        pool = [
            element("/sfx/long_hit.wav", "impact", 1.9, confidence=0.99),
            element("/sfx/tight_hit.wav", "impact", 0.9, confidence=0.5),
        ]
        assign_elements(plan, make_music(), pool)
        assert plan.sfx[0].file == "/sfx/tight_hit.wav"  # fit beats confidence

    def test_trim_note_when_the_file_outruns_the_montage(self) -> None:
        plan = make_plan()
        plan.sfx = [SfxCue(39.5, 0.4, "sub-drop", "q", "title slot")]
        pool = [element("/sfx/boom.wav", "braam", 3.0)]
        notes = assign_elements(plan, MusicAnalysis("/m.wav", 40.0, 120.0), pool)
        assert plan.sfx[0].duration == pytest.approx(0.5)  # clamped to the end
        assert any("trimmed" in n for n in notes)

    def test_no_two_same_kind_elements_overlap(self) -> None:
        plan = make_plan()
        plan.sfx = [
            SfxCue(9.7, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(10.0, 0.6, "whoosh", "q", "fast cut"),
        ]
        pool = [
            element("/sfx/w1.wav", "whoosh", 0.8),
            element("/sfx/w2.wav", "whoosh", 0.8),
        ]
        assign_elements(plan, MusicAnalysis("/m.wav", 40.0, 120.0), pool)
        filed = [c for c in plan.sfx if c.file]
        assert len(filed) == 1  # the second would overlap the first's span

    def test_no_file_reuse_within_the_kind_gap(self) -> None:
        plan = make_plan()
        plan.sfx = [
            SfxCue(5.0, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(7.5, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(25.0, 0.6, "whoosh", "q", "fast cut"),
        ]
        pool = [element("/sfx/w.wav", "whoosh", 0.6)]
        assign_elements(plan, MusicAnalysis("/m.wav", 40.0, 120.0), pool)
        filed_times = [c.time for c in plan.sfx if c.file]
        # 5.0 files; 7.5 is the same file within the 4s whoosh gap; 25.0 is far
        assert filed_times == pytest.approx([5.0, 25.0])

    def test_travel_density_is_sparse(self) -> None:
        plan = make_plan(style="travel")
        plan.sfx = [
            SfxCue(4.7, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(9.7, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(17.7, 0.6, "whoosh", "q", "fast cut"),  # stutter zone: 20-4..20
        ]
        pool = [
            element("/sfx/w1.wav", "whoosh", 0.6),
            element("/sfx/w2.wav", "whoosh", 0.6),
            element("/sfx/w3.wav", "whoosh", 0.6),
        ]
        assign_elements(plan, make_music(), pool)
        filed = [c for c in plan.sfx if c.kind == "whoosh" and c.file]
        # travel stays sparse (budget 2) — the two clean cuts file, the one in
        # the pre-drop stutter zone (16-20s) never does
        assert sorted(round(c.time, 1) for c in filed) == [4.7, 9.7]
        assert all(c.time < 16.0 for c in filed)  # the stutter zone stays clean

    def test_wedding_density_is_minimal(self) -> None:
        plan = make_plan(style="wedding")
        plan.dips = [(24.0, 0.4)]
        plan.sfx = [
            # opening (quiet: first 15% = 0..6s) and outro (34..40s) cues
            SfxCue(3.0, 1.0, "impact", "q", "too early"),
            SfxCue(36.0, 1.0, "impact", "q", "too late"),
            SfxCue(19.7, 0.6, "whoosh", "q", "fast cut"),
        ]
        pool = [
            element("/sfx/hit.wav", "impact", 0.8),
            element("/sfx/hit2.wav", "impact", 0.8),
            element("/sfx/w.wav", "whoosh", 0.6),
        ]
        assign_elements(plan, make_music(), pool)
        filed = [(c.kind, round(c.time, 1)) for c in plan.sfx if c.file]
        # only the drop impact lands: no whooshes, no dip impact, quiet
        # opening/outro untouched
        assert filed == [("impact", 20.0)]

    def test_trailer_runs_the_full_program(self) -> None:
        plan = make_plan(style="trailer")
        plan.dips = [(32.0, 0.4)]
        plan.sfx = [
            SfxCue(18.0, 2.0, "riser", "q", "build -> climax"),
            SfxCue(20.0, 1.0, "impact", "q", "climax start"),
            SfxCue(32.0, 0.4, "sub-drop", "q", "title slot"),
            SfxCue(25.7, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(27.7, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(29.7, 0.6, "whoosh", "q", "fast cut"),
        ]
        pool = [
            element("/sfx/rise.wav", "riser", 2.0),
            element("/sfx/hit.wav", "impact", 0.8),
            element("/sfx/hit2.wav", "impact", 0.8),
            element("/sfx/boom.wav", "braam", 1.5),
            element("/sfx/w1.wav", "whoosh", 0.6),
            element("/sfx/w2.wav", "whoosh", 0.6),
            element("/sfx/w3.wav", "whoosh", 0.6),
        ]
        notes = assign_elements(plan, make_music(), pool)
        kinds = sorted(
            (c.kind, round(c.time, 1)) for c in plan.sfx if c.file
        )
        assert ("riser", 18.0) in kinds
        assert ("impact", 20.0) in kinds
        assert ("impact", 32.4) in kinds  # the smash-cut hit out of the dip
        assert ("sub-drop", 32.0) in kinds
        assert sum(1 for k, _ in kinds if k == "whoosh") == 3  # full budget
        assert notes[0].startswith("sound elements:")
        assert "trailer density" in notes[0]

    def test_one_riser_per_tension_ramp_not_per_boundary(self) -> None:
        # Three act-change riser cues, but only ONE ramp (the run-up into
        # the drop) gets a real file — the other cues stay honest markers.
        plan = make_plan()
        plan.sfx = [
            SfxCue(6.0, 2.0, "riser", "riser build up", "opening -> build"),
            SfxCue(18.0, 2.0, "riser", "riser build up", "build -> climax"),
            SfxCue(30.0, 2.0, "riser", "riser build up", "climax -> outro"),
        ]
        pool = [
            element("/sfx/rise_a.wav", "riser", 2.5),
            element("/sfx/rise_b.wav", "riser", 3.0),
        ]
        assign_elements(plan, make_music(), pool)
        filed = [c for c in plan.sfx if c.kind == "riser" and c.file]
        assert len(filed) == 1
        assert filed[0].time + filed[0].duration == pytest.approx(20.0)  # the drop
        markers = [c for c in plan.sfx if c.kind == "riser" and not c.file]
        assert len(markers) == 2

    def test_no_music_riser_anchors_on_the_biggest_act_change(self) -> None:
        # Without music there are no drops: the riser anchors on the phase
        # boundary with the biggest energy jump (build -> climax).
        plan = make_plan(style="travel")
        plan.music_path = ""
        plan.phases = [
            (0.0, 8.0, "opening"),
            (8.0, 16.0, "build"),
            (16.0, 32.0, "climax"),
            (32.0, 40.0, "outro"),
        ]
        pool = [element("/sfx/rise.wav", "riser", 3.0)]
        notes = assign_elements(plan, None, pool)
        cue = next(c for c in plan.sfx if c.kind == "riser" and c.file)
        assert cue.time + cue.duration == pytest.approx(16.0)  # climax start
        assert cue.duration == pytest.approx(3.0)  # one contiguous full play
        assert any("ends on the climax start" in n for n in notes)

    def test_no_music_places_whooshes_and_dip_impacts(self) -> None:
        # The no-music program: fast-cut whooshes and hits out of the black
        # still land without a single drop in sight.
        plan = make_plan(style="trailer")
        plan.music_path = ""
        plan.dips = [(12.0, 0.4), (24.0, 0.4)]
        plan.sfx = [
            SfxCue(5.7, 0.6, "whoosh", "q", "fast cut"),
            SfxCue(17.7, 0.6, "whoosh", "q", "fast cut"),
        ]
        pool = [
            element("/sfx/w1.wav", "whoosh", 0.6),
            element("/sfx/w2.wav", "whoosh", 0.6),
            element("/sfx/hit.wav", "impact", 0.8),
        ]
        assign_elements(plan, None, pool)
        filed = sorted((c.kind, round(c.time, 1)) for c in plan.sfx if c.file)
        assert ("whoosh", 5.7) in filed and ("whoosh", 17.7) in filed
        assert ("impact", 12.4) in filed and ("impact", 24.4) in filed

    def test_riser_ends_exactly_at_the_music_entry(self) -> None:
        # THE trailer moment: a delayed music entry (plan.music_in > 0) is
        # a tension ramp of its own — the cold open's riser ends where the
        # song slams in.
        plan = make_plan()
        plan.music_in = 8.0
        pool = [
            element("/sfx/rise_a.wav", "riser", 4.0),
            element("/sfx/rise_b.wav", "riser", 6.0),
        ]
        notes = assign_elements(plan, make_music(), pool)
        risers = [c for c in plan.sfx if c.kind == "riser" and c.file]
        ends = sorted(round(c.time + c.duration, 3) for c in risers)
        assert 8.0 in ends  # the music entry
        assert 20.0 in ends  # the drop keeps its own ramp
        assert any("ends on the music entry at 8.0s" in n for n in notes)

    def test_impact_file_may_repeat_after_four_seconds(self) -> None:
        # Per-kind spacing replaced the blanket 10s rule: the same impact
        # file hits out of both dips 6s apart.
        plan = make_plan()
        plan.dips = [(10.0, 0.4), (16.0, 0.4)]
        pool = [element("/sfx/hit.wav", "impact", 0.8)]
        assign_elements(plan, MusicAnalysis("/m.wav", 40.0, 120.0), pool)
        impacts = sorted(c.time for c in plan.sfx if c.kind == "impact" and c.file)
        assert impacts == pytest.approx([10.4, 16.4])

    def test_trailer_density_hits_five_plus_accents(self) -> None:
        # The field complaint: "only 2 sound effects used out of 9 usable —
        # too sparse". A 35s trailer cut with a 9-file library must land at
        # least 5 real accents, and the riser must play >= 70% of its file.
        plan = make_plan(style="trailer", duration=35.0)
        plan.dips = [(8.0, 0.4), (26.0, 0.4), (31.0, 0.4)]
        plan.sfx = [
            SfxCue(18.0, 2.0, "riser", "riser build up", "build -> climax"),
            SfxCue(20.0, 1.0, "impact", "cinematic impact hit", "climax start"),
            SfxCue(8.0, 0.4, "sub-drop", "sub drop boom", "title slot"),
            SfxCue(26.0, 0.4, "sub-drop", "sub drop boom", "title slot"),
            SfxCue(31.0, 0.4, "sub-drop", "sub drop boom", "title slot"),
            SfxCue(13.7, 0.6, "whoosh", "whoosh transition fast", "fast cut"),
            SfxCue(23.7, 0.6, "whoosh", "whoosh transition fast", "fast cut"),
        ]
        library = [  # 9 usable files
            element("/sfx/rise_a.wav", "riser", 2.0),
            element("/sfx/rise_b.wav", "riser", 4.0),
            element("/sfx/hit_a.wav", "impact", 0.8),
            element("/sfx/hit_b.wav", "impact", 0.9),
            element("/sfx/hit_c.wav", "impact", 1.1),
            element("/sfx/w1.wav", "whoosh", 0.6),
            element("/sfx/w2.wav", "whoosh", 0.6),
            element("/sfx/boom_a.wav", "braam", 1.5),
            element("/sfx/boom_b.wav", "braam", 1.5),
        ]
        music = make_music(duration=35.0, drops=(20.0,))
        notes = assign_elements(plan, music, library)
        filed = [c for c in plan.sfx if c.file]
        assert len(filed) >= 5, [(c.kind, c.time) for c in filed]
        riser = next(c for c in filed if c.kind == "riser")
        riser_file = next(e for e in library if e.path == riser.file)
        assert riser.duration >= 0.7 * riser_file.duration - 1e-6
        # the no-overlap-per-kind invariant holds throughout
        for kind in ("riser", "impact", "whoosh", "sub-drop"):
            spans = sorted(
                (c.time, c.time + c.duration) for c in filed if c.kind == kind
            )
            for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
                assert s2 >= e1 - 1e-6
        assert "trailer density" in notes[0]

    def test_no_usable_elements_is_an_honest_note(self) -> None:
        plan = make_plan()
        plan.sfx = [SfxCue(20.0, 1.0, "impact", "q", "climax start")]
        notes = assign_elements(
            plan, make_music(), [element("/sfx/x.wav", "other", 5.0)]
        )
        assert plan.sfx[0].file == ""
        assert "no usable" in notes[0]

    def test_assignment_is_deterministic(self) -> None:
        pool = [
            element("/sfx/w1.wav", "whoosh", 0.6),
            element("/sfx/hit.wav", "impact", 0.8),
            element("/sfx/rise.wav", "riser", 2.5),
        ]

        def run():
            plan = make_plan()
            plan.sfx = [
                SfxCue(20.0, 1.0, "impact", "q", "climax start"),
                SfxCue(9.7, 0.6, "whoosh", "q", "fast cut"),
            ]
            assign_elements(plan, make_music(), pool)
            return [(c.kind, c.time, c.duration, c.file) for c in plan.sfx]

        assert run() == run() == run()


class TestCarryElementFiles:
    def test_untouched_cues_keep_their_files(self) -> None:
        old = make_plan()
        old.sfx = [
            SfxCue(20.0, 0.8, "impact", "q", "climax start", file="/sfx/hit.wav"),
            SfxCue(9.7, 0.6, "whoosh", "q", "fast cut", file="/sfx/w.wav"),
        ]
        new = make_plan()
        new.sfx = [
            SfxCue(20.0, 1.0, "impact", "q", "climax start"),  # same spot
            SfxCue(14.7, 0.6, "whoosh", "q", "fast cut"),  # replanned away
        ]
        carried = carry_element_files(old, new)
        assert carried == 1
        assert new.sfx[0].file == "/sfx/hit.wav"
        assert new.sfx[0].duration == pytest.approx(0.8)  # the file's length
        assert new.sfx[1].file == ""  # a moved cue is honestly unfiled

    def test_each_old_cue_carries_at_most_once(self) -> None:
        old = make_plan()
        old.sfx = [SfxCue(10.0, 0.6, "whoosh", "q", "n", file="/sfx/w.wav")]
        new = make_plan()
        new.sfx = [
            SfxCue(10.0, 0.6, "whoosh", "q", "n"),
            SfxCue(10.03, 0.6, "whoosh", "q", "n"),
        ]
        assert carry_element_files(old, new) == 1
        assert [c.file for c in new.sfx] == ["/sfx/w.wav", ""]
