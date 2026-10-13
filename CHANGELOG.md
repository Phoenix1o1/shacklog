# Changelog

Versions follow `major.minor.patch`. The minor number goes up when the database schema
changes; ShackLog upgrades the database by itself at the next start, after a backup.

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

**Fixed**
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
