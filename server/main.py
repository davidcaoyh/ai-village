"""The site. Reads the event log and pushes it to browsers, and nothing else.

It cannot start a session: the orchestrator is a separate process writing to the
same SQLite file in WAL mode. A reloaded tab or a restarted uvicorn therefore
cannot disturb a run that costs money, and a viewer can attach to a session that
is already three hours old.

If you are ever tempted to compute something here that the orchestrator also
computes, that value belongs in an event instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from village.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DB_PATH = os.environ.get("VILLAGE_DB_PATH", "runs/village.db")
ADMIN_TOKEN = os.environ.get("VILLAGE_ADMIN_TOKEN", "")
MESSAGE_COOLDOWN = 20        # seconds between spectator messages, per session
POLL_SECONDS = 0.7           # two processes, one file: polling an indexed read is
                             # cheaper than the infrastructure a pubsub would need

app = FastAPI(title="AI Village")
store = Store(DB_PATH)       # one connection, opened once, for a process that only reads
_last_message: dict[str, float] = {}


class MessageIn(BaseModel):
    message: str
    session: str | None = None


class StopIn(BaseModel):
    session: str | None = None


def _session_or_404(session: str | None) -> str:
    session = session or store.latest_session()
    if not session:
        raise HTTPException(404, "no sessions yet")
    return session


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/healthz")
def healthz():
    """One cheap query, so a monitor can tell 'process up' from 'database gone'."""
    n = store.db.execute("SELECT COUNT(DISTINCT session_id) AS n FROM events").fetchone()["n"]
    return {"ok": True, "sessions": n}


@app.get("/api/sessions")
def sessions():
    return store.sessions()


@app.get("/api/events")
def events(session: str | None = None, after_id: int = 0, limit: int = 5000):
    session = _session_or_404(session)
    return {
        "session": session,
        "events": store.tail(session, after_id, limit),
        "cost": store.session_cost(session),
        "stopped": store.stop_requested(session),
    }


@app.post("/api/message")
def message(body: MessageIn):
    """A spectator speaks into the village.

    Stored as a `system` event, never as a `chat` event from a fake agent: the log
    must not blur what a model said with what a person said. Rate limited because a
    viewer typing continuously turns the village into a chat toy, and the transcript
    stops being evidence of anything.
    """
    session = _session_or_404(body.session)
    text = body.message.strip()[:500]
    if not text:
        raise HTTPException(400, "empty message")
    now = time.time()
    if now - _last_message.get(session, 0.0) < MESSAGE_COOLDOWN:
        raise HTTPException(429, f"one message per {MESSAGE_COOLDOWN} seconds")
    _last_message[session] = now
    store.append(session, None, "system", {"kind": "human_message", "message": text})
    return {"ok": True}


@app.post("/api/stop")
def stop(body: StopIn, x_village_token: str | None = Header(default=None)):
    """The kill switch.

    A spectator message is content the village is free to ignore. Stopping is
    control over a run that costs money and cannot be resumed, so on a public URL
    an anonymous stop button is a griefing vector rather than a feature. The token
    is empty by default, which is right on a laptop and wrong on a host, so the
    systemd unit always sets one.
    """
    if ADMIN_TOKEN and x_village_token != ADMIN_TOKEN:
        raise HTTPException(401, "admin token required")
    session = _session_or_404(body.session)
    store.request_stop(session)
    return {"ok": True, "session": session}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Live tail. The client sends its highest seen id so a reconnect resumes."""
    await websocket.accept()
    session = websocket.query_params.get("session") or store.latest_session()
    after = int(websocket.query_params.get("after_id", 0))
    try:
        while True:
            session = session or store.latest_session()
            if session:
                new = store.tail(session, after, limit=200)
                if new:
                    after = new[-1]["id"]
                    await websocket.send_text(json.dumps({
                        "session": session, "events": new,
                        "cost": store.session_cost(session),
                        "stopped": store.stop_requested(session),
                    }))
            await asyncio.sleep(POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception:                                          # noqa: BLE001
        return                                                 # a dead socket is not an outage
