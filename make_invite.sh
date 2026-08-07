#!/usr/bin/env bash
# Render the invitation video. Pass through any flags the CLI understands,
# e.g.  ./make_invite.sh --still 20   or   ./make_invite.sh --jobs 8
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Creating .venv and installing dependencies (first run only)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required but was not found on PATH." >&2
  exit 1
fi

exec .venv/bin/python -m invitation "$@"
