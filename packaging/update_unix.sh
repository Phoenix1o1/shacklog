#!/usr/bin/env bash
# Update a ShackLog desktop install on Linux/macOS: git pull, reinstall, restart.
# Started by Settings › Program & updates › "Update now" with the PID to wait for.
# (The Proxmox hub uses `shacklog-ctl update` instead.)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

if [ -n "${1:-}" ]; then
  for _ in $(seq 1 60); do kill -0 "$1" 2>/dev/null || break; sleep 1; done
fi

old=$(git rev-parse --short HEAD)
git pull --ff-only
new=$(git rev-parse --short HEAD)
echo "Updated $old -> $new"

if [ -x dist/ShackLog ]; then
  # Built executable: rebuild it.
  python3 -m venv .venv-build
  # shellcheck disable=SC1091
  source .venv-build/bin/activate
  pip install -q -r requirements.txt -r requirements-dev.txt
  python -m pytest -q tests
  pyinstaller packaging/shacklog.spec --noconfirm --distpath dist-new --log-level WARN
  mv -f dist-new/ShackLog dist/ShackLog && rm -rf dist-new
  nohup "$REPO/dist/ShackLog" >/dev/null 2>&1 &
else
  # Running from source.
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  nohup .venv/bin/python -m shacklog >/dev/null 2>&1 &
fi
echo "ShackLog restarted."
