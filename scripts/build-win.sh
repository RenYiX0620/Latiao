#!/bin/bash
set -e

echo "=== 1/4 下载 llama-server.exe ==="
LLAMA_TAG=$(curl -s https://api.github.com/repos/ggml-org/llama.cpp/releases/latest | jq -r '.tag_name')
curl -L "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/llama-${LLAMA_TAG}-bin-win-cpu-x64.zip" \
     -o /tmp/llama.zip
unzip -o -j /tmp/llama.zip "*/llama-server.exe" -d sidecar/
echo "llama-server.exe: $(sidecar/llama-server.exe --version 2>&1 || echo 'ok')"

echo "=== 2/4 PyInstaller 打包 sidecar ==="
cd sidecar
pip install pyinstaller
pyinstaller latiao.spec
cd ..

echo "=== 3/4 拷贝到 sidecar/ 根目录（tauri resources 打包） ==="
cp sidecar/dist/sidecar.exe sidecar/

echo "=== 4/4 Tauri 构建 MSI ==="
npm run tauri build -- --target x86_64-pc-windows-msvc
