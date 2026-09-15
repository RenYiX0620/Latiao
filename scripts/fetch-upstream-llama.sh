#!/bin/bash
# 获取上游 llama.cpp 预编译引擎（macOS arm64）并安装到 sidecar/llama-upstream/
#
# 为什么需要「双引擎」（09-15）：
#   - sidecar/llama-server  = XHToken fork（Spark-X2.5 新架构专用补丁，本地编译）
#   - sidecar/llama-upstream/ = 上游最新（普通 GGUF 与新架构兼容性更好）
# Spark 类模型必须用 fork；其余走上游。上游每个构建都发布 macOS 预编译包，
# 无需本地编译（本机 Xcode/CMake 环境缺失也能更新）。
#
# 用法: bash scripts/fetch-upstream-llama.sh [版本 tag，默认自动取最新带 macOS arm64 包的构建]
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/sidecar/llama-upstream"
TAG="${1:-}"

if [ -z "$TAG" ]; then
  echo "→ 扫描上游最近 release 中的 macOS arm64 预编译包…"
  TAG=$(curl -s -m 30 "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15" \
    | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    names = [a['name'] for a in (r.get('assets') or [])]
    if any(n.endswith('bin-macos-arm64.tar.gz') for n in names):
        print(r['tag_name']); break
")
fi
[ -z "$TAG" ] && { echo "✗ 未找到带 macOS arm64 包的上游构建"; exit 1; }
echo "→ 选中上游构建: $TAG"

TMP=$(mktemp -d)
mkdir -p "$DEST"
ASSET="llama-${TAG}-bin-macos-arm64.tar.gz"
curl -sL -m 300 "https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/${ASSET}" -o "$TMP/pkg.tar.gz"
tar -xzf "$TMP/pkg.tar.gz" -C "$TMP"
BIN=$(find "$TMP" -name "llama-server" -type f | head -1)
[ -z "$BIN" ] && { echo "✗ 包内未找到 llama-server"; exit 1; }
SRCDIR=$(dirname "$BIN")

# 安装：二进制 + 其依赖的 dylib（放独立子目录，避免与 fork 的 dylib 同名冲突）
rm -rf "$DEST"; mkdir -p "$DEST"
cp "$BIN" "$DEST/"
cp "$SRCDIR"/*.dylib "$DEST/" 2>/dev/null || true
cp "$SRCDIR"/*.metallib "$DEST/" 2>/dev/null || true
# rpath：让它从自身目录加载 dylib（与 fork 的安装方式一致）
install_name_tool -delete_rpath "$SRCDIR" "$DEST/llama-server" 2>/dev/null || true
install_name_tool -add_rpath @loader_path "$DEST/llama-server" 2>/dev/null || true
rm -rf "$TMP"

echo "✅ 已安装到 sidecar/llama-upstream/（$(ls "$DEST" | wc -l | tr -d ' ') 个文件）"
"$DEST/llama-server" --version 2>&1 | head -2
