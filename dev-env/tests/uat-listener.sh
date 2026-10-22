#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/project"
cat >"$tmp/bin/tmux" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$TEST_TMUX_LOG"
case "$1" in
  has-session) exit 0 ;;
  show-environment) printf 'DEV_BOT_ID=%%5\n' ;;
esac
STUB
chmod +x "$tmp/bin/tmux"
port=$(python3 - <<'PY'
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    print(s.getsockname()[1])
PY
)
printf '{"version":1,"dev_env":{"uat_port":%s}}\n' "$port" >"$tmp/project/.rsh-utils.json"
(cd "$tmp/project" && PATH="$tmp/bin:$PATH" TEST_TMUX_LOG="$tmp/log" "$root/dev-env/dev-env" --attach-uat)
grep -q 'new-window.*agent-time-uat.*serve --project' "$tmp/log"
grep -q "set-environment.*AGENT_TIME_UAT_PORT $port" "$tmp/log"
grep -q 'respawn-pane -k -t %5' "$tmp/log"
if grep -Eq 'kill-session|new-session|__cleanup' "$tmp/log"; then
  echo 'attach must preserve the existing session/backend' >&2; exit 1
fi
for invalid in 0 80 65536 nope; do
  if "$root/dev-env/dev-env" --uat-port "$invalid" --help >/dev/null 2>&1; then exit 1; fi
done
