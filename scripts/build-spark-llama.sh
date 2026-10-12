#!/bin/bash
# 构建 Spark-X2.5 专用 llama.cpp（XHToken/llama.cpp fork）并安装到 sidecar/
# 前置：Xcode + cmake。用法: bash scripts/build-spark-llama.sh [--skip-build]
#
# 2026-10-12 起这是**恢复路径**而非日常路径：fork 已退役（上游 b11554/b11571 能加载
# Spark 并正确解析原生 tools 调用，见 fetch-upstream-llama.sh 的说明）。若某个 Spark
# 量化/模板在上游上复现回归，再跑本脚本把 fork 装回来（分派会把它当兜底候选）。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC=/tmp/llama-spark
if [ "$1" != "--skip-build" ]; then
  rm -rf "$SRC"
  git clone --depth 1 https://github.com/XHToken/llama.cpp.git "$SRC"
  cmake -S "$SRC" -B "$SRC/build" -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON \
        -DCMAKE_BUILD_TYPE=Release
  cmake --build "$SRC/build" --parallel 8
fi
DEST="$ROOT/sidecar"
cp "$SRC/build/bin/llama-server" "$DEST/"
cp "$SRC/build/bin/lib"*.dylib "$DEST/"
# 修复 rpath：@loader_path 让 llama-server 从自身所在目录加载 dylib
install_name_tool -delete_rpath "$SRC/build/bin" "$DEST/llama-server" 2>/dev/null || true
install_name_tool -add_rpath @loader_path "$DEST/llama-server" 2>/dev/null || true
echo "✅ 已安装到 sidecar/（llama-server + $(ls "$DEST"/lib*.dylib 2>/dev/null | wc -l | tr -d ' ') 个 dylib）"
echo "   用法：Latiao 内选择 Spark-X2.5 模型即可（Python 引擎失败自动回退此引擎）"
