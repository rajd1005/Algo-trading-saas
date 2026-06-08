#!/usr/bin/env bash
# One command to start the app. Run it from the project folder:  bash run.sh
set -e
cd "$(dirname "$0")/backend"

# Create a virtual environment the first time only.
if [ ! -d ".venv" ]; then
  echo "First run: creating virtual environment and installing packages..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

echo "Starting Algo Trading dashboard on http://0.0.0.0:8000 ..."
./.venv/bin/python main.py
