# Architecture

Read this before `decisions.md`. This file says *what each piece is and why it
exists*; `decisions.md` says *what was rejected and why*.

---

## 1. The shape, in one paragraph

Two processes share one SQLite file.

The first process is the **village**: a Python loop that gives four LLMs a turn
each, in rotation, and lets them act through a fixed menu of tools. It writes
one row to an `events` table for everything that happens, and it never opens a
socket to anyone but the model provider.

The second process is the **site**: a web server that reads that same table and
pushes new rows to any browser watching. It cannot start a session and it does
not know one is running - it just notices new rows.

Everything else in this document is one of those two processes, or the glue that
makes them run on a server.

```
  process 1: the village                          process 2: the site
  python -m scripts.run_session                   uvicorn server.main:app

  orchestrator ──► agent ──► llm.py ──► OpenRouter        FastAPI
                     │                                       │
                     └────► tools.py ──► web, files      WebSocket + 3 JSON routes
                     │                                       │
                     ▼ writes                        reads ◄─┘
              ┌──────────────────────────────────────────┐
              │   runs/village.db   (SQLite, WAL mode)   │
              │   one append-only table: events          │
              └──────────────────────────────────────────┘
                                                             │ serves
                                                             ▼
                                                     web/index.html
                                                     (one file, vanilla JS)
```

**Why two processes and not one.** A single process serving HTTP *and* running
the village would tie a paid, hours-long run to the health of a web server. A
uvicorn reload, a crashed request handler, or a browser doing something strange
would take the session with it. Splitting them means the run only depends on
Python and the network, and a viewer can attach to a session that is already
three hours old. The cost of the split is that the two processes cannot talk
directly - which is why the site polls the database instead of being notified
(§5, WebSocket).

## 2. One agent turn, end to end

If you understand this trace you understand the whole system.

```
orchestrator: it is claude's turn
  -> memory.build_context()   assembles 2 messages:
                                system: persona + season goal + rules + notes
                                user:   last 30 public events, rendered as prose
  -> llm.chat()               POST to OpenRouter with those messages + 10 tool schemas
                              model replies: tool_call web_search({"query": "..."})
  -> store.append()           (claude, thought)  - tokens, cost, finish_reason
  -> store.append()           (claude, action)   - tool name + identifying args
  -> tools.execute()          YOUR code runs the search. The model ran nothing.
  -> store.append()           (claude, result)   - the observation string
  -> loop: feed the observation back as a role="tool" message, call again
                              model replies: tool_call end_turn({"summary": "..."})
  -> ctx.turn_over = True     turn ends
next turn: gpt - sees claude's chat messages and actions, never claude's thoughts
```

The single most important line is `tools.execute()`. **The model never does
anything.** It emits JSON naming a tool; your executor decides whether and how to
run it. Every safety property in this system follows from that one fact.

## 3. Components we wrote

### 3.1 The village (`village/`) - runs headless, no web server

| Component | What it is | Role | Why it exists as its own module |
|---|---|---|---|
| `config.py` | YAML + `.env` → typed dataclasses | Validates configuration once, at startup | So every other module receives a checked object and never reaches for `os.environ` itself. A typo'd tool name stops the process at second zero with the list of valid names, instead of surfacing at turn 40 as an agent burning budget on `no tool named web_serach`. |
| `llm.py` | HTTP client for one chat completion | The **only** module that knows a provider exists | Swapping OpenRouter for native SDKs becomes a one-file change. Cost counting lives here, next to the call that spends the money. Also draws the line the rest of the system depends on: a *transport* failure (429, 502) is retried; a *model mistake* (malformed tool JSON) is handed upward as `parse_error`, never retried. |
| `tools.py` | Registry of 11 tools: schema + implementation | The **only** module that can affect the world | Every safety rail is enforced here, not requested in a prompt. `execute()` never raises - a failure returns an observation the agent can act on. One registry keeps each tool's schema (which the model reads) next to its code (which you run), so they cannot drift. |
| `agent.py` | One villager's turn: the ReAct loop | build prompt → call → execute → observe → repeat until `end_turn` | `take_turn()` is ~70 lines of code, and it is the thing the whole project exists to demonstrate. Bounded by `max_steps=6` so one confused villager cannot eat the session budget. |
| `memory.py` | Prompt assembly + compaction | Decides what an agent knows when it wakes up | Context does not grow, but history does. A prompt is four bounded parts: persona+goal, compacted notes, last N events, tool schemas. Also the single place that decides what history *looks like* to a model - the first place to tune when behaviour disappoints. |
| `store.py` | SQLite wrapper over one `events` table | The single source of truth for all state | UI, replay, cost, and debugging are all *queries* over this table, so nothing can drift out of sync. `recent_for_prompt()` enforces the visibility rule in SQL, not in Python, so no caller can forget it. |
| `orchestrator.py` | The scheduler | Who acts, when, and when to stop | Round-robin turns, and four stop conditions all leaving through one `finally` that writes `session_end`. A session that merely stops emitting events is indistinguishable from a crash six weeks later. |
| `prompts/*.md` | Prompt text as versioned files | Prompt surface | Prompts are the highest-leverage thing you will tune. `git log village/prompts/` should show exactly what changed on the run where behaviour improved. Not string literals buried in `.py`. |
| `fake.py` | A scripted stand-in with `llm.chat`'s exact signature | Drives the whole pipeline offline | Lets you demo the UI, debug the log, and test the deploy with no key, no balance and no network. A test asserts the signature still matches, because the moment they drift an offline run stops proving anything about a live one. |

**The two chokepoints:** `llm.py` (only module that knows a provider) and
`tools.py` (only module that can affect the world). Everything else is
arrangement.

### 3.2 The site (`server/`, `web/`)

| Component | What it is | Role | Why |
|---|---|---|---|
| `server/main.py` | ~140-line FastAPI app | Reads the log, pushes it to browsers | Deliberately thin. If you feel tempted to *compute* something here that the orchestrator also computes, the value belongs in an event instead. |
| `web/index.html` | One file: HTML + CSS + vanilla JS | The spectator page | A log viewer with a WebSocket and `appendChild`. A build step and a framework would buy nothing. Agent output is inserted with `textContent`, never `innerHTML` - a villager could emit `<script>`. |

Eight routes, and it is worth knowing why each exists:

| Route | Direction | Purpose |
|---|---|---|
| `GET /` | read | serves `web/index.html` |
| `GET /healthz` | read | one cheap query, so a monitor can tell "process up" from "database gone" |
| `GET /api/sessions` | read | list runs, so the page can default to the newest |
| `GET /api/events` | read | full replay of one session; also the page's initial load |
| `GET /api/artifact` | read | the file the village wrote, so a visitor can read the product and not only the process |
| `WS /ws` | read (push) | live tail; client sends its highest seen id so a reconnect resumes rather than replaying |
| `POST /api/message` | write | a spectator speaks into the group chat, once per 20s |
| `POST /api/stop` | write | the kill switch; requires `X-Village-Token` once set |

The two writes are the *human's*, never an agent's. A human message is stored as
a `system` event with `kind=human_message`, not as a `chat` event from a fake
agent, because the log must never blur what a model said with what a person said.

### 3.3 Entry points and support

| Component | Role | Why |
|---|---|---|
| `scripts/run_session.py` | run one session | Entry points stay thin - parse args, load config, call `orchestrator.run_session`. Logic in an entry point is logic tests cannot reach. |
| `scripts/preflight.py` | check the provider before spending | A bad model id, an empty balance, and a model that answers with prose all look identical from inside the loop: a villager that talks and never acts. Each costs a whole session to notice. Preflight makes one real ~$0.0002 tool call per model, because catalogue metadata is a claim and a tool call is proof. |
| `scripts/replay.py` | print a past session | Twelve lines, because the event log did the work. Every feature that reads history is a query, not a subsystem. |
| `scripts/dev.sh` | one-word wrappers | The commands you run fifty times a day should be one word. |
| `tests/` | 70 offline tests | A test suite that needs an API key is one you stop running. The loop is driven through an injected `chat_fn`, which is the same seam `--fake` uses. |
| `configs/*.yaml` | the cast and the goal, as data | Adding a fifth villager or running bigger models is a config edit. It also makes runs *comparable* - one thing changes at a time, which is the difference between an experiment and an anecdote. |
| `deploy/` | systemd units, Caddyfile, bootstrap | See §6. |

## 4. Third-party libraries, and what each one is actually for

For each library: the job it does here, and what would have had to be written
by hand without it.

### FastAPI

**What it is:** a Python web framework for defining HTTP routes and WebSocket
endpoints, built on Starlette (async plumbing) and Pydantic (validation).

**Role here:** it is the entire site - eight route functions in ~140 lines.

**Why we need it:** three things it gives us that would otherwise be hand-written
work. First, **WebSocket support as a first-class route** (`@app.websocket`) -
the live feed is the whole point of the site, and Flask has no native WebSocket.
Second, **request validation from type hints**: `async def stop(body:
StopRequest)` means a malformed POST is rejected with a 422 before our code runs,
so no endpoint contains type checking. Third, **async**, so one process can hold
many open WebSockets while sleeping between polls, instead of one thread per
viewer.

**What we would use instead:** raw Starlette is the honest alternative - FastAPI
*is* Starlette plus validation - and would cost maybe 20 extra lines. Flask is
the wrong shape here because of the WebSocket. Django is orders of magnitude more
machinery than five endpoints justify.

### Uvicorn

**What it is:** an ASGI server.

**Role here:** the process that actually runs. `uvicorn server.main:app` opens
the TCP socket, speaks HTTP/1.1 and the WebSocket upgrade, and runs the asyncio
event loop that our `async def` handlers live in.

**Why we need it:** FastAPI is a framework, not a server. It only *describes*
routes; it cannot accept a connection. Something has to bind a port and turn
bytes into calls to our functions - that split (framework vs server, connected by
the **ASGI** interface) is the same as Django/Rails needing gunicorn or Puma. If
someone asks "what is ASGI", the answer is: the async successor to WSGI, and the
reason a WebSocket can exist in the same app as a GET.

**In production** it is bound to `127.0.0.1:8000` and Caddy is the only thing
that talks to it - see §6.

### Pydantic

**What it is:** runtime data validation driven by Python type hints. Ships with
FastAPI.

**Role here:** two models, `HumanMessage` and `StopRequest`, describing the JSON
bodies of the two write endpoints.

**Why we need it:** those two endpoints are the only places a stranger's data
enters this system. Declaring the shape means a request with a missing field or
a number where a string belongs is rejected by the framework, and our handler
only ever runs on well-formed input.

### SQLite (`sqlite3`, Python standard library)

**What it is:** an embedded SQL database - a library plus one file, with no
server process.

**Role here:** the single source of truth. One table, append-only.

**Why we need it, and why not Postgres:** the workload is a few writes per
second from exactly one writer, and reads that are all "give me the rows for this
session after id N". SQLite handles that on a laptop and on a $6 droplet
identically, with zero operations work and one file to back up. Postgres would
add a service to install, secure, monitor and back up, and would buy nothing at
this scale. Two settings make it work here:

- **WAL mode** (`PRAGMA journal_mode=WAL`) lets the site read while the village
  writes. Without it the reader gets `database is locked` the first time both run.
- **`check_same_thread=False`**, because FastAPI reads the connection from a
  different thread than it was created on. Safe here only because writes are
  single-threaded (round-robin turns) and the server only SELECTs.

**Why not an ORM:** there is one table and about eight queries. An ORM would add
a mapping layer to hide SQL we want to read.

### requests

**What it is:** the standard synchronous HTTP client.

**Role here:** every runtime HTTP call - `llm.py` (OpenRouter), `tools.py`
(Tavily, `fetch_url`), `scripts/preflight.py`. One client, deliberately: an
earlier draft used `httpx` for `fetch_url` and `requests` for the rest, and two
HTTP clients for four call sites was one too many. `httpx` survives only as a
dev dependency, because FastAPI's `TestClient` is built on it.

**Why not a vendor SDK:** the entire integration with OpenRouter is a POST with
a JSON body. Hand-rolling it is what makes `llm.py` provider-agnostic and keeps
the wire format visible.

**Why not async:** the village is a single-threaded turn loop that spends its
life waiting on one model call at a time. Async buys concurrency that D3
deliberately rejects.

### PyYAML

**Role:** parses `configs/agents.yaml` and `configs/season.yaml`.

**Why YAML and not JSON:** these two files are the ones a human edits most, and
they carry multi-line personas and explanatory comments. JSON has no comments and
no multi-line strings, and half the value of these files is the commentary next
to each choice.

**One security note worth knowing:** the code calls `yaml.safe_load`, never
`yaml.load`. Plain `load` can construct arbitrary Python objects from a YAML
file, which is a remote code execution bug in any project that ever reads a file
it did not write.

### python-dotenv

**Role:** reads `.env` into `os.environ` at startup.

**Why:** secrets have to live somewhere that is never committed. The split is the
point: `configs/` is committed and diffable, `.env` never is. Anything that would
be a leak in a screenshot goes in `.env`; anything you want visible in a pull
request goes in YAML. In production systemd supplies the same variables from
`/etc/village.env` and dotenv simply finds nothing to load - which is why nothing
in the code cares which mechanism set the variable.

### ddgs

**Role:** the keyless search backend, used when there is no Tavily key.

**Why:** so that anyone - including whoever is evaluating this project - can
clone the repo and run it with zero accounts. It is rate-limited and is the wrong
choice for a 120-turn session, which is what the Tavily path is for.

### Dev-only: pytest, ruff, uv, setuptools

| Tool | Role | Why |
|---|---|---|
| **pytest** | test runner | Plain functions and `assert`; fixtures give each test its own temp database. 70 tests, no network, ~1s. |
| **ruff** | linter | One fast binary replacing flake8+isort+pyupgrade. Configured to `E, F, I, UP` only - deliberately not the opinionated refactor rules, because this repo is read as an explanation and a linter that rewrites a clear line into a clever one works against that. |
| **uv** | venv + installer | A much faster `pip`/`venv`. Nothing depends on it; `python -m venv` and `pip` work identically. |
| **setuptools** | build backend | Makes `pip install -e .` work, which is what puts `village/` and `server/` on the import path so `from village.store import Store` resolves from any directory. `[tool.setuptools] packages = ["village", "server"]` is required because `configs/`, `runs/` and `web/` sit next to the code and setuptools refuses to guess which are Python packages. |

## 5. External services and protocols

### OpenRouter

**What it is:** one OpenAI-compatible API endpoint that proxies to many model
providers.

**Role:** every model call in the system goes to
`https://openrouter.ai/api/v1/chat/completions`.

**Why:** one key, one bill, one spend dashboard, and changing a villager from
Claude to DeepSeek is editing a string in `agents.yaml`. Three concrete
properties this project depends on:

1. **One wire format** for four model families, so `agent.py` does not branch per
   provider.
2. **`usage.cost` on every response**, in dollars, with no request flag - so the
   spend guard uses the provider's own number instead of a local price table that
   silently drifts the day someone reprices.
3. **An account-level spend cap** in their dashboard, which is a rail that a bug
   in *this* repo cannot switch off. `VILLAGE_MAX_USD` is the other rail. Two
   independent rails is the design.

The cost is roughly a 5.5% fee and one more party in the request path. At $0.50
a session that is irrelevant; at production volume it would not be.

### Tavily

**What it is:** a search API whose results are formatted for LLM consumption.

**Role:** the preferred `web_search` backend when `TAVILY_API_KEY` is set.

**Why:** its snippets are markedly cleaner than scraped ones, which directly
reduces tokens - and every search result is re-sent on every following step of
the same turn. A failure here falls through to DuckDuckGo rather than reaching
the agent as an error.

### Tool calling (function calling)

**What it is:** a provider feature. You send a list of JSON-Schema function
definitions alongside the messages; instead of prose, the model may return
`tool_calls: [{name, arguments}]`.

**Role:** it is the entire action space. `tools.schemas_for()` builds that list.

**Why it matters conceptually:** the model returns a *request* to act. Nothing
happens until your code decides to run it. This is why "the model cannot run
shell commands" is a true statement about this system rather than a hope - there
is no shell tool in the registry, so there is no JSON the model can emit that
would run one.

### WebSocket

**What it is:** a persistent, bidirectional connection opened by upgrading an
HTTP request.

**Role:** `/ws` pushes new events to every open browser tab.

**Why not the alternatives:** HTTP polling from the browser would mean a new
request every second per viewer and a visible lag. Server-Sent Events would
genuinely have been fine - it is one-way, which is all we use - and is the
alternative to name if asked. WebSocket was chosen because FastAPI supports it
natively and reconnect-with-a-cursor is trivial: the client sends its highest
seen event id, so a dropped connection resumes exactly where it left off instead
of replaying from zero.

**The tradeoff:** the *server* still polls SQLite every 0.7s, because the
village is a separate process and a real notification channel would mean a queue
between them. That is the right call at four agents and would be the wrong one
at four hundred.

### Event sourcing

**What it is:** storing the sequence of things that happened, and deriving all
state from it, rather than storing current state and overwriting it.

**Role:** the `events` table is the only state in the system.

**Why:** when an agent goes off the rails at turn 87 you do not guess - you
replay the log. Cost, turn count, who stopped the run, what each agent saw: all
queries, no second copy to drift. It is the same idea behind LLM tracing tools
like LangSmith, applied with one table instead of a product.

## 6. Deployment components

| Component | Role | Why |
|---|---|---|
| **DigitalOcean droplet** (Ubuntu 24.04, 1 GB, Toronto) | the machine | The box never runs a model - it sends HTTP and waits. Peak memory is one Python interpreter and a SQLite page cache. |
| **systemd** | process supervision | Already on the box, no extra dependency. Starts uvicorn at boot, restarts it if it dies, captures stdout into `journalctl`, and runs the session on a schedule. Also where the sandboxing lives: `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, and `ReadWritePaths=/opt/village/runs` mean a compromised web process can write to exactly one directory. |
| `village-web.service` | always-on site | `Restart=always` is right here: the process holds no state, so restarting it loses nothing. |
| `village-session.service` + `.timer` | one session, weekly | `Type=oneshot`, because a session is a **bounded job** - it has a turn cap and a spend cap and ending is the expected outcome. `Restart=always` on this unit would be a machine that spends money forever. |
| **Caddy** | TLS + reverse proxy on :443 | Gets and renews the certificate by itself, with no certbot cron that fails silently in month three. `reverse_proxy` handles the WebSocket upgrade with no extra directives - nginx needs three explicit headers for the same thing, and forgetting one gives you a page that loads and then never updates. |
| **`/etc/village.env`** | secrets, mode 600 | Unit files are world-readable, so no key ever appears in one. |

**Why not Docker:** the artifact is one Python process and one SQLite file. A
container adds a build step, an image, and a volume mount to arrive at the same
place. This is reversible - a Dockerfile would be ten lines the day a second
deployment target appears.

## 7. The rules that keep this from turning into mud

```
scripts/  ->  village/  ->  store.py  <-  server/  ->  web/
   |              |
   |              +-> llm.py   (only module that knows a provider exists)
   |              +-> tools.py (only module that can affect the world)
   |              +-> fake.py  (a stand-in for llm.chat, for --fake)
   |
   +-> preflight.py (checks the provider before a paid run)
```

1. **`village/` never imports `server/`.** The orchestrator must run headless.
   This one constraint is what lets you debug an entire session in a terminal and
   keeps the UI a pure reader rather than a participant.
2. **Everything flows through the event log.** If the UI needs to know something,
   an event should carry it. Resist a second in-memory object the UI reads - that
   is how two views drift apart.
3. **`execute()` never raises, and `llm.py` never retries a model mistake.** One
   404 at turn 60 must not kill a run you paid for.

## 8. Where each concept lives

| Concept | Where |
|---|---|
| Agent loop / ReAct | `village/agent.py` |
| Tool calling & schemas | `village/tools.py` |
| Context management & compaction | `village/memory.py`, `Agent.compact` |
| Bounded memory across a long run | `store.notes_for` + the `compaction` event (D31) |
| Multi-agent coordination | `village/orchestrator.py` + `send_chat` |
| Observability / event sourcing | `village/store.py` |
| Prompt injection & sandboxing | `fetch_url` envelope, `write_file` path check |
| Cost engineering | `village/llm.py` `SpendGuard`, decisions D9-D11 |
| Web layer & real-time | `server/main.py`, `web/index.html` |
| Testing an agent system | `village/fake.py` + `tests/` |
| Failing before you spend | `scripts/preflight.py` |
| Threat model of a public URL | `server/main.py` `/api/stop`, decision D28 |
| Deployment & operations | `deploy/`, `docs/deploy.md` |
| Knowing when to stop | `tools.vote_done` + `orchestrator.run_session` (D36) |
| Scope judgment | `docs/decisions.md` |
