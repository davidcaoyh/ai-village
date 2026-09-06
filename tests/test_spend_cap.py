"""The rail SpendGuard cannot see: total spend across sessions in a rolling day.

`SpendGuard` is per session and in process. A timer that starts a run every few
minutes hands each one a fresh $2 cap and nothing watches the sum, so the ceiling on
an unattended day is (sessions per day) x (per-session cap). This is the other rail.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from village.config import load_settings
from village.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "v.db"))


def test_it_sums_across_sessions_not_just_one(store):
    store.append("s1", "a", "thought", {"usd": 0.10})
    store.append("s2", "a", "thought", {"usd": 0.25})
    store.append("s3", "b", "thought", {"usd": 0.05})

    assert store.spend_since(0) == pytest.approx(0.40)
    assert store.session_cost("s1") == pytest.approx(0.10)      # the per-session view


def test_the_window_is_respected(store):
    store.append("s1", "a", "thought", {"usd": 1.00})

    assert store.spend_since(time.time() - 60) == pytest.approx(1.00)
    assert store.spend_since(time.time() + 60) == 0.0


def test_only_thought_events_carry_money(store):
    """A `chat` or `action` row has no usd. Counting them would read as 0 and hide a bug."""
    store.append("s1", "a", "thought", {"usd": 0.10})
    store.append("s1", "a", "action", {"name": "send_chat"})
    store.append("s1", None, "system", {"kind": "session_end", "usd": 999.0})

    assert store.spend_since(0) == pytest.approx(0.10)          # not 999.10


def test_the_setting_reads_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("VILLAGE_MAX_USD_PER_DAY", "7.5")
    assert load_settings().max_usd_per_day == 7.5

    monkeypatch.delenv("VILLAGE_MAX_USD_PER_DAY")
    assert load_settings().max_usd_per_day == 5.0               # the default


def _run(env, tmp_path, extra=()):
    return subprocess.run(
        [sys.executable, "-m", "scripts.run_session", "--turns", "1", *extra],
        capture_output=True, text=True, env={**env, "PATH": "/usr/bin:/bin",
                                             "VILLAGE_DB_PATH": str(tmp_path / "v.db")})


def test_a_run_over_the_daily_cap_refuses_to_start(tmp_path):
    """Exit non-zero so systemd marks the unit failed instead of logging a quiet no-op."""
    Store(str(tmp_path / "v.db")).append("old", "a", "thought", {"usd": 6.0})
    out = _run({"OPENROUTER_API_KEY": "sk-or-test", "VILLAGE_MAX_USD_PER_DAY": "5.0"},
               tmp_path)

    assert out.returncode == 1
    assert "daily cap reached" in out.stdout
    assert "$6.0000 spent in the last 24h" in out.stdout


def test_zero_disables_the_cap(tmp_path):
    """Turning it off must not be an accident: 0 is explicit, an unset var is the default."""
    Store(str(tmp_path / "v.db")).append("old", "a", "thought", {"usd": 6.0})
    out = _run({"OPENROUTER_API_KEY": "sk-or-test", "VILLAGE_MAX_USD_PER_DAY": "0"},
               tmp_path)

    assert "daily cap reached" not in out.stdout               # got past the gate


def test_a_fake_run_is_never_gated(tmp_path):
    """--fake spends nothing, so a cap on real money must not block an offline test."""
    Store(str(tmp_path / "v.db")).append("old", "a", "thought", {"usd": 99.0})
    out = _run({"VILLAGE_MAX_USD_PER_DAY": "5.0"}, tmp_path, extra=("--fake", "--delay", "0"))

    assert out.returncode == 0
    assert "daily cap reached" not in out.stdout
