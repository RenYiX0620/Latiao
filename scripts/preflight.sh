#!/bin/bash
# 推前门禁（2026-10-12）：把 CI 的两条工作流在**本地按原样命令**跑一遍。
#
# 为什么需要：v0.3.79 的 CI 连红两次，两次都是"本地绿、CI 红"的环境差，而不是 flake——
#   · 本机装了 mlx-lm → 默认 backend='mlx' → `_local_native_tools_ok()` 为真 → 请求带 tools；
#     CI（Linux + 轻量依赖集）为假 → 走围栏路径、请求里没有 tools 键。
#   · 本机工具注册表 32 个、CI 31 个（缺依赖的工具注册不进来）。
#   · 本机 1235 上还有用户 App 的真引擎在跑，冒烟测试会真的去生成内容。
# 现在 sidecar/tests/conftest.py 的 `_ci_parity_engine` 夹具把这些拉平到 CI 的真值
# （无原生后端 + 死端口），本脚本再把 CI 的命令逐条跑一遍。**推之前跑它，才算绿。**
#
# 用法: bash scripts/preflight.sh [--frontend]
#   默认只跑后端（改前端/加了前端依赖时加 --frontend）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="./sidecar/python/bin/python3"

echo "== 1/5 版本号一致性（package.json / tauri.conf.json / Cargo.toml / Cargo.lock）=="
$PY scripts/check_versions.py

echo "== 2/5 ruff 硬门禁（F 未定义名/未用导入、E9 语法级）=="
$PY -m ruff check --select F,E9 .

echo "== 3/5 后端 pytest（从仓库根跑，与 CI 一致）=="
$PY -m pytest sidecar/tests -q

echo "== 4/5 承诺碎片回归集（真实语料重放）=="
$PY scripts/replay_fragments.py

if [ "$1" = "--frontend" ]; then
  echo "== 5/5 前端 tsc + vitest + eslint（与 CI 同参数）=="
  npx tsc --noEmit
  npx vitest run
  npx eslint src/ --rule '{"react-hooks/set-state-in-effect":"warn"}' --max-warnings 200
else
  echo "== 5/5 前端未跑（改过 src/ 时加 --frontend）=="
fi

echo "✅ 推前门禁全过 —— 与 CI 的两条工作流等价（本机路径）"
