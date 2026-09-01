"""What an agent knows when it wakes up.

Context does not grow but history does, so a prompt is four bounded parts:
persona and goal, the agent's own notes, the last N public events rendered as
prose, and the tool schemas. This module is also the single place that decides
what history *looks like* to a model, which makes it the first place to tune
when behaviour disappoints.

Each turn's prompt is rebuilt from the event log rather than carried as a
growing message list. Three things fall out of that: a crashed process resumes
where it stopped, every prompt is reproducible from the database, and the window
cannot quietly grow past its bound.
"""

from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
OWN_RESULT_CHARS = 240      # see below
NOTE_SEPARATOR = "\n---\n"


def _describe(event: dict, viewer: str) -> str | None:
    """One event as one line of prose.

    Models read `[claude] we should split the sources` far more reliably than a
    nested object, at roughly a third of the tokens.
    """
    who = event["agent"]
    payload = event["payload"]
    kind = event["type"]

    if kind == "chat":
        return f"[{who}] {payload.get('message', '')}"
    if kind == "action":
        args = payload.get("arguments") or {}
        detail = args.get("query") or args.get("url") or args.get("path") or ""
        return f"({who} used {payload.get('name')}{': ' + str(detail) if detail else ''})"
    if kind == "result":
        if who != viewer:
            return None                     # other villagers' observations are theirs
        # Your own past observation, bounded. A bare count is not actionable: an
        # agent told only "(result: 5)" re-runs the same search and pays twice.
        text = str(payload.get("text", ""))[:OWN_RESULT_CHARS]
        return f"(what your {payload.get('name')} returned: {text})"
    if kind == "system":
        k = payload.get("kind")
        if k == "human_message":
            return f"[a human watching] {payload.get('message', '')}"
        if k == "turn_end":
            return f"({who} ended their turn: {payload.get('summary', '')})"
        if k == "session_start":
            return "(the session began)"
        return None
    return None


def render_events(events: list[dict], viewer: str) -> str:
    lines = [line for e in events if (line := _describe(e, viewer))]
    return "\n".join(lines) if lines else "(nothing has happened yet - you are first)"


def build_context(store, agent, season, session_id: str, others: list[str]) -> list[dict]:
    """The two messages that make up a turn's prompt."""
    template = (PROMPT_DIR / "agent_system.md").read_text()
    notes = store.notes_for(agent.name)
    system = template.format(
        name=agent.name,
        n_villagers=len(others) + 1,
        others=", ".join(others) if others else "nobody else yet",
        persona=agent.persona,
        goal=season.goal,
        constraints="\n".join(f"- {c}" for c in season.constraints),
        notes=NOTE_SEPARATOR.join(notes) if notes else "(none yet)",
    )
    events = store.recent_for_prompt(session_id, agent.name, season.context_window_events)
    user = (
        "Recent activity in the village:\n"
        f"{render_events(events, agent.name)}\n\n"
        f"It is your turn, {agent.name}. Pick the one action that moves the goal "
        "forward and call the tool for it."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_compaction_messages(store, agent, season, session_id: str) -> list[dict]:
    """The prompt that rewrites one agent's memory.

    Everything the agent is about to lose - its notes and the window that is about
    to roll past - goes in; the system prompt is static, so it is read verbatim.
    """
    system = (PROMPT_DIR / "compaction.md").read_text()
    notes = store.notes_for(agent.name)
    events = store.recent_for_prompt(session_id, agent.name, season.context_window_events)
    user = (
        "Your memory as it stands:\n"
        f"{NOTE_SEPARATOR.join(notes) if notes else '(none yet)'}\n\n"
        "What has happened since:\n"
        f"{render_events(events, agent.name)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def should_compact(turn_index: int, every: int) -> bool:
    return every > 0 and turn_index > 0 and turn_index % every == 0
