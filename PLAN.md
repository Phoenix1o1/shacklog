# ShackLog — Plan & Architecture

Companion to `MANDATES.md`. This file says **how** ShackLog is built and **what comes next**.
`shacklog/models.py` is the source of truth for field names; this file summarises them.

---

## 1. What ShackLog is

A personal, long-term amateur radio record keeper:

- **Logs**: QSOs, listening log (SWL/SDR), antenna & test journal, station diary.
- **Reference data**: repeaters, station locations, equipment, CHIRP radio configurations.
- **Library**: manuals, datasheets, photos, QSL images, NanoVNA sweeps, firmware notes.
- **Navigation**: global full-text search, per-section filters (callsign, repeater, radio,
  antenna, band, mode, dates, distance/bearing from any station, bounding box, locator
  prefix), callsign pages, map, statistics, saved filters.
- **Instances**: every computer runs its own copy with its own database. Instances sync
  with a **hub** (the Proxmox server) over HTTP; offline machines exchange **bundles**.

## 2. Run modes

One codebase, one executable, different command-line modes:

| Command | What it does |
|---|---|
| `shacklog` (double-click) | **Desktop mode.** Listens on `127.0.0.1:8573`, opens the browser. No login on localhost by default. If an instance is already running, it just opens the browser. |
| `shacklog serve --host 0.0.0.0` | **Server mode** (Proxmox). No browser; login always required. |
| `shacklog sync` | Run one sync with the configured hub (for cron/timers). |
| `shacklog import FILE` | Import an ADIF, CHIRP CSV, database or bundle file. |
| `shacklog export-bundle OUT.zip` | Database + all stored files in one zip. |
| `shacklog backup` / `set-password` / `info` / `where` | Maintenance; `where --open` shows the data folder. |

**Data directory** (database, stored files, backups, secret key), first match wins:
1. `--data-dir PATH` or the `SHACKLOG_DATA` environment variable;
2. a folder `ShackLogData` next to the executable (**portable mode**, e.g. on a USB stick);
3. the OS user-data folder (`%LOCALAPPDATA%\ShackLog`, `~/.local/share/ShackLog`,
   `~/Library/Application Support/ShackLog`).

The database and folders are created on first launch.

## 3. Existing software surveyed (Oct 2026)

| Program | Type | Strengths worth copying |
|---|---|---|
| **Wavelog** (fork of Cloudlog) | Self-hosted web (PHP/MariaDB) | Station locations, maps, statistics, awards, LoTW/eQSL/QRZ/ClubLog, contest logger, CAT bridge (WLGate), DX cluster, translations, API |
| **QLog** | Qt desktop (SQLite) | Station location profiles, custom QSO filters, online/offline map, callbook lookups (QRZ/HamQTH), rig/rotator control (Hamlib), DX cluster, statistics, basic awards |
| **HAMRS** | Portable desktop/mobile | Activity templates (POTA/SOTA/Field Day), fully offline, sticky fields, fast entry, map with band filter, ADIF export |

**Adopted now**: station location profiles · ADIF import/export (lossless) · custom and
saved filters · map · statistics · worked-before indicator · quick mobile entry with sticky
fields · activity references (SOTA/POTA/WWFF) · satellite fields · QSL status fields.

**Later (roadmap §9)**: callbook lookup (QRZ/HamQTH) · DXCC/country from `cty.dat` ·
awards · LoTW/eQSL/ClubLog/QRZ upload · CAT/WSJT-X UDP · DX cluster · contest mode/Cabrillo.

**ShackLog-specific** (none of the above does these): repeater database linked to QSOs ·
CHIRP configuration archive with channel search, diff, and *generate a CHIRP CSV from the
repeaters near a station* · equipment inventory with linked manuals · antenna journal with
NanoVNA sweep plots · listening log · document library with full-text search inside PDFs ·
multi-instance sync that never discards a losing version.

## 4. Architecture

```
 Browser (PC / phone)
        │ HTTP (HTML forms, htmx enhancements, Leaflet map)
 ┌──────▼──────────────────────────────────────────────────────────┐
 │ shacklog.web   Flask blueprints + Jinja templates + forms/filters │
 │ shacklog.api   /api/sync/* (bearer token)                          │
 ├──────────────────────────────────────────────────────────────────┤
 │ shacklog.services   adif · chirp · touchstone · geo · bands       │
 │                     filestore · search · backup · bundle · sync   │
 ├──────────────────────────────────────────────────────────────────┤
 │ shacklog.models (SQLAlchemy ORM) · shacklog.db · migrations       │
 ├──────────────────────────────────────────────────────────────────┤
 │ SQLite (WAL) shacklog.sqlite3  +  files/ab/abcdef… (SHA-256 blobs) │
 └──────────────────────────────────────────────────────────────────┘
 Served by waitress (pure-Python WSGI server) in every mode.
```

## 5. Data model

Every **synced** table uses `SyncMixin`:

| Column | Meaning |
|---|---|
| `id` | UUID string, primary key, generated where the record is created |
| `created_at`, `updated_at` | UTC. `updated_at` is the version stamp used by sync |
| `deleted_at` | Soft delete (tombstone); synced like any change |
| `origin_instance` | Instance that created the record |
| `modified_by_instance` | Instance that made the last change (sync tie-breaker) |
| `change_seq` | **Local only.** Monotonic per-instance change counter |
| `received_from` | **Local only.** Peer this version came from (prevents echo) |

Synced tables (in `SYNC_ORDER`, parents first):

| Table | Purpose · key fields |
|---|---|
| `stations` | Station locations: name, kind (home/fixed/portable/mobile), callsign, operator, lat/lon, locator, altitude, default radio/antenna/power |
| `equipment` | Radios, antennas, SDRs, test gear, coax…: kind, maker, model, serial, firmware, status, freq range, connector, specs |
| `repeaters` | name, callsign, ident (R1, RU5…), kind (FM/DMR/C4FM/D-STAR/M17/EchoLink…), output/input MHz, CTCSS up/down, DCS, colour code, talkgroups, EchoLink node, network, position, status, source, last verified |
| `contacts` | Personal notes about a callsign: name, QTH, locator, notes, tags |
| `qsos` | ADIF-shaped QSO: time on/off, call, name, QTH, locator, lat/lon, freq, band, mode, submode, RST, power, **station / radio / antenna / repeater** links, my call/locator/position **snapshot**, prop mode, satellite, distance & bearing, QSL/LoTW/eQSL status, SOTA/POTA/WWFF refs, comment, notes, tags, `extra_adif` (JSON of unmapped ADIF fields) |
| `swl_entries` | Listening log: time, freq, band, mode, category (amateur, repeater, airband, marine, satellite, broadcast, utility, beacon, PMR…), heard ident, signal, receiver, antenna, station, repeater, position, distance, notes |
| `journal_entries` | kind `test` or `diary`; time, title, body (Markdown-ish text), station, equipment under test, tags |
| `measurements` | Rows of a test entry: freq, SWR, forward/reflected power, return loss, R, X, note |
| `radio_configs` | A CHIRP snapshot: radio (equipment), title, firmware, edition/bank, date, current flag, linked `.img` and `.csv` files, settings notes |
| `radio_channels` | Parsed CHIRP CSV rows of a config (+ `raw` columns), auto-linked to a repeater by frequency |
| `files` | Library metadata: sha256, filename, MIME, size, title, category, tags, extracted text |
| `links` | Generic many-to-many links between any two records (`a_type,a_id → b_type,b_id`, role). Attachments, manuals of a radio, sweeps of a test, QSL card of a QSO… |
| `saved_filters` | Named filter query strings per section |

Local-only tables: `settings`, `meta` (schema version, instance id), `seq_counter`,
`sync_peers`, `sync_conflicts`, `import_log`, and the FTS5 table `search_fts`.

**Geo**: a QSO/SWL entry stores the other station's lat/lon (from a locator if only that
is known) and a snapshot of *my* position from the station profile, so moving or editing a
station later never rewrites history. Distances to any reference point are computed in SQL
by a registered `geo_dist_km()` function, so "within 50 km of Grenoble" or "sorted by
distance from home" are plain filters.

## 6. Sync protocol (hub and spokes)

The Proxmox instance is the **hub** (*Settings › Sync › Accept sync*, with a token).
Other instances are **clients** (*Settings › Sync › Hub URL + token*). Any instance can be
either; only one hub is expected.

1. **Push** — client sends records with `change_seq > last_push_seq` whose
   `received_from` is not the hub, in batches, tables in `SYNC_ORDER`.
2. **Blobs** — client asks which of its file hashes the hub lacks and uploads them.
3. **Pull** — client asks for hub records with `change_seq > last_pull_seq` that did not
   come from this client; hub replies in pages with its current `change_seq`.
4. Missing blobs are downloaded (or fetched on demand when *pull files* is off).
5. Watermarks are saved only after each step succeeds; every step is idempotent, so an
   interrupted sync just repeats.

**Merge rule** (one engine for sync, bundles and database imports). Versions are compared
by `(updated_at, modified_by_instance)`; identical versions are skipped. Then:

- *Hub receiving a push.* The client sends the hub `change_seq` it last pulled. If the hub's
  row changed after that (`change_seq > client_last_pull`) and differs, both sides edited
  it concurrently: the newer one wins and the other is saved in `sync_conflicts`.
  Otherwise the client had already seen the hub's version, so its edit simply applies.
- *Client receiving a pull.* The hub has already resolved conflicts, so its version applies.
  Only a local edit made during the sync itself (`change_seq > last_push_seq`, local origin)
  is treated as a conflict: newer wins, loser saved.
- *Bundle / database import.* No history available: newer wins; when the data differs the
  loser is saved in `sync_conflicts`.

Nothing is ever discarded; the Conflicts page shows both versions and can restore either.

**Transport**: JSON over HTTP(S), `Authorization: Bearer <token>`. Designed for LAN or a
VPN; for anything else put it behind an HTTPS reverse proxy (see `deploy/PROXMOX.md`).

## 7. Module map

```
shacklog/
  __init__.py          APP_NAME, __version__
  __main__.py          python -m shacklog
  cli.py               command-line modes (§2)
  config.py            data directory, paths, local settings helpers
  db.py                engine, sessions, SQLite pragmas + SQL functions, init/migrate,
                       change stamping (change_seq/updated_at), search index hooks
  migrations.py        numbered schema migrations
  models.py            all ORM models, SyncMixin, SYNC_ORDER, choice lists
  services/
    geo.py             Maidenhead ↔ lat/lon, distance, bearing, bbox, position parsing
    cty.py             callsign → country/DXCC/zones (AD1C country list, local file)
    bands.py           frequency → band, modes/submodes, default RST, service hints
    adif.py            ADI parser/writer + ADIF ↔ QSO field mapping
    chirp.py           CHIRP CSV read/write, repeater ↔ channel, channel diff
    touchstone.py      .s1p/.s2p parsing, SWR summary, SVG plot
    filestore.py       content-addressed blob store, text extraction
    search.py          FTS5 index maintenance and queries
    backup.py          rotating SQLite backups
    bundle.py          export/import bundles and foreign database files
    sync_engine.py     table-agnostic serialise/merge (no HTTP)
    sync_client.py     client side of the protocol (urllib)
  web/
    __init__.py        create_app()
    auth.py            login, CSRF
    forms.py           declarative form specs → parse/validate/render
    filters.py         declarative filter specs → SQLAlchemy clauses + filter bar
    helpers.py         pagination, template filters, flash helpers
    views/*.py         one blueprint per section; api_sync.py for the sync API
    templates/, static/ (css, js, vendored htmx + Leaflet)
tests/                 pytest
packaging/             PyInstaller spec, build scripts
deploy/                Dockerfile, compose, systemd unit, Proxmox guide
```

URL convention per section `X`: `/X/` list+filters · `/X/new` · `/X/<id>` ·
`/X/<id>/edit` · `/X/<id>/delete` (POST) · exports under `/X/export.<ext>`.

## 8. Dependencies

| Package | Why | Notes |
|---|---|---|
| Flask (+ Jinja2, Werkzeug) | Web framework, templates, password hashing | Pure Python |
| SQLAlchemy 2.x | ORM, readable models, portable queries | Pure-Python fallback available |
| waitress | Production WSGI server on Windows, Linux, macOS | Pure Python |
| platformdirs | Correct per-OS data folder | Pure Python |
| pypdf | Text extraction from PDF manuals for search | Pure Python, optional at runtime |
| markdown | Knowledge-base notes and the in-app help | Pure Python |
| pystray + Pillow | Tray icon for background mode | **Windows only** (environment marker in requirements.txt) |
| htmx, Leaflet (vendored) | Live checks, map | Static files, no CDN |
| pytest, PyInstaller | Tests and executables | Dev only |

## 9. Roadmap

**Phase 1 — this build**: everything in §1–§7: four logs, repeaters, stations, equipment,
CHIRP archive + generator, library, links, callsign pages, search, filters, saved
filters, map, stats, ADIF/CHIRP/CSV import-export, sync, bundles, backups, conflicts,
trash, desktop/server modes, executables, Docker/LXC deployment.

**Phase 1b — done 12 Oct 2026**: dedicated Proxmox LXC creator (`deploy/proxmox/shacklog-lxc.sh`),
`shacklog-ctl` (update with rollback, restart, stop, backup, uninstall), keep/remove-data uninstaller,
web server controls via a systemd path unit, update-from-git on PCs, WAL checkpointing + clean exit,
backup mirror folder, knowledge base (Markdown notes, references, manuals, pins), SDR monitor with
OFFgrid-SDR and memory export.

**Phase 1c — v1.1.0, 13 Oct 2026 (in service)**: duplicate finder + merge (Settings ›
Duplicates), dark "night" theme by default with a light switch, persistent side navigation
(drawer on phones), Windows background mode with a tray icon and start-at-sign-in, in-app
help (user guide, credits), re-import of a newer bundle from the same source without
spurious conflicts.

**Phase 2** (in progress): ~~DXCC/country from `cty.dat`~~ (done: `services/cty.py`,
reads `cty.csv`/`cty.dat` from `<data>/reference/`, fills `qsos.country`, no schema change) ·
callbook lookup (QRZ/HamQTH) with cache ·
QSL card workflow · offline map tiles · ADX (XML ADIF) · repeater list importers
(RepeaterBook, IK2ANE-style CSV) · photo thumbnails.

**Phase 3**: LoTW/eQSL/ClubLog/QRZ upload · WSJT-X UDP logging · CAT via flrig/Hamlib ·
awards · contest mode with Cabrillo · translations (Italian/French).

## 10. Open points to confirm with Andrea

- Callsign and station locations are entered at first launch (*Setup*), not hard-coded.
- Where the code lives long-term (local folder, private GitHub repo with automatic
  Windows/Linux/macOS builds).
