# Running the ShackLog hub on Proxmox

The hub is the instance every other computer syncs with, and the website you open from
your phone. It runs the same code in **server mode**: no browser, login always required.

## Option A — Docker (in a VM or an LXC with nesting enabled)

```bash
git clone https://github.com/Phoenix1o1/shacklog.git && cd shacklog
docker compose -f deploy/docker-compose.yml up -d --build
docker logs shacklog          # shows the one-time setup code
```

Data lives in `deploy/shacklog-data/` (database, library files, backups).

## Option B — plain LXC container (lighter)

1. Create a Debian 12 LXC (512 MB RAM and 1 core are plenty; give the disk room for
   manuals and photos).
2. Inside it, as root: `bash install_lxc.sh` (from this folder). It installs Python,
   clones the repo to `/opt/shacklog`, creates the `shacklog` user, and starts the
   `shacklog` systemd service. Data lives in `/var/lib/shacklog`.
3. `journalctl -u shacklog` shows the one-time setup code.

The repository is private: for `git clone` inside the container use a GitHub token
(`https://<token>@github.com/Phoenix1o1/shacklog.git`) or a read-only deploy key.
Update later with the same script (it pulls and restarts).

## First start

1. Open `http://<container-ip>:8573` from any device on the LAN.
2. Enter the **setup code** from the log and choose a password (8+ characters).
3. Finish *Setup*: name this instance "Proxmox", enter your callsign and stations.
4. *Settings › Sync*: tick **Accept sync from other instances** and generate a token.

## Connecting the other computers

On each PC/laptop: *Settings › Sync* → hub URL `http://<container-ip>:8573` + the token →
**Test connection** → **Sync now**. After that it syncs every 15 minutes while running.
A brand-new instance can use **Join a hub** on its first-run page instead of setup: it
pulls everything from the hub.

## From outside home

Use your own VPN (Tailscale or WireGuard on the Proxmox host) and open the same address.
Do not forward port 8573 on the router. If you ever need it public, put it behind an
HTTPS reverse proxy (Caddy, nginx, Traefik) and start with `--behind-proxy`.

## Backups

ShackLog makes a database backup once a day and before every import
(`backups/` in the data folder). Include the whole data folder in your Proxmox backup
job (vzdump) — the library files are only stored there.
