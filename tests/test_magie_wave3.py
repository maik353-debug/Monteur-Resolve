"""Tests for magie-blueprint wave 3 — picture coherence.

Wave 3 makes the PICTURE cohere after waves 1-2 nailed the sound, grounded
in Murch's Rule of Six: eye-trace and shot grammar are LOW ranks, so every
term is a TIE-BREAKER that never moves a peak-on-beat or drop-forced cut.

3.1 Eye-trace continuity — an attention point (salience centroid) per shot
edge (monteur.spatial.focus_point); casting rewards a candidate whose entry
point sits near the previous shot's exit point (the eye is carried across
the cut), suspended at a phase boundary.

3.2 Shot-size grammar — wide/medium/close classified offline
(classify_shot_size); casting favours establish -> develop -> pay off and
penalizes two equal sizes adjacent (except close->close in the climax).

3.3 Visual rhymes — ONE deliberate echo: the closing shot is tipped toward
the moment most kindred to the opening, respecting zero-repeat.

The offline pass (monteur.spatial) mirrors monteur.daylight: same 64x36
frames, same .monteur-spatial.json cache, per-clip failure isolated,
only-when-set fields. A pool without the signal casts byte-identically.
"""

from __future__ import annotations

import json
import subprocess

import pytest

np = pytest.importorskip("numpy")

import monteur.spatial as spatial
from monteur.spatial import (
    CACHE_FILENAME,
    SHOT_SIZES,
    annotate_reports,
    classify_shot_size,
    focus_point,
)
from monteur.montage import (
    BEST_FIRST,
    _focus_distance,
    _shot_grammar_step,
    _visual_kinship,
    _PoolItem,
    plan_montage,
    plan_to_dict,
)
from monteur.music import MusicAnalysis, MusicSection
from monteur.sift import ClipReport, Moment


# ------------------------------------------------------------- synthetic frames


def _flat(value: int = 120) -> "np.ndarray":
    return np.full((36, 64, 3), value, dtype=np.uint8)


def _wide() -> "np.ndarray":
    """Detail in every corner — a busy, uniform vista."""
    ramp = (np.arange(36 * 64).reshape(36, 64) % 7 * 30).astype(np.uint8)
    return np.stack([ramp, ramp, ramp], axis=-1)


def _medium() -> "np.ndarray":
    """Texture in a central band — a develop shot."""
    f = _flat()
    ys, xs = np.mgrid[6:30, 20:44]
    f[6:30, 20:44] = np.where(((xs + ys) % 2)[..., None], 255, 30)
    return f


def _close() -> "np.ndarray":
    """A lone textured subject over a soft background — a pay-off."""
    f = _flat()
    ys, xs = np.mgrid[14:22, 28:36]
    f[14:22, 28:36] = np.where(((xs + ys) % 2)[..., None], 255, 0)
    return f


# =================================================== 3.2 shot-size classification


class TestShotSize:
    def test_wide_medium_close_discriminated(self):
        assert classify_shot_size(_wide())["label"] == "wide"
        assert classify_shot_size(_medium())["label"] == "medium"
        assert classify_shot_size(_close())["label"] == "close"

    def test_flat_frame_has_no_shot_size(self):
        result = classify_shot_size(_flat())
        assert result["label"] == ""  # nothing to classify, not a guess
        assert result["confidence"] == 0.0

    def test_confidence_in_documented_band(self):
        for frame in (_wide(), _medium(), _close()):
            conf = classify_shot_size(frame)["confidence"]
            assert 0.5 <= conf <= 1.0

    def test_classes_tuple_is_the_grammar_arc(self):
        assert SHOT_SIZES == ("wide", "medium", "close")


# ===================================================== 3.1 attention point (focus)


class TestFocusPoint:
    def test_centroid_follows_the_detail(self):
        # A subject in the top-left quadrant pulls the attention point there.
        f = _flat()
        f[2:12, 4:16] = np.where(
            ((np.mgrid[2:12, 4:16][0] + np.mgrid[2:12, 4:16][1]) % 2)[..., None],
            255, 0,
        )
        x, y = focus_point(f)
        assert x < 0.5 and y < 0.5  # up and to the left

    def test_flat_frame_has_no_attention_point(self):
        assert focus_point(_flat()) is None  # nothing draws the eye

    def test_coordinates_are_normalised(self):
        x, y = focus_point(_wide())
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


# -------------------------------------- analyse_moment end to end (real ffmpeg)


def _make_clip(path, source: str) -> bool:
    from monteur.media import find_ffmpeg

    cmd = [
        find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"{source}=s=64x36:d=1:r=10",
        "-pix_fmt", "yuv420p", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and path.is_file()


def test_analyse_moment_on_a_rendered_clip(tmp_path):
    pytest.importorskip("imageio_ffmpeg")
    clip = tmp_path / "textured.mp4"
    # testsrc2 is a busy, detail-everywhere frame — reads as a wide shot.
    if not _make_clip(clip, "testsrc2"):
        pytest.skip("ffmpeg cannot render lavfi test clips here")
    result = spatial.analyse_moment(str(clip), 0.0, 1.0)
    assert result["shot_size"] in SHOT_SIZES
    assert result["entry_focus"] is not None
    fx, fy = result["entry_focus"]
    assert 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0


# ========================================================= offline pass + caching


class TestAnnotateReports:
    def _reports(self):
        return [
            ClipReport(
                path="/footage/clip.mp4",
                duration=30.0,
                moments=[Moment(0.0, 4.0, 0.9), Moment(10.0, 14.0, 0.8)],
            )
        ]

    def test_fills_shot_size_and_focus(self, tmp_path, monkeypatch):
        calls = []

        def fake_analyse(path, start, end):
            calls.append((path, start, end))
            return {
                "shot_size": "wide",
                "confidence": 0.9,
                "entry_focus": [0.2, 0.3],
                "exit_focus": [0.7, 0.6],
            }

        monkeypatch.setattr(spatial, "analyse_moment", fake_analyse)
        reports = self._reports()
        cache = tmp_path / CACHE_FILENAME
        notes = annotate_reports(reports, cache_path=cache)

        m = reports[0].moments[0]
        assert m.shot_size == "wide"
        assert m.entry_focus == (0.2, 0.3)
        assert m.exit_focus == (0.7, 0.6)
        assert any("spatial:" in n for n in notes)
        assert len(calls) == 2  # both moments analysed
        assert cache.exists()

    def test_second_run_is_served_from_cache(self, tmp_path, monkeypatch):
        def fake_analyse(path, start, end):
            return {
                "shot_size": "close",
                "confidence": 0.8,
                "entry_focus": [0.5, 0.5],
                "exit_focus": [0.5, 0.5],
            }

        monkeypatch.setattr(spatial, "analyse_moment", fake_analyse)
        cache = tmp_path / CACHE_FILENAME
        annotate_reports(self._reports(), cache_path=cache)

        # Second run: analyse must NOT be called (cache hit).
        def boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("cache miss: analyse_moment was called")

        monkeypatch.setattr(spatial, "analyse_moment", boom)
        reports = self._reports()
        notes = annotate_reports(reports, cache_path=cache)
        assert reports[0].moments[0].shot_size == "close"
        assert any("from cache" in n for n in notes)

    def test_flat_frame_leaves_fields_unset(self, tmp_path, monkeypatch):
        # analyse returns no shot size and no focus (a flat clip) -> the
        # moment stays at defaults, only-when-set.
        def fake_analyse(path, start, end):
            return {"shot_size": "", "confidence": 0.0, "entry_focus": None,
                    "exit_focus": None}

        monkeypatch.setattr(spatial, "analyse_moment", fake_analyse)
        reports = self._reports()
        annotate_reports(reports, cache_path=tmp_path / CACHE_FILENAME)
        m = reports[0].moments[0]
        assert m.shot_size == "" and m.entry_focus is None and m.exit_focus is None

    def test_unreadable_clip_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        def boom(path, start, end):
            raise RuntimeError("no frame")

        monkeypatch.setattr(spatial, "analyse_moment", boom)
        reports = self._reports()
        notes = annotate_reports(reports, cache_path=tmp_path / CACHE_FILENAME)
        assert reports[0].moments[0].shot_size == ""  # untouched
        assert any("clip skipped" in n for n in notes)


# ======================================================== scoring-helper unit math


class TestScoringHelpers:
    def test_grammar_rewards_progression(self):
        assert _shot_grammar_step("wide", "medium", False) > 0
        assert _shot_grammar_step("medium", "close", False) > 0

    def test_grammar_penalizes_equal_neighbours(self):
        assert _shot_grammar_step("medium", "medium", False) < 0

    def test_grammar_allows_close_close_intensification_in_climax(self):
        assert _shot_grammar_step("close", "close", True) == 0.0
        assert _shot_grammar_step("close", "close", False) < 0

    def test_grammar_neutral_without_signal(self):
        assert _shot_grammar_step("", "close", False) == 0.0
        assert _shot_grammar_step("wide", "", False) == 0.0

    def test_focus_distance_none_when_unknown(self):
        assert _focus_distance(None, (0.5, 0.5)) is None
        assert _focus_distance((0.5, 0.5), None) is None
        assert _focus_distance((0.0, 0.0), (0.0, 0.0)) == 0.0

    def test_kinship_zero_without_spatial_signal(self):
        a = _PoolItem("/a.mp4", 10.0, Moment(0.0, 4.0, 0.9))
        b = _PoolItem("/b.mp4", 10.0, Moment(5.0, 9.0, 0.9))
        assert _visual_kinship(a, b) == 0.0

    def test_kinship_high_for_kindred_shots(self):
        a = _PoolItem(
            "/a.mp4", 10.0,
            Moment(0.0, 4.0, 0.9, shot_size="wide", entry_focus=(0.5, 0.5)),
        )
        b = _PoolItem(
            "/b.mp4", 10.0,
            Moment(5.0, 9.0, 0.9, shot_size="wide", entry_focus=(0.5, 0.5)),
        )
        assert _visual_kinship(a, b) > 0.9


# ===================================================== integration: eye-trace flip


def _music(duration: float = 12.0) -> MusicAnalysis:
    return MusicAnalysis(
        path="/m.wav", duration=duration, tempo=120.0,
        beats=[i * 0.5 for i in range(int(duration * 2))],
        sections=[MusicSection(0.0, duration, 0.6, "mid")],
    )


class TestEyeTraceOrdering:
    def _reports(self, with_focus: bool):
        def mk(s, e, sc, **kw):
            return Moment(s, e, sc, **(kw if with_focus else {}))

        return [
            ClipReport(
                path="/a.mp4", duration=60.0,
                moments=[
                    # slot-0 anchor: exit attention point up-left.
                    mk(0.0, 4.0, 0.9, entry_focus=(0.1, 0.1), exit_focus=(0.1, 0.1)),
                    # pool-order-first candidate for slot 1: FAR (down-right).
                    mk(10.0, 14.0, 0.8, entry_focus=(0.9, 0.9), exit_focus=(0.9, 0.9)),
                    # next candidate: NEAR the anchor's exit point.
                    mk(20.0, 24.0, 0.8, entry_focus=(0.12, 0.12), exit_focus=(0.12, 0.12)),
                    mk(30.0, 34.0, 0.7),
                    mk(40.0, 44.0, 0.7),
                ],
                usable_ratio=0.9,
            )
        ]

    def test_eye_trace_carries_the_near_shot_earlier(self):
        # Without the signal the second slot takes the pool-order candidate
        # (source 10s); with eye-trace the near-focus candidate (source 20s)
        # is carried across the cut instead — a sanctioned ordering change.
        plain = plan_montage(self._reports(False), _music(), style="travel",
                             order=BEST_FIRST, sfx=False)
        eyed = plan_montage(self._reports(True), _music(), style="travel",
                            order=BEST_FIRST, sfx=False)
        assert plain.entries[1].source_start == pytest.approx(10.0)
        assert eyed.entries[1].source_start == pytest.approx(20.0)

    def test_attention_distance_across_the_cut_measurably_drops(self):
        # Abnahme (W3): the average attention-point distance across the cut
        # is measurably lower with eye-trace than the baseline ordering.
        # Focus each source WOULD carry (the with_focus mapping):
        focus = {0.0: (0.1, 0.1), 10.0: (0.9, 0.9), 20.0: (0.12, 0.12)}
        plain = plan_montage(self._reports(False), _music(), style="travel",
                             order=BEST_FIRST, sfx=False)
        eyed = plan_montage(self._reports(True), _music(), style="travel",
                            order=BEST_FIRST, sfx=False)

        def cut_distance(plan):
            a = focus[round(plan.entries[0].source_start, 0)]
            b = focus[round(plan.entries[1].source_start, 0)]
            return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

        assert cut_distance(eyed) < cut_distance(plain)


# ================================================ integration: shot-grammar order


class TestShotGrammarOrdering:
    def _reports(self, with_size: bool):
        def mk(s, e, sc, size):
            return Moment(s, e, sc, shot_size=size) if with_size else Moment(s, e, sc)

        return [
            ClipReport(
                path="/a.mp4", duration=60.0,
                moments=[
                    mk(0.0, 4.0, 0.9, "medium"),   # slot-0 anchor: a medium
                    mk(10.0, 14.0, 0.8, "medium"),  # pool-first: EQUAL (penalized)
                    mk(20.0, 24.0, 0.8, "close"),   # progression medium->close
                    mk(30.0, 34.0, 0.7, "wide"),
                    mk(40.0, 44.0, 0.7, "medium"),
                ],
                usable_ratio=0.9,
            )
        ]

    def test_grammar_prefers_the_progression_over_an_equal_size(self):
        plain = plan_montage(self._reports(False), _music(), style="travel",
                             order=BEST_FIRST, sfx=False)
        graded = plan_montage(self._reports(True), _music(), style="travel",
                              order=BEST_FIRST, sfx=False)
        # Without sizes the pool-order medium (source 10) follows the medium
        # anchor; with grammar the establish->pay-off step to the close
        # (source 20) wins instead — fewer equal-size neighbours.
        assert plain.entries[1].source_start == pytest.approx(10.0)
        assert graded.entries[1].source_start == pytest.approx(20.0)


# =================================================== integration: peak/drop unmoved


def _drop_music() -> MusicAnalysis:
    return MusicAnalysis(
        path="/m.wav", duration=24.0, tempo=120.0,
        beats=[i * 0.5 for i in range(48)],
        downbeats=[i * 2.0 for i in range(12)],
        phrases=[0.0, 8.0, 16.0],
        drops=[12.0],
        sections=[
            MusicSection(0.0, 8.0, 0.3, "low"),
            MusicSection(8.0, 16.0, 0.9, "high"),
            MusicSection(16.0, 24.0, 0.5, "mid"),
        ],
    )


def _drop_reports(with_focus: bool):
    def mk(s, e, sc, **kw):
        extra = {k: v for k, v in kw.items() if k in ("highlight", "hero")}
        if with_focus:
            extra.update({k: v for k, v in kw.items()
                          if k in ("shot_size", "entry_focus", "exit_focus")})
        return Moment(s, e, sc, **extra)

    return [
        ClipReport(
            path="/a.mp4", duration=80.0,
            moments=[
                mk(0.0, 6.0, 0.9, shot_size="wide", entry_focus=(0.2, 0.2),
                   exit_focus=(0.8, 0.8)),
                mk(10.0, 15.0, 0.7, shot_size="medium", entry_focus=(0.5, 0.5),
                   exit_focus=(0.5, 0.5)),
                mk(20.0, 26.0, 0.95, highlight=0.9, hero=0.9, shot_size="close",
                   entry_focus=(0.9, 0.1), exit_focus=(0.1, 0.9)),
                mk(30.0, 36.0, 0.8, shot_size="medium", entry_focus=(0.3, 0.7),
                   exit_focus=(0.7, 0.3)),
                mk(40.0, 46.0, 0.75, shot_size="wide", entry_focus=(0.2, 0.2),
                   exit_focus=(0.2, 0.2)),
                mk(50.0, 56.0, 0.7, shot_size="close", entry_focus=(0.5, 0.5),
                   exit_focus=(0.5, 0.5)),
            ],
            usable_ratio=0.9,
        )
    ]


class TestPeakDropUnmoved:
    def test_drop_cut_time_is_identical_with_and_without_spatial(self):
        plain = plan_montage(_drop_reports(False), _drop_music(), style="trailer",
                             order=BEST_FIRST, sfx=True)
        spat = plan_montage(_drop_reports(True), _drop_music(), style="trailer",
                            order=BEST_FIRST, sfx=True)
        # The drop is registered and a cut lands on it in BOTH plans...
        assert 12.0 in plain.drop_marks and 12.0 in spat.drop_marks
        plain_cut = [c for c in (e.record_start for e in plain.entries)
                     if abs(c - 12.0) <= 0.05]
        spat_cut = [c for c in (e.record_start for e in spat.entries)
                    if abs(c - 12.0) <= 0.05]
        assert plain_cut and spat_cut
        # ...at the SAME record time: Wave-3 scoring never moved the pin.
        assert plain_cut[0] == pytest.approx(spat_cut[0])

    def test_every_cut_time_is_unmoved_by_wave3(self):
        # The strongest invariant: cut TIMES come from the beat grid and
        # pins, not from casting, so the full record-boundary sequence is
        # byte-identical whichever moment eye-trace/grammar chose to fill it.
        plain = plan_montage(_drop_reports(False), _drop_music(), style="trailer",
                             order=BEST_FIRST, sfx=True)
        spat = plan_montage(_drop_reports(True), _drop_music(), style="trailer",
                            order=BEST_FIRST, sfx=True)
        plain_cuts = [(e.record_start, e.record_end) for e in plain.entries]
        spat_cuts = [(e.record_start, e.record_end) for e in spat.entries]
        assert plain_cuts == spat_cuts


# ============================================================ integration: rhyme


class TestVisualRhyme:
    def _reports(self):
        # Opening shot is a distinctive wide with a top-left attention point;
        # one LATER moment rhymes with it (same size + focus), the rest do
        # not. Zero-repeat: the rhyme is a DIFFERENT source moment.
        return [
            ClipReport(
                path="/a.mp4", duration=80.0,
                moments=[
                    Moment(0.0, 5.0, 0.95, shot_size="wide",
                           entry_focus=(0.15, 0.15), exit_focus=(0.15, 0.15)),
                    Moment(12.0, 17.0, 0.8, shot_size="close",
                           entry_focus=(0.8, 0.8), exit_focus=(0.8, 0.8)),
                    Moment(24.0, 29.0, 0.8, shot_size="medium",
                           entry_focus=(0.5, 0.5), exit_focus=(0.5, 0.5)),
                    Moment(36.0, 41.0, 0.78, shot_size="close",
                           entry_focus=(0.9, 0.1), exit_focus=(0.9, 0.1)),
                    # the rhyme: kindred to the opening (wide, same corner).
                    Moment(48.0, 53.0, 0.6, shot_size="wide",
                           entry_focus=(0.16, 0.14), exit_focus=(0.16, 0.14)),
                    Moment(60.0, 65.0, 0.6, shot_size="medium",
                           entry_focus=(0.4, 0.6), exit_focus=(0.4, 0.6)),
                ],
                usable_ratio=0.9,
            )
        ]

    def test_closing_shot_echoes_the_opening(self):
        plan = plan_montage(self._reports(), _music(10.0), style="travel",
                            order=BEST_FIRST, sfx=False, pace=2.0)
        assert any("rhyme" in n for n in plan.notes), plan.notes
        # The last shot is the kindred wide (source 48s), not the pool-order
        # tail — the closing echoes the opening.
        assert plan.entries[-1].source_start == pytest.approx(48.0)

    def test_rhyme_respects_zero_repeat(self):
        plan = plan_montage(self._reports(), _music(10.0), style="travel",
                            order=BEST_FIRST, sfx=False, pace=2.0)
        starts = [round(e.source_start, 2) for e in plan.entries]
        # The opening moment (source 0) is used exactly once; the rhyme is a
        # different moment, never a duplicate of it.
        assert starts.count(0.0) == 1


# ================================================= byte-parity when signal absent


class TestByteParityWithoutSignal:
    def _reports(self):
        return [
            ClipReport(
                path="/a.mp4", duration=60.0,
                moments=[Moment(i * 10.0, i * 10.0 + 5.0, 0.9 - i * 0.05)
                         for i in range(5)],
                usable_ratio=0.9,
            )
        ]

    def test_inert_spatial_fields_change_nothing(self):
        # Moments with shot_size="" and focus None (a flat/unanalysed clip)
        # must plan byte-identically to moments without the fields at all.
        bare = plan_montage(self._reports(), _music(24.0), style="travel",
                            order=BEST_FIRST, sfx=False)
        reports = self._reports()
        for m in reports[0].moments:
            m.shot_size = ""
            m.entry_focus = None
            m.exit_focus = None
        inert = plan_montage(reports, _music(24.0), style="travel",
                            order=BEST_FIRST, sfx=False)
        assert json.dumps(plan_to_dict(bare), sort_keys=True) == json.dumps(
            plan_to_dict(inert), sort_keys=True
        )
