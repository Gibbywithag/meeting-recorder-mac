#!/bin/zsh
# Developer setup: a local .venv for running the Python tests. (Installing the app: ./build_app.sh)
set -euo pipefail
cd "$(dirname "$0")"
PY=""
for v in python3.14 python3.13 python3.12 python3.11; do
  for d in /opt/homebrew/bin /usr/local/bin; do [ -x "$d/$v" ] && { PY="$d/$v"; break 2; }; done
done
[ -n "$PY" ] || { echo "[setup] ERROR: need Python 3.11+. Install it with: brew install python@3.13"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q google-genai python-dotenv pytest
command -v ffmpeg >/dev/null || { echo "[setup] ERROR: ffmpeg not found (brew install ffmpeg)"; exit 1; }
echo "[setup] OK. Run the tests with: ./.venv/bin/python -m pytest -q tests"
