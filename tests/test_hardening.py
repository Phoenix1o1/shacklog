"""Guard rails found in the 1.1.0 review: file names, URLs, redirects, proxies."""

import pytest

from shacklog.services import markdown_render, sync_engine
from shacklog.services.filestore import FileStore
from shacklog.web.helpers import f_safe_url
from tests.conftest import csrf


def test_filestore_refuses_paths(tmp_path):
    store = FileStore(tmp_path / "files")
    assert not store.exists("/etc/hostname") and not store.exists("../../x")
    with pytest.raises(ValueError):
        store.path_for("../" + "a" * 61)
    assert store.path_for("a" * 64).name == "a" * 64


def test_synced_file_with_a_path_as_hash_is_not_applied(s):
    from shacklog.models import StoredFile, SyncConflict

    stats = sync_engine.apply_records(
        s, [{"table": "files", "row": {"id": "f-1", "sha256": "/etc/hostname", "filename": "x"}}],
        "peer", "Peer", mode="import")
    assert stats["errors"] == 1 and s.get(StoredFile, "f-1") is None
    assert s.query(SyncConflict).count() == 1


def test_urls_are_web_only(client):
    assert f_safe_url("javascript:alert(1)") == "#" and f_safe_url("https://x.org") == "https://x.org"
    html = markdown_render.render("![a](javascript:alert(1)) ![b](https://x.org/a.png) [c](//evil.org)")
    assert "javascript:" not in html and 'src="https://x.org/a.png"' in html
    assert 'href="//evil.org"' not in html
    r = client.post("/equipment/new", data={"_csrf": csrf(client), "kind": "radio", "name": "Rig",
                                            "status": "active", "url": "javascript:alert(1)"})
    assert r.status_code == 422 and "https://" in r.text


def test_login_never_redirects_off_site(tmp_path):
    from tests.conftest import build_app

    app, _db, _paths = build_app(tmp_path / "srv", mode="server")
    c = app.test_client()
    with app.app_context():
        from werkzeug.security import generate_password_hash

        with _db.transaction() as s:
            from shacklog.db import set_setting

            set_setting(s, "password_hash", generate_password_hash("correct horse"))
    for target in ("//evil.example/x", "/\\evil.example", "https://evil.example"):
        c.get("/login")
        r = c.post(f"/login?next={target}", data={"_csrf": csrf(c), "password": "correct horse"})
        assert r.status_code == 302 and "evil" not in r.headers["Location"]
