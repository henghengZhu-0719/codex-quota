#!/usr/bin/env bash
#
# 把 dist/CodexQuota.app 打成可拖拽安装的 DMG。
# 既可由 build_macos.sh 自动调用，也可在已有 .app 时单独运行。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="${PROJECT_DIR}/dist/CodexQuota.app"
VOLUME_NAME="CodexQuota"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: DMG 只能在 macOS 上制作。" >&2
    exit 1
fi

if [[ ! -d "${APP_PATH}" ]]; then
    echo "Error: 找不到 ${APP_PATH}，请先运行 ./build_macos.sh。" >&2
    exit 1
fi

# 版本号取自 pyproject.toml，失败时回退到 0.0.0，避免因此中断打包。
VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
    "${PROJECT_DIR}/pyproject.toml" | head -n 1)"
VERSION="${VERSION:-0.0.0}"
DMG_PATH="${PROJECT_DIR}/dist/CodexQuota-${VERSION}.dmg"

# 没有 Apple 开发者证书时用 ad-hoc 签名。这不能让 Gatekeeper 放行
# （用户仍需去掉隔离属性），但能保证 .app 内的二进制签名自洽，
# 否则 macOS 会直接以「已损坏」为由拒绝启动。
echo "[1/3] Ad-hoc 签名 CodexQuota.app..."
codesign --force --deep --sign - "${APP_PATH}"
codesign --verify --deep --strict "${APP_PATH}"

echo "[2/3] 生成 DMG..."
STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGING_DIR}"' EXIT

cp -R "${APP_PATH}" "${STAGING_DIR}/"
# 指向 /Applications 的软链接，让用户在 DMG 窗口里直接拖拽安装。
ln -s /Applications "${STAGING_DIR}/Applications"

rm -f "${DMG_PATH}"
hdiutil create \
    -volname "${VOLUME_NAME}" \
    -srcfolder "${STAGING_DIR}" \
    -fs HFS+ \
    -format UDZO \
    -quiet \
    "${DMG_PATH}"

echo "[3/3] 完成。"
echo "Output: ${DMG_PATH}"
