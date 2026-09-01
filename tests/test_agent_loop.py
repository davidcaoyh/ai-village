"""End-to-end tests for the turn loop and the scheduler, with a fake model.

Everything here runs offline and costs nothing. That is the point: a test suite
that needs an API key is a test suite you stop running, and an agent system
whose loop can only be tested by spending money is one you cannot refactor.

The fake model is scripted - it returns a fixed sequence of tool calls - which
lets us assert on the exact behaviours that are hard to eyeball in a live run:
the private-reasoning boundary, self-correction after a bad tool call, and every
one of the four stop conditions.
"""

from __future__ import annotations

import copy

import pytest

from village.agent import Agent
from village.config import SeasonConfig
from village.llm import LLMResponse, SpendGuard
from village.orchestrator import run_session
from village.store import Store


def _resp(tool=None, args=None, text=None, parse_error=None, usd=0.001):
    calls = []
    if tool:
        calls = [{"id": "c1", "name": tool, "arguments": args or {}, "parse_error": parse_error}]
    return LLMResponse(text=text, tool_calls=calls,
                       finish_reason="tool_calls" if tool else "stop",
                       prompt_tokens=100, completion_tokens=20, reasoning_tokens=0,
                       usd=usd, raw={})


class FakeModel:
    """Replays a scripted list of responses and records the prompts it saw."""

    def __init__(self, script):
        self.script, self.seen, self.i = list(script), [], 0

    def __call__(self, model, messages, tools=None, temperature=0.7,
                 max_tokens=900, reasoning=None):
        # deepcopy, not a reference: agent.take_turn appends to this same list
        # object as the turn proceeds, so storing the reference would make every
        # recorded prompt show the *final* state. Aliasing like this is the
        # classic way a test passes while asserting nothing.
        self.seen.append(copy.deepcopy(messages))
        r = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return r


@pytest.fixture
def season():
    return SeasonConfig(season_id="test", goal="Test the village.", turns_per_session=4,
                        seconds_between_turns=0, context_window_events=30,
                        compaction_every_turns=0, constraints=["do not contact real people"])


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "v.db"))


def _agent(fake, name="claude", tools=("send_chat", "write_note", "end_turn")):
    return Agent(name=name, model="fake/model", persona="You are a test villager.",
                 tools=list(tools), chat_fn=fake)


# --- the loop -------------------------------------------------------------

def test_turn_runs_tools_then_ends(store, season):
    fake = FakeModel([
        _resp("send_chat", {"message": "starting on sources"}),
        _resp("end_turn", {"summary": "said hello"}),
    ])
    ctx = _agent(fake).take_turn(store, season, SpendGuard(1.0), "s1")

    assert ctx.turn_over and ctx.turn_summary == "said hello"
    types = [e["type"] for e in store.tail("s1")]
    assert types.count("thought") == 2      # one per model call
    assert "chat" in types and "action" in types and "result" in types


def test_tool_result_message_carries_tool_call_id(store, season):
    """If this shape is wrong the *next* provider call 400s - and it is invisible
    in a single-step test, which is exactly why it gets its own assertion."""
    fake = FakeModel([_resp("send_chat", {"message": "hi"}), _resp("end_turn", {"summary": "s"})])
    _agent(fake).take_turn(store, season, SpendGuard(1.0), "s1")

    second_prompt = fake.seen[1]
    tool_msgs = [m for m in second_prompt if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"
    assistant = [m for m in second_prompt if m.get("role") == "assistant"][0]
    assert assistant["tool_calls"][0]["function"]["name"] == "send_chat"
    args = assistant["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str)               # JSON string on the wire, not a dict


def test_malformed_json_is_handed_back_not_crashed(store, season):
    fake = FakeModel([
        _resp("send_chat", {}, parse_error="Expecting value: line 1 column 1"),
        _resp("end_turn", {"summary": "recovered"}),
    ])
    ctx = _agent(fake).take_turn(store, season, SpendGuard(1.0), "s1")

    assert ctx.turn_over                       # the turn survived the bad call
    tool_reply = [m for m in fake.seen[1] if m.get("role") == "tool"][0]
    assert "not valid JSON" in tool_reply["content"]


def test_prose_without_tool_call_is_nudged_then_abandoned(store, season):
    fake = FakeModel([_resp(text="I think we should consider several angles.")])
    ctx = _agent(fake).take_turn(store, season, SpendGuard(1.0), "s1")

    assert ctx.turn_over
    assert fake.i == 2                         # nudged exactly once, then gave up
    assert any(e["payload"].get("kind") == "no_tool_call" for e in store.tail("s1"))


def test_step_cap_bounds_a_looping_agent(store, season):
    fake = FakeModel([_resp("write_note", {"text": "again"})])   # never calls end_turn
    _agent(fake).take_turn(store, season, SpendGuard(1.0), "s1", max_steps=3)

    assert fake.i == 3
    assert any(e["payload"].get("kind") == "step_cap_reached" for e in store.tail("s1"))


# --- the visibility rule --------------------------------------------------

def test_agent_never_sees_another_agents_reasoning(store, season):
    store.append("s1", "gpt", "thought", {"text": "SECRET PLAN: fabricate a source"})
    store.append("s1", "gpt", "chat", {"message": "I'll take the first two sources"})

    fake = FakeModel([_resp("end_turn", {"summary": "read the room"})])
    _agent(fake, name="claude").take_turn(store, season, SpendGuard(1.0), "s1")

    prompt = "\n".join(m.get("content") or "" for m in fake.seen[0])
    assert "I'll take the first two sources" in prompt      # public chat: visible
    assert "SECRET PLAN" not in prompt                      # private reasoning: not


# --- stop conditions ------------------------------------------------------

def test_budget_exceeded_ends_session_cleanly(store, season):
    fake = FakeModel([_resp("end_turn", {"summary": "x"}, usd=0.60)])
    run_session([_agent(fake)], season, store, SpendGuard(1.0), "s1", verbose=False)

    kinds = [e["payload"].get("kind") for e in store.tail("s1")]
    assert "budget_exceeded" in kinds
    end = [e for e in store.tail("s1") if e["payload"].get("kind") == "session_end"][0]
    assert end["payload"]["reason"] == "budget_exceeded"


def test_human_kill_switch_stops_before_next_turn(store, season):
    fake = FakeModel([_resp("end_turn", {"summary": "x"})])
    store.request_stop("s1")
    run_session([_agent(fake)], season, store, SpendGuard(1.0), "s1", verbose=False)

    assert fake.i == 0                                       # never called the model
    end = [e for e in store.tail("s1") if e["payload"].get("kind") == "session_end"][0]
    assert end["payload"]["reason"] == "human_stop"


def test_session_always_ends_with_session_end(store, season):
    fake = FakeModel([_resp("end_turn", {"summary": "x"})])
    run_session([_agent(fake)], season, store, SpendGuard(1.0), "s1", max_turns=2, verbose=False)

    assert store.tail("s1")[-1]["payload"]["kind"] == "session_end"
    assert store.tail("s1")[0]["payload"]["kind"] == "session_start"


def test_round_robin_visits_every_agent_in_order(store, season):
    fakes = {n: FakeModel([_resp("end_turn", {"summary": n})]) for n in ("a", "b", "c")}
    agents = [_agent(f, name=n) for n, f in fakes.items()]
    run_session(agents, season, store, SpendGuard(1.0), "s1", max_turns=6, verbose=False)

    order = [e["agent"] for e in store.tail("s1") if e["type"] == "thought"]
    assert order == ["a", "b", "c", "a", "b", "c"]


def test_cost_is_summed_from_the_log(store, season):
    fake = FakeModel([_resp("end_turn", {"summary": "x"}, usd=0.002)])
    run_session([_agent(fake)], season, store, SpendGuard(1.0), "s1", max_turns=3, verbose=False)
    assert store.session_cost("s1") == pytest.approx(0.006)


# --- the offline cast -----------------------------------------------------

def test_scripted_model_matches_the_signature_of_the_real_one():
    """`--fake` is only useful if the seam is exactly the same seam.

    If ScriptedModel and llm.chat ever drift apart, an offline run stops proving
    anything about a live one - which is the whole claim the fake mode makes.
    """
    import inspect

    from village.fake import ScriptedModel
    from village.llm import chat

    real = list(inspect.signature(chat).parameters)
    fake = [p for p in inspect.signature(ScriptedModel.__call__).parameters if p != "self"]
    assert fake == real


def test_fake_session_produces_a_complete_log(store, season):
    """One end-to-end offline session: every villager acts, and the run closes."""
    from village.fake import ScriptedModel

    agents = [Agent(name=n, model="fake/model", persona="p",
                    tools=["send_chat", "write_note", "end_turn"],
                    chat_fn=ScriptedModel(n)) for n in ("claude", "gpt")]
    run_session(agents, season, store, SpendGuard(10.0), "s1", max_turns=6, verbose=False)

    events = store.tail("s1")
    assert events[-1]["payload"]["kind"] == "session_end"
    assert events[-1]["payload"]["reason"] == "turn_cap"
    assert {e["agent"] for e in events if e["type"] == "chat"} == {"claude", "gpt"}
    assert store.session_cost("s1") > 0        # the cost pill has something to show
