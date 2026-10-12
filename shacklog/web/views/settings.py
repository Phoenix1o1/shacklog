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
from sqlalchemy import func, literal, select, union_all

from shacklog import __version__, migrations
from shacklog.models import (
    ENTITIES,
    SYNC_ORDER,
    TABLES,
    ImportLog,
    Link,
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
        min_password=MIN_PASSWORD_LENGTH,
    )


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
        elif action == "import_path" and current_app.config["MODE"] == "desktop":
            result = _import_local_path(request.form.get("path", ""))
            if result is not None:
                return result
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
    return render_template(
        "settings/data.html", backups=backup.list_backups(_paths().backups), history=history,
        desktop=current_app.config["MODE"] == "desktop", local_bundles=_local_bundles(),
        backups_dir=str(_paths().backups),
        keep_daily=h.setting("backup_keep_daily"), keep_other=h.setting("backup_keep_import"),
    )


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


def _local_bundles() -> list[str]:
    """Bundles waiting next to the program (e.g. ShackLog-import-*.zip in the working copy),
    offered as one-click imports in desktop mode."""
    from shacklog.services.system import repo_root

    if current_app.config["MODE"] != "desktop":
        return []
    found: list[str] = []
    for folder in filter(None, [repo_root(), Path.home() / "Downloads"]):
        try:
            found += [str(p) for p in sorted(folder.glob("ShackLog-*.zip"))]
        except OSError:
            continue
    return found


def _import_local_path(raw: str):
    """Desktop only: import a file already on this computer, by path. Browsers can't send
    very large files reliably, and a path avoids copying a big bundle twice."""
    from shacklog.services import bundle

    path = Path(raw.strip().strip('"')).expanduser()
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
        elif action == "check_update":
            status = system.repo_status(check_remote=True)
            if status.error:
                flash(f"Couldn't reach GitHub: {status.error}", "error")
            elif status.behind == 0:
                flash(f"Up to date (version {__version__}, commit {status.commit}).", "ok")
            elif status.behind:
                flash(f"{status.behind} new change{'s' if status.behind != 1 else ''} on GitHub. "
                      "Use “Update now”.", "ok")
        elif action == "update" and desktop:
            status = system.repo_status()
            if not status.updatable:
                abort(400, description="This copy isn't a git working copy, or git is missing.")
            system.launch_desktop_update(status.path)
            return _quit(message="update")
        elif action in system.SERVER_REQUESTS and not desktop:
            if action == "restart" and not system.control_available():
                return _quit(message="restart")
            if not system.control_available():
                abort(400, description="Server controls need the systemd install from deploy/.")
            system.write_server_request(paths.data_dir, action)
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
        mirror_dir=h.setting("backup_mirror_dir") or "",
        mirror_keep=h.setting("backup_mirror_keep") or 30,
        sdr_path=h.setting("offgrid_sdr_path") or "",
        sdr_found=_sdr_found(),
        can_quit=desktop and bool(current_app.config.get("SHUTDOWN")),
        os_name=os.name,
    )


def _sdr_found() -> str | None:
    try:
        from shacklog.web.views.sdr import find_offgrid_sdr

        found = find_offgrid_sdr()
        return str(found) if found else None
    except Exception:
        return None
