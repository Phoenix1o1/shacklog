# ShackLog hub on Proxmox

The hub is the ShackLog instance every PC syncs with, and the website you open from your
phone. It gets **its own unprivileged LXC container** that runs nothing else: isolated from
your other systems, easy to back up to the NAS as one unit, easy to throw away and rebuild.

```
Proxmox host ── CT "shacklog" (Debian, 1 core, 512 MB, 8 GB)
                  /opt/shacklog        program (git working copy)
                  /var/lib/shacklog    database, library files, backups   ← your data
                  /etc/shacklog        settings + GitHub token (root only)
                  shacklog-ctl         status / update / restart / logs / backup / uninstall
```

---

## 1. Remove the earlier install (if you tried one)

On the Proxmox host, in your clone of the repository:

```bash
cd ~/shacklog && git pull
bash deploy/uninstall.sh
```

It shows what it found (service, `/opt/shacklog`, data, Docker container, ShackLog
containers) and asks:

1. **Keep the data** — archives it to `/root/shacklog-data-…tar.gz` and leaves it in place
   (recommended; the new container can restore it), or
2. **Remove it** — asks you to type `DELETE`.

For ShackLog containers it finds, it asks one by one whether to destroy them (optionally
after a vzdump backup). Nothing is removed without a question.

## 2. Create the dedicated container

Still on the host, in the clone:

```bash
bash deploy/proxmox/shacklog-lxc.sh
```

In the style of the Proxmox VE Helper Scripts it offers:

| Choice | Default | Why |
|---|---|---|
| Container ID | next free | |
| Hostname | `shacklog` | |
| CPU cores | **1** | The hub is idle nearly always. 2 only if you index many large PDFs. |
| RAM | **512 MB** | ShackLog uses ~120 MB; headroom for PDF text extraction. |
| Swap | 512 MB | |
| Disk | **8 GB** | System ~1.5 GB, program ~0.3 GB, the rest for manuals, photos and local backups. Grow later without downtime: `pct resize <ID> rootfs +8G`. |
| Storage | your container storage | e.g. `local-lvm` |
| Network | `vmbr0`, DHCP | **Advanced** lets you set a static address — better for a server, since the PCs sync to it by address. VLAN tag optional. |
| Unprivileged + nesting | yes | Nesting lets systemd work properly inside the container. |
| Start at boot | yes | |

Then it asks:

- **GitHub token** (the repository is private): github.com › Settings › Developer settings ›
  *Fine-grained tokens* › only the `shacklog` repository › *Contents: Read-only*. It is
  stored only inside the container, root-only, and used for install and updates.
- **Restore data**: picks up the archive the uninstaller made in `/root`.
- **Nightly backup**: creates a vzdump job to your NAS storage (keeps 7 daily, 4 weekly,
  6 monthly). Your NAS must be added first in *Datacenter › Storage* with content
  *VZDump backup file*.

At the end it prints the address and the one-time **setup code**.

## 3. First visit

Open `http://<container-ip>:8573`, enter the setup code, choose a password (8+
characters). If you restored data, your stations and logs are already there; if not, run
the setup (name the instance "Proxmox"). Then *Settings › Sync* › **Accept sync from
other instances** and generate the token for your PCs.

## 4. Day to day — `shacklog-ctl`

From the host: `pct enter <ID>`, then:

| Command | What it does |
|---|---|
| `shacklog-ctl status` | Service state, version and commit, address, data size |
| `shacklog-ctl update` | `git pull`, reinstall packages, restart. Backs up the database first and **rolls back by itself** if the new version doesn't start. |
| `shacklog-ctl restart` / `stop` / `start` | |
| `shacklog-ctl logs` (`-f` to follow) | |
| `shacklog-ctl backup` | Database backup + archive of the whole data folder in `/var/lib/shacklog/exports` |
| `shacklog-ctl setup-code` | Shows the first-password code again |
| `shacklog-ctl password` | Sets a new web password |
| `shacklog-ctl cli info` | Any ShackLog command, run as the service user |
| `shacklog-ctl uninstall` | Removes ShackLog from the container (asks about the data) |

The same **Update and restart**, **Restart** and **Stop server** buttons are in the web
interface: *Settings › Program & updates*. They hand the request to `shacklog-ctl`
through a systemd path unit, because the web service itself runs unprivileged.

## Backups

Three layers, all automatic once set up:

1. ShackLog backs up its database daily (also while it keeps running) and before every
   import or merge (`backups/` in the data
   folder, 14 dailies kept).
2. *Settings › Program › Second copy of backups* can copy each backup to another folder
   (for the hub: a NAS share mounted in the container; on a PC: a OneDrive folder).
3. The vzdump job saves the whole container to the NAS every night.

## From outside home

Use your own VPN (Tailscale or WireGuard on the host) and open the same address. Do not
forward port 8573 on the router. For a public HTTPS address, put a reverse proxy in front
and set `SHACKLOG_ARGS=--behind-proxy` in `/etc/shacklog/shacklog.env`, then
`shacklog-ctl restart`. Only a proxy running in the container itself (127.0.0.1) is
trusted by default; for a proxy on another machine also add
`SHACKLOG_TRUSTED_PROXY=<its address>` to the same file.

## Why the first attempt misbehaved

The earlier systemd unit used sandboxing options (`ProtectSystem`, `PrivateTmp`,
`ProtectHome`) that need mount namespaces. Unprivileged LXC containers often refuse them,
and the service then fails with `status=226/NAMESPACE`. Run on the host, it also mixed
ShackLog into the hypervisor. The new unit drops those options (the container is the
isolation), the installer refuses to run on the host, and containers get `nesting=1`.

## Docker (alternative)

`deploy/Dockerfile` and `deploy/docker-compose.yml` still work for a Docker host. The
LXC route above is the recommended one on Proxmox.
