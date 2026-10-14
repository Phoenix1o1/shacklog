"""``GET /api/ha/state``: the station's state for Home Assistant (services/ha.py).

No login: each request carries ``Authorization: Bearer <Home Assistant token>``, a
read-only token generated in Settings › General (separate from the sync token, which
could change the logbook). Answers 401 for a wrong or missing token and 403 when no
token has been generated yet.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from shacklog import APP_NAME, __version__
from shacklog.models import utcnow
from shacklog.services import ha
from shacklog.web import helpers as h

bp = Blueprint("api_ha", __name__, url_prefix="/api/ha")


def _error(status: int, message: str):
    response = jsonify(error=message)
    response.status_code = status
    return response


@bp.before_request
def authenticate():
    stored = h.setting("ha_token_hash")
    if not stored:
        return _error(403, "Home Assistant access is off. Generate a token in "
                           "Settings › General › Home Assistant.")
    header = request.headers.get("Authorization", "")
    sent = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not ha.token_ok(sent, stored):
        return _error(401, "Wrong or missing token. Copy it again into Home Assistant's "
                           "secrets.yaml as: shacklog_token: \"Bearer <token>\".")
    return None


@bp.route("/state")
def state():
    from shacklog.services import backup, updates

    s = h.session()
    paths = current_app.config["PATHS"]
    hub = current_app.config["MODE"] == "server"
    db = h.database()
    response = jsonify(ha.state(
        s, now=utcnow(),
        instance={"app": APP_NAME, "version": __version__, "name": db.instance_name,
                  "role": "hub" if hub else "pc"},
        station=h.default_station(s), backups=backup.list_backups(paths.backups),
        sync_status=current_app.config.get("SYNC_STATUS"), hub=hub,
        update_behind=updates.read_status(paths.data_dir).get("behind")))
    response.headers["Cache-Control"] = "no-store"
    return response
