#!/usr/bin/env bash
# Ask the Salesforce assistant a question (local Qwen by default). See ./ask.sh --help (sf_assistant/cli.py).
# Same as: .venv/bin/python -m sf_assistant ask "$@"
# OKEVAL_* names work as before; each also reads its SFA_* alias (SFA_BASE_URL, SFA_MODEL, SFA_API_KEY ...).
cd "$(dirname "$0")"
export OKEVAL_BASE_URL="${OKEVAL_BASE_URL:-${SFA_BASE_URL:-http://localhost:8000/v1}}"
export OKEVAL_MODEL="${OKEVAL_MODEL:-${SFA_MODEL:-Qwen/Qwen3.6-35B-A3B-NVFP4}}"
export OKEVAL_API_KEY="${OKEVAL_API_KEY:-${SFA_API_KEY:-EMPTY}}"
exec .venv/bin/python -m sf_assistant ask "$@"
