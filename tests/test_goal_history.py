"""Questions from earlier sessions, so a daily village stops repeating itself.

A session only sees its own `runs/<id>/artifacts/`, so `list_files` cannot tell a
villager what yesterday wrote. Left alone, a timer that fires every morning
re-answers the same question all week. The goal carries the history instead of a
tool, because a tool costs a step and might never be called, and the village needs
this before it chooses rather than after.
"""

from __future__ import annotations

import os
import time

import pytest

from village.agent import Agent
from village.config import SeasonConfig
from village.llm import LLMResponse, SpendGuard
from village.orchestrator import _goal_text, past_questions, run_session
from village.store import Store

GOAL = ("Round {round}. Write {file}. Already taken, pick something else:\n"
        "{answered}")


def _brief(runs, session, name, question, age=0.0):
    d = runs / session / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"## Question\n\n{question}\n\n## Answer\n\nProbably.\n")
    when = time.time() - age
    os.utime(d, (when, when))


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "runs"


# --- reading the history off disk -----------------------------------------

def test_it_finds_questions_from_a_session_that_is_over(runs):
    _brief(runs, "20260901-aaaa", "brief.md", "Does class size cause achievement?")

    assert past_questions(str(runs)) == ["Does class size cause achievement?"]


def test_newest_run_first_and_no_duplicates(runs):
    """Same question, two days, one entry - and the recent phrasing wins."""
    _brief(runs, "old", "brief.md", "Does aid cause growth?", age=9000)
    _brief(runs, "old", "brief-2.md", "Do cameras cause less force?", age=9000)
    _brief(runs, "new", "brief.md", "Does   aid   cause growth?", age=1)

    assert past_questions(str(runs)) == ["Does aid cause growth?",
                                         "Do cameras cause less force?"]


def test_a_brief_with_no_question_heading_is_skipped_not_fatal(runs):
    """A session killed mid-write leaves half a file. That must not end the next one."""
    (runs / "half" / "artifacts").mkdir(parents=True)
    (runs / "half" / "artifacts" / "brief.md").write_text("## Answer\n\nno heading\n")
    _brief(runs, "whole", "brief.md", "Does schooling cause earnings?")

    assert past_questions(str(runs)) == ["Does schooling cause earnings?"]


def test_the_list_is_capped(runs):
    """Every entry is prompt tokens paid on every model call, for the whole session."""
    for i in range(40):
        _brief(runs, "s", f"brief-{i}.md", f"Does x{i} cause y?")

    assert len(past_questions(str(runs), limit=5)) == 5


def test_no_runs_directory_is_not_an_error(tmp_path):
    assert past_questions(str(tmp_path / "nothing-here")) == []


# --- reaching the villagers -----------------------------------------------

def test_the_goal_says_none_yet_on_a_first_ever_run(runs):
    season = SeasonConfig(season_id="t", goal=GOAL)
    text = _goal_text(season, 1, [GOAL], str(runs))

    assert "(none yet)" in text
    assert "{answered}" not in text          # no placeholder ever reaches a model


def test_the_goal_lists_what_earlier_sessions_answered(runs):
    _brief(runs, "yesterday", "brief.md", "Does class size cause achievement?")
    season = SeasonConfig(season_id="t", goal=GOAL)
    text = _goal_text(season, 2, [GOAL], str(runs))

    assert "- Does class size cause achievement?" in text
    assert "Round 2" in text and "brief-2.md" in text     # {round}/{file} still work


def test_session_start_logs_the_goal_the_agents_actually_got(runs, tmp_path):
    """The logged goal is evidence in the eval. It must not differ from the real one."""
    _brief(runs, "yesterday", "brief.md", "Does aid cause growth?")
    store = Store(str(tmp_path / "v.db"))
    season = SeasonConfig(season_id="t", goal=GOAL, seconds_between_turns=0,
                          max_idle_rounds=0)
    quiet = Agent(name="a", model="m", persona="p", tools=["end_turn"],
                  chat_fn=lambda *a, **k: LLMResponse(
                      text=None,
                      tool_calls=[{"id": "c", "name": "end_turn",
                                   "arguments": {"summary": "done"}, "parse_error": None}],
                      finish_reason="tool_calls", prompt_tokens=1, completion_tokens=1,
                      reasoning_tokens=0, usd=0.0, raw={}))

    run_session([quiet], season, store, SpendGuard(1.0), "s1", max_turns=1,
                runs_dir=str(runs), verbose=False)

    start = [e["payload"] for e in store.tail("s1")
             if e["payload"].get("kind") == "session_start"][0]
    assert "{answered}" not in start["goal"]
    assert "Does aid cause growth?" in start["goal"]
