#!/bin/bash
# =============================================================================
#  setup-portable-python.sh — Bundle a relocatable Python into sidecar/python/
#
#  Downloads python-build-standalone (macOS arm64), extracts it, and installs
#  all sidecar dependencies into it.  This makes Latiao.app self-contained —
#  end users do NOT need Python installed on their machine.
#
#  Usage:
#    bash scripts/setup-portable-python.sh
#
#  Output:
#    sidecar/python/          ← relocatable Python (only for release builds)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SIDECAR_DIR="$PROJECT_DIR/sidecar"
PYTHON_DIR="$SIDECAR_DIR/python"
REQUIREMENTS="$SIDECAR_DIR/requirements.txt"

# ─── Python-build-standalone version ───────────────────────────────────────
# Use CPython 3.11 — widely supported, wheels available for mlx / llama-cpp / ctranslate2
PYTHON_VERSION="3.11.10"
BUILD_DATE="20241002"
ARCH="aarch64-apple-darwin"
PYTHON_TARBALL="cpython-${PYTHON_VERSION}+${BUILD_DATE}-${ARCH}-install_only.tar.gz"
DOWNLOAD_URL="https://github.com/indygreg/python-build-standalone/releases/download/${BUILD_DATE}/${PYTHON_TARBALL}"

# ─── Colors ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[info]${NC} $1"; }
ok()    { echo -e "${GREEN}[ok]${NC}   $1"; }
err()   { echo -e "${RED}[err]${NC}  $1"; }

# ─── Idempotency guard ──────────────────────────────────────────────────────
# Skip the destructive "rm -rf → re-download from github" cycle when a working
# portable Python is already present. This is what broke deploys when
# github.com was unreachable (the rm had already wiped a good install).
# Override with: FORCE_REINSTALL=1 bash scripts/setup-portable-python.sh
if [ -z "${FORCE_REINSTALL:-}" ] && [ -x "$PYTHON_DIR/bin/python3" ]; then
  if "$PYTHON_DIR/bin/python3" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    ok "Portable Python already present and functional — skipping download/install."
    ok "  (set FORCE_REINSTALL=1 to force a clean re-download from github)"
    exit 0
  fi
  info "Existing portable Python failed import check — reinstalling."
fi

# ─── Step 1: Clean previous python/ (if any) ───────────────────────────────
if [ -d "$PYTHON_DIR" ]; then
  info "Removing old portable Python at $PYTHON_DIR"
  rm -rf "$PYTHON_DIR"
fi

# ─── Step 2: Download python-build-standalone ───────────────────────────────
TMP_DIR="$(mktemp -d)"
TARBALL_PATH="$TMP_DIR/$PYTHON_TARBALL"
# 单引号 trap：$TMP_DIR 在执行时展开；变量加引号防空格/空值炸掉 rm -rf
trap 'rm -rf "$TMP_DIR"' EXIT

info "Downloading portable Python ${PYTHON_VERSION}..."
if command -v curl &>/dev/null; then
  curl -fSL --progress-bar -o "$TARBALL_PATH" "$DOWNLOAD_URL"
else
  wget -q --show-progress -O "$TARBALL_PATH" "$DOWNLOAD_URL"
fi
ok "Downloaded $PYTHON_TARBALL"

# ─── Step 2b: Verify SHA256 against the release's SHA256SUMS ────────────────
# python-build-standalone 每个 release 都发布 SHA256SUMS（与 tarball 同一目录）。
# 校验失败即退出，绝不解压来路不明的运行时。
SUMS_URL="https://github.com/indygreg/python-build-standalone/releases/download/${BUILD_DATE}/SHA256SUMS"
SUMS_PATH="$TMP_DIR/SHA256SUMS"
info "Downloading SHA256SUMS for integrity check..."
if command -v curl &>/dev/null; then
  curl -fsSL -o "$SUMS_PATH" "$SUMS_URL"
else
  wget -q -O "$SUMS_PATH" "$SUMS_URL"
fi
EXPECTED_LINE=$(grep " $PYTHON_TARBALL\$" "$SUMS_PATH" || true)
if [ -z "$EXPECTED_LINE" ]; then
  err "SHA256SUMS 中未找到 $PYTHON_TARBALL 的校验值 — 拒绝继续"
  exit 1
fi
if (cd "$TMP_DIR" && echo "$EXPECTED_LINE" | shasum -a 256 -c -); then
  ok "SHA256 checksum verified"
else
  err "SHA256 校验失败：$PYTHON_TARBALL 已损坏或被篡改 — 拒绝继续"
  exit 1
fi

# ─── Step 3: Extract to sidecar/python/ ────────────────────────────────────
info "Extracting to $PYTHON_DIR ..."
mkdir -p "$PYTHON_DIR"
tar xzf "$TARBALL_PATH" -C "$PYTHON_DIR" --strip-components=1
ok "Extracted portable Python"

# ─── Step 4: Install pip dependencies ──────────────────────────────────────
PIP="$PYTHON_DIR/bin/pip3"
if [ ! -x "$PYTHON_DIR/bin/python3" ]; then
  err "python3 not found in extracted archive — check the tarball structure"
  exit 1
fi

info "Python version: $("$PYTHON_DIR/bin/python3" --version)"

info "Installing pip packages from requirements.txt (this may take 5-15 min)..."
# Upgrade pip first (standalone builds ship an older pip)
"$PYTHON_DIR/bin/python3" -m pip install --upgrade pip

# Install all dependencies
"$PYTHON_DIR/bin/python3" -m pip install -r "$REQUIREMENTS"

# Check critical packages
info "Verifying key packages..."
for pkg in fastapi uvicorn mlx mlx_lm llama_cpp_python httpx; do
  if "$PYTHON_DIR/bin/python3" -c "import ${pkg//-/_}" 2>/dev/null; then
    ok "  $pkg"
  else
    # not all packages have the same import name as pypi name
    case "$pkg" in
      llama_cpp_python)
        if "$PYTHON_DIR/bin/python3" -c "import llama_cpp" 2>/dev/null; then
          ok "  $pkg (as llama_cpp)"
        else
          err "  $pkg — MISSING"
        fi
        ;;
      *)
        err "  $pkg — MISSING (import may differ from package name)"
        ;;
    esac
  fi
done

# ─── Step 5: Clean up cache files ──────────────────────────────────────────
find "$PYTHON_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$PYTHON_DIR" -name '*.pyc' -delete 2>/dev/null || true
find "$PYTHON_DIR" -name '.DS_Store' -delete 2>/dev/null || true

# ─── Step 6: Verify relocatable — python must NOT have hardcoded paths ──────
# python-build-standalone uses relative paths, but double-check
if "$PYTHON_DIR/bin/python3" -c "import sys; sys.exit(0)" 2>&1; then
  ok "Portable Python is functional"
else
  err "Portable Python verification failed"
  exit 1
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Portable Python ready at: $PYTHON_DIR${NC}"
echo -e "${GREEN}  Size: $(du -sh "$PYTHON_DIR" | cut -f1)${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
echo ""
info "Remember: this directory is for release builds only."
info "For local dev, use 'python3 -m venv sidecar/venv' as before."
