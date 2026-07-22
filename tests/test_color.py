"""The deterministic colour grade (:mod:`monteur.color`)."""

from __future__ import annotations

import pytest

from monteur.color import (
    Grade,
    LOOKS,
    LOOK_META,
    clamp_grade,
    grade_from_dict,
    grade_to_dict,
    grade_to_ffmpeg,
    is_neutral,
    look,
)


class TestNeutral:
    def test_default_grade_is_neutral(self):
        assert is_neutral(Grade())

    def test_neutral_compiles_to_empty_filter(self):
        # a no-op grade must not touch the render at all
        assert grade_to_ffmpeg(Grade()) == ""

    def test_neutral_serializes_to_empty_dict(self):
        # so plans/projects without a grade stay byte-identical
        assert grade_to_dict(Grade()) == {}

    def test_look_label_alone_still_serializes(self):
        # a "neutral" look carries its label even with zero controls
        assert grade_to_dict(Grade(look="neutral")) == {"look": "neutral"}


class TestClamp:
    def test_out_of_range_is_clamped(self):
        g = clamp_grade(Grade(brightness=5, contrast=-9, saturation=2, warmth=-3))
        assert g.brightness == 1.0
        assert g.contrast == -1.0
        assert g.saturation == 1.0
        assert g.warmth == -1.0

    def test_clamp_preserves_look_label(self):
        assert clamp_grade(Grade(look="warm")).look == "warm"


class TestFfmpeg:
    def test_brightness_and_contrast_build_eq(self):
        f = grade_to_ffmpeg(Grade(brightness=0.5, contrast=0.5))
        assert f.startswith("eq=")
        assert "contrast=" in f and "brightness=" in f
        # saturation untouched -> not emitted
        assert "saturation=" not in f

    def test_full_desaturation_reaches_zero(self):
        f = grade_to_ffmpeg(Grade(saturation=-1.0))
        assert "saturation=0" in f

    def test_warmth_uses_colorbalance_symmetrically(self):
        f = grade_to_ffmpeg(Grade(warmth=1.0))
        assert "colorbalance=rm=0.12:bm=-0.12" == f

    def test_cool_is_the_mirror_of_warm(self):
        assert grade_to_ffmpeg(Grade(warmth=-1.0)) == "colorbalance=rm=-0.12:bm=0.12"

    def test_combined_grade_chains_eq_then_colorbalance(self):
        f = grade_to_ffmpeg(Grade(contrast=0.4, warmth=0.5))
        assert f.startswith("eq=")
        assert f.count(",") == 1
        assert "colorbalance=" in f.split(",", 1)[1]

    def test_out_of_range_clamped_before_compiling(self):
        # brightness 9 clamps to 1.0 -> +0.25 additive, not a wild value
        assert grade_to_ffmpeg(Grade(brightness=9)) == "eq=brightness=0.25"

    def test_saturation_never_negative(self):
        # even a clamped -1 lands exactly at 0, never below
        assert "saturation=0" in grade_to_ffmpeg(Grade(saturation=-5))


class TestLooks:
    def test_every_look_has_meta_and_vice_versa(self):
        assert sorted(LOOKS) == sorted(m["key"] for m in LOOK_META)

    def test_meta_rows_are_complete(self):
        for row in LOOK_META:
            assert row["key"] and row["label"] and row["note"]

    def test_neutral_look_is_neutral(self):
        assert is_neutral(LOOKS["neutral"])

    def test_looks_are_subtle(self):
        # a "look" is a keepable grade, not a destructive demo filter:
        # every control stays well inside the range
        for name, g in LOOKS.items():
            for v in (g.brightness, g.contrast, g.saturation, g.warmth):
                assert -0.5 <= v <= 0.5, name

    @pytest.mark.parametrize("name", ["filmic", "muted", "warm", "cool", "faded"])
    def test_named_looks_actually_grade(self, name):
        assert grade_to_ffmpeg(LOOKS[name]) != ""

    def test_look_returns_a_copy(self):
        g = look("warm")
        g.warmth = 0.0
        assert LOOKS["warm"].warmth == 0.40  # the preset is untouched

    def test_unknown_look_is_neutral(self):
        assert is_neutral(look("nonexistent"))

    def test_look_name_is_case_insensitive(self):
        assert look("WARM").look == "warm"


class TestPersistence:
    def test_round_trip_preserves_every_control(self):
        g = Grade(brightness=0.2, contrast=-0.3, saturation=0.4, warmth=-0.5, look="custom")
        back = grade_from_dict(grade_to_dict(g))
        assert back == clamp_grade(g)

    def test_from_none_is_neutral(self):
        assert is_neutral(grade_from_dict(None))

    def test_from_empty_is_neutral(self):
        assert is_neutral(grade_from_dict({}))

    def test_partial_dict_fills_the_rest_neutral(self):
        g = grade_from_dict({"warmth": 0.3})
        assert g.warmth == 0.3
        assert g.brightness == 0.0 and g.contrast == 0.0 and g.saturation == 0.0

    def test_unknown_keys_are_ignored(self):
        g = grade_from_dict({"warmth": 0.2, "bogus": 99, "hue": 5})
        assert g.warmth == 0.2

    def test_values_are_clamped_on_load(self):
        assert grade_from_dict({"brightness": 9.0}).brightness == 1.0

    def test_string_values_coerce(self):
        # tolerant of JSON that stored numbers as strings
        g = grade_from_dict({"contrast": "0.5"})
        assert g.contrast == 0.5


class TestRenderIntegration:
    """The grade bakes into the export's filter_complex video chain."""

    def test_empty_grade_leaves_the_graph_untouched(self):
        from monteur.preview import _export_video_graph

        base = _export_video_graph([2.0, 2.0], [0, 0], 0.0, 0.0, 4.0)
        assert _export_video_graph([2.0, 2.0], [0, 0], 0.0, 0.0, 4.0, grade="") == base

    def test_grade_is_applied_before_fades_and_format(self):
        from monteur.preview import _export_video_graph

        g = grade_to_ffmpeg(Grade(contrast=0.4, warmth=0.3))
        graph = _export_video_graph([2.0, 2.0], [0, 0], 0.5, 1.0, 4.0, grade=g)
        assert g in graph
        # grade first, then the fades resolve to true black, then format
        assert graph.index(g) < graph.index("fade=t=in")
        assert graph.index(g) < graph.index("fade=t=out")
        assert graph.index(g) < graph.index("format=yuv420p")

    def test_render_export_accepts_a_grade(self):
        import inspect
        from monteur.preview import render_export

        assert "grade" in inspect.signature(render_export).parameters
