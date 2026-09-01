#!/usr/bin/env bash
# The commands you run fifty times a day should be one word.
set -euo pipefail
case "${1:-help}" in
  install)   uv venv && uv pip install -e ".[dev]" ;;
  test)      shift; pytest -q "$@" ;;
  preflight) shift; python -m scripts.preflight "$@" ;;
  fake)      shift; python -m scripts.run_session --fake --turns "${1:-16}" --delay 0 ;;
  run)       shift; python -m scripts.run_session "$@" ;;
  serve)     uvicorn server.main:app --host 0.0.0.0 --port 8000 ;;
  replay)    shift; python -m scripts.replay "$@" ;;
  lint)      ruff check . ;;
  *) echo "usage: bash scripts/dev.sh {install|test|preflight|fake|run|serve|replay|lint}" ;;
esac
