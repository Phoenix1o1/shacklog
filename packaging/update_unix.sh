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
# Everything above this line must stay as it is: bash reads a script from disk as it
# runs, and an older copy busy pulling this version continues at the same byte offset.
# Below, all the work is one function called on one line, so it is read in full first.
# ShackLog now runs a copy of this script from dist/, which git never changes.
#   $1 PID to wait for · $2 "rebuild" to build even without new commits · $3… arguments
#   for the restarted ShackLog (--data-dir).
update_main() {
  local new mode="${2:-}" exe="$REPO/dist/ShackLog"
  shift 2 2>/dev/null || shift $#
  new=$(git rev-parse --short HEAD)
  echo "Updated $old -> $new"

  if [ -x "$exe" ]; then
    # Built executable: rebuild it; a failed step starts the old one again.
    if [ "$old" = "$new" ] && [ "$mode" != "rebuild" ]; then
      echo "Already up to date."
    elif ! { python3 -m venv .venv-build &&
             .venv-build/bin/pip install -q -r requirements.txt -r requirements-dev.txt &&
             .venv-build/bin/python -m pytest -q tests &&
             .venv-build/bin/pyinstaller packaging/shacklog.spec --noconfirm \
               --distpath dist-new --log-level WARN &&
             mv -f dist-new/ShackLog "$exe" && rm -rf dist-new; }; then
      echo "Update not completed: starting the previous version again."
    fi
    PYINSTALLER_RESET_ENVIRONMENT=1 nohup "$exe" "$@" >/dev/null 2>&1 &
  else
    # Running from source.
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt || echo "Couldn't install the packages."
    nohup .venv/bin/python -m shacklog "$@" >/dev/null 2>&1 &
  fi
  echo "ShackLog restarted."
}
update_main "$@"; exit $?
