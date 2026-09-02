"""How a season ends when the goal is met, and when nobody will say so.

Written from the Sep 1 120-turn run: 33 of 35 edits landed in the first 40 turns,
and the remaining 82 turns produced 2 edits and 67 chat messages agreeing the
brief was finished. Nothing malfunctioned. The village simply had no way to be
done, so the two mechanisms here are a vote and a backstop behind it.
"""

from __future__ import annotations

import pytest

from village.agent import Agent
from village.config import SeasonConfig
from village.llm import LLMResponse, SpendGuard
from village.orchestrator import run_session
from village.store import Store


def _call(tool, args):
    return LLMResponse(text=None,
                       tool_calls=[{"id": "c1", "name": tool, "arguments": args,
                                    "parse_error": None}],
                       finish_reason="tool_calls", prompt_tokens=10, completion_tokens=5,
                       reasoning_tokens=0, usd=0.0, raw={})


class Script:
    """One response per call. Repeats the last forever, or the whole list if cycling.

    The difference matters here: a villager that runs out of script goes quiet,
    which is exactly what an idle village looks like, so a test about *working*
    villagers has to cycle or it measures the backstop by accident.
    """

    def __init__(self, *responses, cycle=False):
        self.responses, self.i, self.cycle = list(responses), 0, cycle

    def __call__(self, model, messages, tools=None, temperature=0.7,
                 max_tokens=900, reasoning=None):
        n = len(self.responses)
        r = self.responses[self.i % n] if self.cycle else self.responses[min(self.i, n - 1)]
        self.i += 1
        return r


TOOLS = ["send_chat", "edit_file", "write_file", "vote_done", "end_turn"]


def _agent(fn, name):
    return Agent(name=name, model="fake/model", persona="p", tools=TOOLS, chat_fn=fn)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "v.db"))


@pytest.fixture
def season():
    return SeasonConfig(season_id="test", goal="Write the brief.", turns_per_session=40,
                        seconds_between_turns=0, context_window_events=30,
                        compaction_every_turns=0, max_idle_rounds=0)


def _end(store, session="s1"):
    return [e["payload"] for e in store.tail(session)
            if e["payload"].get("kind") == "session_end"][0]


# --- the vote ------------------------------------------------------------

def test_unanimous_votes_end_the_goal(store, season):
    voters = [_agent(Script(_call("vote_done", {"reason": "done"})), n)
              for n in ("a", "b", "c")]
    run_session(voters, season, store, SpendGuard(1.0), "s1", max_turns=30, verbose=False)

    assert _end(store)["reason"] == "goal_met"
    # stopped on the third vote, not at the turn cap
    assert len([e for e in store.tail("s1") if e["type"] == "vote_done"]) == 3


def test_one_holdout_keeps_the_village_alive(store, season):
    agents = [_agent(Script(_call("vote_done", {"reason": "done"})), "a"),
              _agent(Script(_call("vote_done", {"reason": "done"})), "b"),
              _agent(Script(_call("send_chat", {"message": "not yet"}),
                            _call("end_turn", {"summary": "spoke"})), "c")]
    run_session(agents, season, store, SpendGuard(1.0), "s1", max_turns=9, verbose=False)

    assert _end(store)["reason"] == "turn_cap"      # unanimity, not a majority


def test_an_edit_cancels_the_votes_cast_before_it(store, season):
    """The correction to the spec: edit_file is how the artifact actually changes.

    In the live run 34 of 35 changes were edit_file and one was write_file, so a
    rule that only watched write_file would let a vote survive a rewritten section.
    """
    store.append("s1", "a", "vote_done", {"reason": "done"})
    store.append("s1", "b", "vote_done", {"reason": "done"})
    assert store.standing_votes("s1") == {"a", "b"}

    store.append("s1", "b", "action",
                 {"name": "edit_file", "arguments": {"path": "brief.md", "section": "Evidence"}})
    assert store.standing_votes("s1") == set()

    store.append("s1", "a", "vote_done", {"reason": "still done"})
    assert store.standing_votes("s1") == {"a"}


def test_chat_does_not_cancel_a_vote(store, season):
    """67 messages agreeing the brief was finished must not keep a village alive."""
    store.append("s1", "a", "vote_done", {"reason": "done"})
    store.append("s1", "b", "chat", {"message": "I agree, it is complete"})
    store.append("s1", "b", "action", {"name": "read_file", "arguments": {"path": "brief.md"}})

    assert store.standing_votes("s1") == {"a"}


# --- the backstop --------------------------------------------------------

def test_an_idle_village_stalls_without_anyone_voting(store, season):
    season.max_idle_rounds = 2
    talkers = [_agent(Script(_call("send_chat", {"message": "looks done to me"}),
                             _call("end_turn", {"summary": "said so"})), n)
               for n in ("a", "b")]
    run_session(talkers, season, store, SpendGuard(1.0), "s1", max_turns=30, verbose=False)

    assert _end(store)["reason"] == "stalled"


def test_reading_the_brief_is_not_work(store, season):
    """The observed idle loop was re-reading a finished file, so read_file is exempt."""
    season.max_idle_rounds = 2
    readers = [_agent(Script(_call("write_file", {"path": "brief.md", "text": "x"}),
                             _call("end_turn", {"summary": "wrote"}),
                             _call("send_chat", {"message": "re-read it, still fine"}),
                             _call("end_turn", {"summary": "checked"})), n)
               for n in ("a", "b")]
    run_session(readers, season, store, SpendGuard(1.0), "s1", max_turns=30, verbose=False)

    assert _end(store)["reason"] == "stalled"


def test_working_villagers_are_never_called_stalled(store, season):
    season.max_idle_rounds = 1
    workers = [_agent(Script(_call("edit_file", {"path": "brief.md", "section": "Evidence",
                                                 "text": "more"}),
                             _call("end_turn", {"summary": "edited"}), cycle=True), n)
               for n in ("a", "b")]
    run_session(workers, season, store, SpendGuard(1.0), "s1", max_turns=6, verbose=False)

    assert _end(store)["reason"] == "turn_cap"


# --- the queue -----------------------------------------------------------

def test_a_finished_goal_hands_over_to_the_next_one(store, season):
    season.goals = ["write the brief", "now critique it"]
    voters = [_agent(Script(_call("vote_done", {"reason": "done"})), n) for n in ("a", "b")]
    run_session(voters, season, store, SpendGuard(1.0), "s1", max_turns=20, verbose=False)

    advanced = [e["payload"] for e in store.tail("s1")
                if e["payload"].get("kind") == "goal_advanced"]
    assert [a["goal"] for a in advanced] == ["now critique it"]
    assert _end(store)["reason"] == "goal_met"      # ends on the last goal, not the first


def test_a_repeating_season_starts_a_new_round_instead_of_ending(store, season):
    """One goal, run again and again on a fresh file - the rolling season."""
    season.goals = ["Round {round}: write {file}"]
    season.repeat = True
    voters = [_agent(Script(_call("vote_done", {"reason": "done"})), n) for n in ("a", "b")]
    run_session(voters, season, store, SpendGuard(1.0), "s1", max_turns=12, verbose=False)

    advanced = [e["payload"] for e in store.tail("s1")
                if e["payload"].get("kind") == "goal_advanced"]
    # every pair of votes opens the next round, numbered from 2 and never repeating
    assert [a["file"] for a in advanced] == [f"brief-{n}.md"
                                             for n in range(2, len(advanced) + 2)]
    assert advanced[0]["goal"] == "Round 2: write brief-2.md"
    assert len(advanced) == 6                       # 12 turns, 2 voters, one round each
    # never runs out of goals, so it stops on the budget the operator set
    assert _end(store)["reason"] == "turn_cap"


def test_a_finite_queue_still_ends_when_it_runs_out(store, season):
    season.goals = ["one", "two"]
    season.repeat = False
    voters = [_agent(Script(_call("vote_done", {"reason": "done"})), n) for n in ("a", "b")]
    run_session(voters, season, store, SpendGuard(1.0), "s1", max_turns=20, verbose=False)

    assert _end(store)["reason"] == "goal_met"


def test_advancing_clears_the_votes_that_finished_the_last_goal(store, season):
    store.append("s1", "a", "vote_done", {"reason": "done"})
    store.append("s1", None, "system", {"kind": "goal_advanced", "index": 1, "goal": "next"})

    assert store.standing_votes("s1") == set()
    assert store.goals_advanced("s1") == 1
