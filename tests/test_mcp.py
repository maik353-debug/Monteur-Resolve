"""Tests for the Monteur MCP server (monteur/mcp_server.py)."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None, reason="mcp package not installed"
)

if importlib.util.find_spec("mcp") is not None:
    from monteur import mcp_server

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_EDL = str(FIXTURES / "sample.edl")

EXPECTED_TOOLS = {
    "resolve_status",
    "analyze_timeline",
    "analyze_scenes",
    "compare_cuts",
    "check_genre",
    "mark_slow_sections",
    "sift_footage",
    "analyze_song",
    "create_montage",
    "build_assembly",
    "save_version",
    "list_versions",
}


def test_server_exposes_all_tools():
    tools = asyncio.run(mcp_server.mcp_instance.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names


def test_tools_have_docstrings():
    tools = asyncio.run(mcp_server.mcp_instance.list_tools())
    for tool in tools:
        assert tool.description, f"tool {tool.name} has no description"


def test_analyze_timeline_from_edl_file():
    result = mcp_server.analyze_timeline(file=SAMPLE_EDL, fps=25)
    assert "error" not in result
    assert result["shot_count"] == 5
    assert result["duration_seconds"] == 19.0
    assert set(result["histogram"]) == {
        "0–1s", "1–2s", "2–4s", "4–8s", "8–15s", "15–30s", "30s+",
    }
    assert len(result["longest_shots"]) <= 5
    for section in result["sections"]:
        assert {"start", "end", "label", "avg_shot_seconds"} <= set(section)
    assert result["characterization"]


def test_analyze_timeline_edl_without_fps_is_graceful():
    result = mcp_server.analyze_timeline(file=SAMPLE_EDL)
    assert "error" in result


def test_analyze_scenes_from_file():
    result = mcp_server.analyze_scenes(file=SAMPLE_EDL, fps=25)
    assert "error" not in result
    assert result["scene_count"] >= 1
    assert result["scenes"][0]["shot_count"] > 0


def test_compare_cuts_files():
    result = mcp_server.compare_cuts(file_a=SAMPLE_EDL, file_b=SAMPLE_EDL, fps=25)
    assert "error" not in result
    assert result["shot_count"]["delta"] == 0
    assert "verdict" in result


def test_compare_cuts_missing_side_is_graceful():
    result = mcp_server.compare_cuts(file_a=SAMPLE_EDL, fps=25)
    assert "error" in result


def test_check_genre_known_and_unknown():
    ok = mcp_server.check_genre("drama", file=SAMPLE_EDL, fps=25)
    assert ok["position"] in ("below", "inside", "above")
    bad = mcp_server.check_genre("telenovela", file=SAMPLE_EDL, fps=25)
    assert "error" in bad
    assert "action" in bad["genres"]


def test_resolve_status_without_resolve_returns_error_dict():
    result = mcp_server.resolve_status()
    assert "error" in result
    assert "hint" in result
    assert "Resolve" in result["hint"]


def test_mark_slow_sections_without_resolve_returns_error_dict():
    result = mcp_server.mark_slow_sections()
    assert "error" in result
    assert "hint" in result


def test_sift_footage_empty_dir_is_graceful(tmp_path):
    result = mcp_server.sift_footage(str(tmp_path))
    assert "error" in result


def test_sift_footage_missing_dir_is_graceful(tmp_path):
    result = mcp_server.sift_footage(str(tmp_path / "nope"))
    assert "error" in result


def test_create_montage_requires_destination(tmp_path):
    result = mcp_server.create_montage(
        folder=str(tmp_path), music=str(tmp_path / "song.mp3")
    )
    assert "error" in result
    assert "output" in result["error"] or "into_resolve" in result["error"]


def test_create_montage_no_footage_is_graceful(tmp_path):
    result = mcp_server.create_montage(
        folder=str(tmp_path),
        music=str(tmp_path / "song.mp3"),
        output=str(tmp_path / "out.fcpxml"),
    )
    assert "error" in result


def test_create_montage_rejects_unknown_order(tmp_path):
    result = mcp_server.create_montage(
        folder=str(tmp_path),
        music=str(tmp_path / "song.mp3"),
        output=str(tmp_path / "out.fcpxml"),
        order="shuffled",
    )
    assert "error" in result
    assert "order" in result["error"]


def test_build_assembly_requires_output(tmp_path):
    result = mcp_server.build_assembly(
        script=str(tmp_path / "script.fountain"), takes_dir=str(tmp_path)
    )
    assert "error" in result


def test_build_assembly_no_transcripts_is_graceful(tmp_path):
    script = tmp_path / "script.fountain"
    script.write_text("INT. KITCHEN - NIGHT\n\nANNA\nHello there.\n", encoding="utf-8")
    result = mcp_server.build_assembly(
        script=str(script),
        takes_dir=str(tmp_path),
        output=str(tmp_path / "out.edl"),
    )
    assert "error" in result


def test_save_and_list_versions_roundtrip(tmp_path):
    saved = mcp_server.save_version(
        label="v1 test", file=SAMPLE_EDL, fps=25, project_dir=str(tmp_path)
    )
    assert "error" not in saved
    assert saved["label"] == "v1 test"
    assert saved["shot_count"] == 5

    listed = mcp_server.list_versions(project_dir=str(tmp_path))
    assert "error" not in listed
    assert listed["count"] == 1
    assert listed["versions"][0]["label"] == "v1 test"
    assert listed["versions"][0]["shot_count"] == 5


def test_save_version_without_resolve_and_file_returns_error_dict():
    result = mcp_server.save_version()
    assert "error" in result
    assert "hint" in result


def test_importing_module_does_not_start_server():
    # Import already happened at module load; reaching here means run()
    # was not triggered. Sanity-check the guard exists.
    import inspect

    source = inspect.getsource(mcp_server)
    assert 'if __name__ == "__main__":' in source
