"""One villager's turn. The loop the whole project exists to demonstrate.

    build prompt -> call model -> execute tool -> feed the observation back
                 -> repeat until end_turn or the step cap

Everything else in this repo is arrangement around this function.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import memory, tools
from .llm import LLMError

COMPACTION_MAX_TOKENS = 400


@dataclass
class Agent:
    name: str
    model: str
    persona: str
    tools: list[str]
    chat_fn: Callable[..., Any]                 # village.llm.chat, or a fake with the
    temperature: float = 0.7                    # same signature - that seam is what
    max_tokens: int = 900                       # makes the offline suite possible
    reasoning: dict | None = None
    others: list[str] = field(default_factory=list)

    def take_turn(self, store, season, spend_guard, session_id: str,
                  runs_dir: str = "runs", max_steps: int = 6) -> tools.ToolContext:
        ctx = tools.ToolContext(agent=self.name, runs_dir=runs_dir,
                                session_id=session_id, store=store)
        messages = memory.build_context(store, self, season, session_id, self.others)
        schemas = tools.schemas_for(self.tools)
        nudged = False

        for _ in range(max_steps):
            try:
                response = self.chat_fn(self.model, messages, schemas,
                                        self.temperature, self.max_tokens, self.reasoning)
            except LLMError as exc:
                # The provider is the one thing a villager cannot route around, and
                # this is the only call in the turn that spends money. D13 keeps a
                # tool failure from killing a paid run; the same has to hold here,
                # or one 502 at turn 100 throws the whole session away.
                store.append(session_id, self.name, "system",
                             {"kind": "provider_error", "text": str(exc)[:500]})
                ctx.provider_error = str(exc)[:500]
                ctx.turn_over = True
                return ctx

            spend_guard.add(response.usd, self.model)
            store.append(session_id, self.name, "thought", {
                "text": response.text,
                "finish_reason": response.finish_reason,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "reasoning_tokens": response.reasoning_tokens,
                "usd": response.usd,
            })

            if not response.tool_calls:
                # Prose with no tool call changes nothing. Nudge once, then stop:
                # a villager that will not act should not consume the whole session.
                if nudged:
                    store.append(session_id, self.name, "system",
                                 {"kind": "no_tool_call",
                                  "text": f"{self.name} did not act after a nudge"})
                    ctx.turn_over = True
                    return ctx
                nudged = True
                messages.append({"role": "assistant", "content": response.text or ""})
                messages.append({"role": "user", "content":
                                 "You did not call a tool, so nothing happened. Call one "
                                 "now, or call end_turn."})
                continue

            messages.append(self._assistant_message(response))

            for call in response.tool_calls:
                observation = self._run(call, ctx, session_id, store)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": observation})

            if ctx.turn_over:
                return ctx

        store.append(session_id, self.name, "system",
                     {"kind": "step_cap_reached",
                      "text": f"{self.name} used all {max_steps} steps without ending its turn"})
        ctx.turn_over = True
        return ctx

    def compact(self, store, season, spend_guard, session_id: str) -> None:
        """Rewrite this agent's memory into one note that supersedes the rest.

        A plain completion, not a tool call: this call has to produce text. Failure
        is logged and swallowed - losing a summary must not end a paid session.
        """
        messages = memory.build_compaction_messages(store, self, season, session_id)
        try:
            response = self.chat_fn(self.model, messages, [], self.temperature,
                                    COMPACTION_MAX_TOKENS, self.reasoning)
        except LLMError as exc:
            store.append(session_id, self.name, "system",
                         {"kind": "compaction_failed", "text": str(exc)})
            return

        spend_guard.add(response.usd, self.model)
        # A thought without prose, so the cost pill and the eval queries count this
        # call like any other rather than reading a session as cheaper than it was.
        store.append(session_id, self.name, "thought", {
            "text": None,
            "kind": "compaction",
            "finish_reason": response.finish_reason,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "reasoning_tokens": response.reasoning_tokens,
            "usd": response.usd,
        })
        if response.text:
            store.append(session_id, self.name, "compaction", {"text": response.text})
        else:
            store.append(session_id, self.name, "system",
                         {"kind": "compaction_failed", "text": "model returned no text"})

    def _run(self, call: dict, ctx, session_id: str, store) -> str:
        if call.get("parse_error"):
            # The model wrote bad JSON. That is its mistake to see and correct, so it
            # comes back as an observation - never as a retry, which pays twice for
            # the same input, and never as a crash.
            store.append(session_id, self.name, "result",
                         {"name": call.get("name"), "text": f"parse_error: {call['parse_error']}"})
            return (f"Your arguments for {call.get('name')} were not valid JSON "
                    f"({call['parse_error']}). Call the tool again with valid JSON.")

        store.append(session_id, self.name, "action",
                     {"name": call["name"], "arguments": call.get("arguments") or {}})
        observation = tools.execute(call["name"], call.get("arguments") or {}, ctx)
        store.append(session_id, self.name, "result",
                     {"name": call["name"], "text": observation[:2000]})
        return observation

    @staticmethod
    def _assistant_message(response) -> dict:
        """Rebuilt from normalized fields, not echoed from the provider payload.

        The raw message carries provider-specific extras that other providers reject
        on the way back in, and the whole promise of llm.py is that the rest of the
        system stays provider-neutral. Arguments go back as a JSON string because
        that is what the wire format expects.
        """
        return {
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"],
                                         "arguments": json.dumps(c.get("arguments") or {})}}
                           for c in response.tool_calls],
        }
