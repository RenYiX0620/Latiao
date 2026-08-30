#!/bin/bash
set -euo pipefail

# =============================================================================
#  Latiao Release Script
#  Builds the app with updater signing, creates GitHub Release with all
#  required artifacts so users get auto-update notifications.
#
#  Usage:
#    npm run release          # interactive
#    npm run release -- 0.2.0  # specify version non-interactively
#
#  Prerequisites:
#    1. gh CLI authenticated: gh auth login -h github.com
#    2. tauri-key file present (generated via: npx tauri signer generate -w tauri-key)
#    3. Public key already in tauri.conf.json plugins.updater.pubkey
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# 失败路径也清理临时产物（latest.json 已提交进 git，不再删除）
cleanup() { rm -f "$PROJECT_DIR/.release-notes.md"; }
trap cleanup EXIT

# ─── Color helpers ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[info]${NC} $1"; }
ok()    { echo -e "${GREEN}[ok]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $1"; }
err()   { echo -e "${RED}[err]${NC}  $1"; }

# ─── Checks ───────────────────────────────────────────────────────────────
if ! command -v gh &>/dev/null; then
  err "gh CLI not found. Install: brew install gh"; exit 1
fi
if ! gh auth status &>/dev/null; then
  err "gh not authenticated. Run: gh auth login -h github.com"; exit 1
fi
GH_REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "RenYiX0620/Latiao")
info "Publishing to: $GH_REPO"

# ─── Version ──────────────────────────────────────────────────────────────
CURRENT_VERSION=$(python3 -c "import json; print(json.load(open('src-tauri/tauri.conf.json'))['version'])")
if [ $# -ge 1 ]; then
  VERSION="$1"; VERSION="${VERSION#v}"
else
  echo ""
  echo "Current version in tauri.conf.json: ${CYAN}$CURRENT_VERSION${NC}"
  read -p "Enter new version (leave empty to keep current): " VERSION
  VERSION="${VERSION:-$CURRENT_VERSION}"
fi
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  err "Invalid version: $VERSION (expected semver like 0.2.0)"; exit 1
fi
if [ "$VERSION" != "$CURRENT_VERSION" ]; then
  info "Updating tauri.conf.json version: $CURRENT_VERSION -> $VERSION"
  python3 -c "
import json
fp = 'src-tauri/tauri.conf.json'
d = json.load(open(fp))
d['version'] = '$VERSION'
json.dump(d, open(fp, 'w'), indent=2)
"
  # package.json 版本同步（此前只改 tauri.conf 导致两处不一致）
  python3 -c "
import json
fp = 'package.json'
d = json.load(open(fp))
d['version'] = '$VERSION'
json.dump(d, open(fp, 'w'), indent=2, ensure_ascii=False)
open(fp, 'a').write('\n')
"
  ok "Version updated"
else
  info "Keeping current version $VERSION"
fi

# ─── Signing key ──────────────────────────────────────────────────────────
# 密钥路径可用 LATIAO_KEY_FILE 覆盖（应用密钥实际在 ~/.config/latiao/tauri-key）
KEY_FILE="${LATIAO_KEY_FILE:-$PROJECT_DIR/tauri-key}"
if [ ! -f "$KEY_FILE" ]; then
  err "No signing key found at $KEY_FILE"
  echo "  Generate one: npx tauri signer generate -w tauri-key"
  echo "  Or point to the app key: LATIAO_KEY_FILE=~/.config/latiao/tauri-key npm run release -- $VERSION"
  exit 1
fi
# 密码可从环境变量传入（CI/非交互）；无环境变量且有 TTY 时交互输入
if [ -n "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}" ]; then
  KEY_PASSWORD="$TAURI_SIGNING_PRIVATE_KEY_PASSWORD"
elif [ -t 0 ]; then
  read -s -p "Private key password: " KEY_PASSWORD
  echo
else
  KEY_PASSWORD=""
fi

# ─── Release notes ────────────────────────────────────────────────────────
NOTES_FILE=".release-notes.md"
cat > "$NOTES_FILE" << EOF
Latiao v$VERSION
$(printf '=%.0s' $(seq 1 $(( ${#VERSION} + 8 ))))

### Changes
-

### Changelog
$(git log --oneline --no-decorate "v${CURRENT_VERSION}...HEAD" 2>/dev/null | head -30 || echo "-")
EOF
if [ -t 0 ]; then
  info "Opening release notes in editor... (save with Ctrl+O, exit with Ctrl+X)"
  if command -v nano &>/dev/null; then nano "$NOTES_FILE"
  elif command -v vim &>/dev/null; then vim "$NOTES_FILE"
  elif command -v vi &>/dev/null; then vi "$NOTES_FILE"
  fi
else
  warn "No TTY - using auto-generated release notes"
fi

# ─── Build ────────────────────────────────────────────────────────────────
echo ""
info "Building Latiao v${VERSION}..."
echo ""

# Ensure portable Python is set up (and old venv is NOT bundled)
rm -rf "$PROJECT_DIR/sidecar/venv"
if [ ! -d "$PROJECT_DIR/sidecar/python" ]; then
  info "Setting up portable Python for release..."
  bash "$PROJECT_DIR/scripts/setup-portable-python.sh"
else
  info "Portable Python already present, skipping setup"
fi

if npm run tauri:build; then
  ok "Build complete"
else
  err "Build failed"; exit 1
fi

# ─── Create updater archive ──────────────────────────────────────────────
info "Creating updater archive..."
BUNDLE_DIR="src-tauri/target/release/bundle/macos"

# Determine app bundle name and architecture
# 注意必须 ls -d：ls 对目录参数默认列出目录内容（会把 "Contents" 当结果）
APP_BUNDLE=$(ls -d "$BUNDLE_DIR"/*.app 2>/dev/null | head -1)
if [ -z "$APP_BUNDLE" ]; then
  err "No .app bundle found in $BUNDLE_DIR/"; exit 1
fi

ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
  ARCH_STR="aarch64"
else
  ARCH_STR="x86_64"
fi

ARCHIVE_NAME="Latiao_${VERSION}_${ARCH_STR}.app.tar.gz"
ARCHIVE_PATH="$BUNDLE_DIR/$ARCHIVE_NAME"

info "Archive: $ARCHIVE_NAME"
cd "$BUNDLE_DIR"
tar czf "$ARCHIVE_NAME" "$(basename "$APP_BUNDLE")"
cd "$PROJECT_DIR"
ok "Archive created: $ARCHIVE_NAME"

# ─── Sign archive ─────────────────────────────────────────────────────────
info "Signing archive..."
SIG_FILE="${ARCHIVE_PATH}.sig"

export TAURI_SIGNING_PRIVATE_KEY_PATH="$KEY_FILE"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$KEY_PASSWORD"

if npx tauri signer sign "$ARCHIVE_PATH" 2>&1; then
  ok "Archive signed"
fi

if [ ! -f "$SIG_FILE" ]; then
  err "Signature file not created at $SIG_FILE"
  ls -la "$BUNDLE_DIR"/*.sig 2>/dev/null
  exit 1
fi

SIGNATURE=$(cat "$SIG_FILE")
PUB_DATE=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
PLATFORM="darwin-${ARCH_STR}"
DOWNLOAD_URL="https://github.com/$GH_REPO/releases/download/v${VERSION}/${ARCHIVE_NAME}"

info "Signature: $(basename "$SIG_FILE")"

# ─── Generate latest.json ────────────────────────────────────────────────
cat > latest.json << JSONEOF
{
  "version": "$VERSION",
  "notes": "",
  "pub_date": "$PUB_DATE",
  "platforms": {
    "$PLATFORM": {
      "signature": "$SIGNATURE",
      "url": "$DOWNLOAD_URL"
    }
  }
}
JSONEOF
ok "latest.json generated"

# ─── Git tag ──────────────────────────────────────────────────────────────
info "Committing version change..."
git add src-tauri/tauri.conf.json latest.json 2>/dev/null || true
git commit -m "release: v$VERSION" 2>/dev/null || warn "Nothing to commit"

TAG="v$VERSION"
if git tag | grep -q "^$TAG$"; then
  warn "Tag $TAG exists - force updating"
  git tag -f "$TAG" >/dev/null
else
  git tag "$TAG"
fi
ok "Tagged $TAG"

# ─── GitHub Release ──────────────────────────────────────────────────────
echo ""
# 幂等：同名 release 已存在则先删掉，让脚本可重跑
if gh release view "$TAG" --repo "$GH_REPO" >/dev/null 2>&1; then
  warn "Release $TAG already exists - deleting before re-creating"
  gh release delete "$TAG" --repo "$GH_REPO" --yes
fi

# dmg glob 无匹配时不要传字面 '*.dmg'（nullglob + bash 3.2 安全的空数组展开）
shopt -s nullglob
DMG_FILES=("$PROJECT_DIR/src-tauri/target/release/bundle/dmg"/*.dmg)
shopt -u nullglob
if [ ${#DMG_FILES[@]} -eq 0 ]; then
  warn "No .dmg found in bundle/dmg - release will not include an installer asset"
fi

info "Creating GitHub release $TAG..."
gh release create "$TAG" \
  --repo "$GH_REPO" \
  --title "Latiao v$VERSION" \
  --notes-file "$NOTES_FILE" \
  --latest \
  "$ARCHIVE_PATH" \
  "$SIG_FILE" \
  ${DMG_FILES[@]+"${DMG_FILES[@]}"} \
  latest.json

ok "GitHub release: https://github.com/$GH_REPO/releases/tag/$TAG"

# ─── Push tag ────────────────────────────────────────────────────────────
echo ""
info "Pushing tag $TAG..."
git push origin "$TAG" 2>/dev/null && ok "Tag pushed" || warn "Tag push failed"
git push origin HEAD 2>/dev/null || true

# ─── Cleanup ─────────────────────────────────────────────────────────────
# latest.json 保留：已 git commit（第 5 步），且作为仓库最新清单供参考
rm -f "$NOTES_FILE"

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Latiao v$VERSION released!${NC}"
echo -e "${GREEN}  Users running v$CURRENT_VERSION will get auto-update.${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "Next steps:"
echo "  - git push origin HEAD"
echo "  - Check: gh release view $TAG"
echo "  - Users need to install the .dmg for the first time from:"
echo "      https://github.com/$GH_REPO/releases/tag/$TAG"
echo ""
