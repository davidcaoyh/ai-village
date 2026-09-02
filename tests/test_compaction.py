"""Compaction: the third part of D6, and the rule that keeps a prompt bounded.

Three things are worth pinning, and they are the three that would break silently:
the superseding rule in the store, the shape of the call itself, and the fact
that a failed compaction costs a session nothing.
"""

from __future__ import annotations

import pytest

from village.agent import Agent
from village.config import SeasonConfig
from village.llm import LLMError, LLMResponse, SpendGuard
from village.orchestrator import run_session
from village.store import Store


def _text(text=None, usd=0.001):
    return LLMResponse(text=text, tool_calls=[], finish_reason="stop",
                       prompt_tokens=100, completion_tokens=20, reasoning_tokens=0,
                       usd=usd, raw={})


def _call(tool, args, usd=0.001):
    return LLMResponse(text=None,
                       tool_calls=[{"id": "c1", "name": tool, "arguments": args,
                                    "parse_error": None}],
                       finish_reason="tool_calls", prompt_tokens=100, completion_tokens=20,
                       reasoning_tokens=0, usd=usd, raw={})


class Model:
    """Returns a text response when called with no tools, a tool call otherwise."""

    def __init__(self, summary="compacted", tool="end_turn", raises=False):
        self.summary, self.tool, self.raises = summary, tool, raises
        self.seen = []

    def __call__(self, model, messages, tools=None, temperature=0.7,
                 max_tokens=900, reasoning=None):
        self.seen.append({"tools": tools, "max_tokens": max_tokens, "messages": messages})
        if not tools:
            if self.raises:
                raise LLMError("provider is down")
            return _text(self.summary)
        return _call(self.tool, {"summary": "x"})


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "v.db"))


@pytest.fixture
def season():
    # max_idle_rounds=0: this file is about compaction, and its stub model only
    # calls end_turn, which the backstop correctly reads as an idle village.
    return SeasonConfig(season_id="test", goal="Test the village.", turns_per_session=4,
                        seconds_between_turns=0, context_window_events=30,
                        compaction_every_turns=0, max_idle_rounds=0)


def _agent(model, name="claude"):
    return Agent(name=name, model="fake/model", persona="p",
                 tools=["send_chat", "write_note", "end_turn"], chat_fn=model)


def _note(store, agent, text, session="s1"):
    store.append(session, agent, "action", {"name": "write_note", "arguments": {"text": text}})


# --- stage 1: the store ---------------------------------------------------

def test_compaction_supersedes_earlier_notes(store):
    _note(store, "claude", "note one")
    _note(store, "claude", "note two")
    store.append("s1", "claude", "compaction", {"text": "summary of one and two"})
    _note(store, "claude", "note three")

    assert store.notes_for("claude") == ["summary of one and two", "note three"]


def test_notes_are_unchanged_without_a_compaction(store):
    _note(store, "claude", "note one")
    _note(store, "claude", "note two")
    assert store.notes_for("claude") == ["note one", "note two"]


def test_another_agents_compaction_is_never_in_the_window(store):
    store.append("s1", "gpt", "compaction", {"text": "SECRET SUMMARY"})
    seen = store.recent_for_prompt("s1", "claude", 30)
    assert [e["type"] for e in seen] == []


# --- stage 2: the call ----------------------------------------------------

def test_compact_writes_the_models_text_as_a_compaction_event(store, season):
    model = Model(summary="the state of the brief")
    _agent(model).compact(store, season, SpendGuard(1.0), "s1")

    rows = [e for e in store.tail("s1") if e["type"] == "compaction"]
    assert [r["payload"]["text"] for r in rows] == ["the state of the brief"]
    assert model.seen[0]["tools"] == []          # text, not an action


def test_compaction_cost_reaches_the_guard_and_the_log(store, season):
    guard = SpendGuard(1.0)
    _agent(Model()).compact(store, season, guard, "s1")

    assert guard.spent == pytest.approx(0.001)
    assert store.session_cost("s1") == pytest.approx(0.001)


def test_a_provider_failure_does_not_end_the_session(store, season):
    _agent(Model(raises=True)).compact(store, season, SpendGuard(1.0), "s1")

    kinds = [e["payload"].get("kind") for e in store.tail("s1")]
    assert "compaction_failed" in kinds
    assert not [e for e in store.tail("s1") if e["type"] == "compaction"]


def test_no_text_back_is_also_a_failure(store, season):
    _agent(Model(summary=None)).compact(store, season, SpendGuard(1.0), "s1")

    kinds = [e["payload"].get("kind") for e in store.tail("s1")]
    assert "compaction_failed" in kinds


# --- stage 3: the trigger -------------------------------------------------

def test_orchestrator_compacts_every_n_turns(store, season):
    season.compaction_every_turns = 2
    model = Model()
    run_session([_agent(model)], season, store, SpendGuard(1.0), "s1",
                max_turns=5, verbose=False)

    # turns 0..4, compacting before the third and fifth
    assert len([e for e in store.tail("s1") if e["type"] == "compaction"]) == 2


def test_compaction_is_off_when_the_season_says_zero(store, season):
    run_session([_agent(Model())], season, store, SpendGuard(1.0), "s1",
                max_turns=5, verbose=False)
    assert not [e for e in store.tail("s1") if e["type"] == "compaction"]
