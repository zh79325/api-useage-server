#!/usr/bin/env bash
# 首次部署初始化：自检 python3 / sqlite3 → 建 data、logs 目录 → 跑一遍自测
# 幂等，可反复执行；没有配置文件，运行参数一律用环境变量或命令行参数传
set -euo pipefail

cd "$(dirname "$0")"
SKIP_TEST=0
for arg in "$@"; do
  case "$arg" in
    --skip-test) SKIP_TEST=1 ;;
    -h|--help)
      echo "用法: ./init.sh [--skip-test 跳过自测]"
      exit 0 ;;
    *) echo "未知参数: $arg（可用 --skip-test）" >&2; exit 1 ;;
  esac
done

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || { echo "[ERROR] 找不到 python3，请先安装（>=3.8）" >&2; exit 1; }

"$PYTHON" - <<'PY'
import sqlite3, sys
if sys.version_info < (3, 8):
    sys.exit(f"[ERROR] 需要 Python >= 3.8，当前 {sys.version.split()[0]}")
print(f"[OK] Python {sys.version.split()[0]}，sqlite3 {sqlite3.sqlite_version}")
PY

# 无第三方依赖，这步只为让部署流程统一（失败也不阻塞）
if command -v pip3 >/dev/null 2>&1; then
  pip3 install -q -r requirements.txt >/dev/null 2>&1 || true
fi

mkdir -p data logs
echo "[OK] 目录就绪: ./data（SQLite 库）、./logs（运行日志）"

if [[ $SKIP_TEST -eq 0 ]]; then
  echo "[INFO] 跑自测（起真服务打真接口）..."
  "$PYTHON" test_usage_server.py 2>&1 | tail -3
fi

echo
echo "初始化完成，接下来："
echo "  ./run.sh start                              # 0.0.0.0:2697（无鉴权，只在内网跑）"
echo "  ./run.sh status                             # 查看状态与健康检查"
echo
echo "可用环境变量: API_USAGE_HOST(默认 0.0.0.0) / API_USAGE_PORT(默认 2697) /"
echo "              API_USAGE_DB(默认 ./data/api_usage.db)"
echo
echo "客户端（ai-skills/wx_gzh_article_writer/config/api_secrets.json → usage_server）填："
echo "  { \"base_url\": \"http://<本机IP>:2697\", \"enabled\": true }"
