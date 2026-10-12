#!/usr/bin/env bash
# =====================================================================================
#  ShackLog — dedicated LXC container on Proxmox VE (in the style of the community
#  Proxmox VE Helper Scripts). Run on the Proxmox HOST as root, from a clone of the repo:
#
#      bash deploy/proxmox/shacklog-lxc.sh
#
#  It asks for (or uses defaults for) container ID, name, CPU, RAM, disk, storage and
#  network; creates an unprivileged Debian container that runs only ShackLog; installs
#  it with deploy/install.sh; can restore data from an archive; and can add a nightly
#  vzdump backup job to your NAS storage.
# =====================================================================================
set -Eeuo pipefail

APP="ShackLog"
NSAPP="shacklog"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Defaults ("Default settings"). Sized for a single-user logbook:
#   1 core  — the hub is idle almost always; sync and search are light.
#   512 MB  — Python + web server use ~120 MB; headroom for indexing big PDF manuals.
#   8 GB    — system ~1.5 GB, ShackLog ~0.3 GB, the rest for manuals, photos, backups.
#             Grow later without downtime: pct resize <id> rootfs +8G
var_cpu=1
var_ram=512
var_swap=512
var_disk=8
var_bridge=vmbr0
var_port=8573
var_unprivileged=1

# ------------------------------------------------------------------ look & feel
YW=$'\033[33m'; BL=$'\033[36m'; RD=$'\033[01;31m'; GN=$'\033[1;92m'; DGN=$'\033[32m'
CL=$'\033[m'; BLD=$'\033[1m'; BFR="\\r\\033[K"; HOLD=" "
CM="${GN}✓${CL}"; CROSS="${RD}✗${CL}"; TAB="  "

header_info() {
  clear
  cat <<"EOF"
   _____ __               __   __
  / ___// /_  ____ ______/ /__/ /   ____  ____ _
  \__ \/ __ \/ __ `/ ___/ //_/ /   / __ \/ __ `/
 ___/ / / / / /_/ / /__/ ,< / /___/ /_/ / /_/ /
/____/_/ /_/\__,_/\___/_/|_/_____/\____/\__, /
                                       /____/
EOF
  echo -e "${TAB}${BLD}Amateur radio logbook — dedicated LXC for the hub${CL}\n"
}

SPINNER_PID=""
spinner() {
  local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
  while true; do printf "\r ${YW}%s${CL}" "${frames:i++%${#frames}:1}"; sleep 0.1; done
}
msg_info() {
  stop_spinner
  printf " %s %s${YW}%s${CL}" "$HOLD" "" "$1"
  spinner & SPINNER_PID=$!
}
stop_spinner() {
  if [ -n "$SPINNER_PID" ] && kill -0 "$SPINNER_PID" 2>/dev/null; then kill "$SPINNER_PID" 2>/dev/null; wait "$SPINNER_PID" 2>/dev/null || true; fi
  SPINNER_PID=""
}
msg_ok()    { stop_spinner; echo -e "${BFR} ${CM} ${GN}$1${CL}"; }
msg_error() { stop_spinner; echo -e "${BFR} ${CROSS} ${RD}$1${CL}"; }
on_error() {
  local code=$? line=$1
  stop_spinner
  echo -e "\n ${CROSS} ${RD}Failed (exit $code) at line $line: ${BASH_COMMAND}${CL}"
  if [ -n "${CTID:-}" ] && pct status "$CTID" >/dev/null 2>&1 && [ "${CT_CREATED:-0}" = 1 ]; then
    echo -e " Container $CTID was created. Inspect it with ${BL}pct enter $CTID${CL},"
    echo -e " or remove it with ${BL}pct stop $CTID; pct destroy $CTID${CL}."
  fi
  exit "$code"
}
trap 'on_error $LINENO' ERR
trap 'stop_spinner' EXIT

BACKTITLE="Proxmox VE — $APP"
wt() { whiptail --backtitle "$BACKTITLE" "$@" 3>&1 1>&2 2>&3; }

# ------------------------------------------------------------------ checks
pve_check() {
  [ "$(id -u)" -eq 0 ] || { msg_error "Run as root on the Proxmox host."; exit 1; }
  command -v pveversion >/dev/null 2>&1 || { msg_error "This isn't a Proxmox VE host (no pveversion)."; exit 1; }
  local ver; ver=$(pveversion | grep -oE 'pve-manager/[0-9]+' | cut -d/ -f2)
  if [ "${ver:-0}" -lt 7 ]; then msg_error "Proxmox VE 7 or newer is needed."; exit 1; fi
  command -v whiptail >/dev/null 2>&1 || apt-get install -y whiptail >/dev/null
  [ -f "$REPO_ROOT/deploy/install.sh" ] || { msg_error "Run this from a ShackLog clone (deploy/install.sh missing)."; exit 1; }
  if systemctl list-unit-files 2>/dev/null | grep -q '^shacklog\.service'; then
    whiptail --backtitle "$BACKTITLE" --title "ShackLog installed on the host" --yesno \
"ShackLog is installed directly on this Proxmox host (an earlier install).

It should live only in its own container. Remove it from the host now? You'll be asked whether to keep its data (it is archived to /root and can be restored into the new container)." 14 72 \
      && bash "$REPO_ROOT/deploy/uninstall.sh"
  fi
}

# ------------------------------------------------------------------ choices
storage_menu() {  # $1 = content type (rootdir|vztmpl|backup), $2 = title
  local content=$1 title=$2 menu=() line name type avail
  while read -r line; do
    name=$(awk '{print $1}' <<<"$line"); type=$(awk '{print $2}' <<<"$line")
    avail=$(awk '{printf "%.1f GB free", $6/1024/1024}' <<<"$line")
    menu+=("$name" "$type, $avail")
  done < <(pvesm status -content "$content" 2>/dev/null | awk 'NR>1 && $3=="active"')
  if [ ${#menu[@]} -eq 0 ]; then echo ""; return; fi
  if [ ${#menu[@]} -eq 2 ]; then echo "${menu[0]}"; return; fi
  wt --title "$title" --menu "Choose the storage:" 18 70 8 "${menu[@]}" || true
}

bridge_menu() {
  local menu=() b
  for b in $(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1); do
    menu+=("$b" "$(ip -4 -o addr show "$b" 2>/dev/null | awk '{print $4}' | head -1)")
  done
  if [ ${#menu[@]} -le 2 ]; then echo "${menu[0]:-vmbr0}"; return; fi
  wt --title "Network bridge" --menu "Bridge the container connects to:" 16 60 6 "${menu[@]}" || echo vmbr0
}

default_settings() {
  CTID=$(pvesh get /cluster/nextid)
  HN=$NSAPP; CORES=$var_cpu; RAM=$var_ram; SWAP=$var_swap; DISK=$var_disk
  BRIDGE=$var_bridge; NET="dhcp"; GATE=""; VLAN=""; ONBOOT=1; PORT=$var_port
  STORAGE=$(storage_menu rootdir "Container storage")
}

advanced_settings() {
  CTID=$(wt --title "Container ID" --inputbox "Container ID:" 9 58 "$(pvesh get /cluster/nextid)") || exit 0
  if pct status "$CTID" >/dev/null 2>&1 || qm status "$CTID" >/dev/null 2>&1; then
    msg_error "ID $CTID is already used."; exit 1
  fi
  HN=$(wt --title "Hostname" --inputbox "Hostname of the container:" 9 58 "$NSAPP") || exit 0
  CORES=$(wt --title "CPU cores" --inputbox \
"CPU cores. 1 is plenty: the hub is idle most of the time.
Use 2 only if you index many large PDF manuals." 11 62 "$var_cpu") || exit 0
  RAM=$(wt --title "RAM (MB)" --inputbox \
"Memory in MB. ShackLog uses about 120 MB.
512 is recommended; 1024 if you upload many big PDFs." 11 62 "$var_ram") || exit 0
  SWAP=$(wt --title "Swap (MB)" --inputbox "Swap in MB:" 9 58 "$var_swap") || exit 0
  DISK=$(wt --title "Disk (GB)" --inputbox \
"Root disk in GB. System ~1.5 GB, program ~0.3 GB; the rest
holds your database, manuals, photos and local backups.
8 GB is a good start; grow it later with: pct resize ID rootfs +8G" 12 66 "$var_disk") || exit 0
  STORAGE=$(storage_menu rootdir "Container storage (where the disk lives)")
  BRIDGE=$(bridge_menu)
  if wt --title "IP address" --yesno "Get the address by DHCP?\n\nChoose No to set a static address (recommended for a server: the other computers sync to it by address)." 11 64; then
    NET=dhcp; GATE=""
  else
    NET=$(wt --title "Static IP" --inputbox "Address with prefix, e.g. 192.168.1.50/24:" 9 58 "") || exit 0
    GATE=$(wt --title "Gateway" --inputbox "Gateway, e.g. 192.168.1.1:" 9 58 "") || exit 0
  fi
  VLAN=$(wt --title "VLAN" --inputbox "VLAN tag (empty for none):" 9 58 "") || exit 0
  PORT=$(wt --title "Web port" --inputbox "Port ShackLog listens on:" 9 58 "$var_port") || exit 0
  if wt --title "Start at boot" --yesno "Start the container when Proxmox boots?" 8 58; then ONBOOT=1; else ONBOOT=0; fi
}

extra_questions() {
  # GitHub access for the private repository (needed for install and for updates).
  TOKEN=""
  local remote; remote=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || echo "https://github.com/Phoenix1o1/shacklog.git")
  REPO_URL=$(sed -E 's#https://[^@/]+@#https://#' <<<"$remote")
  if [[ "$REPO_URL" == git@github.com:* ]]; then REPO_URL="https://github.com/${REPO_URL#git@github.com:}"; fi
  if ! GIT_TERMINAL_PROMPT=0 timeout 15 git ls-remote "$REPO_URL" HEAD >/dev/null 2>&1; then
    TOKEN=$(wt --title "GitHub access" --passwordbox \
"The repository is private. Paste a GitHub token with READ access to it:
github.com › Settings › Developer settings › Fine-grained tokens ›
Repository access: only shacklog · Permissions: Contents = Read-only.

It is stored only inside the container (/etc/shacklog, root only)
and is used for installing and for 'Update' later." 15 72) || exit 0
    [ -n "$TOKEN" ] || { msg_error "A token is needed for a private repository."; exit 1; }
  fi

  RESTORE=""
  local newest; newest=$(ls -1t /root/shacklog-data-*.tar.gz 2>/dev/null | head -1 || true)
  if [ -n "$newest" ] && wt --title "Restore data" --yesno \
"Found a ShackLog data archive:

  $newest

Restore it into the new container? (Your previous hub's database,
files and backups, made by the uninstaller or 'shacklog-ctl backup'.)" 13 72; then
    RESTORE=$newest
  elif wt --title "Restore data" --defaultno --yesno "Restore data from another archive (.tar.gz)?" 8 60; then
    RESTORE=$(wt --title "Restore data" --inputbox "Path of the archive on this host:" 9 70 "/root/") || exit 0
    [ -f "$RESTORE" ] || { msg_error "No such file: $RESTORE"; exit 1; }
  fi

  BACKUP_STORAGE=""; BACKUP_TIME="03:30"
  if wt --title "Automatic backups" --yesno \
"Add a nightly Proxmox backup (vzdump) of this container?

Pick your NAS storage (e.g. an NFS/SMB/PBS storage added in
Datacenter › Storage). It saves the whole container — program,
database and every library file — and keeps 7 daily, 4 weekly
and 6 monthly copies." 14 68; then
    BACKUP_STORAGE=$(storage_menu backup "Backup storage (your NAS)")
    if [ -z "$BACKUP_STORAGE" ]; then
      whiptail --backtitle "$BACKTITLE" --msgbox "No storage accepts backups yet. Add your NAS in Datacenter › Storage (content: VZDump backup file), then create the job in Datacenter › Backup." 10 70
    else
      BACKUP_TIME=$(wt --title "Backup time" --inputbox "Time of day (HH:MM, host time):" 9 58 "03:30") || exit 0
    fi
  fi
}

summary() {
  local backup="no"; [ -n "$BACKUP_STORAGE" ] && backup="to $BACKUP_STORAGE at $BACKUP_TIME"
  local net="$NET"; [ -n "$GATE" ] && net="$NET gw $GATE"; [ -n "$VLAN" ] && net="$net vlan $VLAN"
  wt --title "Create the $APP container?" --yesno \
"  ID / hostname      $CTID / $HN
  CPU / RAM / swap   $CORES core(s) / $RAM MB / $SWAP MB
  Disk               $DISK GB on ${STORAGE:-?}
  Network            $BRIDGE, $net
  Web port           $PORT
  Unprivileged       yes (nesting on for systemd)
  Start at boot      $([ "$ONBOOT" = 1 ] && echo yes || echo no)
  Restore data       ${RESTORE:-no}
  Nightly backup     $backup" 18 72
}

# ------------------------------------------------------------------ build
pick_template() {
  msg_info "Updating the template list"
  pveam update >/dev/null 2>&1 || true
  TEMPLATE=$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E '^debian-13-standard' | sort -V | tail -1)
  [ -n "$TEMPLATE" ] || TEMPLATE=$(pveam available --section system | awk '{print $2}' | grep -E '^debian-12-standard' | sort -V | tail -1)
  [ -n "$TEMPLATE" ] || { msg_error "No Debian template available (check the host's internet)."; exit 1; }
  TEMPLATE_STORAGE=$(pvesm status -content vztmpl | awk 'NR>1 && $3=="active"{print $1; exit}')
  if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
    msg_info "Downloading $TEMPLATE"
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
  fi
  msg_ok "Template $TEMPLATE"
}

create_container() {
  local net0="name=eth0,bridge=$BRIDGE,ip=$NET"
  [ -n "$GATE" ] && net0="$net0,gw=$GATE"
  [ -n "$VLAN" ] && net0="$net0,tag=$VLAN"
  [ "$NET" = dhcp ] && net0="$net0,ip6=auto"
  msg_info "Creating container $CTID"
  pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "$HN" --cores "$CORES" --memory "$RAM" --swap "$SWAP" \
    --rootfs "$STORAGE:$DISK" --net0 "$net0" --ostype debian \
    --unprivileged "$var_unprivileged" --features nesting=1 --onboot "$ONBOOT" \
    --timezone host --tags "$NSAPP" \
    --description "$(printf '<div align="center"><h2>ShackLog</h2><p>Amateur radio logbook hub</p><p><a href="http://%s:%s/">Open ShackLog</a> · manage with <code>shacklog-ctl</code></p></div>' "$HN" "$PORT")" \
    >/dev/null
  CT_CREATED=1
  msg_ok "Created container $CTID ($HN)"
  msg_info "Starting the container"
  pct start "$CTID"
  local i
  for i in $(seq 1 60); do
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
    sleep 1
  done
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || { msg_error "The container has no network/DNS."; exit 1; }
  msg_ok "Container running and online"
}

install_app() {
  local tmp; tmp=$(mktemp -d)
  pct push "$CTID" "$REPO_ROOT/deploy/install.sh" /root/shacklog-install.sh --perms 0700
  local envs=("SHACKLOG_REPO=$REPO_URL" "SHACKLOG_PORT=$PORT")
  if [ -n "$TOKEN" ]; then
    (umask 077; printf '%s' "$TOKEN" > "$tmp/token")
    pct push "$CTID" "$tmp/token" /root/.shacklog-token --perms 0600
    envs+=("GITHUB_TOKEN_FILE=/root/.shacklog-token")
  fi
  if [ -n "$RESTORE" ]; then
    msg_info "Copying the data archive into the container"
    pct push "$CTID" "$RESTORE" /root/shacklog-restore.tar.gz
    envs+=("SHACKLOG_RESTORE=/root/shacklog-restore.tar.gz")
    msg_ok "Data archive copied"
  fi
  rm -rf "$tmp"
  stop_spinner
  echo -e " ${BL}Installing $APP inside the container…${CL}"
  pct exec "$CTID" -- env "${envs[@]}" bash /root/shacklog-install.sh
  pct exec "$CTID" -- rm -f /root/.shacklog-token /root/shacklog-restore.tar.gz /root/shacklog-install.sh
}

backup_job() {
  [ -n "$BACKUP_STORAGE" ] || return 0
  msg_info "Adding the nightly backup job"
  if pvesh create /cluster/backup --vmid "$CTID" --storage "$BACKUP_STORAGE" \
      --schedule "$BACKUP_TIME" --mode snapshot --compress zstd --enabled 1 \
      --prune-backups "keep-daily=7,keep-weekly=4,keep-monthly=6" \
      --notes-template "{{guestname}} ShackLog" --comment "ShackLog nightly" >/dev/null 2>&1; then
    msg_ok "Nightly backup at $BACKUP_TIME to $BACKUP_STORAGE (Datacenter › Backup)"
  else
    msg_error "Couldn't create the backup job: add it in Datacenter › Backup"
  fi
}

# ------------------------------------------------------------------ main
header_info
pve_check
CHOICE=$(wt --title "$APP LXC" --menu "Create a dedicated container for the ShackLog hub:" 13 64 3 \
  "1" "Default settings (1 core, 512 MB, 8 GB, DHCP)" \
  "2" "Advanced settings (choose everything)" \
  "3" "Exit") || exit 0
case "$CHOICE" in
  1) default_settings ;;
  2) advanced_settings ;;
  *) exit 0 ;;
esac
[ -n "$STORAGE" ] || { msg_error "No storage for containers found (pvesm status -content rootdir)."; exit 1; }
extra_questions
summary || exit 0
header_info
pick_template
create_container
install_app
backup_job

IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
CODE=$(pct exec "$CTID" -- bash -c "journalctl -u shacklog --no-pager -n 200 | grep -o 'setup code [0-9-]*' | tail -1 | awk '{print \$3}'" || true)
echo
msg_ok "Completed successfully!\n"
echo -e "${TAB}${BLD}Open ShackLog:${CL}  ${BL}http://${IP}:${PORT}/${CL}"
[ -n "$CODE" ] && echo -e "${TAB}First visit:    enter setup code ${GN}${CODE}${CL} and choose a password"
echo -e "${TAB}Manage:         ${DGN}pct enter $CTID${CL}  then  ${DGN}shacklog-ctl status | update | restart | logs | backup${CL}"
echo -e "${TAB}Remove:         ${DGN}pct enter $CTID${CL}  then  ${DGN}shacklog-ctl uninstall${CL}  (or on the host: bash deploy/uninstall.sh)"
echo -e "${TAB}Other computers: Settings › Sync › hub address http://${IP}:${PORT}"
echo
