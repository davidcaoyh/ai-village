"""The scoreboard, against a log with every shape it will meet on a live run.

The fake cast never fails, never hits the step cap and never writes malformed JSON,
so a suite that only runs `--fake` would prove none of the columns that matter. This
builds the log by hand instead: one row per failure shape actually in tools.py.
"""

from __future__ import annotations

import pytest

from scripts import eval as ev
from village.store import Store


@pytest.fixture
def db(tmp_path):
    store = Store(str(tmp_path / "v.db"))
    s = "s1"
    store.append(s, None, "system", {
        "kind": "session_start", "season_id": "s01", "max_turns": 8, "max_usd": 2.0,
        "cast": [{"name": "a", "model": "vendor/a"}, {"name": "b", "model": "vendor/b"}]})

    for _ in range(3):
        store.append(s, "a", "thought", {"usd": 0.01, "prompt_tokens": 100,
                                         "completion_tokens": 10, "reasoning_tokens": 2})
    store.append(s, "b", "thought", {"usd": 0.02, "prompt_tokens": 50,
                                     "completion_tokens": 5, "reasoning_tokens": 0})

    # a: one good call, then one of every failure shape tools.py can produce
    store.append(s, "a", "action", {"name": "web_search", "arguments": {}})
    store.append(s, "a", "result", {"name": "web_search", "text": "[tavily] results"})
    store.append(s, "a", "action", {"name": "read_file", "arguments": {}})
    store.append(s, "a", "result", {"name": "read_file", "text": "Error: path must be simple"})
    store.append(s, "a", "action", {"name": "fetch_url", "arguments": {}})
    store.append(s, "a", "result", {"name": "fetch_url", "text":
                 "x.org refuses automated readers (403). Do not fetch this url again. "
                 "Instead: web_search for the same claim on another site."})
    store.append(s, "a", "action", {"name": "fetch_url", "arguments": {}})
    store.append(s, "a", "result", {"name": "fetch_url", "text":
                 "You already tried this url this turn. x.org refuses automated readers."})
    store.append(s, "a", "result", {"name": "edit_file", "text": "parse_error: bad json"})

    # b: checks two dois, one of which the registry has never heard of
    store.append(s, "b", "action", {"name": "resolve_doi", "arguments": {}})
    store.append(s, "b", "result", {"name": "resolve_doi", "text":
                 "<doi_metadata doi='10.1/x'>\nCard (1999). A real paper."})
    store.append(s, "b", "action", {"name": "resolve_doi", "arguments": {}})
    store.append(s, "b", "result", {"name": "resolve_doi", "text":
                 "No work is registered under doi 10.9999/fake. OpenAlex indexes most"})

    store.append(s, "a", "system", {"kind": "turn_end", "ended_by": "step_cap"})
    store.append(s, "a", "system", {"kind": "turn_end", "ended_by": "end_turn"})
    store.append(s, "b", "system", {"kind": "turn_end", "ended_by": "no_tool_call"})
    store.append(s, "b", "system", {"kind": "turn_end", "ended_by": "vote_done"})
    store.append(s, None, "system", {"kind": "goal_advanced", "round": 2})
    store.append(s, None, "system", {"kind": "session_end", "reason": "turn_cap", "usd": 0.05})
    return store.db


def row(rows, agent):
    return next(r for r in rows if r["agent"] == agent)


def test_spend_matches_the_session_end_total(db):
    rows = ev.build(db, "s1")
    assert round(sum(r["usd"] for r in rows), 4) == 0.05      # == session_end.usd
    assert row(rows, "a")["calls"] == 3
    assert row(rows, "a")["tok_in"] == 300


def test_the_null_agent_rows_never_become_a_villager(db):
    """session_start, session_end and goal_advanced have agent NULL."""
    assert sorted(r["agent"] for r in ev.build(db, "s1")) == ["a", "b"]


def test_every_failure_shape_in_tools_py_is_counted(db):
    """Not just 'Error%': D38 observations start with the host name, D39 with 'You'."""
    a = row(ev.build(db, "s1"), "a")

    assert a["tool_fail"] == 3            # Error:, the 403 observation, the memo hit
    assert a["actions"] == 4
    assert a["tool_fail_pct"] == pytest.approx(75.0)


def test_a_doi_the_registry_never_heard_of_is_not_a_tool_failure(db):
    """resolve_doi ran and answered. The answer is that the citation was invented."""
    b = row(ev.build(db, "s1"), "b")

    assert b["doi_checked"] == 2
    assert b["doi_fake"] == 1
    assert b["tool_fail"] == 0            # the finding is not a malfunction


def test_malformed_calls_are_scored_over_model_calls_not_actions(db):
    """A malformed call never produced an action, so actions is the wrong denominator."""
    a = row(ev.build(db, "s1"), "a")

    assert a["malformed"] == 1
    assert a["malformed_pct"] == pytest.approx(100 / 3)      # 1 of 3 model calls


def test_turn_endings_keep_every_kind_not_just_the_printed_two(db):
    endings = ev.turn_endings(db, "s1")

    assert endings["a"] == {"turns": 2, "step_cap": 1, "end_turn": 1}
    assert endings["b"] == {"turns": 2, "no_tool_call": 1, "vote_done": 1}


def test_tool_mix_separates_agents_and_counts_repeats(db):
    mix = ev.tool_mix(db, "s1")

    assert mix["a"] == {"web_search": 1, "read_file": 1, "fetch_url": 2}
    assert mix["b"] == {"resolve_doi": 2}


def test_session_facts_reads_both_ends_of_the_run(db):
    facts = ev.session_facts(db, "s1")

    assert facts["ended"] == "turn_cap"
    assert facts["cost"] == "$0.0500"
    assert facts["turn cap"] == 8
    assert facts["rounds"] == 2                  # one goal_advanced means two rounds
    assert "vendor/a" in facts["cast"]


def test_render_prints_a_row_per_agent_and_never_raises(db):
    out = ev.render(ev.build(db, "s1"))

    lines = out.splitlines()
    assert lines[0].split() == ["agent", "calls", "usd", "tok", "in", "tok", "out",
                               "reasoning", "actions", "tool", "fail", "%", "malformed",
                               "%", "step", "cap", "no", "tool", "dois", "ck", "dois", "fake"]
    assert len(lines) == 4                       # header, rule, two agents
    assert [ln.split()[0] for ln in lines[2:]] == ["a", "b"]   # most expensive first
