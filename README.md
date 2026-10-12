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
- **Keep it forever**: soft deletes with a trash, sync that never drops a losing version,
  daily backups, bundles for offline machines.

Docs: `PLAN.md` (architecture, roadmap) · `MANDATES.md` (directives) ·
`CONVENTIONS.md` (how the code is written) · `deploy/PROXMOX.md` (hub setup).

## Get the executable

Every push to `main` builds Windows, Linux and macOS executables in GitHub Actions
(*Actions › build › Artifacts*). Tagging `v1.0.0` attaches them to a release.
Or build locally: `packaging\build_windows.bat` (Windows) / `packaging/build_unix.sh`.

Double-click it: a console window shows the address and the browser opens. Close the
console (or *Settings › Quit*) to stop. Data goes to `%LOCALAPPDATA%\ShackLog` — or create
a `ShackLogData` folder next to the executable for **portable mode** (USB stick).

## Commands

```
ShackLog                      desktop mode (default)
ShackLog serve                server mode (hub / Proxmox)
ShackLog sync                 sync once with the configured hub
ShackLog import FILE          ADIF, bundle (.zip) or database file
ShackLog export-bundle OUT    database + all library files in one zip
ShackLog backup | info | set-password
```

## From source

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m shacklog            # desktop mode
python -m pytest -q           # tests
```
