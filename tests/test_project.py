import pytest

from fable.analysis import analyze_timeline
from fable.model import Clip, Timeline
from fable.project import Project


def _stats(name="cut", lengths=(2.0, 4.0, 3.0)):
    clips, at = [], 0
    for i, seconds in enumerate(lengths):
        frames = int(seconds * 25)
        clips.append(Clip(name=f"s{i}", record_in=at, record_out=at + frames))
        at += frames
    return analyze_timeline(Timeline(name=name, fps=25, clips=clips))


class TestProject:
    def test_add_and_list(self, tmp_path):
        project = Project(tmp_path)
        entry = project.add_version(_stats("v1"), label="first cut", saved_at="2026-07-07")
        assert entry["id"] == 1
        versions = project.versions()
        assert len(versions) == 1
        assert versions[0]["label"] == "first cut"
        assert versions[0]["shot_count"] == 3
        assert "stats" not in versions[0]

    def test_roundtrip_stats(self, tmp_path):
        project = Project(tmp_path)
        original = _stats("v1")
        vid = project.add_version(original)["id"]
        loaded = project.get_stats(vid)
        assert loaded == original

    def test_ids_increment_after_delete(self, tmp_path):
        project = Project(tmp_path)
        project.add_version(_stats("v1"))
        second = project.add_version(_stats("v2"))["id"]
        project.delete_version(1)
        assert project.add_version(_stats("v3"))["id"] == second + 1
        assert [v["id"] for v in project.versions()] == [2, 3]

    def test_get_missing_raises(self, tmp_path):
        with pytest.raises(KeyError):
            Project(tmp_path).get_stats(99)

    def test_default_label_from_timeline(self, tmp_path):
        entry = Project(tmp_path).add_version(_stats("director_cut_v3"))
        assert entry["label"] == "director_cut_v3"
