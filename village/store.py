"""One append-only `events` table. The single source of truth for all state.

The UI, replay, the cost pill and your debugging are all queries over this table,
so nothing can drift out of sync with anything else.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           REAL NOT NULL,
  session_id   TEXT NOT NULL,
  agent        TEXT,
  type         TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session ON events(session_id, id);
"""

PRIVATE_TYPES = ("thought", "compaction")   # reasoning and compacted memory are its own


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- writes ----------------------------------------------------------

    def append(self, session_id: str, agent: str | None, type: str, payload: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO events (ts, session_id, agent, type, payload_json) VALUES (?,?,?,?,?)",
            (time.time(), session_id, agent, type, json.dumps(payload)),
        )
        self.db.commit()
        return cur.lastrowid

    def request_stop(self, session_id: str, who: str = "human") -> int:
        """The kill switch is an event, so 'who stopped this run, and when' is answerable."""
        return self.append(session_id, None, "system", {"kind": "stop_requested", "by": who})

    # --- reads -----------------------------------------------------------

    def tail(self, session_id: str, after_id: int = 0, limit: int = 5000) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
            (session_id, after_id, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def recent_for_prompt(self, session_id: str, viewer: str, limit: int) -> list[dict]:
        """The rolling window as one agent sees it.

        The visibility rule is enforced in SQL rather than in Python so that no
        caller can forget it.
        """
        private = ",".join("?" * len(PRIVATE_TYPES))
        rows = self.db.execute(
            f"""SELECT * FROM events
                WHERE session_id=? AND (agent IS NULL OR agent=?
                                        OR type NOT IN ({private}))
                ORDER BY id DESC LIMIT ?""",
            (session_id, viewer, *PRIVATE_TYPES, limit),
        ).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def latest_compaction(self, agent: str) -> tuple[int, str] | None:
        r = self.db.execute(
            """SELECT id, json_extract(payload_json,'$.text') AS text FROM events
               WHERE agent=? AND type='compaction' ORDER BY id DESC LIMIT 1""",
            (agent,),
        ).fetchone()
        return (r["id"], r["text"]) if r and r["text"] else None

    def notes_for(self, agent: str, limit: int = 8) -> list[str]:
        """Notes are read back out of the log rather than kept in a second table.

        They are `write_note` actions, so they survive across sessions for free and
        there is still exactly one place where state lives. A compaction supersedes
        every note written before it, which is what keeps this section of the prompt
        bounded however long a season runs.
        """
        compaction = self.latest_compaction(agent)
        after_id, notes = (compaction[0], [compaction[1]]) if compaction else (0, [])
        rows = self.db.execute(
            """SELECT payload_json FROM events
               WHERE agent=? AND id>? AND type='action'
                 AND json_extract(payload_json,'$.name')='write_note'
               ORDER BY id DESC LIMIT ?""",
            (agent, after_id, limit - len(notes)),
        ).fetchall()
        for r in reversed(rows):
            args = json.loads(r["payload_json"]).get("arguments") or {}
            if args.get("text"):
                notes.append(args["text"])
        return notes

    def stop_requested(self, session_id: str) -> bool:
        r = self.db.execute(
            """SELECT 1 FROM events WHERE session_id=? AND type='system'
               AND json_extract(payload_json,'$.kind')='stop_requested' LIMIT 1""",
            (session_id,),
        ).fetchone()
        return r is not None

    def session_cost(self, session_id: str) -> float:
        r = self.db.execute(
            """SELECT COALESCE(SUM(json_extract(payload_json,'$.usd')),0) AS c
               FROM events WHERE session_id=? AND type='thought'""",
            (session_id,),
        ).fetchone()
        return round(float(r["c"] or 0.0), 6)

    def sessions(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            """SELECT session_id, MIN(ts) AS started, MAX(id) AS last_id, COUNT(*) AS events
               FROM events GROUP BY session_id ORDER BY started DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_session(self) -> str | None:
        r = self.db.execute("SELECT session_id FROM events ORDER BY id DESC LIMIT 1").fetchone()
        return r["session_id"] if r else None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "ts": r["ts"], "session_id": r["session_id"],
                "agent": r["agent"], "type": r["type"],
                "payload": json.loads(r["payload_json"])}
