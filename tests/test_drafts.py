"""Tests for monteur.drafts — the Create wizard's WIP store.

The store follows monteur.settings' contract: one plain JSON file, atomic
writes, guarded loads (a mangled file is "no drafts", never a crash), and a
MONTEUR_DRAFTS_PATH override so tests stay out of the real home directory.
"""

import json
import time

import pytest

from monteur import drafts
from monteur.drafts import (
    AUTOSAVE_ID,
    MAX_DRAFTS,
    delete_draft,
    drafts_path,
    list_drafts,
    load_draft,
    save_draft,
)


@pytest.fixture(autouse=True)
def _isolated_drafts(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTEUR_DRAFTS_PATH", str(tmp_path / "drafts.json"))


def _plan_json(duration=31.0, cuts=3, style="travel"):
    """A minimal plan dict — the store never parses it beyond the summary."""
    return {
        "monteur_plan": 1,
        "music_path": "/music/song.mp3",
        "duration": duration,
        "entries": [{"clip_path": f"c{i}.mp4"} for i in range(cuts)],
        "notes": [f'style "{style}": Some style'] if style else [],
    }


def _record(name="wip", folder="/footage", **extra):
    record = {
        "name": name,
        "folder": folder,
        "music": "/music/song.mp3",
        "settings": {"style": "travel", "fps": 25},
        "plan_json": _plan_json(),
    }
    record.update(extra)
    return record


class TestPath:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MONTEUR_DRAFTS_PATH", str(tmp_path / "elsewhere.json"))
        assert drafts_path() == tmp_path / "elsewhere.json"
        save_draft(_record())
        assert (tmp_path / "elsewhere.json").exists()

    def test_default_is_home_dot_monteur(self, monkeypatch):
        monkeypatch.delenv("MONTEUR_DRAFTS_PATH", raising=False)
        path = drafts_path()
        assert path.name == "drafts.json"
        assert path.parent.name == ".monteur"


class TestSaveAndLoad:
    def test_round_trip_keeps_the_full_record(self):
        stored = save_draft(_record(pins=[1.5], review={"score": 80}))
        assert stored["id"] and stored["saved_at"]
        loaded = load_draft(stored["id"])
        assert loaded == stored
        assert loaded["plan_json"] == _plan_json()
        assert loaded["pins"] == [1.5]
        assert loaded["review"] == {"score": 80}

    def test_stamps_id_and_saved_at(self):
        stored = save_draft(_record())
        assert len(stored["id"]) == 32  # uuid4 hex
        # ISO-8601 UTC, parseable by strptime
        time.strptime(stored["saved_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_summary_derived_from_plan(self):
        stored = save_draft(_record())
        assert stored["summary"] == {"duration": 31.0, "cuts": 3, "style": "travel"}

    def test_summary_style_falls_back_to_auto(self):
        stored = save_draft(_record(plan_json=_plan_json(style=None)))
        assert stored["summary"]["style"] == "auto"

    def test_upsert_by_id(self):
        first = save_draft(_record(name="v1"))
        second = save_draft(_record(name="v2", id=first["id"]))
        assert second["id"] == first["id"]
        assert [d["name"] for d in list_drafts()] == ["v2"]
        assert load_draft(first["id"])["name"] == "v2"

    def test_missing_folder_is_a_value_error(self):
        with pytest.raises(ValueError, match="folder"):
            save_draft({"plan_json": _plan_json()})

    def test_missing_plan_json_is_a_value_error(self):
        with pytest.raises(ValueError, match="plan_json"):
            save_draft({"folder": "/footage"})

    def test_unknown_id_loads_none(self):
        assert load_draft("nope") is None


class TestList:
    def test_newest_first(self):
        ids = [save_draft(_record(name=f"d{i}"))["id"] for i in range(3)]
        listed = list_drafts()
        assert [d["id"] for d in listed] == list(reversed(ids))

    def test_list_is_light_no_plan_json(self):
        save_draft(_record())
        (listed,) = list_drafts()
        assert "plan_json" not in listed
        assert listed["summary"]["cuts"] == 3
        assert listed["folder"] == "/footage"
        assert listed["settings"] == {"style": "travel", "fps": 25}

    def test_cap_drops_the_oldest(self):
        for i in range(MAX_DRAFTS + 5):
            save_draft(_record(name=f"d{i}"))
        listed = list_drafts()
        assert len(listed) == MAX_DRAFTS
        names = [d["name"] for d in listed]
        assert names[0] == f"d{MAX_DRAFTS + 4}"  # newest survives
        assert "d0" not in names  # oldest evicted

    def test_corrupt_file_is_just_empty(self):
        drafts_path().parent.mkdir(parents=True, exist_ok=True)
        drafts_path().write_text("{ not json", encoding="utf-8")
        assert list_drafts() == []
        assert load_draft("x") is None

    def test_wrong_shape_is_just_empty(self):
        drafts_path().parent.mkdir(parents=True, exist_ok=True)
        drafts_path().write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert list_drafts() == []


class TestAutosave:
    def test_single_slot_replaces_itself(self):
        save_draft(_record(autosave=True, name="Auto-saved cut"))
        save_draft(_record(autosave=True, plan_json=_plan_json(cuts=7)))
        autos = [d for d in list_drafts() if d.get("autosave")]
        assert len(autos) == 1
        assert autos[0]["id"] == AUTOSAVE_ID
        assert autos[0]["summary"]["cuts"] == 7
        assert load_draft(AUTOSAVE_ID)["plan_json"]["entries"]

    def test_flagged_in_list_and_outside_the_cap(self):
        save_draft(_record(autosave=True))
        for i in range(MAX_DRAFTS + 3):
            save_draft(_record(name=f"d{i}"))
        listed = list_drafts()
        assert len(listed) == MAX_DRAFTS + 1  # the cap counts named drafts only
        autos = [d for d in listed if d.get("autosave")]
        assert len(autos) == 1  # ...and the autosave survived the churn
        named = [d for d in listed if not d.get("autosave")]
        assert not any(d.get("autosave") for d in named)

    def test_delete_clears_the_autosave(self):
        save_draft(_record(autosave=True))
        assert delete_draft(AUTOSAVE_ID) is True
        assert list_drafts() == []


class TestDelete:
    def test_delete_removes_and_reports(self):
        stored = save_draft(_record())
        assert delete_draft(stored["id"]) is True
        assert load_draft(stored["id"]) is None
        assert delete_draft(stored["id"]) is False

    def test_delete_unknown_is_false(self):
        assert delete_draft("nope") is False
