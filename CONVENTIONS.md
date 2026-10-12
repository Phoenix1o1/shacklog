# ShackLog — Conventions & Contracts

How code is written in this project. Read with `MANDATES.md` (the rules) and `PLAN.md`
(the architecture). **`shacklog/web/views/qsos.py` and `templates/qsos/` are the
reference implementation of a section — copy their structure.**

---

## 1. A section (blueprint)

```
shacklog/web/views/<section>.py      bp = Blueprint("<section>", __name__, url_prefix="/<section>")
shacklog/web/templates/<section>/    index.html, form.html, view.html, …
```

- Register the detail endpoint so links/search can point to it:
  `h.register_entity_view("<entity>", "<section>.view")`.
- Route names and URLs (all ids are UUID strings named **`record_id`**):

  | endpoint | URL | method |
  |---|---|---|
  | `index` | `/<section>/` | GET (list + filters) |
  | `new` | `/<section>/new` | GET, POST |
  | `view` | `/<section>/<record_id>` | GET |
  | `edit` | `/<section>/<record_id>/edit` | GET, POST |
  | `delete` | `/<section>/<record_id>/delete` | POST → soft delete, redirect to index |

- Declare `FORM` (list of `forms.Section`), `FILTERS` (list of `filters.Filter`) and `SORTS`.
  Provide `filtered_query()` returning the `FilterResult` for the current request: exports
  and the map reuse it.
- Database: `h.session()` (request session), `h.live(Model)` (non-deleted rows),
  `h.get_or_404(Model, id)`, `h.setting(key)`. Commit explicitly with `h.session().commit()`.
- Never hard-delete synced rows: `obj.soft_delete()`. Never set `updated_at`, `change_seq`,
  `modified_by_instance`, `received_from` by hand (db.py stamps them).
- Form POST with errors → re-render with `raw` values and status **422**.
- Lazy-import service modules owned by someone else inside the route function
  (`from shacklog.services import chirp`) so the app boots even if one is missing.

## 2. Templates

- Extend `base.html`; use the macros, don't re-invent them:
  - `_ui.html`: `page_head` (call block for buttons), `facts`, `record_meta`,
    `delete_button`, `links_panel`, `tag_list`, `pagination`, `count_line`, `empty`
  - `_forms.html`: `render_form`, `render_sections`, `field`, `csrf`
  - `_filters.html`: `filter_bar`, `sort_th`
- Lists are `<div class="ledger-wrap"><table class="ledger">`. The **first column is the
  line number** (`page.first_index + loop.index0`, class `num`). Every `<td>` gets
  `data-label="…"`; the main cell (callsign/name/title) gets `class="primary"`. On phones
  the table turns into cards using these attributes.
- Every POST form includes `{{ csrf() }}` (or the hidden `_csrf` input).
- Detail pages: `detail-grid` with main column + `<aside>`; end the main column with
  `links_panel(entity, obj, linked)` where `linked = links_for(s, entity, obj.id)`.
- New CSS goes at the **end** of `static/css/app.css` under a `/* ---------- <section> ---------- */`
  comment, using the existing custom properties (`--accent`, `--rule`, `--bar`…).
  No inline styles beyond one-off spacing. No new fonts, colours or CDNs.

## 3. Words in the interface

- Sentence case everywhere. No ALL-CAPS labels. No exclamation marks.
- Buttons say what happens: "Log QSO", "Import CHIRP CSV", "Restore", not "Submit"/"OK".
- Flash messages confirm with the same verb ("Imported 42 repeaters."). Errors say what is
  wrong and how to fix it. Empty states invite the next action.
- Times are UTC and say so. Frequencies in MHz with `|freq`. Distances with `|km`.

## 4. Python

- Module docstring first: what it does, how it fits. Comments say *why*.
- Type hints on public functions. Pure logic lives in `shacklog/services/` with no Flask
  imports. Keep functions short; prefer plain data (dicts, dataclasses) across boundaries.
- No new third-party dependency without updating `PLAN.md` §8 and `requirements.txt`.

## 5. Tests

- `pytest` from the project root. Fixtures in `tests/conftest.py`:
  - `app` — app with a fresh temp data dir, setup already done (home station Milano JN45ol,
    fixed station Grenoble JN25vd), desktop mode, no login;
  - `client` — its test client; `csrf(client)` returns a valid token;
  - `db`, `s` (a session), `filestore`;
  - modules missing from `VIEW_MODULES` are replaced by stub blueprints exposing the
    endpoints in `STUB_ENDPOINTS`, so each section can be tested on its own.
- Name files `tests/test_<module>.py`.

---

## 6. Contracts between modules

Implement exactly these signatures (more helpers are fine).

### `services/adif.py`
```python
parse_adi(data: bytes | str) -> tuple[dict[str, str], list[dict[str, str]]]   # (header, records), keys lower-case
write_adi(records: list[dict[str, str]], header: dict[str, str] | None = None) -> str
record_to_qso_fields(rec: dict[str, str]) -> tuple[dict, dict]   # (Qso field values, extra ADIF fields)
qso_to_record(qso) -> dict[str, str]                              # includes extras from qso.extra_adif
```

### `services/qso_io.py`
```python
@dataclass class ImportSummary: created: int; duplicates: int; skipped: int; errors: list[str]; created_ids: list[str]
import_adif(session, data: bytes | str, station_id: str | None = None, filename: str | None = None) -> ImportSummary
export_adif(qsos: Iterable[Qso]) -> str
summary_json(summary: ImportSummary) -> str
```
Duplicate = same call, same band, same normalised mode, `time_on` within ±2 minutes.
Uses `services.enrich.enrich_qso(session, qso, is_new=True)`.

### `services/chirp.py`
```python
CHIRP_COLUMNS: list[str]   # standard CHIRP CSV header
read_chirp_csv(text: str) -> list[dict]     # normalised channel dicts with keys == RadioChannel fields (+ "raw": dict)
write_chirp_csv(channels: list[dict]) -> str
repeater_to_channel(rep, location: int) -> dict     # rep: Repeater (or object with same attrs)
channel_to_repeater_fields(ch: dict) -> dict        # Repeater field values from a channel
diff_channels(old: list[dict], new: list[dict]) -> dict   # {"added": [...], "removed": [...], "changed": [(old, new, [field,…])]} keyed by location
```

### `services/touchstone.py`
```python
parse_touchstone(text: str) -> dict          # {"freq_hz": [...], "s11": [complex…], "z0": float}
swr(s: complex) -> float ; return_loss_db(s: complex) -> float ; impedance(s: complex, z0: float) -> complex
summarize(parsed: dict) -> dict              # min_swr, freq_min_mhz, r_ohm, x_ohm, bw_swr2_low_mhz, bw_swr2_high_mhz, start_mhz, stop_mhz, points
swr_svg(parsed: dict, width: int = 640, height: int = 260, shade: list[tuple[float, float]] | None = None) -> str
```

### `services/backup.py`
```python
create_backup(db, backups_dir: Path, reason: str = "manual") -> Path
daily_backup_if_due(db, backups_dir: Path) -> Path | None
list_backups(backups_dir: Path) -> list[dict]      # name, path, size, created, reason
prune(backups_dir: Path, keep_daily: int = 14, keep_other: int = 20) -> None
```

### `services/sync_engine.py`
```python
serialize(obj) -> dict                          # JSON-safe row, LOCAL_COLUMNS excluded
changes_since(session, seq: int, exclude_peer: str | None, limit: int) -> tuple[list[dict], int]
       # records as {"table": …, "row": {…}}, ordered by change_seq; returns (records, last_seq_included)
apply_records(session, records: list[dict], peer_id: str, peer_name: str | None, *,
              mode: str,                          # "hub" | "client" | "import"
              client_last_pull: int | None = None,   # mode="hub": hub seq the client last pulled
              local_dirty_after: int | None = None,  # mode="client": our last_push_seq
              ) -> dict
       # {"inserted", "updated", "unchanged", "conflicts"} — merge rule of PLAN.md §6
restore_conflict(session, conflict_id: int) -> None
```

### `services/sync_client.py`
```python
class SyncClient: __init__(db, filestore, hub_url, token, transport=None); run() -> dict; hello() -> dict
fetch_blob_from_hub(db, filestore, sha256: str) -> bool
class SyncWorker: start(app) / stop() / trigger()    # background thread, writes app.config["SYNC_STATUS"]
```

### `services/bundle.py`
```python
export_bundle(db, paths, out_path: Path) -> Path
import_bundle(db, paths, bundle_path: Path) -> dict          # merges via sync_engine, copies blobs
import_database_file(db, paths, db_path: Path) -> dict       # same without blobs
```

### Endpoints other sections link to (`url_for` names)
`swl.index/new/view` · `journal.index/new/view` · `repeaters.index/new/view/import_chirp/export_chirp` ·
`radio.index/new/view` · `stations.index/new/view` · `equipment.index/new/view` ·
`contacts.index/view/callsign(call)` · `files.index/upload/view/download` · `search.index` ·
`map.index` (query: `layer=qso|swl|repeater|station` + that section's filters) · `stats.index` ·
`settings.index/sync/data/conflicts/trash` · `api_sync.hello`
