"""The server's contract with the browser, and the one endpoint that is not public.

Everything here is offline: the FastAPI TestClient drives the app in-process
against a temporary SQLite file. There are only four things worth pinning:

  - the page's two read endpoints return the shape the JS expects
  - the kill switch refuses an unauthenticated caller once a token is set (D28)
  - the human-message cooldown actually rejects the second message
  - a human message lands as `system`, never as a `chat` event from an agent,
    because the log must never blur what a model said with what a person said
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from village.store import Store


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """A fresh app bound to a throwaway database.

    server.main reads VILLAGE_DB_PATH and VILLAGE_ADMIN_TOKEN at import time and
    opens the store once, so the env has to be set before the reload - which is
    also the reason those are module-level constants rather than per-request
    lookups: one connection, opened once, for a process that only reads.
    """
    db = tmp_path / "v.db"
    monkeypatch.setenv("VILLAGE_DB_PATH", str(db))
    monkeypatch.setenv("VILLAGE_ADMIN_TOKEN", "s3cret")

    import server.main as main
    importlib.reload(main)

    store = Store(str(db))
    store.append("s1", None, "system", {"kind": "session_start", "goal": "g", "cast": []})
    store.append("s1", "claude", "chat", {"message": "hello village"})
    return TestClient(main.app), store


def test_healthz(app_client):
    client, _ = app_client
    assert client.get("/healthz").json()["ok"] is True


def test_events_endpoint_returns_the_shape_the_page_expects(app_client):
    client, _ = app_client
    body = client.get("/api/events", params={"session": "s1"}).json()
    assert {"events", "cost", "stopped"} <= set(body)
    assert [e["type"] for e in body["events"]] == ["system", "chat"]
    assert body["stopped"] is False


def test_sessions_endpoint_lists_the_run(app_client):
    client, _ = app_client
    assert client.get("/api/sessions").json()[0]["session_id"] == "s1"


def test_stop_requires_the_admin_token(app_client):
    client, store = app_client
    assert client.post("/api/stop", json={"session": "s1"}).status_code == 401
    assert store.stop_requested("s1") is False

    ok = client.post("/api/stop", json={"session": "s1"},
                     headers={"X-Village-Token": "s3cret"})
    assert ok.status_code == 200
    assert store.stop_requested("s1") is True


def test_human_message_is_logged_as_system_not_chat(app_client):
    client, store = app_client
    r = client.post("/api/message", json={"session": "s1", "message": "try a narrower question"})
    assert r.status_code == 200

    ev = store.tail("s1")[-1]
    assert ev["type"] == "system"          # never `chat`: a person is not a villager
    assert ev["agent"] is None
    assert ev["payload"]["kind"] == "human_message"


def test_second_message_is_rate_limited(app_client):
    client, _ = app_client
    client.post("/api/message", json={"session": "s1", "message": "first"})
    again = client.post("/api/message", json={"session": "s1", "message": "second"})
    assert again.status_code == 429


def test_empty_message_is_rejected(app_client):
    client, _ = app_client
    assert client.post("/api/message", json={"session": "s1", "message": "   "}).status_code == 400
