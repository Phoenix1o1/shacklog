"""Flask application factory.

``create_app`` wires the database, file store and every blueprint listed in
:data:`VIEW_MODULES`. To add a section: create ``shacklog/web/views/<name>.py`` exposing a
Blueprint called ``bp``, then add ``"<name>"`` to the list.
"""

from __future__ import annotations

import importlib
import logging
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, url_for

from shacklog import APP_NAME, __version__
from shacklog import models
from shacklog.config import Paths, load_secret_key
from shacklog.db import Database
from shacklog.services import bands
from shacklog.services.filestore import FileStore
from shacklog.web import auth, helpers as h

log = logging.getLogger(__name__)

#: Blueprint modules under shacklog/web/views, in menu order.
VIEW_MODULES = [
    "main",
    "qsos",
    "swl",
    "journal",
    "repeaters",
    "radio",
    "stations",
    "equipment",
    "contacts",
    "files",
    "links",
    "search",
    "map",
    "stats",
    "settings",
    "api_sync",
]

#: Endpoints allowed before first-run setup is complete.
SETUP_EXEMPT = {"main.setup", "auth.login", "auth.logout", "static", "health"}


def create_app(database: Database, paths: Paths, mode: str = "desktop",
               setup_code: str | None = None, modules: list[str] | None = None) -> Flask:
    """Build the web app. ``mode`` is "desktop" (localhost) or "server" (network).

    ``modules`` overrides :data:`VIEW_MODULES` (tests use it to load a subset).
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        DB=database,
        PATHS=paths,
        FILESTORE=FileStore(paths.files),
        MODE=mode,
        SETUP_CODE=setup_code,
        SECRET_KEY=load_secret_key(paths),
        SESSION_COOKIE_NAME="shacklog_session",
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=2 * 1024 ** 3,  # 2 GB uploads (manuals, recordings)
        SYNC_STATUS={},  # filled by the sync worker, shown in the footer
    )

    h.register_template_helpers(app)
    app.teardown_appcontext(h.close_session)
    app.register_blueprint(auth.bp)
    for name in (VIEW_MODULES if modules is None else modules):
        module = importlib.import_module(f"shacklog.web.views.{name}")
        app.register_blueprint(module.bp)

    # Jinja *globals* are visible inside imported macro files too (context-processor
    # values are not), so everything macros need lives here.
    app.jinja_env.globals.update(
        app_name=APP_NAME,
        app_version=__version__,
        csrf_token=auth.csrf_token,
        request=request,
        M=models,  # choice lists in templates: M.SWL_CATEGORIES …
        BAND_NAMES=bands.BAND_NAMES,
        MODE_CHOICES=bands.MODE_CHOICES,
    )

    @app.context_processor
    def inject_per_request():
        return {
            "instance_name": database.instance_name,
            "mode": mode,
            "login_enabled": auth.login_required_now(),
            "sync_status": app.config["SYNC_STATUS"],
        }

    @app.before_request
    def require_setup():
        if request.endpoint in SETUP_EXEMPT or request.blueprint in auth.TOKEN_BLUEPRINTS:
            return None
        if not h.setting("setup_done"):
            return redirect(url_for("main.setup"))
        return None

    @app.route("/healthz")
    def health():
        """Liveness probe; the desktop launcher also uses it to find a running copy."""
        return jsonify(app=APP_NAME, version=__version__, instance=database.instance_id,
                       database=str(paths.database) if request.remote_addr in (
                           "127.0.0.1", "::1") else None)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               message="That page or record doesn't exist here."), 404

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("error.html", code=400, message=e.description), 400

    @app.errorhandler(500)
    def server_error(_e):
        log.exception("Unhandled error on %s", request.path)
        return render_template("error.html", code=500,
                               message="Something broke. The details are in the log file "
                                       f"({paths.log_file.name} in the data folder)."), 500

    return app
