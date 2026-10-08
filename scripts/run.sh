#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ ! -x .venv/bin/python ]]; then
  echo "Create the environment first: python3 -m venv .venv"
  echo "Then install: .venv/bin/python -m pip install --upgrade pip"
  echo "             .venv/bin/python -m pip install -e '.[dev]'"
  exit 1
fi
if [[ "${1:-demo}" == "live" ]]; then
  export KUBERECOVERY_MODE=live
  export KUBERECOVERY_KUBECONFIG="$PROJECT_DIR/.data/live/kubeconfig"
  if [[ ! -f "$KUBERECOVERY_KUBECONFIG" || ! -f .data/live/api-token ]]; then
    echo "First run: .venv/bin/python scripts/lab.py setup --context kind-kuberecovery"
    exit 1
  fi
  KUBERECOVERY_CONTEXT="$(.venv/bin/python -c 'import json; print(json.load(open(".data/live/kubeconfig"))["current-context"])')"
  export KUBERECOVERY_CONTEXT
  KUBERECOVERY_TOKEN="$(cat .data/live/api-token)"
  export KUBERECOVERY_TOKEN
elif [[ "${1:-demo}" == "demo" ]]; then
  export KUBERECOVERY_MODE=demo
else
  echo "Usage: bash scripts/run.sh [demo|live]"
  exit 1
fi
export PYTHONPATH="$PROJECT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m uvicorn kuberecovery.app:create_app --factory --host 127.0.0.1 --port "${PORT:-8080}" --workers 1
