#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.build-venv-macos"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: the macOS app must be built on macOS." >&2
    exit 1
fi

cd "${PROJECT_DIR}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "Error: Python 3 was not found. Install Python 3.9 or newer first." >&2
    exit 1
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
    echo "Error: Python 3.9 or newer is required." >&2
    exit 1
fi

echo "[1/4] Creating build environment..."
"${PYTHON_BIN}" -m venv "${VENV_DIR}"

echo "[2/4] Installing build dependencies..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r requirements.txt

echo "[3/4] Building CodexQuota.app..."
"${VENV_DIR}/bin/python" -m PyInstaller --noconfirm --clean CodexQuota.spec

echo "[4/4] Build complete."
echo "Output: ${PROJECT_DIR}/dist/CodexQuota.app"
