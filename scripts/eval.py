"""Per-agent scoreboard for one session, straight out of the event log.

Every metric here is a GROUP BY over `events`. Nothing was instrumented for this
file: D4 said the log is the only state, and this is the invoice for that choice.
That is also the sentence to say when asked how you would evaluate agents.

    python -m scripts.eval <session-id>
    python -m scripts.eval <session-id> --db runs/village.db --json

SKELETON. The queries are David's - each one is a GROUP BY over a single event
type, and the shape is documented above every TODO. `render()`, the argparse and
the table formatting are done.

Checkpoint: run it on a `--fake` session first. The fake cast's numbers are
predictable, which is what makes them a test.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from scripts._dbsafe import connect_readonly

# What a row of the scoreboard holds. Adding a metric means adding a key here and
# a query below; `render()` derives its columns from COLUMNS, not from the data,
# so a query that returns nothing still shows the agent with zeros.
COLUMNS = [
    ("agent", "agent", "{}"),
    ("calls", "calls", "{}"),
    ("usd", "usd", "{:.4f}"),
    ("tok_in", "tok in", "{}"),
    ("tok_out", "tok out", "{}"),
    ("reasoning", "reasoning", "{}"),
    ("actions", "actions", "{}"),
    ("tool_fail_pct", "tool fail %", "{:.1f}"),
    ("malformed_pct", "malformed %", "{:.1f}"),
    ("step_cap", "step cap", "{}"),
    ("no_tool_call", "no tool", "{}"),
    ("doi_checked", "dois ck", "{}"),
    ("doi_fake", "dois fake", "{}"),
]

# What counts as a tool failure. Not a regex over "Error", because D38 replaced the
# raw HTTPError with typed observations that start with the host name, and D39/D43
# added two more shapes. Every one of them ends the same way, so the marker is the
# instruction rather than the prefix.
#
# Deliberately NOT a failure: "No work is registered under doi ..." - the tool ran,
# answered, and the answer is that the citation was invented. That is the finding
# the tool exists to produce, counted in its own column.
FAILED_RESULT = """(   json_extract(payload_json,'$.text') LIKE 'Error%'
     OR json_extract(payload_json,'$.text') LIKE '%Do not fetch this url again%'
     OR json_extract(payload_json,'$.text') LIKE 'You already tried this url%'
     OR json_extract(payload_json,'$.text') LIKE 'Could not reach the doi registry%'
     OR json_extract(payload_json,'$.text') LIKE 'The doi registry returned%')"""


def connect(path: str) -> sqlite3.Connection:
    # Read-only, and guarded against being run as the wrong user - see
    # scripts/_dbsafe.py. The eval is most useful run against the live database on
    # the box, which is exactly where opening it read-write as root does damage.
    return connect_readonly(path)


# --- the queries ----------------------------------------------------------
# Each returns {agent: {...}}. `thought` carries the money and the tokens;
# `action` carries what was attempted; `result` carries what came back; `system`
# carries how the turn ended. Compaction writes a `thought` too (D31), so decide
# per query whether to filter `kind='compaction'` in or out, and say which.


def spend(db, session: str) -> dict[str, dict]:
    """Model calls, tokens and cost per agent. Worked example from PLAN.md."""
    rows = db.execute(
        """SELECT agent,
                  COUNT(*)                                              AS calls,
                  SUM(json_extract(payload_json,'$.usd'))               AS usd,
                  SUM(json_extract(payload_json,'$.prompt_tokens'))     AS tok_in,
                  SUM(json_extract(payload_json,'$.completion_tokens')) AS tok_out,
                  SUM(json_extract(payload_json,'$.reasoning_tokens'))  AS reasoning
           FROM events WHERE session_id=? AND type='thought' GROUP BY agent""",
        (session,),
    ).fetchall()
    return {r["agent"]: dict(r) for r in rows}


def actions(db, session: str) -> dict[str, dict]:
    """Actions attempted per agent.

    Shape: SELECT agent, COUNT(*) FROM events WHERE session_id=? AND type='action'
    GROUP BY agent.
    """
    rows = db.execute(
        """SELECT agent, COUNT(*) AS actions
           FROM events WHERE session_id=? AND type='action' AND agent IS NOT NULL
           GROUP BY agent""",
        (session,),
    ).fetchall()
    return {r["agent"]: {"actions": r["actions"]} for r in rows}


def failures(db, session: str) -> dict[str, dict]:
    """Tool failure rate and malformed-call rate per agent.

    A failed tool call is a `result` whose text starts with "Error" (D13 - every
    failure comes back as an observation, never an exception). A malformed call is
    a `result` whose text starts with "parse_error:" (agent.py writes that shape).
    Both are counts here; render() turns them into percentages, so return the raw
    numbers as {"tool_fail": n, "malformed": n}.

    Watch the denominator: tool failures are over `actions`, malformed calls are
    over model `calls`. A malformed call never produced an action.
    """
    rows = db.execute(
        f"""SELECT agent,
                   SUM(CASE WHEN {FAILED_RESULT} THEN 1 ELSE 0 END) AS tool_fail,
                   SUM(json_extract(payload_json,'$.text') LIKE 'parse_error:%%') AS malformed
            FROM events WHERE session_id=? AND type='result' AND agent IS NOT NULL
            GROUP BY agent""",
        (session,),
    ).fetchall()
    return {r["agent"]: {"tool_fail": r["tool_fail"], "malformed": r["malformed"]}
            for r in rows}


def turn_endings(db, session: str) -> dict[str, dict]:
    """How each agent's turns ended.

    D35: every turn writes exactly one `system` event with kind='turn_end' and an
    `ended_by` of end_turn / vote_done / step_cap / no_tool_call / provider_error.
    Counting off the `end_turn` tool instead undercounted by 40% on the Sep 2 run,
    because a turn that hits the cap calls nothing.
    """
    rows = db.execute(
        """SELECT agent, json_extract(payload_json,'$.ended_by') AS ended_by, COUNT(*) AS n
           FROM events
           WHERE session_id=? AND type='system' AND agent IS NOT NULL
             AND json_extract(payload_json,'$.kind')='turn_end'
           GROUP BY agent, ended_by""",
        (session,),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        # Every ending is kept, not just the two printed: `turns` is the honest
        # denominator for anything per-turn, and D35 exists because counting off the
        # end_turn tool undercounted by 40%.
        row = out.setdefault(r["agent"], {"turns": 0})
        row[r["ended_by"] or "unknown"] = r["n"]
        row["turns"] += r["n"]
    return out


def citations(db, session: str) -> dict[str, dict]:
    """How many dois each agent checked, and how many turned out not to exist.

    The second number is the point of D40. A doi the registry has never heard of is
    a citation the model produced from memory, and before resolve_doi existed those
    shipped into briefs unchallenged - 26 of 37 cited urls on Sep 2 had never been
    successfully retrieved.
    """
    rows = db.execute(
        """SELECT agent,
                  SUM(type='action' AND json_extract(payload_json,'$.name')='resolve_doi')
                      AS doi_checked,
                  SUM(type='result'
                      AND json_extract(payload_json,'$.text') LIKE 'No work is registered%')
                      AS doi_fake
           FROM events WHERE session_id=? AND agent IS NOT NULL
             AND type IN ('action','result')
           GROUP BY agent""",
        (session,),
    ).fetchall()
    return {r["agent"]: {"doi_checked": r["doi_checked"], "doi_fake": r["doi_fake"]}
            for r in rows}


def tool_mix(db, session: str) -> dict[str, dict[str, int]]:
    """Which tools each agent actually reached for: {agent: {tool_name: count}}.

    Printed as its own block rather than a column, because the interesting result
    is the shape of the distribution, not any single number. On Sep 2 this is what
    showed read_notes burning 32 calls re-reading text already in the prompt (D34).
    """
    rows = db.execute(
        """SELECT agent, json_extract(payload_json,'$.name') AS tool, COUNT(*) AS n
           FROM events WHERE session_id=? AND type='action' AND agent IS NOT NULL
           GROUP BY agent, tool""",
        (session,),
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["agent"], {})[r["tool"] or "?"] = r["n"]
    return out


def search_cost(db, session: str) -> dict[str, dict]:
    """What search cost, per agent - the one spend the model bill never showed.

    `spend()` sums `thought.usd`, which is what OpenRouter reports on a chat
    completion. A search is a requests.post to another vendor and appears in no
    thought. Measured Sep 5: $0.27 of models against ~$3.10 of Tavily, and the
    only reason the second number was ever known is that it was reconstructed by
    hand from a tool-mix count. This query is that reconstruction, made routine.
    """
    rows = db.execute(
        """SELECT agent,
                  COUNT(*)                                            AS searches,
                  SUM(json_extract(payload_json,'$.cached'))          AS cached,
                  SUM(json_extract(payload_json,'$.credits'))         AS credits,
                  SUM(json_extract(payload_json,'$.usd'))             AS search_usd,
                  SUM(CASE WHEN json_extract(payload_json,'$.near_dup_ratio')
                           IS NOT NULL THEN 1 ELSE 0 END)             AS near_dups
           FROM events WHERE session_id=? AND type='search' AND agent IS NOT NULL
           GROUP BY agent""",
        (session,),
    ).fetchall()
    return {r["agent"]: dict(r) for r in rows}


def session_facts(db, session: str) -> dict:
    """One row about the run itself: goal, cast, turns, cost, why it ended.

    `session_start` and `session_end` are both system events with agent NULL.
    Wall clock is MAX(ts) - MIN(ts).
    """
    rows = db.execute(
        """SELECT json_extract(payload_json,'$.kind') AS kind, payload_json
           FROM events WHERE session_id=? AND type='system'
             AND json_extract(payload_json,'$.kind') IN ('session_start','session_end')
           ORDER BY id""",
        (session,),
    ).fetchall()
    start = next((json.loads(r["payload_json"]) for r in rows if r["kind"] == "session_start"), {})
    end = next((json.loads(r["payload_json"]) for r in rows if r["kind"] == "session_end"), {})

    span = db.execute("SELECT MIN(ts) AS a, MAX(ts) AS b, COUNT(*) AS n FROM events "
                      "WHERE session_id=?", (session,)).fetchone()
    rounds = db.execute("SELECT COUNT(*) AS n FROM events WHERE session_id=? AND type='system' "
                        "AND json_extract(payload_json,'$.kind')='goal_advanced'",
                        (session,)).fetchone()["n"]
    seconds = (span["b"] or 0) - (span["a"] or 0)
    cast = start.get("cast") or []
    return {
        "season": start.get("season_id", "?"),
        "cast": ", ".join(f"{c['name']}={c['model']}" for c in cast) or "?",
        "turn cap": start.get("max_turns", "?"),
        "spend cap": f"${start.get('max_usd', 0):.2f}",
        # No session_end means one of two things, and they look identical in a table:
        # the run is still going, or the database is a copy that lost the tail. A
        # plain `cp`/`scp` of village.db without its -wal file does exactly that.
        "ended": end.get("reason", "NO session_end - still running, or a truncated copy"),
        "cost": f"${end.get('usd', 0):.4f}" if end else "unknown (no session_end)",
        "wall clock": f"{seconds / 60:.1f} min",
        "events": span["n"],
        "rounds": rounds + 1,          # goal_advanced fires between rounds, not before the first
    }


# --- output ---------------------------------------------------------------

def build(db, session: str) -> list[dict]:
    """Merge every query into one row per agent. Missing metrics render as zero."""
    merged: dict[str, dict] = {}
    for query in (spend, actions, failures, turn_endings, citations):
        for agent, values in query(db, session).items():
            merged.setdefault(agent, {"agent": agent}).update(values)

    for row in merged.values():
        calls, acts = row.get("calls") or 0, row.get("actions") or 0
        row["tool_fail_pct"] = 100.0 * (row.get("tool_fail") or 0) / acts if acts else 0.0
        row["malformed_pct"] = 100.0 * (row.get("malformed") or 0) / calls if calls else 0.0
    return sorted(merged.values(), key=lambda r: -(r.get("usd") or 0))


def render(rows: list[dict]) -> str:
    """Fixed-width table. Columns come from COLUMNS so a missing metric shows 0."""
    head = [label for _, label, _ in COLUMNS]
    body = [[fmt.format(r.get(key) or (0 if fmt != "{}" else 0)) if key != "agent"
             else str(r.get("agent", "?"))
             for key, _, fmt in COLUMNS] for r in rows]
    widths = [max(len(head[i]), *(len(b[i]) for b in body)) if body else len(head[i])
              for i in range(len(COLUMNS))]
    def line(cells):
        return "  ".join(c.rjust(w) for c, w in zip(cells, widths))

    return "\n".join([line(head), line(["-" * w for w in widths]), *(line(b) for b in body)])


def render_tools(mix: dict[str, dict[str, int]]) -> str:
    out = []
    for agent in sorted(mix):
        counts = ", ".join(f"{t} {n}" for t, n in
                           sorted(mix[agent].items(), key=lambda kv: -kv[1]))
        out.append(f"  {agent:10} {counts}")
    return "\n".join(out)


def render_search(cost: dict[str, dict]) -> str:
    """Search spend beside the model spend it has always hidden behind."""
    def row(name, searches, cached, dups, usd):
        return f"  {name:10}{searches:>9}{cached:>8}{dups:>10}{usd:>11}"

    out = [row("", "searches", "cached", "near-dup", "usd"), "  " + "-" * 48]
    total = 0.0
    for agent in sorted(cost):
        c = cost[agent]
        usd = c["search_usd"] or 0
        total += usd
        out.append(row(agent, c["searches"], c["cached"] or 0, c["near_dups"] or 0,
                       f"${usd:.4f}"))
    out.append(row("total", sum(c["searches"] for c in cost.values()),
                   sum(c["cached"] or 0 for c in cost.values()),
                   sum(c["near_dups"] or 0 for c in cost.values()), f"${total:.4f}"))
    return "\n".join(out)


def render_header(facts: dict) -> str:
    return "\n".join(f"  {k:14} {v}" for k, v in facts.items())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("session", help="session id, or 'latest'")
    ap.add_argument("--db", default="runs/village.db")
    ap.add_argument("--json", action="store_true", help="machine-readable, for the API route")
    args = ap.parse_args()

    db = connect(args.db)
    session = args.session
    if session == "latest":
        row = db.execute("SELECT session_id FROM events ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            sys.exit("no sessions in that database")
        session = row["session_id"]

    if not db.execute("SELECT 1 FROM events WHERE session_id=? LIMIT 1",
                      (session,)).fetchone():
        known = [r["session_id"] for r in db.execute(
            "SELECT DISTINCT session_id FROM events ORDER BY id DESC LIMIT 8")]
        sys.exit(f"no session {session} in {args.db}. Most recent: " + ", ".join(known))

    rows = build(db, session)
    if args.json:
        print(json.dumps({"session": session, "facts": session_facts(db, session),
                          "agents": rows, "tools": tool_mix(db, session),
                          "search": search_cost(db, session)}, indent=2))
        return

    print(f"\nsession {session}\n")
    print(render_header(session_facts(db, session)))
    print()
    print(render(rows))
    print("\ntools used\n")
    print(render_tools(tool_mix(db, session)))
    search = search_cost(db, session)
    if search:
        print("\nsearch\n")
        print(render_search(search))
    print()


if __name__ == "__main__":
    main()
