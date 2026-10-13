"""Settings of this instance: general preferences, sync, data (import/export/backups),
conflicts and the trash.

Everything here is *local* (MANDATES.md §3): settings, password, tokens and peers never
sync. The pages follow the qsos reference pattern; every POST carries a CSRF token.

Pages (endpoint → URL)
----------------------
``index``      /settings/            instance name, defaults, password, search index, about
``sync``       /settings/sync        hub side (accept, token, clients) and client side
``data``       /settings/data        bundles, database import, backups, import history
``conflicts``  /settings/conflicts   losing versions kept by sync and imports
``trash``      /settings/trash       soft-deleted records of every type, restorable
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    after_this_request,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import func, literal, or_, select, union_all

from shacklog import APP_NAME, __version__, migrations
from shacklog.models import (
    ENTITIES,
    SYNC_ORDER,
    TABLES,
    ImportLog,
    Link,
    Qso,
    SyncConflict,
    SyncPeer,
)
from shacklog.web import helpers as h
from shacklog.web.auth import MIN_PASSWORD_LENGTH, hash_password

log = logging.getLogger(__name__)

bp = Blueprint("settings", __name__, url_prefix="/settings")

SYNC_INTERVALS = [(0, "Only when I click “Sync now”"), (5, "Every 5 minutes"),
                  (15, "Every 15 minutes"), (30, "Every 30 minutes"), (60, "Every hour"),
                  (360, "Every 6 hours"), (1440, "Once a day")]
PAGE_SIZES = [25, 50, 100, 200]
TRASH_MODELS = [m for m in SYNC_ORDER if m is not Link]


def _db():
    return h.database()


def _paths():
    return current_app.config["PATHS"]


def _redirect_self():
    return redirect(request.path)


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------

@bp.route("/", methods=["GET", "POST"])
def index():
    s = h.session()
    desktop = current_app.config["MODE"] == "desktop"
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "general":
            name = request.form.get("instance_name", "").strip()
            if name and name != _db().instance_name:
                _db().rename_instance(name)
            for key in ("default_station_id", "reference_station_id"):
                value = request.form.get(key) or None
                if value is None or value in dict(h.station_choices(s)):
                    h.save_setting(key, value)
            try:
                size = int(request.form.get("page_size", 50))
            except ValueError:
                size = 50
            h.save_setting("page_size", size if size in PAGE_SIZES else 50)
            s.commit()
            flash("Settings saved.", "ok")
        elif action == "password":
            pw1, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            if len(pw1) < MIN_PASSWORD_LENGTH:
                flash(f"Use at least {MIN_PASSWORD_LENGTH} characters for the password.", "error")
            elif pw1 != pw2:
                flash("The two passwords don't match. Type the same password twice.", "error")
            else:
                h.save_setting("password_hash", hash_password(pw1))
                if desktop:
                    h.save_setting("require_login_local", bool(request.form.get("require_login_local")))
                s.commit()
                flash("Password saved.", "ok")
        elif action == "login_local" and desktop:
            want = bool(request.form.get("require_login_local"))
            if want and not h.setting("password_hash"):
                flash("Set a password first, then ask for it on this computer.", "error")
            else:
                h.save_setting("require_login_local", want)
                s.commit()
                flash("Saved. ShackLog will ask for the password on this computer." if want
                      else "Saved. No password needed on this computer.", "ok")
        elif action == "remove_password" and desktop:
            h.save_setting("password_hash", None)
            h.save_setting("require_login_local", False)
            s.commit()
            flash("Password removed. Only this computer can open ShackLog in desktop mode.", "ok")
        elif action.startswith("callbook"):
            _callbook_action(action)
            return redirect(url_for(".index") + "#callbook")
        elif action == "rebuild_search":
            from shacklog.services import search

            started = time.monotonic()
            count = search.rebuild(s)
            s.commit()
            flash(f"Rebuilt the search index: {count} records in "
                  f"{time.monotonic() - started:.1f} s.", "ok")
        elif action == "quit" and desktop and current_app.config.get("SHUTDOWN"):
            return _quit()
        else:
            abort(400, description="Unknown action.")
        return redirect(url_for(".index"))

    about = {
        "version": __version__,
        "data_dir": str(_paths().data_dir),
        "database": str(_paths().database),
        "instance_id": _db().instance_id,
        "schema_version": migrations.LATEST,
        "mode": "Desktop (this computer only)" if desktop else "Server (network)",
    }
    return render_template(
        "settings/index.html", stations=h.station_choices(s), desktop=desktop,
        instance_name=_db().instance_name, page_sizes=PAGE_SIZES,
        default_station_id=h.setting("default_station_id"),
        reference_station_id=h.setting("reference_station_id"),
        page_size=h.setting("page_size"), has_password=bool(h.setting("password_hash")),
        require_login_local=bool(h.setting("require_login_local")), about=about,
        can_quit=desktop and bool(current_app.config.get("SHUTDOWN")),
        min_password=MIN_PASSWORD_LENGTH, callbook=h.callbook(), callbook_services=_callbook_services(),
    )


def _callbook_services():
    from shacklog.services.callbook import SERVICES

    return SERVICES


def _callbook_action(action: str) -> None:
    """Settings › General › Callbook: service and login, test, empty the cache."""
    from shacklog.services.callbook import SERVICES, CallbookError

    if action == "callbook":
        service = request.form.get("callbook_service", "")
        if service not in dict(SERVICES):
            abort(400, description="Unknown callbook.")
        h.save_setting("callbook_service", service)
        h.save_setting("callbook_user", request.form.get("callbook_user", "").strip() or None)
        password = request.form.get("callbook_password", "")
        if password or not service:  # empty box = keep the saved one
            h.save_setting("callbook_password", password or None)
        h.session().commit()
        flash("Callbook saved." if service else "Callbook turned off.", "ok")
    elif action == "callbook_test":
        try:
            flash(h.callbook().test(), "ok")
        except CallbookError as exc:
            flash(f"The callbook doesn't work: {exc}", "error")
    elif action == "callbook_clear":
        h.callbook().cache.clear()
        flash("Emptied the callbook cache.", "ok")
    else:
        abort(400, description="Unknown action.")


def _quit(message: str = "quit"):
    """Answer with the "stopped" page, then stop the process a moment later."""
    shutdown = current_app.config["SHUTDOWN"]

    def later():
        time.sleep(1.0)  # let the "stopped" page reach the browser first
        try:
            shutdown()
        except Exception:
            log.exception("Shutdown failed")

    threading.Thread(target=later, name="shacklog-quit", daemon=True).start()
    return render_template("settings/stopped.html", reason=message)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _sync_client():
    from shacklog.services.sync_client import SyncClient

    return SyncClient.from_settings(_db(), h.filestore(), current_app.config.get("SYNC_TRANSPORT"))


@bp.route("/sync", methods=["GET", "POST"])
def sync():
    s = h.session()
    new_token = None
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "hub":
            enabled = bool(request.form.get("sync_hub_enabled"))
            h.save_setting("sync_hub_enabled", enabled)
            if enabled and not h.setting("sync_hub_token"):
                new_token = secrets.token_urlsafe(32)
                h.save_setting("sync_hub_token", new_token)
            s.commit()
            flash("This instance now accepts sync from your other computers." if enabled
                  else "This instance no longer accepts sync.", "ok")
            if new_token is None:
                return _redirect_self()
        elif action == "rotate_token":
            new_token = secrets.token_urlsafe(32)
            h.save_setting("sync_hub_token", new_token)
            s.commit()
            flash("New token generated. Paste it into every computer that syncs with this one; "
                  "the old token stopped working.", "ok")
        elif action == "client":
            from shacklog.services.sync_client import normalize_hub_url

            url = normalize_hub_url(request.form.get("sync_hub_url"))
            token = request.form.get("sync_client_token", "").strip()
            h.save_setting("sync_hub_url", url or None)
            if token:  # empty field = keep the saved token (it is never shown again)
                h.save_setting("sync_client_token", token)
            if request.form.get("forget_token"):
                h.save_setting("sync_client_token", None)
            try:
                interval = int(request.form.get("sync_interval_min", 15))
            except ValueError:
                interval = 15
            h.save_setting("sync_interval_min", max(0, interval))
            h.save_setting("sync_pull_files", bool(request.form.get("sync_pull_files")))
            s.commit()
            flash("Sync settings saved.", "ok")
            return _redirect_self()
        elif action == "test":
            client = _sync_client()
            if client is None:
                flash("Enter the hub address and token first.", "error")
            else:
                from shacklog.services.sync_client import SyncError

                try:
                    hub = client.hello()
                    flash(f"Connected to {hub.get('instance_name') or 'the hub'} "
                          f"(ShackLog {hub.get('app_version', '?')}). Ready to sync.", "ok")
                except SyncError as exc:
                    flash(f"Connection failed: {exc}", "error")
            return _redirect_self()
        elif action == "sync_now":
            return _sync_now()
        else:
            abort(400, description="Unknown action.")

    hub_peer = s.query(SyncPeer).filter(SyncPeer.role == "hub").order_by(
        SyncPeer.last_sync_at.desc().nullslast()).first()
    clients = s.query(SyncPeer).filter(SyncPeer.role == "client").order_by(
        SyncPeer.last_sync_at.desc().nullslast()).all()
    return render_template(
        "settings/sync.html", new_token=new_token,
        hub_enabled=bool(h.setting("sync_hub_enabled")),
        has_hub_token=bool(h.setting("sync_hub_token")),
        hub_url=h.setting("sync_hub_url") or "",
        has_client_token=bool(h.setting("sync_client_token")),
        interval=h.setting("sync_interval_min"), intervals=SYNC_INTERVALS,
        pull_files=bool(h.setting("sync_pull_files")),
        hub_peer=hub_peer, clients=clients, status=current_app.config.get("SYNC_STATUS") or {},
        worker=current_app.config.get("SYNC_WORKER") is not None,
        this_url=request.host_url.rstrip("/"),
        unresolved=s.query(SyncConflict).filter(SyncConflict.resolved_at.is_(None)).count(),
    )


def _sync_now():
    worker = current_app.config.get("SYNC_WORKER")
    if worker is not None:
        worker.trigger()
        flash("Sync started. This page shows the result when it's done; reload in a moment.", "ok")
        return redirect(url_for(".sync"))
    from shacklog.services.sync_client import SyncError, summary_text

    client = _sync_client()
    if client is None:
        flash("Enter the hub address and token first.", "error")
        return redirect(url_for(".sync"))
    try:
        stats = client.run()
    except SyncError as exc:
        flash(f"Sync failed: {exc}", "error")
    else:
        flash(summary_text(stats), "warn" if stats.get("problems") else "ok")
    return redirect(url_for(".sync"))


# ---------------------------------------------------------------------------
# Data: bundles, database import, backups, import history
# ---------------------------------------------------------------------------

@bp.route("/data", methods=["GET", "POST"])
def data():
    from shacklog.services import backup

    s = h.session()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "backup":
            path = backup.create_backup(_db(), _paths().backups, reason="manual")
            backup.prune(_paths().backups, int(h.setting("backup_keep_daily") or 14),
                         int(h.setting("backup_keep_import") or 20))
            flash(f"Backed up to {path.name}.", "ok")
        elif action == "export":
            return _export_bundle()
        elif action == "import":
            result = _import_upload()
            if result is not None:
                return result
        elif action == "import_path":
            result = _import_local_path(request.form.get("path", ""))
            if result is not None:
                return result
        elif action.startswith("cty_"):
            _country_action(action)
            return redirect(url_for(".data") + "#countries")
        elif action.startswith("tiles_"):
            _tiles_action(action)
            return redirect(url_for(".data") + "#map")
        else:
            abort(400, description="Unknown action.")
        return redirect(url_for(".data"))

    history = []
    for entry in s.query(ImportLog).order_by(ImportLog.at.desc()).limit(50):
        try:
            summary = json.loads(entry.summary or "{}")
        except ValueError:
            summary = {}
        history.append((entry, summary if isinstance(summary, dict) else {}))
    from shacklog.services import cty, tiles

    missing = (h.live(Qso).filter(or_(Qso.country.is_(None), Qso.country == "")).count())
    return render_template(
        "settings/data.html", backups=backup.list_backups(_paths().backups), history=history,
        cty_info=cty.info(_paths().data_dir), cty_url=cty.SOURCE_URL,
        cty_auto=bool(h.setting("cty_auto_update")), qsos_without_country=missing,
        map_files=[tiles.mbtiles_info(p) for p in tiles.mbtiles_files(_paths().data_dir)],
        map_dir=str(tiles.maps_dir(_paths().data_dir)),
        tile_cache=tiles.cache_usage(_paths().data_dir),
        tiles_online=bool(h.setting("map_tiles_online")),
        tile_url=h.setting("map_tile_url") or "", tile_cache_mb=h.setting("map_tile_cache_mb"),
        desktop=current_app.config["MODE"] == "desktop", local_bundles=_local_bundles(),
        backups_dir=str(_paths().backups), incoming_dir=str(_paths().incoming),
        keep_daily=h.setting("backup_keep_daily"), keep_other=h.setting("backup_keep_import"),
    )


def _country_action(action: str) -> None:
    """Settings › Data › Countries: get the list, or fill in the QSOs that lack one."""
    from shacklog.services import backup, cty

    data_dir = _paths().data_dir
    if action == "cty_download":
        try:
            table = cty.download(data_dir)
        except Exception as exc:  # offline, site down, not a country file
            flash(f"Couldn't download the country list ({exc}). Download cty.csv from "
                  f"{cty.SOURCE_URL} on another computer and upload it here.", "error")
            return
        flash(f"Downloaded the country list: {len(table)} countries"
              f"{', version ' + table.version if table.version else ''}.", "ok")
    elif action == "cty_upload":
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a cty.csv or cty.dat file first.", "error")
            return
        try:
            table = cty.install(data_dir, upload.read(cty.MAX_SIZE + 1), upload.filename)
        except ValueError as exc:
            flash(f"That isn't a country list: {exc}", "error")
            return
        flash(f"Installed the country list: {len(table)} countries.", "ok")
    elif action == "cty_fill":
        if cty.load(data_dir) is None:
            flash("Download the country list first.", "error")
            return
        backup.create_backup(_db(), _paths().backups, reason="before-country-fill")
        s = h.session()
        filled = unknown = 0
        for qso in h.live(Qso).filter(or_(Qso.country.is_(None), Qso.country == "")).all():
            entity = cty.lookup(qso.call)
            if entity is None:
                unknown += 1
            else:
                qso.country = entity.name
                filled += 1
        s.commit()
        flash(f"Filled in the country of {filled} QSO{'s' if filled != 1 else ''}"
              + (f"; {unknown} callsign{'s' if unknown != 1 else ''} matched no country." if unknown
                 else ".") + " A backup was made first.", "ok")
    elif action == "cty_auto":
        want = request.form.get("enabled") == "1"
        h.save_setting("cty_auto_update", want)
        h.session().commit()
        flash("The country list will " + ("be downloaded again every month." if want
                                          else "only change when you download or upload it."), "ok")
    else:
        abort(400, description="Unknown action.")


def _tiles_action(action: str) -> None:
    """Settings › Data › Map: offline map files, the tile cache, where tiles come from."""
    import sqlite3
    from contextlib import closing

    from shacklog.services import tiles

    data_dir = _paths().data_dir
    folder = tiles.maps_dir(data_dir)
    if action == "tiles_settings":
        url = request.form.get("map_tile_url", "").strip() or None
        if url and not tiles.safe_url(url):
            flash("A tile server address starts with http:// or https:// and contains "
                  "{z}, {x} and {y}.", "error")
            return
        try:
            limit = max(0, min(100_000, int(request.form.get("map_tile_cache_mb", 500))))
        except ValueError:
            limit = 500
        h.save_setting("map_tile_url", url)
        h.save_setting("map_tile_cache_mb", limit)
        h.save_setting("map_tiles_online", request.form.get("map_tiles_online") == "1")
        h.session().commit()
        tiles.prune_cache(data_dir, limit)
        flash("Saved the map settings.", "ok")
    elif action == "tiles_clear":
        removed = tiles.clear_cache(data_dir)
        flash(f"Emptied the map tile cache ({removed} tiles).", "ok")
    elif action == "tiles_upload":
        upload = request.files.get("file")
        name = Path(upload.filename).name if upload is not None and upload.filename else ""
        if not name.lower().endswith(".mbtiles"):
            flash("Choose an .mbtiles file.", "error")
            return
        folder.mkdir(parents=True, exist_ok=True)
        tmp = _paths().tmp / f"upload-{uuid.uuid4().hex}.mbtiles"
        upload.save(tmp)
        try:
            # closing(): on Windows an open file can't be deleted or moved.
            with closing(sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)) as conn:
                conn.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles LIMIT 1")
        except sqlite3.Error:
            tmp.unlink(missing_ok=True)
            flash(f"{name} isn't an MBTiles map file.", "error")
            return
        target = folder / name
        os.replace(tmp, target)
        info = tiles.mbtiles_info(target)
        if not info["ok"]:
            flash(f"Installed {name}, but it can't be shown: {info.get('error')}.", "error")
        else:
            flash(f"Installed the offline map {info['name']} (zoom {info['minzoom']}–"
                  f"{info['maxzoom']}).", "ok")
    elif action == "tiles_remove":
        name = request.form.get("file", "")
        target = folder / Path(name).name
        if target.suffix == ".mbtiles" and target.is_file():
            target.unlink()
            flash(f"Removed the offline map {target.name}.", "ok")
    elif action == "tiles_open" and current_app.config["MODE"] == "desktop":
        from shacklog.services import system

        folder.mkdir(parents=True, exist_ok=True)
        system.open_folder(folder)
    else:
        abort(400, description="Unknown action.")


def _export_bundle():
    from shacklog.services import bundle

    name = bundle.default_export_name(_db())
    out = _paths().tmp / f"export-{uuid.uuid4().hex[:12]}.zip"
    bundle.export_bundle(_db(), _paths(), out)

    @after_this_request
    def _remove(response):
        response.call_on_close(lambda: out.unlink(missing_ok=True))
        return response

    return send_file(out, mimetype="application/zip", as_attachment=True, download_name=name)


def _import_upload():
    from shacklog.services import bundle

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Choose a bundle (.zip) or a database file (.sqlite3) first.", "error")
        return None
    tmp = _paths().tmp / f"upload-{uuid.uuid4().hex[:12]}"
    _paths().tmp.mkdir(parents=True, exist_ok=True)
    upload.save(tmp)
    try:
        summary = bundle.import_upload(_db(), _paths(), tmp, upload.filename)
    except bundle.BundleError as exc:
        flash(f"Nothing was imported: {exc}", "error")
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return render_template("settings/import_done.html", summary=summary, filename=upload.filename)


IMPORTABLE = ("*.zip", "*.sqlite3", "*.sqlite", "*.db")


def _local_bundles() -> list[str]:
    """Files offered as one-click imports: everything in the data folder's incoming/
    (every instance), plus on a PC ShackLog-*.zip next to the program and in Downloads."""
    from shacklog.services.system import repo_root

    found: list[str] = []
    incoming = _paths().incoming
    if incoming.is_dir():
        for pattern in IMPORTABLE:
            found += [str(p) for p in sorted(incoming.glob(pattern))]
    if current_app.config["MODE"] == "desktop":
        for folder in filter(None, [repo_root(), Path.home() / "Downloads"]):
            try:
                found += [str(p) for p in sorted(folder.glob("ShackLog-*.zip"))]
            except OSError:
                continue
    return found


def _import_local_path(raw: str):
    """Import a file already on this machine, by path. Browsers can't send very large
    files reliably, and a path avoids copying a big bundle twice. A PC takes any path;
    the hub (reachable from the network) only files in its incoming/ folder."""
    from shacklog.services import bundle

    path = Path(raw.strip().strip('"')).expanduser()
    if current_app.config["MODE"] != "desktop":
        incoming = _paths().incoming.resolve()
        try:
            inside = path.resolve().parent == incoming
        except OSError:
            inside = False
        if not inside:
            flash(f"On the hub, put the file in {incoming} first, then import it from the list.",
                  "error")
            return None
    if not raw.strip() or not path.is_file():
        flash(f"No file at “{raw.strip()}”. Paste the full path of the .zip or .sqlite3.", "error")
        return None
    try:
        summary = bundle.import_upload(_db(), _paths(), path, path.name)
    except bundle.BundleError as exc:
        flash(f"Nothing was imported: {exc}", "error")
        return None
    return render_template("settings/import_done.html", summary=summary, filename=path.name)


@bp.route("/data/backups/<name>")
def download_backup(name: str):
    from shacklog.services import backup

    path = backup.resolve_backup(_paths().backups, name)
    if path is None:
        abort(404)
    return send_file(path, mimetype="application/vnd.sqlite3", as_attachment=True,
                     download_name=path.name)


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

@bp.route("/conflicts")
def conflicts():
    from shacklog.services import sync_engine

    s = h.session()
    query = s.query(SyncConflict).order_by(
        SyncConflict.resolved_at.is_not(None), SyncConflict.detected_at.desc(), SyncConflict.id.desc())
    page = h.paginate(query, per_page=20)
    views = [sync_engine.describe_conflict(s, c) for c in page.items]
    unresolved = s.query(SyncConflict).filter(SyncConflict.resolved_at.is_(None)).count()
    sources = (s.query(SyncConflict.peer_id, SyncConflict.peer_name, func.count())
               .filter(SyncConflict.resolved_at.is_(None))
               .group_by(SyncConflict.peer_id, SyncConflict.peer_name).all())
    return render_template("settings/conflicts.html", page=page, views=views,
                           unresolved=unresolved, label_of=_label_of, sources=sources)


@bp.route("/conflicts/dismiss-all", methods=["POST"])
def dismiss_all_conflicts():
    """Accept the current versions of every open conflict from one source (e.g. an import
    that deliberately updated records). The old versions stay on file."""
    from shacklog.services import sync_engine

    s = h.session()
    peer = request.form.get("peer_id") or None
    query = s.query(SyncConflict).filter(SyncConflict.resolved_at.is_(None))
    query = query.filter(SyncConflict.peer_id == peer) if peer else query.filter(SyncConflict.peer_id.is_(None))
    ids = [c.id for c in query.all()]
    for conflict_id in ids:
        sync_engine.dismiss_conflict(s, conflict_id)
    s.commit()
    flash(f"Dismissed {len(ids)}. The older versions are still stored under Resolved.", "ok")
    return redirect(url_for(".conflicts"))


def _label_of(table: str) -> str:
    model = TABLES.get(table)
    return model.__label__ if model is not None else table


@bp.route("/conflicts/<int:conflict_id>/<action>", methods=["POST"])
def conflict_action(conflict_id: int, action: str):
    from shacklog.services import sync_engine

    s = h.session()
    conflict = s.get(SyncConflict, conflict_id)
    if conflict is None:
        abort(404)
    if action == "restore":
        try:
            sync_engine.restore_conflict(s, conflict_id)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for(".conflicts"))
        s.commit()
        flash("Restored that version. It reaches your other instances at the next sync. "
              "The version it replaced is kept under Conflicts › Resolved.", "ok")
        model = TABLES.get(conflict.table_name)
        target = h.entity_url(model.__entity__, conflict.record_id) if model else None
        return redirect(target or url_for(".conflicts"))
    if action == "dismiss":
        sync_engine.dismiss_conflict(s, conflict_id)
        s.commit()
        flash("Dismissed. The losing version is still stored.", "ok")
        return redirect(url_for(".conflicts"))
    abort(404)


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------

@bp.route("/trash")
def trash():
    s = h.session()
    kind = request.args.get("type", "")
    models = [m for m in TRASH_MODELS if not kind or m.__entity__ == kind]
    if not models:
        abort(404)
    parts = [select(literal(m.__entity__).label("entity"), m.id.label("id"),
                    m.deleted_at.label("deleted_at")).where(m.deleted_at.is_not(None))
             for m in models]
    union = union_all(*parts).subquery()
    total = s.execute(select(func.count()).select_from(union)).scalar() or 0
    per_page = int(h.setting("page_size") or 50)
    try:
        page_no = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page_no = 1
    rows = s.execute(select(union.c.entity, union.c.id, union.c.deleted_at)
                     .order_by(union.c.deleted_at.desc(), union.c.id)
                     .limit(per_page).offset((page_no - 1) * per_page)).all()
    items = []
    for entity, rid, _deleted in rows:
        obj = s.get(ENTITIES[entity], rid)
        if obj is not None:
            items.append((entity, obj))
    counts = {m.__entity__: s.query(m).filter(m.deleted_at.is_not(None)).count()
              for m in TRASH_MODELS}
    page = h.Page(items, total, page_no, per_page)
    return render_template("settings/trash.html", page=page, kind=kind, counts=counts,
                           types=[(m.__entity__, m.__label__) for m in TRASH_MODELS])


@bp.route("/trash/<entity>/<record_id>/restore", methods=["POST"])
def restore(entity: str, record_id: str):
    model = ENTITIES.get(entity)
    if model is None or model is Link:
        abort(404)
    obj = h.get_or_404(model, record_id, allow_deleted=True)
    obj.restore()
    h.session().commit()
    flash(f"Restored {model.__label__.lower()} “{obj.display_name()}”.", "ok")
    back = request.form.get("back") or ""
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    return redirect(h.entity_url(entity, obj.id) or url_for(".trash"))


# ---------------------------------------------------------------------------
# Program: data folder, updates, server controls, backup mirror, OFFgrid-SDR path
# ---------------------------------------------------------------------------

@bp.route("/program", methods=["GET", "POST"])
def program():
    from shacklog.services import system

    s = h.session()
    desktop = current_app.config["MODE"] == "desktop"
    paths = _paths()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "open_data" and desktop:
            if system.open_folder(paths.data_dir):
                flash("Opened the data folder.", "ok")
            else:
                flash(f"Couldn't open the folder. It is {paths.data_dir}", "error")
        elif action == "mirror":
            folder = request.form.get("backup_mirror_dir", "").strip() or None
            if folder:
                try:
                    Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    flash(f"Can't use that folder: {exc}", "error")
                    return redirect(url_for(".program"))
            h.save_setting("backup_mirror_dir", folder)
            try:
                keep = max(1, min(365, int(request.form.get("backup_mirror_keep", 30))))
            except ValueError:
                keep = 30
            h.save_setting("backup_mirror_keep", keep)
            s.commit()
            if folder:
                from shacklog.services import backup

                made = backup.create_backup(_db(), paths.backups, reason="manual")
                flash(f"Backups will also be copied to {folder}. First copy: {made.name}.", "ok")
            else:
                flash("Backups are kept in the data folder only.", "ok")
        elif action == "sdr_path" and desktop:
            value = request.form.get("offgrid_sdr_path", "").strip().strip('"') or None
            if value and not Path(value).expanduser().is_file():
                flash("No file there. Point to OFFgrid-SDR's index.html.", "error")
            else:
                h.save_setting("offgrid_sdr_path", value)
                s.commit()
                flash("Saved the OFFgrid-SDR location.", "ok")
        elif action == "autostart" and desktop:
            from shacklog.services import autostart

            want = request.form.get("enabled") == "1"
            try:
                autostart.set_enabled(want, paths.data_dir)
                flash(f"{APP_NAME} will {'now' if want else 'no longer'} start when you sign in"
                      f"{' (in the background)' if want else ''}.", "ok")
            except OSError as exc:
                flash(f"Couldn't change that: {exc}", "error")
        elif action == "check_update" and desktop:
            from shacklog.services import updates

            info = updates.check_now(paths.data_dir)
            if info.get("error"):
                flash(f"Couldn't check GitHub: {info['error']}", "error")
            elif info.get("behind") == 0:
                flash(f"Up to date (version {__version__}, commit {info.get('current')}).", "ok")
            elif info.get("behind"):
                flash(f"{info['behind']} new change{'s' if info['behind'] != 1 else ''} on GitHub. "
                      "Use “Update now”, or restart ShackLog to update.", "ok")
        elif action == "auto_update":
            from shacklog.services import updates

            want = request.form.get("enabled") == "1"
            if desktop:
                h.save_setting("auto_update", want)
                s.commit()
                flash("ShackLog will " + ("check GitHub at every start and update itself first."
                                          if want else "no longer update by itself at start."), "ok")
            else:
                try:
                    updates.set_hub_auto_install(paths.data_dir, want)
                except OSError as exc:
                    flash(f"Couldn't change that: {exc}", "error")
                    return redirect(url_for(".program"))
                flash("Updates found by the 6-hourly check will " +
                      ("be installed automatically (with rollback if they fail)." if want
                       else "only be shown here."), "ok")
        elif action == "update" and desktop:
            status = system.repo_status()
            if not status.updatable:
                abort(400, description="This copy isn't a git working copy, or git is missing.")
            from shacklog.services import updates

            system.launch_desktop_update(
                status.path, rebuild=bool(updates.exe_outdated(status.path)),
                relaunch_args=current_app.config.get("RELAUNCH_ARGS"))
            return _quit(message="update")
        elif action in system.SERVER_REQUESTS and not desktop:
            if action == "restart" and not system.control_available():
                return _quit(message="restart")
            if not system.control_available():
                abort(400, description="Server controls need the systemd install from deploy/.")
            system.write_server_request(paths.data_dir, action)
            if action == "check":
                flash("Checking GitHub… reload this page in a few seconds.", "ok")
                return redirect(url_for(".program") + "#update")
            return render_template("settings/stopped.html", reason=action)
        else:
            abort(400, description="Unknown action.")
        return redirect(url_for(".program"))

    status = system.repo_status()
    return render_template(
        "settings/program.html", settings_tab="program", desktop=desktop,
        data_dir=str(paths.data_dir), database=str(paths.database), status=status,
        version=__version__, control=system.control_available(),
        control_log=system.last_control_log(paths.data_dir),
        update_info=_update_info(paths.data_dir),
        auto_update=(bool(h.setting("auto_update")) if desktop
                     else _hub_auto_install(paths.data_dir)),
        instance_id=_db().instance_id,
        addresses=system.local_addresses(),
        mirror_dir=h.setting("backup_mirror_dir") or "",
        mirror_keep=h.setting("backup_mirror_keep") or 30,
        sdr_path=h.setting("offgrid_sdr_path") or "",
        sdr_found=_sdr_found(),
        can_quit=desktop and bool(current_app.config.get("SHUTDOWN")),
        os_name=os.name,
        autostart=_autostart_state() if desktop else None,
        in_docker=system.in_docker(), update_script=system.update_script_name(),
    )


def _update_info(data_dir) -> dict:
    from shacklog.services import updates

    return updates.read_status(data_dir)


def _hub_auto_install(data_dir) -> bool:
    from shacklog.services import updates

    return updates.hub_auto_install(data_dir)


def _autostart_state() -> bool | None:
    """Is "start when I sign in" on? None if this system can't tell."""
    from shacklog.services import autostart

    try:
        return autostart.is_enabled() if autostart.supported() else None
    except OSError:
        return None


def _sdr_found() -> str | None:
    try:
        from shacklog.web.views.sdr import find_offgrid_sdr

        found = find_offgrid_sdr()
        return str(found) if found else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Duplicates: find and merge
# ---------------------------------------------------------------------------

@bp.route("/duplicates", methods=["GET", "POST"])
def duplicates():
    from shacklog.models import ENTITIES
    from shacklog.services import merge as merge_svc

    s = h.session()
    if request.method == "POST":
        model = ENTITIES.get(request.form.get("entity", ""))
        keep = request.form.get("keep", "")
        others = [i for i in request.form.getlist("ids") if i and i != keep]
        if model is None or model not in merge_svc.MERGEABLE or not keep or not others:
            flash("Pick the record to keep.", "error")
            return redirect(url_for(".duplicates"))
        from shacklog.services import backup

        backup.create_backup(_db(), _paths().backups, reason="before-merge")
        try:
            result = merge_svc.merge(s, model, keep, others)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for(".duplicates"))
        s.commit()
        flash(f"Merged {result.merged} into one: {result.references} references and "
              f"{result.links} links moved. The merged copies are in the Trash.", "ok")
        return redirect(url_for(".duplicates"))
    groups = merge_svc.find_duplicates(s)
    usage = {r.id: merge_svc.usage_count(s, r) for g in groups for r in g.records}
    return render_template("settings/duplicates.html", settings_tab="duplicates",
                           groups=groups, usage=usage)
