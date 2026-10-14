#!/usr/bin/env bash
# =====================================================================================
#  Input checks for shacklog-lxc.sh (sourced by it; also sourced by the tests).
#
#  ask() asks again until a check passes (it needs the script's wt() whiptail wrapper).
#  Each check_* function takes the typed value and prints NOTHING when it is fine, or
#  one short sentence saying what is wrong (shown above the input box when it is asked
#  again). Pure bash, no Proxmox commands, so the tests can run them anywhere.
# =====================================================================================

# --- IPv4 ----------------------------------------------------------------------------

# 0 if $1 is a dotted IPv4 address with every part 0-255 (no leading zeros: "010" is
# octal to some tools and decimal to others).
is_ipv4() {
  local ip=$1 part
  local -a parts
  [[ $ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  IFS=. read -r -a parts <<<"$ip"
  for part in "${parts[@]}"; do
    [[ $part =~ ^(0|[1-9][0-9]{0,2})$ ]] || return 1
    ((part <= 255)) || return 1
  done
}

ip_to_int() {
  local a b c d
  IFS=. read -r a b c d <<<"$1"
  echo $(((a << 24) | (b << 16) | (c << 8) | d))
}

int_to_ip() {
  local n=$1
  echo "$(((n >> 24) & 255)).$(((n >> 16) & 255)).$(((n >> 8) & 255)).$((n & 255))"
}

prefix_mask() {  # prefix length → netmask as an integer
  local p=$1
  ((p == 0)) && { echo 0; return; }
  echo $(((0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF))
}

# Static address with prefix, e.g. 192.168.1.50/24.
check_cidr() {
  local value=$1 ip prefix
  if [[ $value != */* ]]; then
    echo "Add the prefix length after a slash, e.g. 192.168.1.50/24."; return
  fi
  ip=${value%/*}; prefix=${value#*/}
  is_ipv4 "$ip" || { echo "\"$ip\" isn't an IPv4 address (four numbers 0-255, e.g. 192.168.1.50)."; return; }
  [[ $prefix =~ ^[0-9]{1,2}$ ]] && ((prefix >= 8 && prefix <= 32)) \
    || { echo "The prefix after the slash must be 8-32 (a home network is usually /24)."; return; }
  local n mask net bcast first
  n=$(ip_to_int "$ip"); mask=$(prefix_mask "$prefix")
  net=$((n & mask)); bcast=$((net | (~mask & 0xFFFFFFFF)))
  first=${ip%%.*}
  if ((first == 0 || first == 127 || first >= 224)); then
    echo "$ip can't be a container address (0.x, 127.x loopback, 224+ multicast/reserved)."; return
  fi
  if ((prefix <= 30 && n == net)); then
    echo "$ip is the network address of $(int_to_ip "$net")/$prefix; use a host address, e.g. $(int_to_ip $((net + 50)))/$prefix."; return
  fi
  if ((prefix <= 30 && n == bcast)); then
    echo "$ip is the broadcast address of $(int_to_ip "$net")/$prefix; use a host address."; return
  fi
}

# Gateway for a static address ($2 = the address with prefix). Required: the container
# downloads ShackLog (and later its updates) from GitHub.
check_gateway() {
  local gw=$1 cidr=$2
  [ -z "$gw" ] && { echo "A gateway is needed: the container downloads ShackLog from GitHub."; return; }
  is_ipv4 "$gw" || { echo "\"$gw\" isn't an IPv4 address (e.g. 192.168.1.1)."; return; }
  local ip=${cidr%/*} prefix=${cidr#*/} mask
  [ "$gw" = "$ip" ] && { echo "The gateway can't be the container's own address."; return; }
  ((prefix >= 31)) && return  # point-to-point: the gateway is outside by definition
  mask=$(prefix_mask "$prefix")
  if (( ($(ip_to_int "$gw") & mask) != ($(ip_to_int "$ip") & mask) )); then
    echo "$gw isn't in $cidr's network ($(int_to_ip $(($(ip_to_int "$ip") & mask)))/$prefix): the router of that network is usually its .1 or .254."; return
  fi
  local n net bcast
  n=$(ip_to_int "$gw"); net=$(($(ip_to_int "$ip") & mask)); bcast=$((net | (~mask & 0xFFFFFFFF)))
  if ((prefix <= 30 && (n == net || n == bcast))); then
    echo "$gw is the network or broadcast address, not a router."; return
  fi
}

# --- other values ---------------------------------------------------------------------

check_int_range() {  # value min max what
  local v=$1 min=$2 max=$3 what=$4
  [[ $v =~ ^[0-9]+$ ]] && ((10#$v >= min && 10#$v <= max)) || echo "$what must be a whole number from $min to $max."
}

check_ctid() { check_int_range "$1" 100 999999999 "The container ID"; }
check_cores() { check_int_range "$1" 1 "${2:-128}" "CPU cores"; }
check_ram() { check_int_range "$1" 256 65536 "Memory (MB)"; }
check_swap() { check_int_range "$1" 0 65536 "Swap (MB)"; }
check_disk() { check_int_range "$1" 4 4096 "The disk (GB)"; }

check_port() {
  [[ $1 =~ ^[0-9]+$ ]] && ((10#$1 >= 1024 && 10#$1 <= 65535)) \
    || echo "The port must be a whole number from 1024 to 65535 (below 1024 needs root; ShackLog runs as its own user)."
}

check_vlan() {  # empty = no VLAN
  [ -z "$1" ] && return
  check_int_range "$1" 1 4094 "The VLAN tag"
}

# RFC 1123 host name: letters, digits and hyphens, 1-63 characters, not starting or
# ending with a hyphen.
check_hostname() {
  [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] \
    || echo "Use letters, digits and hyphens only (1-63, not starting or ending with -), e.g. shacklog."
}

check_archive() {  # a data archive made by the uninstaller or "shacklog-ctl backup"
  [ -f "$1" ] || { echo "No file at $1."; return; }
  [[ $1 == *.tar.gz || $1 == *.tgz ]] || { echo "Choose a .tar.gz archive (shacklog-data-….tar.gz)."; return; }
  tar -tzf "$1" >/dev/null 2>&1 || echo "$1 isn't a readable .tar.gz archive."
}

check_time() {  # HH:MM, 24 h
  [[ $1 =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || echo "Write the time as HH:MM, 24 h (e.g. 03:30)."
}

# --- asking ---------------------------------------------------------------------------

trim() { local v=$1; v=${v#"${v%%[![:space:]]*}"}; echo "${v%"${v##*[![:space:]]}"}"; }

# Ask until the answer passes a check from validate.sh (or one below). The reason a
# value was refused is shown above the box. Returns 1 on Cancel, so call it as
#   VAR=$(ask "Title" "Question" "default" check_fn [extra args]) || exit 0
ask() {
  local title=$1 prompt=$2 value=$3 check=$4 problem="" text height
  shift 4
  while true; do
    text=$prompt; height=10
    if [ -n "$problem" ]; then text="✗ $problem\n\n$prompt"; height=14; fi
    value=$(wt --title "$title" --inputbox "$text" "$height" 74 "$value") || return 1
    value=$(trim "$value")
    problem=$("$check" "$value" "$@")
    [ -z "$problem" ] && { echo "$value"; return 0; }
  done
}
