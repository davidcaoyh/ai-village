"""Check everything a live session depends on, before spending money on one.

Run this once after adding your key, and again any time you change a model id:

    python -m scripts.preflight            # checks, plus one tiny real call per model
    python -m scripts.preflight --dry      # no paid calls: catalogue and config only

Why it exists. A 120-turn session is the worst possible place to discover that
one model id has a typo, that a provider silently ignores `tools`, or that the
account balance is zero. Each of those failures looks the same from inside the
loop - an agent that talks but never acts - and each costs a session's worth of
turns to notice. The fix is to make the assumptions explicit and test them for
about a tenth of a cent.

Six checks, in the order that a failure in one makes the next meaningless:

  1. key present                 - config would fail anyway, but with a worse message
  2. account has credit          - a 402 mid-session ends the run at an arbitrary turn
  3. each model id resolves      - a typo is a 404 on turn 1 for that villager only
  4. each model advertises tools - a model without tool support never acts, only talks
  5. each model actually calls a tool when asked  (the only check that costs money)
  6. a search backend answers    - the village's only route to evidence

Exit status is 0 only when every check passes, so this can gate a deploy.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys

import requests
from dotenv import load_dotenv

from village.config import load_agents

_API = "https://openrouter.ai/api/v1"
_TIMEOUT = 45

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "


def _line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label:<34} {detail}")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"}


# --- checks ---------------------------------------------------------------

def check_key() -> bool:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        _line(BAD, "OPENROUTER_API_KEY", "not set - copy .env.example to .env")
        return False
    _line(OK, "OPENROUTER_API_KEY", f"{key[:12]}...{key[-4:]}")
    return True


def check_credits() -> bool:
    """GET /key reports the limit and usage of *this key*, not of the account.

    That distinction cost a live session on Sep 1, 2026: with no per-key limit
    set, this check warned and passed while the balance was nearly empty, and the
    run died at turn 10 on a 402. OpenRouter exposes no account-balance endpoint,
    so a key with no limit is genuinely unknowable from here - say so rather than
    imply the check covered it.

    VILLAGE_MAX_USD is the code rail. A per-key limit is the provider rail, and
    the point of two is that a bug in this repo cannot disable the other one.
    """
    try:
        r = requests.get(f"{_API}/key", headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        _line(BAD, "account credit", f"could not reach OpenRouter: {exc}")
        return False
    if r.status_code != 200:
        _line(BAD, "account credit", f"HTTP {r.status_code}: {r.text[:120]}")
        return False

    d = (r.json() or {}).get("data") or {}
    limit, usage, remaining = d.get("limit"), d.get("usage"), d.get("limit_remaining")
    if limit is None:
        _line(WARN, "key credit", f"no per-key limit set (this key has spent "
                                  f"${usage or 0:.4f}). Your balance cannot be read from "
                                  "the API - check the dashboard, or set a per-key limit "
                                  "so this becomes a real check. A 402 mid-session ends "
                                  "the run at whatever turn it reaches.")
        return True
    left = float(remaining) if remaining is not None else float(limit) - float(usage or 0)
    if left <= 0.25:
        _line(BAD, "key credit", f"${left:.2f} left - top up before running")
        return False
    _line(OK, "key credit", f"${left:.2f} left of ${float(limit):.2f}")
    return True


def _suggest(model: str) -> str:
    """Close ids from the catalogue, so a typo is one edit away from fixed.

    A bare 404 sends you to the website to guess. The vendor prefix is usually
    the part that is right, so candidates are ranked within it first.
    """
    try:
        r = requests.get(f"{_API}/models", timeout=_TIMEOUT)
        ids = [m["id"] for m in (r.json() or {}).get("data") or []]
    except (requests.RequestException, ValueError, KeyError):
        return ""
    vendor = model.split("/")[0] + "/"
    pool = [i for i in ids if i.startswith(vendor)] or ids
    near = difflib.get_close_matches(model, pool, n=3, cutoff=0.5)
    return f"  did you mean: {', '.join(near)}" if near else ""


def check_model(model: str) -> tuple[bool, dict]:
    """Does this exact id exist, and does any endpoint for it accept `tools`?

    Checked against /models/<id>/endpoints rather than the flat /models list
    because that is where `supported_parameters` lives, per provider. A model
    can exist and still be useless here: without tool support a villager can
    only produce prose, which this system correctly treats as doing nothing.
    """
    try:
        r = requests.get(f"{_API}/models/{model}/endpoints", headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        _line(BAD, f"model {model}", str(exc)[:100])
        return False, {}
    if r.status_code != 200:
        _line(BAD, f"model {model}", f"HTTP {r.status_code} - id does not resolve")
        if r.status_code == 404 and (hint := _suggest(model)):
            print(hint)
        return False, {}

    data = (r.json() or {}).get("data") or {}
    endpoints = data.get("endpoints") or []
    with_tools = [e for e in endpoints if "tools" in (e.get("supported_parameters") or [])]
    if not with_tools:
        _line(BAD, f"model {model}", "no endpoint advertises tool calling")
        return False, data

    pricing = (with_tools[0].get("pricing") or {})
    p_in = float(pricing.get("prompt") or 0) * 1e6
    p_out = float(pricing.get("completion") or 0) * 1e6
    _line(OK, f"model {model}", f"${p_in:.3f}/M in, ${p_out:.3f}/M out, "
                                f"{len(with_tools)}/{len(endpoints)} endpoints w/ tools")
    return True, {"in": p_in, "out": p_out}


def check_live_tool_call(model: str) -> bool:
    """The only check that costs money, and the only one that proves anything.

    Everything above is the catalogue's claim about the model. This is the model
    answering. Roughly 60 prompt tokens and a handful of completion tokens - on
    the priciest model in this cast that is well under a tenth of a cent.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Call ping with the word 'hello'. Nothing else."}],
        "tools": [{"type": "function", "function": {
            "name": "ping",
            "description": "Reply with a word.",
            "parameters": {"type": "object",
                           "properties": {"word": {"type": "string"}},
                           "required": ["word"]}}}],
        "max_tokens": 200,
        "temperature": 0,
    }
    try:
        r = requests.post(f"{_API}/chat/completions", headers=_headers(),
                          json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        _line(BAD, f"live call {model}", str(exc)[:100])
        return False
    if r.status_code != 200:
        _line(BAD, f"live call {model}", f"HTTP {r.status_code}: {r.text[:140]}")
        return False

    body = r.json()
    choice = (body.get("choices") or [{}])[0]
    calls = (choice.get("message") or {}).get("tool_calls") or []
    usage = body.get("usage") or {}
    cost = float(usage.get("cost") or 0)

    if not calls:
        # The failure this whole script exists to catch: the model answered, but
        # with prose. In a session that villager would be nudged once and then
        # abandon its turn, every turn, forever - while still being billed.
        finish = choice.get("finish_reason")
        _line(BAD, f"live call {model}",
              f"returned no tool call (finish_reason={finish}). "
              f"If finish_reason=length, raise max_tokens or set reasoning effort low.")
        return False

    if cost == 0:
        _line(WARN, f"live call {model}", "usage.cost was 0 - the spend guard would be blind")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    _line(OK, f"live call {model}",
          f"called {calls[0]['function']['name']}, ${cost:.6f}, "
          f"{usage.get('completion_tokens')} completion tokens ({reasoning} reasoning)")
    return True


def check_search() -> bool:
    """The village's only route to evidence.

    Reported in two parts on purpose. `web_search` falls back to DuckDuckGo when
    Tavily fails (D30), which is right for a running session and wrong for a
    preflight: it would hide a dead Tavily key behind a passing check. So the
    key is tested directly, and the tool is tested separately.
    """
    from village.tools import ToolContext, _search_tavily, _tavily_key, execute

    raw = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if not raw:
        _line(WARN, "TAVILY_API_KEY", "not set - falling back to keyless DuckDuckGo")
    elif not _tavily_key():
        _line(WARN, "TAVILY_API_KEY", f"{raw!r} is the .env.example placeholder, "
                                      "not a key - treated as unset")
    else:
        try:
            hits = _search_tavily("randomized controlled trial")
            _line(OK, "TAVILY_API_KEY", f"{len(hits)} results")
        except Exception as exc:  # noqa: BLE001
            _line(WARN, "TAVILY_API_KEY", f"rejected ({str(exc)[:70]}) - "
                                          "searches will fall back to DuckDuckGo")

    out = execute("web_search", {"query": "randomized controlled trial definition"},
                  ToolContext(agent="preflight", runs_dir="runs"))
    if out.startswith("Error") or "No results" in out:
        _line(BAD, "web_search", out[:110])
        return False
    _line(OK, "web_search", f"{len(out)} chars returned")

    if not _tavily_key():
        _line(WARN, "search backend", "keyless DuckDuckGo is rate-limited; a 120-turn "
                                      "session will hit it. A free Tavily key is worth it.")
    return True


def estimate(prices: dict[str, dict], turns: int) -> None:
    """A number to sanity-check VILLAGE_MAX_USD against, not a promise.

    Assumes ~2,500 prompt tokens and ~250 completion tokens per model call, and
    ~2.5 calls per turn (act, act, end_turn), plus one compaction call every 20
    turns. Prompt size is the term that grows and the one the rolling window (D6)
    exists to bound.
    """
    if not prices:
        return
    per_agent_turns = turns / len(prices)
    total = 0.0
    for _model, p in prices.items():
        calls = per_agent_turns * 2.55          # 2.5 + one compaction per 20 turns
        total += calls * (2500 * p["in"] + 250 * p["out"]) / 1e6
    print(f"\nEstimated cost of a {turns}-turn session: ~${total:.2f} "
          f"(VILLAGE_MAX_USD is ${os.environ.get('VILLAGE_MAX_USD', '2.00')})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify a live run will work before paying for one.")
    ap.add_argument("--agents", default="configs/agents.yaml")
    ap.add_argument("--dry", action="store_true", help="skip the paid one-token calls")
    ap.add_argument("--turns", type=int, default=120, help="turn count to estimate cost for")
    args = ap.parse_args()

    load_dotenv()
    print("preflight: checking everything a live session depends on\n")

    passed = check_key()
    if not passed:
        sys.exit(1)
    passed &= check_credits()

    configs = load_agents(args.agents)
    prices: dict[str, dict] = {}
    for cfg in configs:
        ok, price = check_model(cfg.model)
        passed &= ok
        if ok:
            prices[cfg.model] = price

    if not args.dry:
        for model in list(prices):
            passed &= check_live_tool_call(model)

    passed &= check_search()
    estimate(prices, args.turns)

    print("\n" + ("all checks passed - safe to run a session"
                  if passed else "something above needs fixing before a live run"))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
