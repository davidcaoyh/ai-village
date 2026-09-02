"""A scripted stand-in for a real model, so the whole pipeline is provable offline.

Why this exists as a committed module rather than a throwaway script: every
other part of this system - the event log, the rolling window, round-robin
scheduling, the WebSocket push, the spectator page, the kill switch - is
independent of whether the text came from a provider or from here. Being able
to demonstrate all of it with `--fake`, on a laptop with no API key and no
balance, is what makes the parts that *do* cost money the only thing you have
to debug when they misbehave. It is also how the UI gets developed without
paying for a session per CSS change.

What it deliberately does NOT do: call `web_search` or `fetch_url`. A fake model
driving real network tools would make an offline run depend on the network,
which defeats the purpose. So the offline cast writes, talks, and remembers -
enough to exercise every code path that is not the provider itself.

This is a test double promoted to a first-class module (D27). The alternative -
a `if FAKE_MODE:` branch inside llm.py - would put test-only code on the path
that spends real money, which is exactly the code you least want to be clever.
"""

from __future__ import annotations

import itertools
import random
import re
from typing import Any

from village.llm import LLMResponse

# A turn's worth of behaviour: say something, record something, occasionally
# produce the deliverable, then hand over. Cycled per agent so the transcript
# reads like four villagers rather than one villager four times.
_LINES = [
    "I'll take the confounder list - starting with income and prior education.",
    "Cross-checked the two effect sizes and they disagree; flagging it before we cite either.",
    "Draft section two is in artifacts/brief.md. Someone should challenge the causal claim.",
    "Correlation here is strong but the sampling frame is a volunteer panel. Weak identification.",
    "Claiming the intro. Will keep it to three sentences and no unsourced numbers.",
    "That study is observational. We can report the association, not the effect.",
]

_NOTES = [
    "Volunteer-panel sampling means selection on the outcome; note it as an unresolved confounder.",
    "Two of our sources cite the same 2019 dataset - count them as one, not two.",
    "Agreed split: intro / evidence / confounders / limits.",
]

# One section each, so an offline run exercises the same read -> edit -> hand over
# path a live one uses, and ends with a real sectioned brief.md on disk.
_SECTIONS = [
    ("Question", "Does insufficient sleep cause reduced cognitive performance in adults?"),
    ("Answer", "Yes for vigilance and working memory, on experimental evidence."),
    ("Evidence", "Randomised restriction studies show dose-dependent decline."),
    ("Correlation vs causation", "Survey correlations cannot separate cause from selection."),
    ("Confounders we cannot rule out", "- selection into the sample\n- caffeine use"),
    ("Sources", "- (offline demo: a live run cites real urls here)"),
]


_COMPACTED = (
    "(compacted memory) Brief split four ways; I own the confounder section. Two "
    "effect sizes still disagree and neither is cited yet. Volunteer-panel sampling "
    "is the open threat to identification."
)


# A queued season names a different file in each goal (brief.md, brief-2.md...).
# The fake finds it the way a real villager would - by reading the goal - so an
# offline run exercises the queue rather than rewriting goal one three times.
_TARGET = re.compile(r"brief(?:-\d+)?\.md")


class ScriptedModel:
    """Callable with the same signature as `village.llm.chat`.

    Same signature is the requirement, not same behaviour: agent.py accepts any
    `chat_fn`, which is the seam that makes both this and the test suite
    possible. If this class ever needs a signature change, so does every test.
    """

    def __init__(self, agent_name: str, seed: int | None = None) -> None:
        self.agent = agent_name
        self.rng = random.Random(seed if seed is not None else hash(agent_name) & 0xFFFF)
        self.step = itertools.count()
        self.edits = 0
        self.path = "brief.md"

    def __call__(self, model: str, messages: list[dict[str, Any]], tools=None,
                 temperature: float = 0.7, max_tokens: int = 900,
                 reasoning=None) -> LLMResponse:
        system = (messages[0].get("content") or "") if messages else ""
        found = _TARGET.search(system)
        if found and found.group(0) != self.path:
            self.path, self.edits = found.group(0), 0     # new goal, new file, start over

        if not tools:
            # No tools means the compaction call, so offline runs exercise it too.
            return self._response(text=_COMPACTED, calls=[], finish_reason="stop",
                                  messages=messages, usd=0.0008)

        allowed = {t["function"]["name"] for t in (tools or [])}
        n = next(self.step)

        # Three model calls per turn: act, act, end. Bounded on purpose - an
        # agent that never calls end_turn is a real failure mode, and the fake
        # cast should not be the thing that exercises the step cap.
        # Once the brief has all its sections there is nothing left to do, and the
        # offline cast votes rather than inventing verification work - the same
        # behaviour the prompt asks of a live one.
        if self.edits >= len(_SECTIONS) and "vote_done" in allowed:
            return self._response(text=None, messages=messages, finish_reason="tool_calls",
                                  calls=[{"id": f"call_{n}", "name": "vote_done",
                                          "arguments": {"reason": "every section is written"},
                                          "parse_error": None}])

        phase = n % 4
        if phase == 0 and "send_chat" in allowed:
            call = ("send_chat", {"message": self.rng.choice(_LINES)})
        elif phase == 1 and "read_file" in allowed:
            call = ("read_file", {"path": self.path})
        elif phase == 2 and "edit_file" in allowed:
            heading, text = _SECTIONS[self.edits % len(_SECTIONS)]
            self.edits += 1
            call = ("edit_file", {"path": self.path, "section": heading, "text": text})
        elif phase == 2 and "write_note" in allowed:
            call = ("write_note", {"text": self.rng.choice(_NOTES)})
        else:
            call = ("end_turn", {"summary": f"{self.agent}: took one step toward the brief"})

        if call[0] not in allowed:
            call = ("end_turn", {"summary": "nothing available to do"})

        return self._response(
            text=None, messages=messages,
            calls=[{"id": f"call_{n}", "name": call[0],
                    "arguments": call[1], "parse_error": None}])

    @staticmethod
    def _response(text, calls, messages, finish_reason="tool_calls", usd=0.0012):
        # A plausible non-zero price so the cost counter, the spend guard and the
        # UI's cost pill are all exercised offline too. Nothing is spent.
        return LLMResponse(
            text=text, tool_calls=calls, finish_reason=finish_reason,
            prompt_tokens=sum(len(m.get("content") or "") for m in messages) // 4,
            completion_tokens=40, reasoning_tokens=0, usd=usd, raw={"fake": True},
        )
