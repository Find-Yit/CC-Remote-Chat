#!/bin/bash
# 安全地跑全部单测：在 macOS sandbox 里禁止访问真实的本机 tmux 套接字目录（/private/tmp/tmux-<uid>），
# 跑前跑后比对 `tmux -L ccr list-panes -a`，确保没有碰到用户正在用的会话（2026-11-23 曾因测试清理误杀真实会话）。
# 3 个依赖 ps 的测试在沙箱里必然失败，会在沙箱外单独再跑一次。
# 用法：tools/test.sh [unittest 参数，默认 discover 全部]
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3)"
UID_N="$(id -u)"
PROFILE="$(mktemp "${TMPDIR:-/tmp}/ccr-test-sandbox.XXXXXX")"
trap 'rm -f "$PROFILE"' EXIT
cat > "$PROFILE" <<SB
(version 1)
(allow default)
(deny file-read* file-write* (subpath "/private/tmp/tmux-$UID_N"))
(deny network-outbound (regex #"^/private/tmp/tmux-$UID_N/"))
SB
panes() { tmux -L ccr list-panes -a -F '#{session_name} #{pane_pid}' 2>/dev/null | sort; }
BEFORE="$(panes)"
PS_TESTS="tests.test_agent_link.FakeEndToEndTests tests.test_api_keys.AgentStartKeyTests tests.test_node_agent.ProcessTreeTests"
if [ $# -gt 0 ]; then
  /usr/bin/sandbox-exec -f "$PROFILE" "$PY" -m unittest "$@"; RC=$?
else
  LOG="$(mktemp "${TMPDIR:-/tmp}/ccr-test-log.XXXXXX")"
  /usr/bin/sandbox-exec -f "$PROFILE" "$PY" -m unittest discover -s tests -t . >"$LOG" 2>&1
  tail -3 "$LOG"
  BAD="$(grep -E '^(FAIL|ERROR):' "$LOG" | grep -vE 'FakeEndToEndTests|AgentStartKeyTests|ProcessTreeTests' || true)"
  if [ -n "$BAD" ]; then echo "沙箱内失败（非 ps 相关）："; echo "$BAD"; echo "完整日志：$LOG"; RC=1; else RC=0; fi
  echo "--- 沙箱外跑依赖 ps 的测试"
  # shellcheck disable=SC2086
  "$PY" -m unittest $PS_TESTS 2>&1 | tail -2 || RC=1
fi
AFTER="$(panes)"
if [ "$BEFORE" != "$AFTER" ]; then echo "!!! 本机真实 tmux（-L ccr）的会话在测试前后不一致，请立即检查"; RC=2; else echo "本机真实 tmux 未受影响"; fi
exit $RC
