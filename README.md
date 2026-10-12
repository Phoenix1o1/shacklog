# ShackLog

A personal amateur radio logbook, station archive and document library.
One executable for Windows, Linux and macOS with a browser interface; the same code runs
as a hub on Proxmox that every other instance syncs with, and that your phone can browse.

- **Logs**: QSOs (ADIF import/export), listening log, antenna & test journal with NanoVNA
  sweep plots, station diary.
- **Station**: repeaters (CHIRP CSV import, *export the repeaters near a place as a CHIRP
  channel list*), radio configurations (CHIRP images + parsed channels, diff), stations,
  equipment, contacts.
- **Find anything**: full-text search (including inside PDF manuals), filters by callsign,
  repeater, radio, antenna, band, mode, dates, distance from any station, map, statistics,
  saved filters, callsign pages.
- **Knowledge base**: notes (Markdown, editable in the browser), quick references, manuals,
  band plans — searchable inside, pinned items on the dashboard.
- **SDR monitor**: OFFgrid-SDR (WebUSB RTL-SDR receiver) beside a one-click listening log;
  exports your repeaters or a radio's channels as OFFgrid-SDR memories to Scan for activity.
- **Keep it forever**: soft deletes with a trash, sync that never drops a losing version,
  daily backups (optionally mirrored to OneDrive/NAS), bundles for offline machines.

Docs: `PLAN.md` (architecture, roadmap) · `MANDATES.md` (directives) ·
`CONVENTIONS.md` (how the code is written) · `deploy/PROXMOX.md` (hub: dedicated LXC,
`shacklog-ctl`, uninstall, backups).

## Get the executable

Every push to `main` builds Windows, Linux and macOS executables in GitHub Actions
(*Actions › build › Artifacts*). Tagging `v1.0.0` attaches them to a release.
Or build locally: `packaging\build_windows.bat` (Windows) / `packaging/build_unix.sh`.

Double-click it: a console window shows the address **and the data folder**, and the
browser opens. Close the console (or *Settings › Quit*) to stop.

**Where is my data?** `%LOCALAPPDATA%\ShackLog` on Windows
(`C:\Users\<you>\AppData\Local\ShackLog`), `~/.local/share/ShackLog` on Linux,
`~/Library/Application Support/ShackLog` on macOS. *Settings › Program & updates* shows it
and opens it; `ShackLog where --open` does the same from a terminal. The database is
`shacklog.sqlite3`; while ShackLog runs, recent changes may sit in `shacklog.sqlite3-wal`
beside it (folded in every few minutes and on exit). To keep the data next to the program
instead (USB stick), create a `ShackLogData` folder beside the executable.

## Updating

- **PC (git working copy)**: *Settings › Program & updates › Update now* — pulls from
  GitHub, runs the tests, rebuilds `dist\ShackLog.exe`, restarts. Needs Git for Windows
  and Python. Or double-click `packaging\update_windows.bat`.
- **Proxmox hub**: `shacklog-ctl update` in the container, or the button in the same page.

## Commands

```
ShackLog                      desktop mode (default)
ShackLog serve                server mode (hub / Proxmox)
ShackLog sync                 sync once with the configured hub
ShackLog import FILE          ADIF, bundle (.zip) or database file
ShackLog export-bundle OUT    database + all library files in one zip
ShackLog backup | info | set-password
ShackLog where [--open]        print (or open) the data folder
```

## From source

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m shacklog            # desktop mode
python -m pytest -q           # tests
```
