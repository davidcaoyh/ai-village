"""Is the search backend actually working? One command, run after any session.

    python -m scripts.search_health --db runs/village.db

Answers the three things "is ddgs working" can mean, in order, because they fail
differently and only the middle one is a real risk:

  1. WHICH BACKEND ran. `web_search` prefixes every observation with `[tavily]`
     or `[duckduckgo]`, so the log already knows, and this needs no new code on
     the box - it reads sessions recorded before D49 existed just as well.
     A config change you cannot see in the log is a config change you have not made.

  2. WHETHER IT ANSWERED. The failure is not a worse result, it is no result:
     "No results (both search backends returned nothing)". That is a step the
     agent paid for and got nothing from, and D50 deliberately does not cache it,
     so the agent can ask again and pay again. This is the number that decides
     whether ddgs is viable.

  3. WHETHER THE BRIEFS STILL CITE ANYTHING. Sources per brief, since a brief
     with no urls is what a starved village produces.

Prints one row per session, oldest first, so a Tavily session and a ddgs session
sit next to each other and the comparison is the table rather than an argument.
"""

from __future__ import annotations

import argparse
import re

from scripts._dbsafe import connect_readonly

EMPTY = "No results (both search backends returned nothing)."
BACKEND_RE = re.compile(r"^\[(\w+)\]")


def sessions(db, limit: int) -> list[str]:
    rows = db.execute(
        """SELECT session_id, MIN(id) AS first FROM events
           GROUP BY session_id ORDER BY first DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [r["session_id"] for r in reversed(rows)]


def search_rows(db, session: str) -> list[str]:
    return [r["text"] or "" for r in db.execute(
        """SELECT json_extract(payload_json,'$.text') AS text FROM events
           WHERE session_id=? AND type='result'
             AND json_extract(payload_json,'$.name')='web_search' ORDER BY id""",
        (session,),
    ).fetchall()]


def summarise(db, session: str) -> dict:
    texts = search_rows(db, session)
    backends: dict[str, int] = {}
    empty = 0
    for t in texts:
        if t.startswith(EMPTY[:20]):
            empty += 1
            continue
        m = BACKEND_RE.match(t)
        backends[m.group(1) if m else "?"] = backends.get(m.group(1) if m else "?", 0) + 1

    half = len(texts) // 2 or 1
    tail = texts[half:] or texts
    tail_empty = sum(1 for t in tail if t.startswith(EMPTY[:20])) / len(tail) if tail else 0

    end = db.execute(
        """SELECT json_extract(payload_json,'$.reason') AS reason,
                  json_extract(payload_json,'$.usd')    AS usd
           FROM events WHERE session_id=? AND type='system'
             AND json_extract(payload_json,'$.kind')='session_end' LIMIT 1""",
        (session,),
    ).fetchone()

    return {
        "session": session,
        "searches": len(texts),
        "backends": backends,
        "empty": empty,
        "empty_pct": empty / len(texts) if texts else 0,
        "tail_empty": tail_empty,
        "usd": (end["usd"] if end else None) or 0,
        "ended": (end["reason"] if end else "") or "running/truncated",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="runs/village.db")
    ap.add_argument("--n", type=int, default=8, help="how many recent sessions")
    args = ap.parse_args()

    db = connect_readonly(args.db)
    rows = [summarise(db, s) for s in sessions(db, args.n)]
    rows = [r for r in rows if r["searches"]]
    if not rows:
        print("no web_search results in the last "
              f"{args.n} sessions of {args.db}")
        return

    print(f"\n{'session':26} {'attempts':>8} {'answered by':22} {'empty':>12} "
          f"{'2nd half':>9}  ended")
    print("-" * 100)
    for r in rows:
        mix = ", ".join(f"{k} {v}" for k, v in
                        sorted(r["backends"].items(), key=lambda kv: -kv[1])) or "-"
        print(f"{r['session']:26} {r['searches']:8} {mix:22} "
              f"{r['empty']:5} ({r['empty_pct']:4.0%}) {r['tail_empty']:8.0%}  {r['ended']}")
    print("\n  attempts = every web_search; 'answered by' counts only the ones that "
          "returned something.")

    latest = rows[-1]
    used = set(latest["backends"])
    print("\nlatest session\n")
    if "tavily" in used:
        print("  Tavily is STILL BEING USED. TAVILY_API_KEY is set wherever this ran -\n"
              "  check /etc/village.env on the box, not the .env on your laptop, and\n"
              "  remember systemd reads that file at service start, not on edit.")
    elif "duckduckgo" in used:
        print("  the switch is live: every search went through ddgs, no Tavily calls.")
    else:
        print(f"  no backend prefix recognised: {used}")

    who = "ddgs" if used == {"duckduckgo"} else "search"
    if latest["searches"] < 5:
        print(f"  only {latest['searches']} searches - too few to judge. Let a full "
              "session run.")
    elif latest["tail_empty"] > 0.25:
        print(f"  BUT it is thinning out: {latest['tail_empty']:.0%} of searches in the\n"
              "  second half of the session came back empty. That is the ddgs rate\n"
              "  limit. Raise seconds_between_steps, or move to a paid backend.")
    elif latest["empty_pct"] > 0.1:
        print(f"  {latest['empty_pct']:.0%} of searches came back empty, evenly spread -\n"
              "  worth watching over a few more sessions, not yet a rate limit.")
    else:
        print(f"  and it is answering: {latest['empty_pct']:.0%} empty results. "
              f"{who} is working.")


if __name__ == "__main__":
    main()
