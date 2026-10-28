#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <queue-id> [prompt-queue run options...]" >&2
  exit 2
fi

sleep 2h
exec "$SCRIPT_DIR/prompt-queue" run "$@"
