#!/bin/bash
# 跑后端测试套件（与 CI 同形：从仓库根跑 sidecar/tests）。
#
# 为什么需要（2026-10-19 跟进）：随应用发布的 python **只装运行时依赖**，没有
# pytest——直接拿 APP 里的解释器跑测试会报 No module named pytest。本脚本优先用
# 仓库里的开发解释器（sidecar/python，装过 pytest 与 requirements-test.txt），
# 没有就把安装命令打出来，别让测试"只存在于仓库里"。
#
# 用法: bash scripts/test.sh [额外 pytest 参数，如 -k session_search]
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="./sidecar/python/bin/python3"
[ -x "$PY" ] || PY="./.venv-test/bin/python"
if ! "$PY" -c "import pytest" 2>/dev/null; then
  echo "✗ $PY 没有 pytest。安装开发依赖：$PY -m pip install -r sidecar/requirements-test.txt"
  exit 1
fi
exec "$PY" -m pytest sidecar/tests "$@"
