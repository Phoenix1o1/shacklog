# Changelog

Versions follow `major.minor.patch`. The minor number goes up when the database schema
changes; ShackLog upgrades the database by itself at the next start, after a backup.

## Unreleased

**New**
- **Countries.** The callsign box shows the country, continent, CQ/ITU zones, DXCC
  number and beam heading from your station, and says *New country*. New QSOs get their
  country filled in, *Settings › Import, export & backups › Countries* fills in older
  ones, and *Statistics* counts countries worked. The list is AD1C's `cty.csv` from
  country-files.com, downloaded when missing or a month old (or uploaded by hand). No
  database change: the hub and the PCs keep syncing whichever version they run.
- **Import a repeater list** (*Repeaters* and *Settings › Import, export & backups*):
  RepeaterBook JSON/CSV, national and club lists, any CSV with a header line. A preview
  shows the column guessed for each field and lets you change it; repeaters already
  here are skipped; a backup is made first.
- **QSL cards** (side menu): QSOs to send, printable address labels (A4, 3 × 7),
  mark sent with date and route, cards waiting, and "a card arrived" with scans of the
  card stored in the library and linked to the QSOs. Dates and routes are the ADIF
  fields `QSLSDATE`/`QSLRDATE`/`QSL_SENT_VIA`/`QSL_RCVD_VIA`, so exports carry them.
- **Callbook** (HamQTH or QRZ.com, *Settings › General*): cached details under the
  callsign while logging, **Look up** on demand, **Use these details** fills the empty
  boxes; full entry with postal address on the callsign page; direct-QSL labels carry
  the address. Answers cached a month in `<data>/reference/callbook.sqlite3`.
- **Offline map.** ShackLog serves the map tiles itself: from offline map files
  (`.mbtiles`, *Settings › Import, export & backups › Map*), from a cache of every tile
  already seen (500 MB by default, oldest dropped first), then from OpenStreetMap. With
  no internet, cached and offline areas still show. Tile server can be changed.

- **Home Assistant** (*Settings › General*): a read-only token and `/api/ha/state` with
  QSOs today/total, last QSO, last heard, countries, last backup, each PC's last sync,
  *on the air* and *update available*; a ready-to-paste `configuration.yaml` block.
  Phase 1 of `NEXT_FEATURE.md` (REST); MQTT is left for when live sources (CAT,
  WSJT-X) exist.

**Changed**
- *Proxmox container creator*: every answer is checked as you type it and asked again
  with the reason when it's wrong: IPv4 address and prefix (not the network or broadcast
  address), gateway in the same network, address already used by another guest or
  answering on the network, ID, hostname, cores, memory, disk, VLAN, port, backup time,
  restore archive. The static address is suggested from the bridge's network.
- The same features on every kind of instance: the hub (and Docker) can import a big
  bundle by path too, from the data folder's new `incoming/` folder; *Program & updates*
  names the right update helper on Linux/macOS and explains Docker updates; a table
  *What works where* in the user guide.
- Folders tidied: the data folder's layout is defined in one place, leftovers of
  interrupted writes are removed at start, the Docker files are in `deploy/docker/`
  (an existing Docker install keeps its data folder).

- `shacklog-ctl` works on the Proxmox host too: the container creator installs a host
  helper that runs it in the ShackLog container (`--ct <ID>` to choose one).

**Fixed**
- Hub, *Program & updates*: the commit and branch showed git's "detected dubious
  ownership" error (the code belongs to root, the web service runs as `shacklog`). Git
  now trusts the program's own folder for these read-only checks, and a git problem is
  shown once as a warning, never in place of a value. A hub that has never checked
  says to press *Check now*.
- Docker: the first start failed when Docker created the data folder itself (owned by
  root, while ShackLog runs as its own user). The container now hands the folder over
  before starting. CI builds and starts the image.
- Bundles no longer contain this instance's secrets (web password hash, sync tokens,
  callbook password): they are removed from the copy of the database that goes in the
  zip. Local backups still keep them.

**Fixed** (review of the Windows background mode and automatic updates)
- An update that changed the update script itself could derail it: Windows (and bash)
  read a script from disk while running it. The PC now runs a copy of the script from
  `dist\`, and the hub's `shacklog-ctl` stops reading at its last line.
- The update window no longer stops at "ShackLog.exe is still running, press a key": it
  waits for the second process of the one-file executable too, and retries replacing
  the exe for a few seconds.
- If an update pulled the code but the tests or the build failed, the old program kept
  running and every later check said "up to date". The executable now knows the commit
  it was built from, and the next start (or *Update now*) rebuilds it.
- After an update, ShackLog restarts on the same data folder when it was started with
  `--data-dir`.
- Starting `ShackLog.exe` from a command prompt no longer ties the background program
  to that window (Ctrl+C there, or closing it, used to stop it).
- *Start when I sign in* writes a properly quoted command, and leaves out the data folder
  when it's the usual one.
- Linux/macOS updater: a failed build starts the previous version again instead of
  leaving ShackLog stopped.
- CI runs the tests on Windows too, since the PC updater runs them there before every
  rebuild.

## 1.1.0 — 13 October 2026 · ready for service

**New**
- **Settings › Duplicates**: finds stations, equipment, repeaters, contacts and files
  that look like the same thing (e.g. two *Home* stations after the first sync) and
  merges them. QSOs, listening entries, configs, links and settings pointing at the extra
  copy move to the one you keep; the extra copy goes to the Trash. The merge syncs.
- **Windows background mode**: the executable has no window. It runs in the notification
  area with a menu (Open, Open data folder, Start when I sign in, Quit). Double-clicking it
  again just reopens the browser.
- **Start when I sign in** for Windows, Linux and macOS (also in *Settings › Program &
  updates*).
- **Automatic updates.** PC: at every start, if online, ShackLog checks GitHub and
  updates itself before opening (tests + rebuild; a failed update isn't retried for 12 h);
  while running it checks every 6 h and shows *Update available*. Hub: a systemd timer
  runs `shacklog-ctl check` after boot and every 6 h; updates are shown in the web page and
  installed automatically (with rollback) if you turn that on. Both under *Settings ›
  Program & updates*.
- **Instance name and IP address** at the top of the side menu and in the footer, and a
  *This instance* panel (addresses, port, id, the address to use from a phone).
- **Help** in the app: user guide, credits and licences (including OFFgrid-SDR).
- New app icon (tray, executable, browser tab).

**Changed**
- Dark "night" theme by default, with a calmer slate palette and a ☀/☾ switch for the
  light ledger theme (remembered per browser).
- Navigation: a permanent side menu on computers; on phones a bright **Menu** button
  opens it as a drawer. (The old menu button was invisible in dark mode.)
- Importing a newer bundle from the same source updates the records without filling
  *Conflicts* with one entry per record.
- The red margin rule of the log tables is continuous on every row.

**Fixed** (including a pre-release review)
- Merges follow late references: a merge is recorded and travels with sync, so a QSO
  logged on another instance against the merged copy before it synced is moved to the
  kept copy wherever it shows up. A backup is taken before every merge.
- The daily backup also happens while ShackLog keeps running for days (hub, tray), not
  only at start.
- File records from sync or imports can only name files inside the library (a crafted
  hash could point at any file on the server).
- Web addresses saved in records must be http(s); Markdown images too.
- The login page no longer redirects to another site via `//host` links.
- `--behind-proxy` trusts only a proxy on the same machine (`SHACKLOG_TRUSTED_PROXY` for
  another one).
- *Start when I sign in* starts on the same data folder, and works from a source checkout.
- Quitting from the web page also removes the tray icon.
- An empty "Map area" label above the filters of QSOs, listening log and repeaters.

## 1.0.x — 12–13 October 2026

- Proxmox: dedicated LXC creator in the style of the Proxmox VE Helper Scripts,
  `shacklog-ctl` (update with rollback, restart, stop, backup, uninstall), keep/remove-data
  uninstaller, server controls from the web page. Fixed container start failures (Debian
  release per PVE version; template matched to the host CPU architecture).
- PCs: *Update now* (git pull, tests, rebuild, restart), import a bundle from a path,
  WAL folded into the database every few minutes and on exit, backup mirror folder.
- Knowledge base (Markdown notes, references, manuals, band plans, pins) and SDR monitor
  (OFFgrid-SDR with a quick listening log and memory export).
- Conflicts: dismiss all from one source.

## 1.0.0 — 12 October 2026

First version. Logs: QSOs, listening, journal, diary. Station: repeaters, radio
configurations (CHIRP), stations, equipment, contacts. Library, links, search, filters,
saved filters, map, statistics, ADIF/CHIRP/CSV import and export, hub-and-spoke sync,
bundles, backups, conflicts, trash, desktop and server modes, executables, Docker and LXC
deployment.
