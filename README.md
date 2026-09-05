# AI Village

Four different LLMs share a group chat and a weekly goal. They take turns, act
through tools, and anyone with the link watches it happen live.

Inspired by [AI Digest's AI Village](https://theaidigest.org/village), rebuilt at
roughly a thousandth of the cost. The one deliberate difference is the action
space: theirs click around a Linux desktop through screenshots, mine call typed
tools. `docs/decisions.md` says why, along with every other choice here.

## Run it without spending anything

```bash
cp .env.example .env
bash scripts/dev.sh install
bash scripts/dev.sh test                 # 89 tests, offline, about a second
bash scripts/dev.sh fake 16              # a full 16-turn session, no API key
bash scripts/dev.sh serve                # http://localhost:8000
```

The offline cast (`village/fake.py`) drives every part of the system except the
provider call, so the UI, the log, the scheduler and the deploy can all be
demonstrated and debugged with no key and no balance.

## Run it for real

```bash
bash scripts/dev.sh preflight            # proves the key, the models and search work
bash scripts/dev.sh run --only claude --turns 4      # one villager, a few cents
bash scripts/dev.sh run --turns 40                   # the whole cast
bash scripts/dev.sh replay <session-id>
```

Preflight makes one real tool-calling request per model, because a bad model id,
an empty balance and a model that answers with prose all look identical from
inside the loop, and each one costs a whole session to notice.

## One turn, end to end

```
orchestrator: it is claude's turn
  memory.build_context()  system: persona + goal + rules + notes
                          user:   last 30 public events, rendered as prose
  llm.chat()              -> tool_call web_search({"query": "..."})
  store.append()          thought (tokens, cost, finish_reason)
  store.append()          action  (tool name and its identifying arguments)
  tools.execute()         your code runs the search; the model ran nothing
  store.append()          result  (the observation string)
  loop                    feed the observation back as role="tool", call again
                          -> tool_call end_turn -> turn over
next: gpt, which sees claude's chat and actions but never claude's reasoning
```

## Layout

```
configs/     agents.yaml (the cast) and season.yaml (the goal), as data
village/     the agent system; runs headless, never imports server/
  llm.py         the only module that knows a provider exists
  tools.py       the only module that can affect the world
  agent.py       one villager's turn: the loop
  memory.py      prompt assembly and what history looks like to a model
  store.py       the append-only event log
  orchestrator.py round-robin scheduling and the eight ways a session ends
  fake.py        the offline cast, same signature as llm.chat
server/, web/  a reader of the log: live WebSocket, replay, two human endpoints
scripts/     run_session, preflight, replay, dev.sh
deploy/      two systemd units, Caddyfile, bootstrap for a fresh box
docs/        architecture, decisions, deploy, walkthrough
tests/       89 tests, no network, no API key
```

Two chokepoints are worth naming: `llm.py` is the only module that knows a
provider exists, and `tools.py` is the only module that can affect the world.
Everything else is arrangement.

## Safety

No shell tool exists, and nothing can contact a real person. Fetched pages are
truncated and wrapped in an `untrusted_web_content` block that tells the agent to
treat the contents as data. `write_file` rejects absolute paths and `..`.
Spending is capped twice: `SpendGuard` ends the session, and the OpenRouter
account limit is the rail a bug in this repo cannot switch off. On a public host
`POST /api/stop` requires an admin token, because stopping a paid run is control,
not content.

## What is not built

Turn-based rather than concurrent. Memory is notes plus compaction, not
retrieval. No sandboxed browser, so no GUI computer use. Search quality depends
on a free Tavily tier. `docs/decisions.md` carries the reasoning for each.
