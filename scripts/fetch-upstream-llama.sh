#!/bin/bash
# 获取上游 llama.cpp 预编译引擎（macOS arm64）并安装到 sidecar/llama-upstream/
#
# 为什么需要「双引擎」（09-15）：
#   - sidecar/llama-server  = XHToken fork（Spark-X2.5 新架构专用补丁，本地编译）
#   - sidecar/llama-upstream/ = 上游最新（普通 GGUF 与新架构兼容性更好）
# Spark 类模型必须用 fork；其余走上游。上游每个构建都发布 macOS 预编译包，
# 无需本地编译（本机 Xcode/CMake 环境缺失也能更新）。
#
# 2026-10-12 收编：fork 已退役、从包内移除——实测上游 b11554/b11571 能加载
# Spark-X2.5（模型 loaded）并正确解析原生 tools 调用（finish=tool_calls、参数类型
# 无误），同一提示词下与 fork 行为一致；需要恢复时跑 scripts/build-spark-llama.sh。
# 现在是「单引擎」：所有模型统一走 sidecar/llama-upstream/。
#
# 用法: bash scripts/fetch-upstream-llama.sh [版本 tag，默认自动取最新带 macOS arm64 包的构建]
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/sidecar/llama-upstream"
TAG="${1:-}"

# 认证：CI 共享 runner IP 的匿名配额极小，未认证时会返回限流消息而不是列表
# （09-15 实测：匿名请求返回 {message: ...} → 解析报 'str' object has no attribute 'get'）
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

if [ -z "$TAG" ]; then
  echo "→ 扫描上游最近 release 中的 macOS arm64 预编译包…"
  TAG=$(curl -s -m 30 "${AUTH[@]}" \
      "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15" \
    | python3 -c "
import sys, json
try:
    rels = json.load(sys.stdin)
except Exception as e:
    print('', end=''); sys.stderr.write(f'✗ 无法解析 releases 响应: {e}\n'); sys.exit(0)
if not isinstance(rels, list):
    sys.stderr.write(f'✗ releases 响应非列表（可能被限流/未认证）: {str(rels)[:160]}\n')
    sys.exit(0)
for r in rels:
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
# cp -a（不是 cp）：上游包的 dylib 是**符号链接**结构（libX.dylib → libX.0.dylib
# → libX.0.<ver>.dylib）。之前的裸 cp 会把三个名字展开成三份真拷贝——单份引擎
# 因此从 ~23MB 涨到 68MB，包体积白翻两倍（2026-10-12 排查，用户报"包越来越大"）。
rm -rf "$DEST"; mkdir -p "$DEST"
cp -a "$BIN" "$DEST/"
cp -a "$SRCDIR"/*.dylib "$DEST/" 2>/dev/null || true
cp -a "$SRCDIR"/*.metallib "$DEST/" 2>/dev/null || true
# rpath：让它从自身目录加载 dylib
install_name_tool -delete_rpath "$SRCDIR" "$DEST/llama-server" 2>/dev/null || true
install_name_tool -add_rpath @loader_path "$DEST/llama-server" 2>/dev/null || true

# ── 精简：每个库只留一个名字（2026-10-12）────────────────────────────
# 为什么：Tauri 资源打包会把符号链接**展开成真拷贝**（实测：已装 App 包内软链数 0，
# 而仓库 sidecar/python 里有 9 个）——同一个库带三个名字进包就变三份真文件，
# 单份引擎从 ~19MB 涨到 68MB。上游包的三个名字里运行期只用 soname 那个
# （@rpath/libX.0.dylib），裸名与完整版本名是给链接器用的。
# 所以这里把 soname 那份**解引用成真文件**、其余删掉：不依赖打包器保留软链。
# 保留清单 = `otool -L llama-server` 的 @rpath 闭包（含传递依赖，共 10 个）。
_KEEP_SONAMES="libggml.0 libggml-base.0 libggml-blas.0 libggml-cpu.0 libggml-metal.0 libggml-rpc.0 libllama.0 libllama-common.0 libmtmd.0 libllama-server-impl"
for _s in $_KEEP_SONAMES; do
  _src=$(ls "$DEST/$_s.dylib" "$DEST/$_s".*.dylib 2>/dev/null | head -1)
  if [ -n "$_src" ]; then
    cp -fL "$_src" "$DEST/$_s.dylib.real" && mv -f "$DEST/$_s.dylib.real" "$DEST/$_s.dylib"
  fi
done
for _f in "$DEST"/lib*.dylib; do
  [ -e "$_f" ] || continue
  _b=$(basename "$_f" .dylib)
  case " $_KEEP_SONAMES " in *" $_b "*) : ;; *) rm -f "$_f" ;; esac
done
# 迁移自检：精简后 llama-server 的每个 @rpath 依赖都必须在位
_missing=0
for _lib in $(otool -L "$DEST/llama-server" | awk '/@rpath/{print $1}' | sort -u); do
  [ -e "$DEST/$(basename "$_lib")" ] || { echo "  ✗ 精简后缺依赖: $_lib"; _missing=$((_missing+1)); }
done
[ "$_missing" = "0" ] || { echo "✗ 精简后依赖缺失 $_missing 个，中止"; exit 1; }
rm -rf "$TMP"

echo "✅ 已安装到 sidecar/llama-upstream/（$(ls "$DEST" | wc -l | tr -d ' ') 个文件）"
"$DEST/llama-server" --version 2>&1 | head -2
