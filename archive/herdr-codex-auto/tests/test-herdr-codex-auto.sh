#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="$project_dir/herdr-codex-auto"
test_dir="$(mktemp -d)"
trap 'rm -rf -- "$test_dir"' EXIT

mkdir -p "$test_dir/bin"

cat >"$test_dir/bin/herdr" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-} ${2:-}" in
  "agent list")
    printf '%s\n' "$MOCK_AGENTS_JSON"
    ;;
  "agent read")
    printf '%s\n' "$MOCK_SCREEN"
    ;;
  "pane send-keys")
    printf '%s\n' "$3 $4" >>"$MOCK_SEND_LOG"
    ;;
  *)
    printf 'unexpected fake herdr call: %s\n' "$*" >&2
    exit 2
    ;;
esac
EOF
chmod +x "$test_dir/bin/herdr"

export PATH="$test_dir/bin:$PATH"
export HERDR_ENV=1
export HERDR_WORKSPACE_ID=w1
export HERDR_PANE_ID=w1:p1
export MOCK_SEND_LOG="$test_dir/send.log"

export MOCK_AGENTS_JSON='{
  "id": "cli:agent:list",
  "result": {
    "agents": [
      {"agent":"codex","agent_status":"blocked","pane_id":"w1:p1","workspace_id":"w1"},
      {"agent":"codex","agent_status":"blocked","pane_id":"w1:p2","workspace_id":"w1"},
      {"agent":"claude","agent_status":"blocked","pane_id":"w1:p3","workspace_id":"w1"},
      {"agent":"codex","agent_status":"blocked","pane_id":"w2:p1","workspace_id":"w2"}
    ]
  }
}'

export MOCK_SCREEN='Would you like to run the following command?
$ ssh example.test

  Press p to proceed and remember this choice.'

"$script" --once

[[ "$(cat "$MOCK_SEND_LOG")" == "w1:p2 p" ]]

: >"$MOCK_SEND_LOG"
export MOCK_SCREEN='Which environment should I deploy to?

› 1. Staging
  2. Production'

"$script" --once
[[ ! -s "$MOCK_SEND_LOG" ]]

if "$script" --pane w2:p1 --once >/dev/null 2>&1; then
  printf 'expected an out-of-workspace pane to be rejected\n' >&2
  exit 1
fi

if HERDR_ENV=0 "$script" --once >/dev/null 2>&1; then
  printf 'expected execution outside Herdr to be rejected\n' >&2
  exit 1
fi

printf 'herdr-codex-auto tests passed\n'
