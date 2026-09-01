"""Tests for the parts that must not fail silently.

Pin down _post's retry rule against a fake `requests.post`, so nothing costs
money and nothing sleeps for real:

  - a transient status (429/5xx) retries, and a success after a retry works
  - retries are capped at 3 attempts, then LLMError
  - a permanent status (4xx outside 429) fails after exactly one call
  - malformed tool-call JSON is a model mistake, not a transport failure —
    it comes back as parse_error and is never retried

The last one is the interesting test: it's the distinction — transport
failure vs. model mistake — that the whole module is organised around.
"""

import json

import pytest

from village.llm import LLMError, chat


class FakeResponse:
    """Just enough of requests.Response for _post to work with."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload


def _success_payload(arguments="{}"):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
    }


@pytest.fixture(autouse=True)
def _no_sleep_no_missing_key(monkeypatch):
    # A retry test sleeps for real (2**attempt seconds) unless this is patched out.
    monkeypatch.setattr("village.llm.time.sleep", lambda *_: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def test_retry_then_success(monkeypatch):
    responses = [FakeResponse(429, text="rate limited"), FakeResponse(200, _success_payload())]
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr("village.llm.requests.post", fake_post)

    result = chat("some/model", [{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

    assert len(calls) == 2
    assert result.tool_calls[0]["name"] == "web_search"


def test_retry_exhausted_raises(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(429, text="rate limited")

    monkeypatch.setattr("village.llm.requests.post", fake_post)

    with pytest.raises(LLMError):
        chat("some/model", [{"role": "user", "content": "hi"}])

    assert len(calls) == 3


def test_permanent_error_fails_fast(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(400, text="bad request: unknown model id")

    monkeypatch.setattr("village.llm.requests.post", fake_post)

    with pytest.raises(LLMError):
        chat("some/model", [{"role": "user", "content": "hi"}])

    assert len(calls) == 1


def test_malformed_tool_json_is_not_retried(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(200, _success_payload(arguments="{not json"))

    monkeypatch.setattr("village.llm.requests.post", fake_post)

    result = chat("some/model", [{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

    assert len(calls) == 1
    assert result.tool_calls[0]["arguments"] == {}
    assert result.tool_calls[0]["parse_error"] is not None
