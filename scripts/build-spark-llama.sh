#!/bin/bash
# 构建 Spark-X2.5 专用 llama.cpp（XHToken/llama.cpp fork）并安装到 sidecar/
# 前置：Xcode + cmake。用法: bash scripts/build-spark-llama.sh [--skip-build]
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
