"""load_season: every session key in the YAML has to reach SeasonConfig.

seconds_between_steps was declared on the dataclass and read by Agent.take_turn
but never passed here, so the 45 in season.yaml was dead config and the village
ran its steps back to back. A field that is only ever read through its default
looks identical to a field that works. D51.
"""

import textwrap

import pytest

from village.config import load_season

SEASON = """
season_id: s-test
title: "test"
goal: >
  Write {file} in round {round}.
session:
  turns_per_session: 40
  seconds_between_turns: 3
  seconds_between_steps: 45
  context_window_events: 12
  compaction_every_turns: 7
  max_steps_per_turn: 5
  max_idle_rounds: 2
  max_searches_per_turn: 3
"""


@pytest.fixture
def season_file(tmp_path):
    p = tmp_path / "season.yaml"
    p.write_text(textwrap.dedent(SEASON))
    return str(p)


def test_every_session_key_is_loaded(season_file):
    s = load_season(season_file)
    assert s.turns_per_session == 40
    assert s.seconds_between_turns == 3
    assert s.seconds_between_steps == 45
    assert s.context_window_events == 12
    assert s.compaction_every_turns == 7
    assert s.max_steps_per_turn == 5
    assert s.max_idle_rounds == 2
    assert s.max_searches_per_turn == 3


def test_missing_session_block_uses_defaults(tmp_path):
    p = tmp_path / "season.yaml"
    p.write_text("season_id: s\ngoal: hello\n")
    s = load_season(str(p))
    assert s.seconds_between_steps == 0
    assert s.seconds_between_turns == 2


def test_pacing_is_not_dropped_by_a_partial_session_block(tmp_path):
    p = tmp_path / "season.yaml"
    p.write_text("season_id: s\ngoal: hello\nsession:\n  seconds_between_steps: 12\n")
    s = load_season(str(p))
    assert s.seconds_between_steps == 12
