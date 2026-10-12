#!/usr/bin/env bash
# Build the ShackLog executable on Linux or macOS. Result: dist/ShackLog
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv-build
# shellcheck disable=SC1091
source .venv-build/bin/activate
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
pyinstaller packaging/shacklog.spec --noconfirm
echo "Built dist/ShackLog"
