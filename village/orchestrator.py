"""The scheduler: who acts, when, and every way a session can end.

Round-robin and turn-based rather than concurrent. That buys reproducible
ordering, a readable transcript, and one budget knob - turns per session.

Four stop conditions, all leaving through one `finally` that writes `session_end`.
A session that merely stops emitting events is indistinguishable from a crash when
you read the log six weeks later.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from . import memory
from .llm import BudgetExceeded


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def run_session(agents, season, store, spend_guard, session_id: str | None = None,
                max_turns: int | None = None, runs_dir: str = "runs",
                verbose: bool = True) -> str:
    session_id = session_id or new_session_id()
    artifacts = Path(runs_dir) / session_id / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    names = [a.name for a in agents]
    for a in agents:
        a.others = [n for n in names if n != a.name]

    turns = max_turns if max_turns is not None else season.turns_per_session
    store.append(session_id, None, "system", {
        "kind": "session_start",
        "goal": season.goal,
        "season_id": season.season_id,
        "cast": [{"name": a.name, "model": a.model} for a in agents],
        "max_turns": turns,
        "max_usd": spend_guard.max_usd,
    })

    turns_taken = {a.name: 0 for a in agents}
    failed_in_a_row = 0
    reason = "turn_cap"
    try:
        for turn in range(turns):
            # Checked between turns, never mid-turn, so the log never ends with a
            # tool call that has no result.
            if store.stop_requested(session_id):
                reason = "human_stop"
                break

            agent = agents[turn % len(agents)]
            if verbose:
                print(f"turn {turn + 1}/{turns}  {agent.name}  "
                      f"${store.session_cost(session_id):.4f}", flush=True)

            # Compaction runs before the turn, so the agent acts on the memory it
            # just rewrote rather than on the window that is about to roll past.
            if memory.should_compact(turns_taken[agent.name], season.compaction_every_turns):
                agent.compact(store, season, spend_guard, session_id)

            ctx = agent.take_turn(store, season, spend_guard, session_id,
                                 runs_dir=runs_dir, max_steps=season.max_steps_per_turn)
            turns_taken[agent.name] += 1

            # One villager failing to reach the provider is that villager's turn.
            # A whole round of it is the provider, and burning the turn cap on
            # failures that cost nothing still writes a log nobody can read.
            failed_in_a_row = failed_in_a_row + 1 if ctx.provider_error else 0
            if failed_in_a_row >= len(agents):
                reason = "provider_unavailable"
                break

            if season.seconds_between_turns:
                time.sleep(season.seconds_between_turns)
    except BudgetExceeded as exc:
        reason = "budget_exceeded"
        store.append(session_id, None, "system", {"kind": "budget_exceeded", "text": str(exc)})
    except KeyboardInterrupt:
        reason = "interrupted"
    except Exception as exc:                                   # noqa: BLE001
        # Without this the finally below writes the initial "turn_cap" and a crash
        # reads as a clean finish six weeks later. Re-raised so the exit code is
        # still non-zero and systemd marks the unit failed.
        reason = "crashed"
        store.append(session_id, None, "system",
                     {"kind": "crashed", "text": f"{type(exc).__name__}: {exc}"[:500]})
        raise
    finally:
        store.append(session_id, None, "system", {
            "kind": "session_end",
            "reason": reason,
            "usd": store.session_cost(session_id),
            "by_model": spend_guard.by_model,
        })
    return session_id
