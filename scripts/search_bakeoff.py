"""Compare the two search backends on the same queries, before spending model money.

    python -m scripts.search_bakeoff --db runs/village.db          # ddgs vs recorded tavily
    python -m scripts.search_bakeoff --live --n 20                 # ddgs vs live tavily
    python -m scripts.search_bakeoff --delay 30 --n 30             # does pacing rescue ddgs?

D48 proposes running unattended on keyless `ddgs` because Tavily was 11x the model
bill. D12 chose Tavily for snippets "written for LLM consumption and markedly
cleaner". Both claims are about quality; neither was measured. This measures them.

Two different risks, reported separately, because conflating them is how the wrong
backend gets chosen:

  AVAILABILITY - does ddgs answer at all, and does it thin out over a long run?
    This is the real risk and the one D48 waves at ("rate-limited and will thin
    out over a long session"). A backend that returns nothing is not a worse
    backend, it is no backend, and the agent burns a step discovering that.
    Measured as empty-rate by position in the sequence, so a cliff is visible
    rather than averaged away.

  QUALITY - for a query that both answer, how different are the answers?
    Result count, snippet length (tokens the model pays for), and domain overlap.
    Overlap is the honest metric here: if both backends return the same domains,
    the snippet prose is a formatting difference, not an evidence difference.

Calls `village.tools._search_tavily` and `_search_duckduckgo` directly rather than
reimplementing them, so what is measured is the code that ships. `--live` spends
1 Tavily credit ($0.008) per query; the default `--db` baseline spends nothing by
reading results Tavily already returned in an earlier run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from urllib.parse import urlparse

from scripts._dbsafe import connect_readonly
from village import tools

# Used when no log is available. Drawn from the season's stated domains (D41) and
# shaped like the queries the cast actually issues - long, specific, several proper
# nouns - because short queries flatter both backends equally and prove nothing.
FALLBACK_QUERIES = [
    "Card Krueger minimum wage employment New Jersey natural experiment",
    "Dale Krueger elite college attendance earnings selection on observables",
    "conditional cash transfers school enrolment Progresa randomized evaluation",
    "compulsory schooling laws returns to education instrumental variable Angrist",
    "microfinance randomized controlled trial household consumption Banerjee",
    "Sesame Street early childhood literacy Kearney Levine grade retention",
    "job training programs earnings meta analysis experimental evidence",
    "housing vouchers Moving to Opportunity neighbourhood effects long run",
    "class size reduction Project STAR test scores causal estimate",
    "unemployment insurance duration job search elasticity regression discontinuity",
]


def queries_from_db(path: str, session: str | None, limit: int) -> list[tuple[str, str]]:
    """Real queries the village issued, paired with the observation Tavily returned.

    The pair is the point: it gives a free quality baseline. A query with no
    recorded `[tavily]` result still counts for the availability half.
    """
    db = connect_readonly(path)
    if session in (None, "latest"):
        row = db.execute("SELECT session_id FROM events ORDER BY id DESC LIMIT 1").fetchone()
        session = row["session_id"] if row else ""

    def searches(where: str, params: tuple):
        return db.execute(
            f"""SELECT id, agent, payload_json FROM events
                WHERE {where} AND type='action'
                  AND json_extract(payload_json,'$.name')='web_search' ORDER BY id""",
            params,
        ).fetchall()

    rows = searches("session_id=?", (session,))
    if not rows:
        # The newest session need not be the one that searched - a local copy of
        # village.db often trails the droplet. Any real query beats a synthetic one.
        rows = searches("1=1", ())

    out: list[tuple[str, str]] = []
    for r in rows:
        query = (json.loads(r["payload_json"]).get("arguments") or {}).get("query")
        if not query:
            continue
        # The result event the loop wrote for this action is the next `result` row.
        res = db.execute(
            """SELECT json_extract(payload_json,'$.text') AS text FROM events
               WHERE id>? AND type='result'
                 AND json_extract(payload_json,'$.name')='web_search'
               ORDER BY id LIMIT 1""",
            (r["id"],),
        ).fetchone()
        out.append((query, (res["text"] if res else "") or ""))
        if len(out) >= limit:
            break
    db.close()
    return out


def domains(results: list[dict]) -> set[str]:
    return {urlparse(r.get("url", "")).netloc.lower().removeprefix("www.")
            for r in results if r.get("url")}


def domains_in_observation(text: str) -> set[str]:
    """Domains inside a recorded `[tavily] results for ...` string."""
    found = set()
    for token in text.split():
        if token.startswith("http"):
            host = urlparse(token).netloc.lower().removeprefix("www.")
            if host:
                found.add(host)
    return found


def run_backend(fn, queries: list[str], delay: float, label: str) -> list[dict]:
    rows = []
    for i, q in enumerate(queries):
        if i and delay:
            time.sleep(delay)
        t0 = time.time()
        try:
            results = fn(q)
            error = ""
        except Exception as exc:                                   # noqa: BLE001
            results, error = [], f"{type(exc).__name__}: {exc}"[:80]
        elapsed = time.time() - t0
        rows.append({"i": i, "query": q, "n": len(results), "seconds": elapsed,
                     "error": error, "domains": domains(results),
                     "chars": sum(len(r.get("snippet", "")) for r in results)})
        state = "empty" if not results else f"{len(results)} results"
        print(f"  {label:11} {i + 1:3}. {elapsed:5.1f}s  {state:12} {error}", flush=True)
    return rows


def availability(rows: list[dict], label: str) -> str:
    """Empty-rate overall and by half, so a mid-run cliff is visible."""
    n = len(rows)
    empty = [r for r in rows if r["n"] == 0]
    half = n // 2 or 1
    first = sum(1 for r in rows[:half] if r["n"] == 0) / half
    second_slice = rows[half:] or rows
    second = sum(1 for r in second_slice if r["n"] == 0) / len(second_slice)
    med = statistics.median([r["seconds"] for r in rows]) if rows else 0
    return (f"  {label:11} {n - len(empty):3}/{n} answered   "
            f"empty {len(empty) / n:5.0%}   "
            f"first half {first:4.0%} -> second half {second:4.0%}   "
            f"median {med:4.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=None,
                    help="village.db to take real queries from (and a free Tavily baseline)")
    ap.add_argument("--session", default="latest")
    ap.add_argument("--n", type=int, default=12, help="how many queries")
    ap.add_argument("--delay", type=float, default=0,
                    help="seconds between queries; use 30-45 to mimic the shipped pacing (D47)")
    ap.add_argument("--live", action="store_true",
                    help="also call Tavily live. Costs 1 credit ($0.008) per query")
    args = ap.parse_args()

    pairs: list[tuple[str, str]] = []
    if args.db:
        pairs = queries_from_db(args.db, args.session, args.n)
        print(f"{len(pairs)} real queries from {args.db}")
    if len(pairs) < args.n:
        need = args.n - len(pairs)
        pairs += [(q, "") for q in FALLBACK_QUERIES[:need]]
        print(f"padded with {min(need, len(FALLBACK_QUERIES))} queries from the season's domains")
    queries = [q for q, _ in pairs]
    if not queries:
        sys.exit("no queries")

    span = args.delay * (len(queries) - 1)
    print(f"\n{len(queries)} queries, {args.delay:.0f}s apart "
          f"(~{span / 60:.1f} min)\n")

    ddg = run_backend(tools._search_duckduckgo, queries, args.delay, "duckduckgo")

    tav: list[dict] = []
    if args.live:
        if not tools._tavily_key():
            sys.exit("--live needs TAVILY_API_KEY set")
        print(f"\n  spending {len(queries)} Tavily credits "
              f"(${len(queries) * tools.TAVILY_USD_PER_CREDIT:.3f})\n")
        tav = run_backend(tools._search_tavily, queries, args.delay, "tavily")

    print("\navailability\n")
    print(availability(ddg, "duckduckgo"))
    if tav:
        print(availability(tav, "tavily"))

    print("\nquality, on queries both backends answered\n")
    baseline = [(d, set(domains_in_observation(text)) if not tav else tav[i]["domains"])
                for i, ((_, text), d) in enumerate(zip(pairs, ddg))]
    overlaps, ddg_chars, compared = [], [], 0
    for d, tav_domains in baseline:
        if not d["domains"] or not tav_domains:
            continue
        compared += 1
        union = d["domains"] | tav_domains
        overlaps.append(len(d["domains"] & tav_domains) / len(union))
        ddg_chars.append(d["chars"])
    if compared:
        print(f"  compared        {compared} queries")
        print(f"  domain overlap  {statistics.mean(overlaps):.0%} "
              f"(share of domains the two backends agree on)")
        print(f"  ddgs snippets   {statistics.mean(ddg_chars):.0f} chars/query")
        if tav:
            print(f"  tavily snippets {statistics.mean([r['chars'] for r in tav]):.0f} chars/query")
    else:
        print("  nothing to compare - no query was answered by both")

    empty_rate = sum(1 for r in ddg if r["n"] == 0) / len(ddg)
    half = len(ddg) // 2 or 1
    tail = ddg[half:] or ddg
    tail_rate = sum(1 for r in tail if r["n"] == 0) / len(tail)
    print("\nverdict\n")
    # The tail matters more than the mean. Thinning out is the documented ddgs
    # failure, and a run that answers the first eight and none of the last four
    # averages to a healthy-looking 33%. A session issues hundreds of queries;
    # what the last ones do is what it will feel like.
    if tail_rate > 0.25 or empty_rate > 0.25:
        print(f"  ddgs returned nothing on {empty_rate:.0%} of queries "
              f"({tail_rate:.0%} in the second half). That is an\n"
              f"  availability failure, not a quality one: the agent burns a step and\n"
              f"  D50 deliberately does not cache the empty answer. Re-run with\n"
              f"  --delay 45 before concluding; if it persists, keep a paid backend.")
    elif overlaps and statistics.mean(overlaps) < 0.2:
        print("  ddgs answers reliably but finds different sources than Tavily.\n"
              "  Judge that on the briefs, not here: run one session on each and\n"
              "  compare the Sources sections.")
    else:
        print("  ddgs answered reliably and largely agreed with Tavily on sources.\n"
              "  The remaining difference is snippet prose, which costs tokens,\n"
              "  not evidence. D48 is safe on this evidence.")


if __name__ == "__main__":
    main()
