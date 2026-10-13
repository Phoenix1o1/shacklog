# ShackLog user guide

ShackLog is a personal amateur-radio logbook, station archive and document library. It
runs on your PC as a small local web server, so everything opens in the browser. The same
program runs as a **hub** on a home server (Proxmox) that your other computers sync with and
your phone can browse.

## Getting around

- **The side menu** (left) has every section. On a phone or a narrow window it folds away:
  tap the green **☰ Menu** button at the top left.
- **Search everything** in the top bar looks through all logs, repeaters, equipment,
  contacts, notes and the text inside PDF manuals. Type part of a word: `ir2u` finds
  `IR2UBX`.
- **Log QSO** (top bar) is always one click away.
- **☾ / ☀** (top right) switches between the dark theme (default, easy on the eyes at
  night) and the light "paper ledger" theme. The choice is remembered per browser.
- The footer shows the version, the sync state and the **UTC clock**.

## Logs

| Section | What goes there |
|---|---|
| **QSOs** | Contacts you made. Import/export ADIF and CSV. Callsign pages collect every QSO, listening entry and note about a call. |
| **Listening log** | What you heard (SWL): repeaters, airband, broadcast, utility, beacons. The SDR monitor logs here with one click. |
| **Journal** | Antenna and test journal, station diary. Attach NanoVNA sweeps (Touchstone `.s1p`): SWR/return-loss plots appear on the entry. |

Dates and times are **UTC**. Fields marked with *Now (UTC)* fill in the current time.

### Finding things

Every list has a filter bar:

- **Callsign** accepts wildcards: `IK2*`, `I?2ABC`. Without wildcards it means *contains*.
- **Near** + **radius** keeps only what lies within *N* km of a locator (`JN45`), a
  position (`45.8,8.8`) or one of your stations.
- Band, mode, radio, antenna, station and date range narrow it further.
- **Save filter** keeps the current filter under a name; the **Saved** button of the same
  list brings it back.
- Click a column heading to sort.

## Station

- **Repeaters**: frequencies, shift, tone, position, notes. Import a CHIRP CSV, and
  **export the repeaters near a place** as a CHIRP channel list ready to load into a radio.
- **Radio configs**: keep every CHIRP image (`.img`) and CSV you load into a radio. The
  channels are read and matched to your repeaters; compare two configurations with
  **Diff**; export a channel list back to CHIRP.
- **Stations**: places you operate from (home, portable spots) with locator and
  coordinates. One is the *default* (new QSOs use it), one the *reference* for distances.
- **Equipment**: radios, antennas, SDRs, cables, with serials and linked manuals.
- **Contacts**: people and their callsigns.

Anything can be **linked** to anything else (a manual to a radio, a sweep to an antenna,
a QSO to a repeater) from the *Links* box on each page.

## Browse

- **Knowledge base**: notes written in the browser (Markdown), quick references,
  manuals, band plans and datasheets. Give an entry the tag `pinned` (or use **Pin to top**) to
  show it on the dashboard. Search reads inside PDFs and notes.
- **Library**: every stored file (manuals, images, recordings, CHIRP images). Files are
  stored once even if uploaded twice.
- **SDR monitor** (desktop only): see below.
- **Map**: QSOs, listening entries, repeaters and stations with a position, as layers
  you switch on and off.
- **Statistics**: QSOs by band, mode, month and year, longest distance per band,
  most worked callsigns and most used repeaters, per
  year and per station.

## SDR monitor (RTL-SDR)

The SDR monitor puts **OFFgrid-SDR**, a browser-based receiver for RTL-SDR sticks, next to
a quick listening log. It runs on the PC the stick is plugged into (WebUSB works only on
`localhost`), not on the hub.

1. Download OFFgrid-SDR from its GitHub page (see *Credits*) and unzip it next to the
   ShackLog folder, or set its location in *Settings › Program & updates*.
2. Open **SDR monitor**. Plug in the stick, press *Connect* in OFFgrid-SDR, allow the USB
   device in the browser. On Windows the stick needs the WinUSB driver (Zadig).
3. **Memories for OFFgrid-SDR** downloads your repeaters (or a radio's channels) as an
   OFFgrid-SDR memory file: import it there and use *Scan* to see which channels are
   active.
4. Heard something? Type the frequency (and callsign, if any) in **Quick log**: it goes
   into the listening log and is matched to a repeater automatically.

## Your data

Everything lives in one folder:

| System | Folder |
|---|---|
| Windows | `%LOCALAPPDATA%\ShackLog` (`C:\Users\<you>\AppData\Local\ShackLog`) |
| Linux | `~/.local/share/ShackLog` |
| macOS | `~/Library/Application Support/ShackLog` |
| Proxmox hub | `/var/lib/shacklog` inside the container |

`shacklog.sqlite3` is the database, `files/` the library, `backups/` the automatic
backups. *Settings › Program & updates* shows the folder and opens it. To keep the data
beside the program (USB stick), create a folder named `ShackLogData` next to the
executable.

**Backups**: one a day, plus one before every import. *Settings › Program & updates ›
Second copy of backups* copies each finished backup to another folder (OneDrive, a NAS
share). Never put the live database itself in a cloud-synced folder.

**Nothing is ever lost quietly**: deleting moves a record to the **Trash** (*Settings ›
Trash*), and when sync finds two different edits of the same record, the losing version
is kept under **Conflicts**.

## Sync: PC, laptop and the hub

Each computer keeps its own complete database; the hub collects everything and hands it
to the others.

1. On the hub: *Settings › Sync › This computer is the hub*: tick *Accept sync*, then
   **Generate token** and copy it.
2. On each PC: *Settings › Sync › This computer syncs with a hub*: the hub address (for
   example `http://192.168.1.50:8573/`) and the token. **Test connection**, then **Sync
   now**. Choose how often it syncs automatically.
3. No network between two machines? *Settings › Import, export & backups* makes a
   **bundle** (one zip with the database and all files) that the other machine imports.

### Duplicates after the first sync

If two computers each created "the same" thing before they first synced (typically a
*Home* station), you'll see it twice. *Settings › Duplicates* lists look-alike stations,
equipment, repeaters, contacts and files. Pick the one to keep and press **Merge**: every
QSO, link and setting that pointed at the other copy now points at the one you kept, its
empty fields are filled in, and the extra copy goes to the Trash. The merge syncs like any
other edit, so do it once, on any computer.

## Running in the background (Windows)

ShackLog has no window: double-click **ShackLog.exe** and it starts in the notification
area next to the clock (click the **^** arrow if the icon is hidden) and opens the browser.

- Right-click the icon: **Open ShackLog**, **Open data folder**, **Start when I sign in**,
  **Quit ShackLog**.
- Closing the browser tab doesn't stop it. Double-clicking the exe again just reopens the
  browser.
- *Start when I sign in* (also in *Settings › Program & updates*) starts it quietly at
  login, without opening the browser.
- Commands still work from a terminal: `ShackLog.exe where`, `ShackLog.exe info`, …

On Linux and macOS ShackLog runs in the terminal you started it from.

## Updating

- **PC with a git copy**: *Settings › Program & updates › Update now*. ShackLog closes,
  pulls the latest version from GitHub, runs the tests, rebuilds `ShackLog.exe` and starts
  again. If anything fails the previous version starts instead. Your data isn't touched.
- **Proxmox hub**: the same page (*Update and restart*), or `shacklog-ctl update` in the
  container. A failed update rolls back.

## Commands

```
ShackLog                      start on this computer (desktop mode)
ShackLog serve                server mode (the hub)
ShackLog sync                 sync once with the configured hub
ShackLog import FILE          ADIF, bundle (.zip) or database file
ShackLog export-bundle OUT    database + all library files in one zip
ShackLog backup               back up the database now
ShackLog info                 paths, identity, record counts
ShackLog where [--open]       print (or open) the data folder
ShackLog set-password         web login password
```

On the hub, `shacklog-ctl` adds `status`, `start`, `stop`, `restart`, `logs`, `update`,
`backup`, `setup-code`, `password` and `uninstall`. See `deploy/PROXMOX.md`.

## Troubleshooting

| Problem | What to do |
|---|---|
| The browser says it can't connect | ShackLog isn't running: start it again. On Windows, check the tray. |
| "Port in use" / it opened on another port | Another program uses 8573: ShackLog picks the next free port; bookmark the address it opens. |
| Sync: *unauthorised* | The token on the PC doesn't match the hub's. Generate a new one on the hub and paste it. |
| Sync: *can't reach the hub* | Open the hub address in the browser of that PC. Check the VPN/LAN. |
| Two copies of the same station | *Settings › Duplicates*. |
| Something deleted by mistake | *Settings › Trash › Restore*. |
| The SDR monitor says OFFgrid-SDR isn't found | Set the location of its `index.html` in *Settings › Program & updates*. |
| Logs for a bug report | `shacklog.log` in the data folder. |
