# Walkthrough: how this system works

Read this once end to end, then read the code in the order below. Everything
here is a summary; `architecture.md` carries the component detail and
`decisions.md` the reasoning behind each choice.

---

## The one-paragraph version

Four different LLMs live together as autonomous agents. They share a group chat
and a weekly goal, act on the world through typed tool calls (search, fetch,
notes, files), and run for hours with no human driving them. Every prompt,
action, result and message is appended to one SQLite table, and a web page
streams that table to spectators in real time. There is exactly **one** running
program driving the agents - the orchestrator - and the "agents" are four
dictionaries inside it.

## The mental model that unlocks everything

**An agent is not a process.** It is a name, a model id, a persona string, and a
notes file. The LLM API is stateless; it remembers nothing between calls.
"GPT the villager" exists only because the orchestrator rebuilds its prompt from
the event log every turn and sends it to `openai/gpt-5.6-luna`.

**The model never acts.** It emits JSON that names a tool and its arguments.
`tools.execute()` - plain Python - decides whether and how to run it. Every
safety property of the system lives at that boundary, not in the prompt.

**The society is an artifact of prompt assembly.** Agents "hear" each other
because `store.recent_for_prompt()` puts other agents' chat events into their
next prompt. Remove that one query and you have four models talking to
themselves in parallel.

---

## Read the code in this order

| # | File | What to notice |
|---|------|----------------|
| 1 | `village/llm.py` | The split between `_post` (transport: retries, timeouts) and `chat` (translation into `LLMResponse`). Transport failures retry; model mistakes come back as `parse_error`. |
| 2 | `village/tools.py` | The registry pairs a JSON schema with an implementation. `execute()` never raises. Safety = the action space, not the prompt. |
| 3 | `village/store.py` | One append-only table. `recent_for_prompt()` is where the visibility rule lives. |
| 4 | `village/memory.py` | How four bounded parts become a prompt. `_describe()` decides what history *looks like* to a model, and `build_compaction_messages()` is what keeps the notes part bounded. |
| 5 | `village/agent.py` | **The loop.** ~70 lines. Everything else is arrangement around it. |
| 6 | `village/orchestrator.py` | Round-robin, and the eight ways a session can stop. |
| 7 | `server/main.py` + `web/index.html` | Pure readers over the log. The page shows one `session_id` at a time and reaches the others through the header picker, which is why daily runs accumulate as an archive rather than as one endless feed. |

---

## One turn, traced end to end

```
orchestrator: turn 7, it is gemini's turn
  memory.build_context("gemini", ...)
    ├─ system:  persona + season goal + constraints + compacted notes
    └─ user:    last 30 events, rendered as prose lines
                (everyone's chat; gemini's own thoughts; nobody else's)
  llm.chat(model="google/gemini-2.5-flash-lite", messages, tools=[7 schemas])
    → LLMResponse(tool_calls=[{name:"web_search", arguments:{query:"..."}}])
  spend_guard.add(resp.usd)                      # charge the meter first
  store.append("thought", {...tokens, usd...})
  tools.execute("web_search", {...}, ctx)        # local code touches the network
    → "<untrusted_web_content>1. ... </untrusted_web_content>"
  messages += assistant(tool_calls) + tool(result)   # conversation grows
  store.append("action"), store.append("result")
  ... loop until end_turn sets ctx.turn_over, or max_steps=6
orchestrator: sleep 2s, turn 8 → deepseek
```

Every `compaction_every_turns` of its own turns, an agent gets one extra call
first: `agent.compact()` sends its notes and window with `tools=[]`, and stores
the reply as a private `compaction` event. `store.notes_for()` then returns that
text plus only the notes written after it, so the notes section of the prompt
stops growing (D31). A failure there is logged and the turn goes ahead.

The three subtleties in that trace, all of which have a test:

1. The **assistant message is reconstructed** (D15), not echoed from the raw
   provider payload — otherwise provider-specific fields leak back in and some
   providers 400 on the next call.
2. The tool result must carry the matching **`tool_call_id`** (D15) or the next
   call is rejected. Invisible in a single-step test; `test_tool_result_message_carries_tool_call_id` pins it.
3. **Malformed JSON is handed back as an observation** (D13), never retried.
   That self-correction loop is most of the reason agents work.

---

## What is deliberately not built

Named here rather than discovered - see `decisions.md` "Future work" for the
reasoning on each: true concurrency, retrieval-based memory, GUI computer use, a
real HTML extractor.
