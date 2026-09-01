"""The only module that knows a model provider exists.

The line this module draws, and that the rest of the system depends on: a
transport failure (429, 502, a dropped socket) is retried, because waiting might
help. A model mistake (malformed tool JSON) is handed upward as `parse_error` and
never retried, because the same input produces the same mistake and you pay twice.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_TIMEOUT = 60                                  # a hung socket kills an unattended run
_MAX_ATTEMPTS = 3
_RETRYABLE = {429, 500, 502, 503, 504}         # 400/401/402/404 are permanent: your bug,
                                               # key, balance or model id. Waiting never
                                               # fixes any of those.
_SUMMARY_LIMIT = 2000                          # chars of reasoning summary kept in raw


class LLMError(RuntimeError):
    """Transport or provider failure. Never raised for model content problems."""


class BudgetExceeded(RuntimeError):
    """SpendGuard tripped. The orchestrator catches this and ends the session cleanly."""


@dataclass
class LLMResponse:
    text: str | None                   # assistant prose, if any
    tool_calls: list[dict[str, Any]]   # [{"id","name","arguments","parse_error"}]
    finish_reason: str | None          # stop | tool_calls | length - worth logging
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    usd: float                         # cost of this call, as OpenRouter reports it
    raw: dict[str, Any]                # provider payload, reasoning blobs stripped


def _headers() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise LLMError("OPENROUTER_API_KEY is not set. Put it in .env")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "AI Village",
    }


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    """Transport only: retries, timeouts, HTTP status. Knows nothing about tools."""
    last_error: Exception | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.post(_ENDPOINT, headers=_headers(), json=payload,
                                     timeout=_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc                                  # retryable: nothing arrived
        else:
            if response.status_code == 200:
                body = response.json()
                if "error" in body:                            # a 200 can still carry one
                    raise LLMError(f"provider error: {body['error']}")
                return body

            detail = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code not in _RETRYABLE:
                raise LLMError(detail)
            last_error = LLMError(detail)

        # Never sleep after the final attempt. Jitter because four villagers hitting
        # one rate limit would otherwise retry in lockstep and collide again.
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(2**attempt + random.uniform(0, 0.3))

    raise LLMError(f"{_MAX_ATTEMPTS} attempts failed for {payload.get('model')}") from last_error


def _normalize_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Provider shape to ours. The anti-corruption layer: wire-format quirks stop here."""
    calls: list[dict[str, Any]] = []

    for tc in raw_calls or []:
        fn = tc.get("function") or {}      # .get() throughout: four providers, and not
        raw_args = fn.get("arguments")     # all of them populate every field

        if isinstance(raw_args, dict):
            arguments, parse_error = raw_args, None
        else:
            try:
                arguments, parse_error = json.loads(raw_args or "{}"), None
            except json.JSONDecodeError as exc:
                arguments = {}
                parse_error = f"{exc} | raw: {str(raw_args)[:200]}"

        calls.append({"id": tc.get("id"), "name": fn.get("name"),
                      "arguments": arguments, "parse_error": parse_error})

    return calls


def _strip_reasoning(body: dict[str, Any]) -> dict[str, Any]:
    """Drop encrypted chain-of-thought before the payload reaches the event log.

    reasoning_details[].data is thousands of characters of encrypted blob. Measured
    on one real turn it was 68% of the logged payload, and it is unreadable to you,
    to the UI and to any later analysis. The human-readable summary is kept.
    """
    for choice in body.get("choices") or []:
        message = choice.get("message") or {}
        details = message.pop("reasoning_details", None)
        if details:
            summaries = [d.get("summary") for d in details
                         if isinstance(d, dict) and d.get("summary")]
            if summaries:
                message["reasoning_summary"] = " ".join(summaries)[:_SUMMARY_LIMIT]
    return body


def chat(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 900,
    reasoning: dict[str, Any] | None = None,
) -> LLMResponse:
    """One model call. The only function in this codebase that knows a network exists."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools      # omit entirely when empty: some providers 400 on []
    if reasoning:
        payload["reasoning"] = reasoning

    data = _strip_reasoning(_post(payload))

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}

    # OpenRouter reports `cost` on every response, so there is no local price table to
    # drift the day a provider reprices. usd == 0.0 alongside nonzero completion tokens
    # means the field went missing: worth investigating rather than trusting.
    return LLMResponse(
        text=message.get("content"),
        tool_calls=_normalize_tool_calls(message.get("tool_calls")),
        finish_reason=choice.get("finish_reason"),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        usd=float(usage.get("cost") or 0.0),
        raw=data,
    )


@dataclass
class SpendGuard:
    """Hard money rail, behind the account-level cap set in the OpenRouter dashboard.

    Single-threaded turn loop, so no locking. That is a property of the scheduler,
    not an accident: revisit it the day agents run concurrently.
    """

    max_usd: float
    spent: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)

    def add(self, usd: float, model: str = "") -> None:
        self.spent += usd
        if model:
            self.by_model[model] = self.by_model.get(model, 0.0) + usd
        if self.spent > self.max_usd:
            raise BudgetExceeded(f"${self.spent:.4f} spent > ${self.max_usd:.2f} cap")

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_usd - self.spent)
