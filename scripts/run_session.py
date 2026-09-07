"""Run one session.

    python -m scripts.run_session                      # live, the whole cast
    python -m scripts.run_session --fake --turns 16    # offline, no key, no spend
    python -m scripts.run_session --only claude --turns 4
"""

from __future__ import annotations

import argparse
import sys
import time

from village import tools
from village.agent import Agent
from village.config import load_agents, load_season, load_settings
from village.fake import ScriptedModel
from village.llm import SpendGuard, chat
from village.orchestrator import run_session
from village.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="configs/agents.yaml")
    ap.add_argument("--season", default="configs/season.yaml")
    ap.add_argument("--turns", type=int, default=None)
    ap.add_argument("--only", default=None, help="run a single villager, for debugging")
    ap.add_argument("--delay", type=float, default=None, help="seconds between turns")
    ap.add_argument("--step-delay", type=float, default=None,
                    help="seconds between steps inside a turn; paces the live page")
    ap.add_argument("--fake", action="store_true",
                    help="offline cast: no API key, no network, no spend")
    args = ap.parse_args()

    settings = load_settings(require_key=not args.fake)
    configs = load_agents(args.agents, valid_tools=tools.names())
    if args.only:
        configs = [c for c in configs if c.name == args.only] or configs[:1]
    season = load_season(args.season)
    if args.delay is not None:
        season.seconds_between_turns = args.delay
    if args.step_delay is not None:
        season.seconds_between_steps = args.step_delay
    elif args.fake:
        # The offline cast exists to run fast; pacing is for the page, and nobody is
        # watching a fake run.
        season.seconds_between_steps = 0

    agents = [Agent(name=c.name, model=c.model, persona=c.persona, tools=c.tools,
                    temperature=c.temperature, max_tokens=c.max_tokens,
                    reasoning=c.reasoning,
                    chat_fn=ScriptedModel(c.name) if args.fake else chat)
              for c in configs]

    turns = args.turns if args.turns is not None else min(settings.max_turns,
                                                          season.turns_per_session)
    store = Store(settings.db_path)

    # Refuse to start rather than dying mid-run. A session killed halfway leaves a
    # half-written brief as the newest thing on a public homepage; a session that
    # never starts leaves yesterday's finished one. D45.
    if not args.fake and settings.max_usd_per_day > 0:
        spent = store.spend_since(time.time() - 86400)
        if spent >= settings.max_usd_per_day:
            print(f"daily cap reached: ${spent:.4f} spent in the last 24h "
                  f">= ${settings.max_usd_per_day:.2f}. Not starting.")
            sys.exit(1)          # non-zero so systemd marks the unit failed and it shows

    guard = SpendGuard(max_usd=1e9 if args.fake else settings.max_usd)

    session_id = run_session(agents, season, store, guard, max_turns=turns)
    print(f"\nsession {session_id}: ${store.session_cost(session_id):.4f}")
    print(f"replay: python -m scripts.replay {session_id}")


if __name__ == "__main__":
    main()
