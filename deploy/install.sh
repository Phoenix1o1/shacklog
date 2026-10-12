#!/usr/bin/env bash
# =====================================================================================
#  Install (or repair) the ShackLog hub on Debian 12/13 or Ubuntu 24.04 — normally
#  inside the dedicated LXC created by deploy/proxmox/shacklog-lxc.sh, which calls it.
#
#  Environment (all optional):
#    SHACKLOG_REPO      git URL                      (default: GitHub Phoenix1o1/shacklog)
#    SHACKLOG_BRANCH    branch                        (default: main)
#    GITHUB_TOKEN       read-only token for the private repo (stored root-only)
#    GITHUB_TOKEN_FILE  …or a file containing it (used by shacklog-lxc.sh)
#    SHACKLOG_PORT      web port                      (default: 8573)
#    SHACKLOG_RESTORE   path of a data archive (shacklog-ctl backup) to restore first
#    SHACKLOG_PASSWORD  set the web password now instead of using the setup code
#
#  Layout: code /opt/shacklog (git) · data /var/lib/shacklog · config /etc/shacklog
#  Do NOT run this on the Proxmox host itself: give ShackLog its own container.
# =====================================================================================
set -Eeuo pipefail

REPO="${SHACKLOG_REPO:-https://github.com/Phoenix1o1/shacklog.git}"
BRANCH="${SHACKLOG_BRANCH:-main}"
PORT="${SHACKLOG_PORT:-8573}"
APP_DIR=/opt/shacklog
DATA=/var/lib/shacklog
CONF=/etc/shacklog

GN=$'\033[1;92m'; RD=$'\033[01;31m'; BL=$'\033[36m'; YW=$'\033[33m'; CL=$'\033[m'
ok()   { echo -e " ${GN}✓${CL} $*"; }
info() { echo -e " ${BL}…${CL} $*"; }
warn() { echo -e " ${YW}!${CL} $*"; }
die()  { echo -e " ${RD}✗${CL} $*" >&2; exit 1; }
trap 'die "Failed at line $LINENO: $BASH_COMMAND"' ERR

[ "$(id -u)" -eq 0 ] || die "Run as root."
if [ -d /etc/pve ]; then
  die "This is the Proxmox host. Run deploy/proxmox/shacklog-lxc.sh instead: it creates a dedicated container."
fi
. /etc/os-release
case "$ID" in debian|ubuntu) ;; *) warn "Untested system: $PRETTY_NAME" ;; esac

info "Installing system packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates sqlite3 tzdata >/dev/null
ok "Packages installed ($(python3 --version))"

id shacklog >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin shacklog
install -d -o shacklog -g shacklog -m 0750 "$DATA" "$DATA/control"
install -d -m 0755 "$CONF"

if [ -z "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_TOKEN_FILE:-}" ] && [ -f "$GITHUB_TOKEN_FILE" ]; then
  GITHUB_TOKEN=$(tr -d '\r\n ' < "$GITHUB_TOKEN_FILE")
fi
if [ -n "${GITHUB_TOKEN:-}" ]; then
  umask 077
  printf 'https://x-access-token:%s@github.com\n' "$GITHUB_TOKEN" > "$CONF/git-credentials"
  umask 022
  ok "GitHub token stored in $CONF/git-credentials (root only)"
fi
gitc() {
  if [ -f "$CONF/git-credentials" ]; then git -c credential.helper="store --file=$CONF/git-credentials" "$@"; else git "$@"; fi
}

if [ -d "$APP_DIR/.git" ]; then
  info "Updating the existing copy in $APP_DIR…"
  gitc -C "$APP_DIR" fetch --quiet origin
  gitc -C "$APP_DIR" checkout --quiet "$BRANCH"
  gitc -C "$APP_DIR" merge --ff-only --quiet "origin/$BRANCH"
else
  info "Downloading ShackLog from $REPO…"
  GIT_TERMINAL_PROMPT=0 gitc clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR" \
    || die "git clone failed. The repository is private: set GITHUB_TOKEN (a read-only token)."
fi
ok "Code at $APP_DIR ($(git -C "$APP_DIR" rev-parse --short HEAD))"

info "Creating the Python environment…"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "Python packages installed"

if [ ! -f "$CONF/shacklog.env" ]; then
  cat > "$CONF/shacklog.env" <<EOF
# ShackLog hub settings. After changing: shacklog-ctl restart
SHACKLOG_DATA=$DATA
SHACKLOG_HOST=0.0.0.0
SHACKLOG_PORT=$PORT
# Extra arguments, e.g. --behind-proxy when an HTTPS reverse proxy sits in front.
SHACKLOG_ARGS=
EOF
fi

if [ -n "${SHACKLOG_RESTORE:-}" ]; then
  [ -f "$SHACKLOG_RESTORE" ] || die "Restore archive not found: $SHACKLOG_RESTORE"
  if [ -f "$DATA/shacklog.sqlite3" ]; then
    warn "Data already present in $DATA: not restoring over it."
  else
    info "Restoring data from $(basename "$SHACKLOG_RESTORE")…"
    tar -xzf "$SHACKLOG_RESTORE" -C "$DATA"
    # The archive holds database copies under backups/; bring the newest back as the live DB.
    if [ ! -f "$DATA/shacklog.sqlite3" ]; then
      newest=$(find "$DATA/backups" -maxdepth 1 -name 'shacklog-*.sqlite3' 2>/dev/null | sort | tail -1)
      [ -n "$newest" ] && cp "$newest" "$DATA/shacklog.sqlite3"
    fi
    chown -R shacklog:shacklog "$DATA"
    ok "Data restored"
  fi
fi

for f in shacklog.service shacklog-control.path shacklog-control.service; do
  install -m 0644 "$APP_DIR/deploy/systemd/$f" "/etc/systemd/system/$f"
done
chmod 0755 "$APP_DIR/deploy/shacklog-ctl" "$APP_DIR/deploy/uninstall.sh"
ln -sf "$APP_DIR/deploy/shacklog-ctl" /usr/local/bin/shacklog-ctl
systemctl daemon-reload

if [ -n "${SHACKLOG_PASSWORD:-}" ]; then
  runuser -u shacklog -- env SHACKLOG_DATA="$DATA" SHACKLOG_PASSWORD="$SHACKLOG_PASSWORD" \
    "$APP_DIR/.venv/bin/python" -m shacklog set-password >/dev/null
  ok "Web password set"
fi

systemctl enable --quiet --now shacklog-control.path
systemctl enable --quiet shacklog
systemctl restart shacklog
info "Waiting for ShackLog to answer…"
for _ in $(seq 1 40); do
  curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 \
  || die "ShackLog didn't start. Look at: journalctl -u shacklog -n 50"

IP=$(hostname -I | awk '{print $1}')
ok "ShackLog is running at ${GN}http://$IP:$PORT/${CL}"
CODE=$(journalctl -u shacklog --no-pager -n 100 | grep -o 'setup code [0-9-]*' | tail -1 | awk '{print $3}' || true)
[ -n "$CODE" ] && echo -e "   First visit: enter setup code ${GN}$CODE${CL} and choose a password."
echo "   Manage it with: shacklog-ctl status | update | restart | logs | backup"
