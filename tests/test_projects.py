"""Tests for monteur.projects — first-class Monteur Cut projects.

A project is a FOLDER bundle (``<root>/<id>/project.json`` +
``versions/`` / ``exports/``) that references its media pool by absolute
path. The store follows the drafts/settings contract: an env override
(``MONTEUR_PROJECTS_PATH``) keeps tests out of the real home, writes are
atomic, and corrupt/missing manifests degrade instead of crashing.

The load-bearing guarantees proven here:

* media is REFERENCED, never copied or moved (the untouched-file assertion);
* migration from drafts is IDEMPOTENT and LOSSLESS (drafts.json untouched);
* env isolation via ``MONTEUR_PROJECTS_PATH``.
"""

import json

import pytest

from monteur import projects
from monteur.projects import (
    PROJECT_FORMAT_VERSION,
    PROJECTS_PATH_ENV,
    Project,
    add_to_pool,
    create_project,
    delete_project,
    list_projects,
    load_project,
    migrate_drafts,
    project_from_dict,
    project_to_dict,
    projects_root,
    remove_from_pool,
    save_project,
)


@pytest.fixture(autouse=True)
def _isolated_projects(tmp_path, monkeypatch):
    monkeypatch.setenv(PROJECTS_PATH_ENV, str(tmp_path / "projects"))


def _plan_json(duration=4.0, cuts=2, style="travel"):
    """A small but REAL plan dict via the production serializer."""
    from monteur.montage import MontageEntry, MontagePlan, plan_to_dict

    plan = MontagePlan(
        music_path=None,
        duration=duration,
        entries=[
            MontageEntry(f"c{i}.mp4", 0.0, 2.0, float(i) * 2, float(i) * 2 + 2, 0.9)
            for i in range(cuts)
        ],
        notes=[f'style "{style}": Some style'],
    )
    return plan_to_dict(plan)


class TestRoot:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PROJECTS_PATH_ENV, str(tmp_path / "elsewhere"))
        assert projects_root() == tmp_path / "elsewhere"

    def test_default_is_home_dot_monteur(self, monkeypatch):
        monkeypatch.delenv(PROJECTS_PATH_ENV, raising=False)
        root = projects_root()
        assert root.name == "projects"
        assert root.parent.name == ".monteur"


class TestCreateLoadSaveListDelete:
    def test_round_trip(self):
        project = create_project(
            "My cut", options={"style": "travel", "fps": 25}, notes=["hi"]
        )
        assert project.id and project.created_at and project.modified_at
        # The bundle: project.json + versions/ + exports/ subfolders.
        assert project.manifest_path.is_file()
        assert (project.root / "versions").is_dir()
        assert (project.root / "exports").is_dir()

        loaded = load_project(project.id)
        assert loaded is not None
        assert loaded.id == project.id
        assert loaded.name == "My cut"
        assert loaded.options == {"style": "travel", "fps": 25}
        assert loaded.notes == ["hi"]
        assert loaded.plan is None
        assert loaded.has_plan is False

    def test_manifest_shape(self):
        project = create_project("Cut", options={"fps": 25})
        data = json.loads(project.manifest_path.read_text(encoding="utf-8"))
        assert data["monteur_project"] == PROJECT_FORMAT_VERSION
        assert data["id"] == project.id
        assert data["name"] == "Cut"
        assert data["media_pool"] == []
        assert data["options"] == {"fps": 25}
        # Only-when-set: no plan, no migration marker on a plain project.
        assert "plan" not in data
        assert "migrated_from_draft" not in data

    def test_save_bumps_modified_at(self, monkeypatch):
        project = create_project("Cut")
        first = project.modified_at
        # Stamp a later time deterministically.
        monkeypatch.setattr(projects, "_now", lambda: "2099-01-01T00:00:00Z")
        save_project(project)
        assert project.modified_at == "2099-01-01T00:00:00Z"
        assert project.modified_at != first
        assert load_project(project.id).modified_at == "2099-01-01T00:00:00Z"

    def test_list_summaries(self):
        a = create_project("Alpha", options={})
        b = create_project("Beta")
        add_to_pool(b, "/footage/trip", "folder")
        summaries = list_projects()
        ids = {s["id"] for s in summaries}
        assert ids == {a.id, b.id}
        by_id = {s["id"]: s for s in summaries}
        assert by_id[b.id]["pool_size"] == 1
        assert by_id[b.id]["has_plan"] is False
        assert set(by_id[a.id]) == {
            "id", "name", "created_at", "modified_at", "pool_size", "has_plan", "type"
        }
        assert by_id[a.id]["type"] == "cut"  # Create is the default writer

    def test_type_defaults_cut_and_is_byte_compatible(self):
        p = create_project("A cut")
        assert p.type == "cut"
        # a cut manifest stays byte-identical to before the field existed
        assert "type" not in project_to_dict(p)

    def test_typed_project_round_trips(self):
        m = create_project("A movie", type="movie")
        assert project_to_dict(m)["type"] == "movie"
        assert project_from_dict(project_to_dict(m)).type == "movie"
        # old manifests without the key load as cuts
        d = project_to_dict(m)
        del d["type"]
        assert project_from_dict(d).type == "cut"

    def test_list_newest_first(self, monkeypatch):
        monkeypatch.setattr(projects, "_now", lambda: "2020-01-01T00:00:00Z")
        old = create_project("Old")
        monkeypatch.setattr(projects, "_now", lambda: "2030-01-01T00:00:00Z")
        new = create_project("New")
        assert [s["id"] for s in list_projects()] == [new.id, old.id]

    def test_delete_removes_bundle(self):
        project = create_project("Cut")
        assert project.root.is_dir()
        assert delete_project(project.id) is True
        assert not project.root.exists()
        assert load_project(project.id) is None
        assert delete_project(project.id) is False  # already gone


class TestMediaReferencedNotCopied:
    def test_media_file_is_untouched_and_never_copied(self, tmp_path):
        # A real media file living OUTSIDE the projects root.
        media = tmp_path / "footage" / "clip.mp4"
        media.parent.mkdir(parents=True)
        payload = b"\x00\x01FAKE-MP4-BYTES\x02\x03"
        media.write_bytes(payload)
        mtime_before = media.stat().st_mtime

        project = create_project("Cut")
        entry = add_to_pool(project, media)
        assert entry["kind"] == "file"
        assert entry["path"] == str(media)  # absolute, referenced

        # The original file is byte-for-byte untouched.
        assert media.read_bytes() == payload
        assert media.stat().st_mtime == mtime_before
        # No copy of the media landed anywhere in the bundle.
        bundle_files = [p for p in project.root.rglob("*") if p.is_file()]
        assert bundle_files == [project.manifest_path]
        for p in bundle_files:
            assert p.read_bytes() != payload

    def test_folder_reference_kind(self, tmp_path):
        folder = tmp_path / "footage"
        folder.mkdir()
        project = create_project("Cut")
        entry = add_to_pool(project, folder)
        assert entry["kind"] == "folder"

    def test_delete_never_touches_referenced_media(self, tmp_path):
        media = tmp_path / "clip.mp4"
        media.write_bytes(b"keepme")
        project = create_project("Cut")
        add_to_pool(project, media)
        delete_project(project.id)
        assert media.exists()
        assert media.read_bytes() == b"keepme"


class TestMediaPool:
    def test_add_and_remove(self, tmp_path):
        project = create_project("Cut")
        add_to_pool(project, "/footage/a", "folder")
        add_to_pool(project, "/music/song.mp3", "file")
        assert [e["path"] for e in load_project(project.id).media_pool] == [
            "/footage/a", "/music/song.mp3"
        ]
        assert remove_from_pool(project, "/footage/a") is True
        assert [e["path"] for e in load_project(project.id).media_pool] == [
            "/music/song.mp3"
        ]
        assert remove_from_pool(project, "/nope") is False

    def test_add_is_idempotent(self):
        project = create_project("Cut")
        assert add_to_pool(project, "/footage/a", "folder") is not None
        assert add_to_pool(project, "/footage/a", "folder") is None  # deduped
        assert len(load_project(project.id).media_pool) == 1

    def test_paths_are_absolute(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        project = create_project("Cut")
        entry = add_to_pool(project, "rel/clip.mp4", "file")
        assert entry["path"] == str(tmp_path / "rel" / "clip.mp4")


class TestPlanRoundTrip:
    def test_plan_survives_save_and_load(self):
        from monteur.montage import plan_from_dict, plan_to_dict

        plan_json = _plan_json()
        project = create_project("Cut", plan=plan_json)
        assert project.has_plan is True
        loaded = load_project(project.id)
        assert loaded.plan == plan_json
        # The stored dict rebuilds a real MontagePlan and re-serializes identically.
        plan = plan_from_dict(loaded.plan)
        assert plan_to_dict(plan) == plan_json
        # The list summary flags the plan.
        assert next(s for s in list_projects() if s["id"] == project.id)["has_plan"]


class TestSerialization:
    def test_project_to_from_dict_round_trip(self):
        project = create_project(
            "Cut",
            options={"fps": 25},
            plan=_plan_json(),
            notes=["a", "b"],
            migrated_from_draft="draft-1",
        )
        add_to_pool(project, "/footage/x", "folder")
        data = project_to_dict(project)
        rebuilt = project_from_dict(data)
        assert project_to_dict(rebuilt) == data
        assert rebuilt.migrated_from_draft == "draft-1"

    def test_from_dict_rejects_non_project(self):
        with pytest.raises(ValueError, match="monteur_project"):
            project_from_dict({"foo": "bar"})

    def test_from_dict_rejects_bad_version(self):
        with pytest.raises(ValueError, match="unsupported project version"):
            project_from_dict({"monteur_project": 999, "id": "x"})


class TestCorruptDegrades:
    def test_corrupt_manifest_loads_as_none(self, tmp_path):
        project = create_project("Cut")
        project.manifest_path.write_text("{not json", encoding="utf-8")
        assert load_project(project.id) is None

    def test_corrupt_bundle_skipped_in_list(self):
        good = create_project("Good")
        bad = create_project("Bad")
        bad.manifest_path.write_text("garbage", encoding="utf-8")
        ids = [s["id"] for s in list_projects()]
        assert ids == [good.id]

    def test_missing_bundle_loads_as_none(self):
        assert load_project("does-not-exist") is None

    def test_list_on_empty_root_is_empty(self):
        assert list_projects() == []


class TestMigration:
    def _draft(self, tmp_path, monkeypatch, name="wip", folder="/footage/trip"):
        monkeypatch.setenv("MONTEUR_DRAFTS_PATH", str(tmp_path / "drafts.json"))
        from monteur import drafts

        return drafts.save_draft(
            {
                "name": name,
                "folder": folder,
                "music": "/music/song.mp3",
                "settings": {"style": "travel", "fps": 25},
                "plan_json": _plan_json(),
            }
        )

    def test_migrates_a_draft_into_a_matching_project(self, tmp_path, monkeypatch):
        draft = self._draft(tmp_path, monkeypatch)
        created = migrate_drafts()
        assert len(created) == 1
        project = created[0]
        assert project.migrated_from_draft == draft["id"]
        assert project.name == "wip"
        assert project.options == {"style": "travel", "fps": 25}
        assert project.plan == _plan_json()
        pool = {e["path"]: e["kind"] for e in project.media_pool}
        assert pool == {"/footage/trip": "folder", "/music/song.mp3": "file"}
        # It is persisted and shows up in the list.
        assert load_project(project.id) is not None
        assert [s["id"] for s in list_projects()] == [project.id]

    def test_is_idempotent(self, tmp_path, monkeypatch):
        self._draft(tmp_path, monkeypatch)
        first = migrate_drafts()
        assert len(first) == 1
        # Re-run: nothing new, and no duplicate project.
        second = migrate_drafts()
        assert second == []
        assert len(list_projects()) == 1

    def test_new_draft_after_migration_is_picked_up(self, tmp_path, monkeypatch):
        self._draft(tmp_path, monkeypatch, name="one", folder="/f/one")
        migrate_drafts()
        self._draft(tmp_path, monkeypatch, name="two", folder="/f/two")
        created = migrate_drafts()
        assert [p.name for p in created] == ["two"]
        assert len(list_projects()) == 2

    def test_autosave_slot_is_never_migrated(self, tmp_path, monkeypatch):
        # the autosave slot is a resume buffer, not a saved draft — migrating it
        # would mint a phantom empty project that comes back after every delete
        monkeypatch.setenv("MONTEUR_DRAFTS_PATH", str(tmp_path / "drafts.json"))
        from monteur import drafts

        drafts.save_draft({
            "autosave": True,
            "folder": "/footage/trip",
            "plan_json": _plan_json(),
        })
        assert migrate_drafts() == []          # nothing migrated
        assert list_projects() == []           # no phantom project
        # a genuine named draft alongside it still migrates (and only it)
        self._draft(tmp_path, monkeypatch, name="real")
        created = migrate_drafts()
        assert [p.name for p in created] == ["real"]

    def test_drafts_file_is_never_mutated(self, tmp_path, monkeypatch):
        self._draft(tmp_path, monkeypatch)
        drafts_path = tmp_path / "drafts.json"
        before = drafts_path.read_bytes()
        migrate_drafts()
        migrate_drafts()  # twice, to be sure
        assert drafts_path.read_bytes() == before

    def test_no_drafts_migrates_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MONTEUR_DRAFTS_PATH", str(tmp_path / "empty.json"))
        assert migrate_drafts() == []
        assert list_projects() == []


class TestVersionHistory:
    """Never lose a cut: every distinct plan is a restorable snapshot."""

    def test_add_and_list_versions_newest_first(self):
        p = create_project("v")
        a = projects.add_version(p, _plan_json(cuts=2), label="first")
        b = projects.add_version(p, _plan_json(cuts=3), label="second")
        assert a and b and a["id"] != b["id"]
        listed = projects.list_versions(p)
        assert [v["label"] for v in listed] == ["second", "first"]  # newest first
        assert listed[0]["shots"] == 3  # cheap stats recorded in the index

    def test_add_version_dedupes_identical_plan(self):
        p = create_project("v")
        plan = _plan_json()
        assert projects.add_version(p, plan) is not None
        assert projects.add_version(p, plan) is None  # unchanged -> no new snapshot
        assert len(p.versions) == 1

    def test_add_version_ignores_empty_plan(self):
        p = create_project("v")
        assert projects.add_version(p, {}) is None
        assert p.versions == []

    def test_load_version_returns_full_plan(self):
        p = create_project("v")
        plan = _plan_json(cuts=3)
        entry = projects.add_version(p, plan)
        assert projects.load_version(p, entry["id"]) == plan
        assert projects.load_version(p, "nope") is None

    def test_restore_brings_a_past_cut_back(self):
        p = create_project("v")
        old = _plan_json(cuts=2)
        projects.add_version(p, old)
        new = _plan_json(cuts=5)
        p.plan = new
        save_project(p)
        old_id = p.versions[0]["id"]
        assert projects.restore_version(p, old_id) is True
        assert p.plan == old
        # restoring snapshotted the plan we left, so it's reversible
        assert any(v.get("label") == "before restore" for v in p.versions)

    def test_restore_unknown_version_is_false(self):
        p = create_project("v")
        assert projects.restore_version(p, "missing") is False

    def test_versions_survive_reload(self):
        p = create_project("v")
        projects.add_version(p, _plan_json(cuts=2), label="keep")
        reloaded = load_project(p.id)
        assert [v["label"] for v in reloaded.versions] == ["keep"]
        assert projects.load_version(reloaded, reloaded.versions[0]["id"]) is not None

    def test_history_is_capped(self, monkeypatch):
        monkeypatch.setattr(projects, "_MAX_VERSIONS", 3)
        p = create_project("v")
        for i in range(5):
            projects.add_version(p, _plan_json(cuts=i + 1))
        assert len(p.versions) == 3  # oldest pruned
        # the pruned snapshot files are gone too
        files = list((p.root / "versions").glob("*.json"))
        assert len(files) == 3

    def test_empty_history_stays_out_of_manifest(self):
        p = create_project("v")
        assert "versions" not in project_to_dict(p)  # byte-identical to before


class TestAnalysisStore:
    """The sift + Claude labels live IN the project (analysis.json), so a
    re-opened project builds from its own stored analysis and never re-scans."""

    def _report(self, path, ratio=0.8, tags=None):
        from monteur.sift import ClipReport, ClipSegment, Moment

        return ClipReport(
            path=str(path), duration=12.0,
            segments=[ClipSegment(0.0, 3.0, "usable", 0.9)],
            moments=[Moment(1.0, 3.0, 0.9, label="curve", tags=tags or ["kurve"], hero=0.7)],
            usable_ratio=ratio, notes=["ok"], media_start=0.0,
        )

    def _clip(self, tmp_path, name):
        p = tmp_path / name
        p.write_bytes(b"not really a video")
        return p

    def test_save_then_load_round_trips_with_labels(self, tmp_path):
        clip = self._clip(tmp_path, "a.mp4")
        project = create_project("p", media_pool=[str(clip)])
        projects.save_reports(project, [self._report(clip, 0.72, tags=["hero", "kurve"])])

        # a FRESH load (a re-opened project) — the analysis is in the bundle
        reopened = load_project(project.id)
        reports = projects.load_reports(reopened)
        assert len(reports) == 1
        assert reports[0].usable_ratio == 0.72
        assert reports[0].moments[0].tags == ["hero", "kurve"]  # Claude labels survive
        assert (reopened.root / "analysis.json").is_file()

    def test_load_skips_a_stale_clip(self, tmp_path):
        import os
        import time

        clip = self._clip(tmp_path, "a.mp4")
        project = create_project("p", media_pool=[str(clip)])
        projects.save_reports(project, [self._report(clip)])
        future = time.time() + 10
        os.utime(clip, (future, future))  # a re-export -> mtime changed
        assert projects.load_reports(project) == []
        assert projects.analyzed_count(project) == 0

    def test_save_merges_and_replaces_same_clip(self, tmp_path):
        a, b = self._clip(tmp_path, "a.mp4"), self._clip(tmp_path, "b.mp4")
        project = create_project("p", media_pool=[str(a), str(b)])
        projects.save_reports(project, [self._report(a, 0.5)])
        projects.save_reports(project, [self._report(b, 0.6)])  # must not drop a
        projects.save_reports(project, [self._report(a, 0.9)])  # re-analyse a
        reports = {r.path: r for r in projects.load_reports(project)}
        assert len(reports) == 2
        assert reports[str(a)].usable_ratio == 0.9  # replaced, not duplicated

    def test_load_is_pool_order(self, tmp_path):
        a, b, c = (self._clip(tmp_path, n) for n in ("a.mp4", "b.mp4", "c.mp4"))
        project = create_project("p", media_pool=[str(a), str(b), str(c)])
        # store out of order
        projects.save_reports(project, [self._report(c), self._report(a), self._report(b)])
        paths = [r.path for r in projects.load_reports(project)]
        assert paths == [str(a), str(b), str(c)]

    def test_empty_when_nothing_analysed(self, tmp_path):
        project = create_project("p", media_pool=[str(self._clip(tmp_path, "a.mp4"))])
        assert projects.load_reports(project) == []
        assert projects.analyzed_count(project) == 0

    def test_corrupt_store_degrades_to_empty(self, tmp_path):
        project = create_project("p")
        (project.root / "analysis.json").write_text("{not json", encoding="utf-8")
        assert projects.load_reports(project) == []


class TestMusicStore:
    """The song's beat/energy analysis lives IN the project (music.json), so a
    re-opened project builds from its own stored grid and never re-runs the DSP."""

    def _song(self, tmp_path, name="song.mp3"):
        p = tmp_path / name
        p.write_bytes(b"ID3 not really audio")
        return p

    def _music(self, path):
        from monteur.music import MusicAnalysis, MusicSection

        return MusicAnalysis(
            path=str(path), duration=30.0, tempo=120.0,
            beats=[0.5, 1.0, 1.5, 2.0],
            sections=[
                MusicSection(0.0, 10.0, 0.3, "low"),
                MusicSection(10.0, 30.0, 0.9, "high"),
            ],
            downbeats=[0.5, 2.5],
            phrases=[0.5],
            drops=[10.0],
            low_energy=[0.2, 0.8],
        )

    def test_save_then_load_round_trips_all_fields(self, tmp_path):
        song = self._song(tmp_path)
        project = create_project("p", media_pool=[str(song)])
        projects.save_music(project, self._music(song))

        loaded = projects.load_music(project, str(song))
        assert loaded is not None
        assert loaded.path == str(song)
        assert loaded.duration == 30.0
        assert loaded.tempo == 120.0
        assert loaded.beats == [0.5, 1.0, 1.5, 2.0]
        assert loaded.downbeats == [0.5, 2.5]
        assert loaded.phrases == [0.5]
        assert loaded.drops == [10.0]
        assert loaded.low_energy == [0.2, 0.8]
        assert len(loaded.sections) == 2
        assert loaded.sections[0].label == "low"
        assert loaded.sections[1].energy == 0.9
        assert (project.root / "music.json").is_file()

    def test_fresh_load_from_reopened_project(self, tmp_path):
        song = self._song(tmp_path)
        project = create_project("p", media_pool=[str(song)])
        projects.save_music(project, self._music(song))

        reopened = load_project(project.id)
        loaded = projects.load_music(reopened, str(song))
        assert loaded is not None
        assert loaded.tempo == 120.0
        assert len(loaded.sections) == 2

    def test_load_skips_a_stale_song(self, tmp_path):
        import os
        import time

        song = self._song(tmp_path)
        project = create_project("p", media_pool=[str(song)])
        projects.save_music(project, self._music(song))
        future = time.time() + 10
        os.utime(song, (future, future))  # a re-export -> mtime changed
        assert projects.load_music(project, str(song)) is None

    def test_load_different_song_is_none(self, tmp_path):
        song = self._song(tmp_path)
        other = self._song(tmp_path, "other.mp3")
        project = create_project("p", media_pool=[str(song)])
        projects.save_music(project, self._music(song))
        assert projects.load_music(project, str(other)) is None

    def test_none_when_nothing_saved(self, tmp_path):
        song = self._song(tmp_path)
        project = create_project("p", media_pool=[str(song)])
        assert projects.load_music(project, str(song)) is None

    def test_save_none_is_noop(self, tmp_path):
        song = self._song(tmp_path)
        project = create_project("p")
        projects.save_music(project, None)
        assert not (project.root / "music.json").exists()

    def test_corrupt_store_degrades_to_none(self, tmp_path):
        song = self._song(tmp_path)
        project = create_project("p")
        (project.root / "music.json").write_text("{not json", encoding="utf-8")
        assert projects.load_music(project, str(song)) is None
