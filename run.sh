#!/usr/bin/env bash
# 服务进程管理：./run.sh start|stop|restart|status|logs|foreground
# 运行参数全走环境变量（无配置文件）：
#   API_USAGE_HOST(默认 0.0.0.0) / API_USAGE_PORT(默认 2697) /
#   API_USAGE_DB(默认 ./data/api_usage.db) / API_USAGE_TOKEN(默认空 = 不校验)
# 例：API_USAGE_TOKEN=xxx ./run.sh start
set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
PID_FILE="logs/server.pid"
LOG_FILE="logs/server.log"

HOST="${API_USAGE_HOST:-0.0.0.0}"
PORT="${API_USAGE_PORT:-2697}"
DB="${API_USAGE_DB:-$(pwd)/data/api_usage.db}"
mkdir -p logs "$(dirname "$DB")"

running_pid() {
  [[ -f $PID_FILE ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null && { echo "$pid"; return 0; }
  return 1
}

health() {
  local url="http://127.0.0.1:${PORT}/health"
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 3 "$url" || true
  else
    "$PYTHON" -c "import urllib.request;print(urllib.request.urlopen('$url',timeout=3).read().decode())" 2>/dev/null || true
  fi
}

start() {
  if pid="$(running_pid)"; then
    echo "[OK] 已在运行 (pid=$pid)，端口 $PORT"
    return 0
  fi
  nohup "$PYTHON" server.py --host "$HOST" --port "$PORT" --db "$DB" \
    --token "${API_USAGE_TOKEN:-}" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  if ! running_pid >/dev/null; then
    echo "[ERROR] 启动失败，日志末尾：" >&2
    tail -20 "$LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
  echo "[OK] 已启动 (pid=$(cat "$PID_FILE"))  http://${HOST}:${PORT}  db=$DB"
  echo "[OK] health: $(health)"
}

stop() {
  if ! pid="$(running_pid)"; then
    echo "[OK] 未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  kill "$pid"
  for _ in $(seq 1 20); do
    running_pid >/dev/null || break
    sleep 0.5
  done
  rm -f "$PID_FILE"
  echo "[OK] 已停止 (pid=$pid)"
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)
    if pid="$(running_pid)"; then
      echo "[OK] 运行中 (pid=$pid)  端口 $PORT  db=$DB"
      echo "     health: $(health)"
    else
      echo "[WARN] 未在运行（./run.sh start 启动）"
    fi ;;
  logs)    tail -f "$LOG_FILE" ;;
  foreground)
    exec "$PYTHON" server.py --host "$HOST" --port "$PORT" --db "$DB" --token "${API_USAGE_TOKEN:-}" ;;
  *)
    echo "用法: ./run.sh start|stop|restart|status|logs|foreground" >&2
    exit 1 ;;
esac
