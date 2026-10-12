"""Shared pytest fixtures. See CONVENTIONS.md §5."""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
from flask import Blueprint

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shacklog.config import Paths  # noqa: E402
from shacklog.db import Database, set_setting  # noqa: E402
from shacklog.models import Station  # noqa: E402
from shacklog.services import geo  # noqa: E402
from shacklog.services.filestore import FileStore  # noqa: E402
from shacklog.web import VIEW_MODULES, create_app  # noqa: E402

#: Endpoints a stub blueprint must expose when its real module isn't written yet.
STUB_ENDPOINTS = {
    "swl": ["index", "new", "view"],
    "journal": ["index", "new", "view"],
    "repeaters": ["index", "new", "view", "import_chirp", "export_chirp"],
    "radio": ["index", "new", "view"],
    "stations": ["index", "new", "view"],
    "equipment": ["index", "new", "view"],
    "contacts": ["index", "view", "callsign"],
    "files": ["index", "upload", "view", "download"],
    "search": ["index"],
    "map": ["index"],
    "stats": ["index"],
    "settings": ["index", "sync", "data", "conflicts", "trash"],
    "api_sync": ["hello"],
}
_RULES = {"view": "/<record_id>", "download": "/<record_id>/download", "callsign": "/call/<call>"}


def _available_modules() -> tuple[list[str], list[str]]:
    present, missing = [], []
    for name in VIEW_MODULES:
        try:
            importlib.import_module(f"shacklog.web.views.{name}")
            present.append(name)
        except ModuleNotFoundError as exc:
            if exc.name == f"shacklog.web.views.{name}":
                missing.append(name)
            else:
                raise
    return present, missing


def build_app(data_dir: Path, mode: str = "desktop", setup: bool = True):
    """App with a fresh database; optionally with first-run setup already done."""
    paths = Paths(data_dir).ensure()
    db = Database(paths.database).init()
    present, missing = _available_modules()
    app = create_app(db, paths, mode, modules=present)
    for name in missing:
        bp = Blueprint(name, __name__, url_prefix=f"/{name}")
        for ep in STUB_ENDPOINTS.get(name, ["index"]):
            bp.add_url_rule(_RULES.get(ep, f"/{ep}"), ep, lambda **_kw: "stub")
        app.register_blueprint(bp)
    app.config["TESTING"] = True
    if setup:
        with db.transaction() as s:
            home = Station(name="Milano", kind="home", callsign="IU2TST", locator="JN45ol")
            home.latitude, home.longitude = geo.locator_to_latlon("JN45ol")
            fixed = Station(name="Grenoble", kind="fixed", callsign="F/IU2TST", locator="JN25vd")
            fixed.latitude, fixed.longitude = geo.locator_to_latlon("JN25vd")
            s.add_all([home, fixed])
            s.flush()
            set_setting(s, "default_station_id", home.id)
            set_setting(s, "reference_station_id", home.id)
            set_setting(s, "setup_done", True)
    return app, db, paths


@pytest.fixture
def app(tmp_path):
    app, _db, _paths = build_app(tmp_path / "data")
    yield app
    app.config["DB"].dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    return app.config["DB"]


@pytest.fixture
def s(db):
    session = db.session()
    yield session
    session.close()


@pytest.fixture
def filestore(app) -> FileStore:
    return app.config["FILESTORE"]


def csrf(client) -> str:
    """A valid CSRF token for this client's session."""
    with client.session_transaction() as sess:
        token = sess.get("_csrf")
        if not token:
            token = "test-token"
            sess["_csrf"] = token
    return token


def extract_csrf(html: str) -> str:
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
