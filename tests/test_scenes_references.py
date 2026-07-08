import pytest

from monteur.analysis import analyze_scenes, analyze_timeline
from monteur.model import Clip, Marker, Timeline
from monteur.references import PROFILES, compare_to_reference


def _timeline_with_markers():
    clips, at = [], 0
    for i, seconds in enumerate([2, 2, 2, 8, 8]):  # fast opening, slow scene 2
        frames = seconds * 25
        clips.append(Clip(name=f"s{i}", record_in=at, record_out=at + frames))
        at += frames
    return Timeline(
        name="cut",
        fps=25,
        clips=clips,
        markers=[Marker(frame=0, name="Sc 1 – Kitchen"), Marker(frame=150, name="Sc 2 – Street")],
    )


class TestScenes:
    def test_scene_split(self):
        scenes = analyze_scenes(_timeline_with_markers())
        assert [s.heading for s in scenes] == ["Sc 1 – Kitchen", "Sc 2 – Street"]
        assert scenes[0].stats.shot_count == 3
        assert scenes[1].stats.shot_count == 2
        assert scenes[0].stats.avg_shot_seconds == 2.0
        assert scenes[1].stats.avg_shot_seconds == 8.0

    def test_positions_are_scene_relative(self):
        scenes = analyze_scenes(_timeline_with_markers())
        assert scenes[1].start == 6.0
        assert scenes[1].stats.shots[0].start == 0.0

    def test_opening_before_first_marker(self):
        t = _timeline_with_markers()
        t.markers[0].frame = 50  # first marker no longer at 0
        scenes = analyze_scenes(t)
        assert scenes[0].heading == "Opening"
        assert len(scenes) == 3

    def test_no_markers_single_scene(self):
        t = _timeline_with_markers()
        t.markers = []
        scenes = analyze_scenes(t)
        assert len(scenes) == 1
        assert scenes[0].stats.shot_count == 5


class TestReferences:
    def _stats(self, shot_seconds):
        clips, at = [], 0
        for i in range(6):
            frames = int(shot_seconds * 25)
            clips.append(Clip(name=f"s{i}", record_in=at, record_out=at + frames))
            at += frames
        return analyze_timeline(Timeline(name="t", fps=25, clips=clips))

    def test_inside_band(self):
        result = compare_to_reference(self._stats(4.0), "thriller")
        assert result["position"] == "inside"
        assert "4.0s" in result["verdict"]

    def test_below_and_above(self):
        assert compare_to_reference(self._stats(1.0), "drama")["position"] == "below"
        assert compare_to_reference(self._stats(9.0), "comedy")["position"] == "above"

    def test_unknown_genre(self):
        with pytest.raises(ValueError, match="pick one of"):
            compare_to_reference(self._stats(4.0), "telenovela")

    def test_profiles_sane(self):
        for profile in PROFILES.values():
            assert 0 < profile.asl_low < profile.asl_high
