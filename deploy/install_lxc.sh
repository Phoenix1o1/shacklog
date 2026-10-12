#!/usr/bin/env bash
# Install or update the ShackLog hub inside a Debian 12 / Ubuntu 24.04 LXC container.
# Run as root inside the container:  bash install_lxc.sh
set -euo pipefail

REPO="${REPO:-https://github.com/Phoenix1o1/shacklog.git}"   # private: needs a token or deploy key

apt-get update
apt-get install -y python3 python3-venv git

id shacklog >/dev/null 2>&1 || useradd --system --home /var/lib/shacklog --shell /usr/sbin/nologin shacklog
mkdir -p /var/lib/shacklog && chown shacklog:shacklog /var/lib/shacklog

if [ -d /opt/shacklog/.git ]; then
  git -C /opt/shacklog pull --ff-only
else
  git clone "$REPO" /opt/shacklog
fi
python3 -m venv /opt/shacklog/.venv
/opt/shacklog/.venv/bin/pip install --upgrade pip
/opt/shacklog/.venv/bin/pip install -r /opt/shacklog/requirements.txt

cp /opt/shacklog/deploy/shacklog.service /etc/systemd/system/shacklog.service
systemctl daemon-reload
systemctl enable --now shacklog
systemctl restart shacklog
sleep 2
journalctl -u shacklog -n 20 --no-pager
echo
echo "ShackLog hub running on port 8573. The setup code (first start only) is in the log above."
