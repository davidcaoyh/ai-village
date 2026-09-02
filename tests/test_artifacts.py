"""The shared-file tools, and the route that finally lets a visitor read the result.

The Sep 2 run is why these exist: with write_file and no way to read it back, four
models spent 15 of 34 chat messages asking each other to paste brief.md, and wrote
the whole document 30 times, clobbering each other at sizes from 49 to 4,150 chars.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from village.store import Store
from village.tools import ToolContext, execute

SKELETON = "# Brief\n\n## Question\n\nold q\n\n## Evidence\n\nold evidence\n\n## Sources\n\n- a\n"


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(agent="claude", runs_dir=str(tmp_path), session_id="s1")


# --- reading -------------------------------------------------------------

def test_read_file_says_what_does_exist_when_the_path_is_wrong(ctx):
    execute("write_file", {"path": "brief.md", "text": "hi"}, ctx)
    out = execute("read_file", {"path": "draft.md"}, ctx)
    assert "does not exist" in out and "brief.md" in out    # points at the real name


def test_read_file_returns_the_text(ctx):
    execute("write_file", {"path": "brief.md", "text": SKELETON}, ctx)
    assert "old evidence" in execute("read_file", {"path": "brief.md"}, ctx)


def test_list_files_reports_an_empty_village(ctx):
    assert execute("list_files", {}, ctx) == "(no files yet)"


# --- editing -------------------------------------------------------------

def test_edit_file_replaces_one_section_and_leaves_the_others(ctx):
    execute("write_file", {"path": "brief.md", "text": SKELETON}, ctx)
    execute("edit_file", {"path": "brief.md", "section": "Evidence",
                          "text": "Lim & Dinges (2010), 70 studies."}, ctx)

    after = execute("read_file", {"path": "brief.md"}, ctx)
    assert "Lim & Dinges" in after
    assert "old evidence" not in after      # the section was replaced
    assert "old q" in after and "- a" in after   # its neighbours were not


def test_edit_file_appends_a_section_that_is_missing(ctx):
    execute("write_file", {"path": "brief.md", "text": SKELETON}, ctx)
    execute("edit_file", {"path": "brief.md", "section": "Confounders",
                          "text": "selection into the sample"}, ctx)

    after = execute("read_file", {"path": "brief.md"}, ctx)
    assert "## Confounders" in after and "old q" in after


def test_edit_file_creates_the_file_when_there_is_none(ctx):
    execute("edit_file", {"path": "brief.md", "section": "Question",
                          "text": "does X cause Y?"}, ctx)
    assert "does X cause Y?" in execute("read_file", {"path": "brief.md"}, ctx)


def test_section_matching_ignores_case_and_stray_hashes(ctx):
    execute("write_file", {"path": "brief.md", "text": SKELETON}, ctx)
    execute("edit_file", {"path": "brief.md", "section": "## EVIDENCE",
                          "text": "replaced once"}, ctx)

    after = execute("read_file", {"path": "brief.md"}, ctx)
    assert after.count("## Evidence") == 1 and "replaced once" in after


@pytest.mark.parametrize("path", ["/etc/passwd", "../../secrets.txt", "a/../../b"])
def test_the_new_tools_refuse_to_escape_the_run(ctx, path):
    for name, args in (("read_file", {"path": path}),
                       ("edit_file", {"path": path, "section": "s", "text": "t"})):
        assert execute(name, {**args}, ctx).startswith("Error")


# --- the route -----------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VILLAGE_DB_PATH", str(tmp_path / "v.db"))
    monkeypatch.setenv("VILLAGE_RUNS_DIR", str(tmp_path))
    import server.main as main
    importlib.reload(main)
    Store(str(tmp_path / "v.db")).append("s1", None, "system", {"kind": "session_start"})
    return TestClient(main.app), tmp_path


def test_artifact_route_serves_what_the_village_wrote(client):
    c, root = client
    execute("write_file", {"path": "brief.md", "text": SKELETON},
            ToolContext(agent="claude", runs_dir=str(root), session_id="s1"))

    body = c.get("/api/artifact", params={"session": "s1"}).json()
    assert body["exists"] is True and "old evidence" in body["text"]


def test_artifact_route_is_calm_about_a_file_that_is_not_there(client):
    c, _ = client
    body = c.get("/api/artifact", params={"session": "s1"}).json()
    assert body["exists"] is False and body["text"] == ""


def test_artifact_route_refuses_to_leave_the_run(client):
    c, _ = client
    r = c.get("/api/artifact", params={"session": "s1", "path": "../../../etc/passwd"})
    assert r.status_code == 400
