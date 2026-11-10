#!/usr/bin/env bash
# Legacy Modernization Hub · macOS / Linux 启动脚本
set -u
cd "$(dirname "$0")"

fail() { printf '  [X] %s\n' "$1"; exit 1; }

command -v node >/dev/null 2>&1 || fail "没找到 Node.js，请安装 18+：https://nodejs.org"
command -v java >/dev/null 2>&1 || fail "没找到 Java，请安装 JDK 17 或 21"
command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 || fail "没找到 Python，请安装 3.9+"

printf '  Node   %s\n' "$(node -v)"
printf '  Java   %s\n' "$(java -version 2>&1 | head -1)"
printf '  Python %s\n\n' "$(python3 -V 2>/dev/null || python -V)"

URL="http://127.0.0.1:${PORT:-8787}"

if [ -f hub/artifacts/.server.pid ]; then
  OLD=$(cat hub/artifacts/.server.pid 2>/dev/null || true)
  if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null; then
    printf '  [i] 已在运行（PID %s），直接打开页面。\n' "$OLD"
    (command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true
    exit 0
  fi
  rm -f hub/artifacts/.server.pid
fi

mkdir -p hub/artifacts
nohup node hub/backend/server.js > hub/artifacts/server.out 2>&1 &
echo $! > hub/artifacts/.server.pid
sleep 2
(command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true
printf '  已启动：%s\n  日志：hub/artifacts/server.out\n  停止：./stop.sh\n' "$URL"
