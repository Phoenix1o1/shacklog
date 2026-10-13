#!/usr/bin/env bash
# =====================================================================================
#  Remove ShackLog from this machine — works on the Proxmox host (cleans up an install
#  made directly on the host by mistake, and can remove ShackLog containers) and inside
#  a ShackLog container. Asks before every step and whether to KEEP or REMOVE the data.
#
#    bash deploy/uninstall.sh            interactive
#    bash deploy/uninstall.sh --keep-data | --remove-data   non-interactive choice
#
#  "Keep" makes an archive in /root first (restore it into a new container with
#  deploy/proxmox/shacklog-lxc.sh) and leaves the data folder where it is.
# =====================================================================================
set -Eeuo pipefail

GN=$'\033[1;92m'; RD=$'\033[01;31m'; BL=$'\033[36m'; YW=$'\033[33m'; CL=$'\033[m'; BLD=$'\033[1m'
ok()   { echo -e " ${GN}✓${CL} $*"; }
info() { echo -e " ${BL}…${CL} $*"; }
warn() { echo -e " ${YW}!${CL} $*"; }
die()  { echo -e " ${RD}✗${CL} $*" >&2; exit 1; }
ask()  { local a; read -r -p " $1 [y/N] " a; [[ "$a" =~ ^[yYsS]$ ]]; }

[ "$(id -u)" -eq 0 ] || die "Run as root."
CHOICE="${1:-}"
STAMP=$(date +%Y%m%d-%H%M)

echo -e "\n${BLD}ShackLog uninstaller${CL} on $(hostname)\n"

# ------------------------------------------------------------------ what is installed?
DATA_DIRS=()
[ -d /var/lib/shacklog ] && DATA_DIRS+=(/var/lib/shacklog)
HAS_SERVICE=0; systemctl list-unit-files 2>/dev/null | grep -q '^shacklog\.service' && HAS_SERVICE=1
HAS_CODE=0; [ -d /opt/shacklog ] && HAS_CODE=1
HAS_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx shacklog; then
  HAS_DOCKER=1
  vol=$(docker inspect shacklog --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)
  [ -n "$vol" ] && DATA_DIRS+=("$vol")
fi
CTS=()
if command -v pct >/dev/null 2>&1; then
  while read -r id name; do
    [ -z "${id:-}" ] && continue
    if [ "${name:-}" = shacklog ] || pct config "$id" 2>/dev/null | grep -q '^tags:.*shacklog'; then CTS+=("$id"); fi
  done < <(pct list 2>/dev/null | awk 'NR>1 {print $1, $NF}')
fi

echo " Found:"
[ "$HAS_SERVICE" = 1 ] && echo "   - systemd service shacklog"
[ "$HAS_CODE" = 1 ]    && echo "   - program files /opt/shacklog"
[ "$HAS_DOCKER" = 1 ]  && echo "   - Docker container shacklog"
for d in "${DATA_DIRS[@]}"; do echo "   - data $d ($(du -sh "$d" 2>/dev/null | awk '{print $1}'))"; done
for id in "${CTS[@]}"; do echo "   - Proxmox container $id ($(pct config "$id" | awk '/^hostname:/{print $2}'))"; done
if [ "$HAS_SERVICE$HAS_CODE$HAS_DOCKER" = 000 ] && [ ${#DATA_DIRS[@]} -eq 0 ] && [ ${#CTS[@]} -eq 0 ]; then
  ok "Nothing to remove."; exit 0
fi
echo

# ------------------------------------------------------------------ keep or remove data
case "$CHOICE" in
  --keep-data) KEEP=1 ;;
  --remove-data) KEEP=0 ;;
  *)
    echo " What about the data (database, library files, backups)?"
    echo "   1) Keep it: archive to /root and leave it in place   (recommended)"
    echo "   2) Remove it permanently"
    read -r -p " Choose 1 or 2: " c
    case "$c" in 1) KEEP=1 ;; 2) KEEP=0 ;; *) die "Nothing done." ;; esac ;;
esac
if [ "$KEEP" = 0 ]; then
  read -r -p " Type DELETE to confirm removing all ShackLog data on this machine: " c
  [ "$c" = DELETE ] || die "Nothing done."
fi

# ------------------------------------------------------------------ stop and archive
if [ "$HAS_SERVICE" = 1 ]; then
  info "Stopping the service…"
  systemctl disable --now shacklog-control.path shacklog-update-check.timer shacklog.service >/dev/null 2>&1 || true
fi
[ "$HAS_DOCKER" = 1 ] && { info "Stopping the Docker container…"; docker stop shacklog >/dev/null 2>&1 || true; }

if [ "$KEEP" = 1 ]; then
  for d in "${DATA_DIRS[@]}"; do
    out="/root/shacklog-data-$(basename "$d")-$STAMP.tar.gz"
    info "Archiving $d…"
    tar --exclude="./tmp" -czf "$out" -C "$d" .
    ok "Data archived: ${GN}$out${CL}"
  done
fi

# ------------------------------------------------------------------ remove the program
for f in shacklog.service shacklog-control.path shacklog-control.service \
         shacklog-update-check.service shacklog-update-check.timer; do
  rm -f "/etc/systemd/system/$f"
done
[ "$HAS_SERVICE" = 1 ] && systemctl daemon-reload
rm -f /usr/local/bin/shacklog-ctl
if [ "$HAS_CODE" = 1 ]; then rm -rf /opt/shacklog; ok "Removed /opt/shacklog"; fi
if [ "$HAS_DOCKER" = 1 ]; then docker rm shacklog >/dev/null 2>&1 && ok "Removed the Docker container"; fi
if [ -d /etc/shacklog ]; then
  if [ "$KEEP" = 1 ]; then cp -a /etc/shacklog "/root/shacklog-config-$STAMP" && ok "Config copied to /root/shacklog-config-$STAMP"; fi
  rm -rf /etc/shacklog
fi

if [ "$KEEP" = 0 ]; then
  for d in "${DATA_DIRS[@]}"; do rm -rf "$d"; ok "Removed data $d"; done
  id shacklog >/dev/null 2>&1 && userdel shacklog 2>/dev/null || true
else
  [ ${#DATA_DIRS[@]} -gt 0 ] && ok "Data left in place: ${DATA_DIRS[*]}"
fi

# ------------------------------------------------------------------ containers (on the host)
for id in "${CTS[@]}"; do
  name=$(pct config "$id" | awk '/^hostname:/{print $2}')
  if ask "Also destroy Proxmox container $id ($name)?"; then
    if [ "$KEEP" = 1 ]; then
      storage=$(pvesm status -content backup 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')
      if [ -n "$storage" ] && ask "Back it up first with vzdump to storage '$storage'?"; then
        vzdump "$id" --storage "$storage" --mode stop --compress zstd --notes-template "ShackLog before removal" \
          && ok "Container $id backed up to $storage"
      fi
      info "Saving its data archive…"
      if pct status "$id" | grep -q running; then
        pct exec "$id" -- bash -c 'shacklog-ctl stop >/dev/null 2>&1; tar --exclude=./tmp -czf /root/shacklog-data.tar.gz -C /var/lib/shacklog .' \
          && pct pull "$id" /root/shacklog-data.tar.gz "/root/shacklog-data-ct$id-$STAMP.tar.gz" \
          && ok "Data archived: /root/shacklog-data-ct$id-$STAMP.tar.gz"
      fi
    fi
    pct stop "$id" >/dev/null 2>&1 || true
    pct destroy "$id" --purge 1 && ok "Container $id destroyed"
  fi
done

echo
ok "ShackLog removed from $(hostname)."
[ "$KEEP" = 1 ] && echo "   To move the data into a new container: bash deploy/proxmox/shacklog-lxc.sh (choose 'Restore data')."
