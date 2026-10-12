# ShackLog — Mandates & Directives

This is a **living file**. Every change to the project must respect it.
When Andrea gives a new directive, add it to §1 or §2 and log it in §4 with the date.
If a directive and the code disagree, the directive wins and the code gets fixed.

---

## 1. Andrea's directives (original request, 12 Oct 2026)

1. A **logbook database in Python**, compilable to an **executable** (.exe on Windows).
2. The database is **created at launch** if it doesn't exist.
3. Runs on **any of my computers** — multi-OS (Windows, Linux, macOS).
4. Can **serve a website** when run on my **Proxmox** server, **navigable from mobile**.
5. The **main instance collects the databases of all other instances**, for safekeeping.
6. Planned for **long-term storage**: **navigability is key**. Retrieve contacts; search by
   callsign/name, **repeater ID**, **antenna** and **radio**; **filter by coordinates**
   relative to the **home station** and **fixed locations**; anything else useful.
7. If similar programs exist, **identify which of their features to implement**.
8. Store **CHIRP settings** and **repeater lists**.
9. A section for **manuals and file storage**.
10. **Readable, well-commented code**; **allow future expansion**.
11. **Ask** when in doubt. **Think before acting, plan before executing.** Keep this file.

## 2. Decisions taken with Andrea (12 Oct 2026)

| Topic | Decision |
|---|---|
| UI | **Browser UI everywhere.** The executable starts a local web server and opens the browser. The same code runs as a server on Proxmox and serves the phone. One UI to maintain. |
| Merging instances | **Network sync from day one** (instances sync with the Proxmox hub). Bundle/database **file import** also exists for machines that never reach the hub. |
| What gets logged | Two-way **QSOs**, **listening log** (SWL/SDR), **antenna & test journal** (NanoVNA sweeps, SWR readings, photos), **station diary**. |
| Web access | **Single user**, home **LAN or own VPN** (Tailscale/WireGuard). One password. |
| Name | **ShackLog** (one constant, `APP_NAME` in `shacklog/__init__.py`). |
| Language | English UI. Strings live in templates so a translation can be added later. |
| Hub deployment | Its **own unprivileged LXC** on Proxmox, created by `deploy/proxmox/shacklog-lxc.sh` (helper-script style: cores, RAM, disk, storage, network, NAS backup job). Never installed on the Proxmox host itself. |
| Server controls | `shacklog-ctl` (status, start, stop, restart, update = git pull + reinstall + restart with rollback, backup, uninstall) and the same buttons in *Settings › Program*. |
| Uninstall | `deploy/uninstall.sh` asks **keep data** (archive + leave in place) or **remove** (type DELETE). |
| PC updates | *Settings › Program › Update now* = git pull + rebuild + restart (`packaging/update_windows.bat`). The PC copy is a git working copy kept in sync with GitHub. |
| Data location (PC) | `%LOCALAPPDATA%\ShackLog` (outside OneDrive: a syncing cloud folder can corrupt a live SQLite file). Shown at startup and in *Settings › Program*; backups can be mirrored to a OneDrive folder. |
| OFFgrid-SDR | Not bundled (someone else's project). *SDR monitor* serves the user's own copy on localhost (WebUSB) beside a quick listening log, and exports repeaters/channels as OFFgrid-SDR memories for Scan. |
| Knowledge base | A view of the library: notes (Markdown), quick references, manuals, band plans; pinned items on the dashboard. Existing docs, manuals and printables are imported into it. |
| Version control | Private GitHub repo **Phoenix1o1/shacklog**. Commit and push **automatically at every milestone**, without being asked. |

## 3. Engineering rules (non-negotiable)

### Data safety
- **Never silently lose data.**
  - Deletes are **soft** (`deleted_at` tombstone). The Trash page can restore.
  - A sync or import conflict keeps the **losing version** in `sync_conflicts`, restorable.
  - Imports never overwrite newer data.
  - Automatic **backup** before every import and once a day at startup.
- All timestamps are stored in **UTC** (naive `datetime` meaning UTC).
- Every synced record has a **UUID primary key**. References between records use UUIDs,
  never local integer IDs, so records keep their links when they move between instances.
- **Local-only data** (settings, password hash, sync tokens, peers, conflicts) lives in
  local tables that **never sync**.
- **Unknown data is preserved**: unmapped ADIF fields go to `extra_adif`, unmapped CHIRP
  columns are kept in `raw`.
- Stored files are **content-addressed** (SHA-256) and never modified in place.

### Code
- Python ≥ 3.11. **Stdlib first.** Every extra dependency is justified in `PLAN.md` §8 and
  must be PyInstaller-friendly (pure Python wherever possible).
- Every module starts with a **docstring** saying what it does and how it fits.
  Comments explain **why**, not what. Type hints on public functions.
- Pure logic (geo, bands, ADIF, CHIRP, Touchstone, sync merge) **has no Flask imports**
  and has **unit tests**.
- **Expandability by convention**: a new record type is a new model with `SyncMixin`,
  registered in `SYNC_ORDER`. Sync, bundles, backups, trash and search pick it up.
- Schema changes **only** through numbered migrations in `migrations.py`. Never edit an
  old migration.
- Server-rendered HTML; core actions work **without JavaScript**. htmx and Leaflet only
  for enhancements. **No CDN at runtime** (assets are vendored) so the exe works offline.
  Map tiles are the only online dependency.
- **Mobile-first**: every page is usable at 360 px width.
- **Security**: login required whenever the server listens beyond localhost; CSRF token on
  every POST form; sync API uses a bearer token; secrets are never logged or synced.

## 4. Directive log

- **2026-10-12** — File created from the original request and the four answers in §2.
- **2026-10-12** — Andrea: create a GitHub repo, commit, and keep tracking automatically from now on.
- **2026-10-12** — Andrea: keep the local copy synced with git; uninstall script with keep/remove data;
  dedicated Proxmox LXC in helper-script style (cores, RAM, storage, NAS backups); server commands to
  stop/restart/update (git pull + rebuild); update-from-git on the PC too; integrate OFFgrid-SDR for
  monitoring during a session (local only); integrate existing documents, quick references and the
  knowledge base; preserve current data; explain where the database is.
