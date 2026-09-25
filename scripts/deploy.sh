#!/usr/bin/env bash
# Hardened deploy: clean build, fresh resource sync, verify required files.
# 防止「tauri build 漏掉新增 .py / .gitignore 排除 sidecar/python/」导致打包包残缺。
set -euo pipefail
cd "$(dirname "$0")/.."

SIDECAR_SRC="sidecar"
BUNDLE="src-tauri/target/release/bundle/macos/Latiao.app"
SIDECAR_DST="$BUNDLE/Contents/Resources/sidecar"

echo "==> 1/7 清理 __pycache__ / *.pyc / 旧 venv"
find "$SIDECAR_SRC" -name '__pycache__' -type d ! -path '*/venv/*' -exec rm -rf {} + 2>/dev/null || true
find "$SIDECAR_SRC" -name '*.pyc' ! -path '*/venv/*' -delete 2>/dev/null || true
rm -rf "$SIDECAR_SRC/venv"

echo "==> 2/7 准备便携 Python（生成 sidecar/python/bin）"
bash scripts/setup-portable-python.sh

echo "==> 3/7 清理旧构建产物（强制 tauri 重新拷贝资源，避免资源缓存）"
rm -rf "$BUNDLE"

echo "==> 4/7 tauri build"
tauri build

echo "==> 5/7 强制全量同步 sidecar 到构建产物"
# tauri 资源打包可能因 .gitignore（如 sidecar/python/）或缓存漏文件，
# 这里用 rsync 以源码为准覆盖，确保新增 .py 与便携 Python 一并进包。
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.ruff_cache' \
  --exclude '.DS_Store' \
  --exclude '.env' \
  --exclude '.env.local' \
  "$SIDECAR_SRC/" "$SIDECAR_DST/"

# Never ship secrets into the bundle. rsync --delete + --exclude won't remove a
# pre-existing dest .env, so explicit rm is required (do NOT use --delete-excluded:
# that would also wipe portable-python's shipped stdlib .pyc, which can't be
# regenerated inside a read-only .app bundle).
rm -f "$SIDECAR_DST/.env" "$SIDECAR_DST/.env.local"

echo "==> 6/7 校验关键文件齐全"
required=(config.py db.py memory.py main.py identity.py local_llm.py tool_system.py requirements.txt python/bin/python3)
missing=()
for f in "${required[@]}"; do
  if [ ! -e "$SIDECAR_DST/$f" ]; then
    missing+=("$f")
  fi
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "❌ 构建产物缺少: ${missing[*]}" >&2
  exit 1
fi
echo "✅ 关键文件齐全"

echo "==> 7/7 部署到 Desktop"
rm -rf ~/Desktop/Latiao.app
cp -R "$BUNDLE" ~/Desktop/Latiao.app/
echo "Deployed to Desktop"
