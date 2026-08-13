#!/bin/bash
set -euo pipefail

echo "=== 1/4 下载 llama-server.exe ==="
LLAMA_TAG=$(curl -sf "https://gh-proxy.com/https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" | grep -o '"tag_name":"[^"]*"' | head -n1 | sed 's/"tag_name":"//; s/"$//')
if [ -z "$LLAMA_TAG" ] || [ "$LLAMA_TAG" = "null" ]; then
  echo "error: 无法从 GitHub API 获取 llama.cpp 最新 release tag" >&2
  exit 1
fi
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
curl -fL "https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/llama-${LLAMA_TAG}-bin-win-cpu-x64.zip" \
     -o "$TMP_DIR/llama.zip"
unzip -o -j "$TMP_DIR/llama.zip" "*/llama-server.exe" -d sidecar/
echo "llama-server.exe: $(sidecar/llama-server.exe --version 2>&1 || echo 'ok')"

echo "=== 2/4 PyInstaller 打包 sidecar ==="
cd sidecar
pip install "pyinstaller==6.*"
pyinstaller latiao.spec
cd ..

echo "=== 3/4 拷贝到 sidecar/ 根目录（tauri resources 打包） ==="
cp sidecar/dist/sidecar.exe sidecar/

echo "=== 4/4 Tauri 构建 MSI ==="
npm run tauri build -- --target x86_64-pc-windows-msvc
