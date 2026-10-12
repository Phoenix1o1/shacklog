"""Dashboard, first-run setup and saved filters."""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func

from shacklog.models import (
    JournalEntry,
    Qso,
    Repeater,
    SavedFilter,
    Station,
    StoredFile,
    SwlEntry,
    utcnow,
)
from shacklog.services import geo
from shacklog.web import helpers as h
from shacklog.web.auth import MIN_PASSWORD_LENGTH, hash_password

bp = Blueprint("main", __name__)
h.register_entity_view("saved_filter", "main.dashboard")


@bp.route("/")
def dashboard():
    s = h.session()
    year_start = datetime(utcnow().year, 1, 1)
    tally = {
        "qsos": h.live(Qso).count(),
        "qsos_year": h.live(Qso).filter(Qso.time_on >= year_start).count(),
        "calls": s.query(func.count(func.distinct(Qso.call))).filter(Qso.deleted_at.is_(None)).scalar(),
        "swl": h.live(SwlEntry).count(),
        "journal": h.live(JournalEntry).count(),
        "repeaters": h.live(Repeater).count(),
        "files": h.live(StoredFile).count(),
    }
    return render_template(
        "main/dashboard.html", tally=tally,
        recent_qsos=h.live(Qso).order_by(Qso.time_on.desc()).limit(8).all(),
        recent_swl=h.live(SwlEntry).order_by(SwlEntry.time_utc.desc()).limit(5).all(),
        recent_journal=h.live(JournalEntry).order_by(JournalEntry.time_utc.desc()).limit(5).all(),
        stations=h.live(Station).order_by(Station.kind, Station.name).all(),
        default_station=h.default_station(s),
    )


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------

def _station_from(name: str, kind: str, callsign: str, position_text: str) -> Station | None:
    if not name and not position_text:
        return None
    station = Station(name=name or kind.title(), kind=kind, callsign=callsign or None)
    if position_text:
        if geo.is_valid_locator(position_text):
            station.locator = geo.normalize_locator(position_text)
            station.latitude, station.longitude = geo.locator_to_latlon(station.locator)
        else:
            pos = geo.parse_position(position_text)
            if pos is None:
                raise ValueError(position_text)
            station.latitude, station.longitude = pos
            station.locator = geo.latlon_to_locator(*pos, precision=6)
    return station


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    s = h.session()
    if h.setting("setup_done"):
        return redirect(url_for(".dashboard"))
    mode = current_app.config["MODE"]
    form = request.form
    errors: list[str] = []
    if request.method == "POST" and form.get("setup_kind") == "join":
        errors = _join_hub(form)
        if not errors:
            return redirect(url_for(".dashboard"))
    elif request.method == "POST":
        callsign = form.get("callsign", "").replace(" ", "").upper()
        try:
            home = _station_from(form.get("home_name", "").strip(), "home", callsign,
                                 form.get("home_position", "").strip())
            fixed = _station_from(form.get("fixed_name", "").strip(), "fixed", callsign,
                                  form.get("fixed_position", "").strip())
        except ValueError as exc:
            errors.append(f"Couldn't read the position “{exc}”. Use a locator like JN45ol "
                          "or coordinates like 45.464, 9.190.")
            home = fixed = None
        password = form.get("password", "")
        if password and len(password) < MIN_PASSWORD_LENGTH:
            errors.append(f"The password needs at least {MIN_PASSWORD_LENGTH} characters.")
        if not errors:
            if form.get("instance_name", "").strip():
                h.database().rename_instance(form["instance_name"])
            for st in (home, fixed):
                if st is not None:
                    s.add(st)
            if home is not None:
                h.save_setting("default_station_id", home.id)
                h.save_setting("reference_station_id", home.id)
            if password:
                h.save_setting("password_hash", hash_password(password))
                h.save_setting("require_login_local", bool(form.get("require_login_local")))
            h.save_setting("setup_done", True)
            s.commit()
            flash("All set. Add more stations, equipment and repeaters whenever you like.", "ok")
            return redirect(url_for(".dashboard"))
    return render_template("main/setup.html", errors=errors, form=form, mode=mode,
                           instance_name=h.database().instance_name)


def _join_hub(form) -> list[str]:
    """First run on another computer: connect to the hub and pull everything.

    Creating stations here instead would give the hub a second "Home" every time a new
    instance is set up, so a joining instance takes its stations from the hub.
    """
    from shacklog.services.sync_client import SyncClient, SyncError, normalize_hub_url

    s = h.session()
    url = normalize_hub_url(form.get("hub_url"))
    token = form.get("hub_token", "").strip()
    password = form.get("password", "")
    if not url or not token:
        return ["Enter the hub address and the sync token shown on the hub (Settings › Sync)."]
    if password and len(password) < MIN_PASSWORD_LENGTH:
        return [f"The password needs at least {MIN_PASSWORD_LENGTH} characters."]
    if form.get("instance_name", "").strip():
        h.database().rename_instance(form["instance_name"])
    h.save_setting("sync_hub_url", url)
    h.save_setting("sync_client_token", token)
    h.save_setting("sync_interval_min", 15)
    h.save_setting("sync_pull_files", True)
    s.commit()  # the sync client reads the settings in its own session
    client = SyncClient.from_settings(h.database(), h.filestore())
    try:
        stats = client.run()
    except SyncError as exc:
        return [f"Couldn't sync with the hub: {exc}"]
    home = (h.live(Station).order_by((Station.kind != "home"), Station.created_at).first())
    if home is not None:
        h.save_setting("default_station_id", home.id)
        h.save_setting("reference_station_id", home.id)
    if password:
        h.save_setting("password_hash", hash_password(password))
        h.save_setting("require_login_local", bool(form.get("require_login_local")))
    h.save_setting("setup_done", True)
    s.commit()
    flash(f"Joined the hub and copied {stats.get('pulled', 0)} records. This computer now syncs every "
          "15 minutes (Settings › Sync).", "ok")
    return []


# ---------------------------------------------------------------------------
# Saved filters (any list page)
# ---------------------------------------------------------------------------

def _back() -> str:
    back = request.form.get("back") or url_for(".dashboard")
    return back if back.startswith("/") else url_for(".dashboard")


@bp.route("/filters/save", methods=["POST"])
def save_filter():
    name = request.form.get("name", "").strip()
    if name:
        h.session().add(SavedFilter(name=name, target=request.form.get("target", ""),
                                    query=request.form.get("query", "")))
        h.session().commit()
        flash(f"Saved filter “{name}”.", "ok")
    return redirect(_back())


@bp.route("/filters/<record_id>/delete", methods=["POST"])
def delete_saved_filter(record_id: str):
    sf = h.get_or_404(SavedFilter, record_id)
    sf.soft_delete()
    h.session().commit()
    flash(f"Removed saved filter “{sf.name}”.", "ok")
    return redirect(_back())
