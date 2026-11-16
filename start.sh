#!/usr/bin/env bash
# CrossBorder Ops Agent 启动脚本（Linux / macOS）
# 用法：
#   ./start.sh           启动服务
#   ./start.sh --check   只检查环境，不启动

set -e
cd "$(dirname "$0")"

echo
echo "=========================================="
echo "  CrossBorder Ops Agent"
echo "=========================================="
echo

PY_BIN=""
if [ -x ".venv/bin/python" ]; then
  PY_BIN=".venv/bin/python"
  echo "[1/3] 虚拟环境已就绪"
else
  BASE_PY="$(command -v python3 || command -v python || true)"
  if [ -z "$BASE_PY" ]; then
    echo "[失败] 未找到 python3，请先安装 Python 3.10 或更高版本"
    exit 1
  fi
  echo "[1/3] 创建虚拟环境..."
  "$BASE_PY" -m venv .venv
  PY_BIN=".venv/bin/python"
fi

echo "[2/3] 检查依赖..."
if "$PY_BIN" -c "import fastapi, uvicorn, pydantic, multipart" >/dev/null 2>&1; then
  echo "      依赖完整"
else
  echo "      正在安装依赖，首次可能需要几分钟..."
  "$PY_BIN" -m pip install -q -r requirements.txt
fi

if [ ! -f "data/seed/ads.csv" ]; then
  echo "[3/3] 生成演示数据..."
  "$PY_BIN" scripts/gen_seed.py
else
  echo "[3/3] 演示数据已就绪"
fi

if [ "${1:-}" = "--check" ]; then
  echo
  echo "环境检查通过，可以启动。"
  exit 0
fi

echo
echo "------------------------------------------"
echo "  访问地址: http://127.0.0.1:8000"
echo "  停止服务: 按 Ctrl+C"
echo "------------------------------------------"
echo
exec "$PY_BIN" run.py
