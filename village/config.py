"""YAML and .env into typed objects, validated once at startup.

A typo'd tool name should stop the process at second zero with the list of valid
names, not surface at turn 40 as a villager burning budget on `web_serach`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class AgentConfig:
    name: str
    model: str
    persona: str
    temperature: float = 0.7
    max_tokens: int = 900
    tools: list[str] = field(default_factory=list)
    reasoning: dict[str, Any] | None = None      # per-agent, see decisions D10


@dataclass
class SeasonConfig:
    season_id: str
    goal: str
    turns_per_session: int = 60
    seconds_between_turns: float = 2
    context_window_events: int = 30
    compaction_every_turns: int = 20
    constraints: list[str] = field(default_factory=list)
    title: str = ""


@dataclass
class Settings:
    openrouter_api_key: str
    tavily_api_key: str | None
    db_path: str
    max_turns: int
    max_usd: float
    admin_token: str


def load_agents(path: str, valid_tools: set[str] | None = None) -> list[AgentConfig]:
    raw = yaml.safe_load(open(path))
    defaults = raw.get("defaults", {})
    agents = []
    for entry in raw["agents"]:
        merged = {**defaults, **entry}
        merged["persona"] = merged["persona"].strip()
        cfg = AgentConfig(**merged)
        if valid_tools is not None:
            unknown = [t for t in cfg.tools if t not in valid_tools]
            if unknown:
                raise ValueError(
                    f"{cfg.name} lists unknown tools {unknown}. "
                    f"valid names: {sorted(valid_tools)}"
                )
        agents.append(cfg)
    if not agents:
        raise ValueError(f"no agents defined in {path}")
    return agents


def load_season(path: str) -> SeasonConfig:
    raw = yaml.safe_load(open(path))
    session = raw.get("session", {})
    return SeasonConfig(
        season_id=raw["season_id"],
        title=raw.get("title", ""),
        goal=raw["goal"].strip(),
        constraints=raw.get("constraints", []),
        turns_per_session=session.get("turns_per_session", 60),
        seconds_between_turns=session.get("seconds_between_turns", 2),
        context_window_events=session.get("context_window_events", 30),
        compaction_every_turns=session.get("compaction_every_turns", 20),
    )


def load_settings(require_key: bool = True) -> Settings:
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if require_key and not key:
        raise RuntimeError("OPENROUTER_API_KEY missing. cp .env.example .env and fill it in.")
    tavily = os.environ.get("TAVILY_API_KEY", "").strip()
    # An unedited placeholder is truthy, which would take the keyed path, 401 on every
    # call, and silently skip the keyless fallback that exists for exactly this case.
    if tavily.startswith("tvly-...") or tavily in {"", "..."}:
        tavily = ""
    return Settings(
        openrouter_api_key=key,
        tavily_api_key=tavily or None,
        db_path=os.environ.get("VILLAGE_DB_PATH", "runs/village.db"),
        max_turns=int(os.environ.get("VILLAGE_MAX_TURNS", "120")),
        max_usd=float(os.environ.get("VILLAGE_MAX_USD", "2.0")),
        admin_token=os.environ.get("VILLAGE_ADMIN_TOKEN", ""),
    )
