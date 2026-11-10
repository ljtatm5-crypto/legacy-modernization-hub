#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
PID_FILE=hub/artifacts/.server.pid
[ -f "$PID_FILE" ] || { echo "  [i] 没有 pid 文件，服务可能没在运行。"; exit 0; }
PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID" 2>/dev/null; sleep 1
  kill -9 "$PID" 2>/dev/null || true
  echo "  已停止服务（PID $PID）。"
else
  echo "  [i] 进程 $PID 已不在。"
fi
rm -f "$PID_FILE"
