"""Navigation, theme switch, help pages, start-at-sign-in and the tray icon helpers."""

import sys

from shacklog.services import autostart
from tests.conftest import csrf


def test_sidebar_navigation_and_theme_toggle(client):
    page = client.get("/qsos/").text
    assert 'id="sidenav"' in page and "data-nav-toggle" in page and "data-theme-toggle" in page
    # the current section is marked in the side menu
    assert 'href="/qsos/" aria-current="page"' in page
    assert 'href="/help/"' in page


def test_help_pages(client):
    guide = client.get("/help/")
    assert guide.status_code == 200 and "Duplicates" in guide.text and "<table>" in guide.text
    credits = client.get("/help/credits").text
    assert "OFFgrid-SDR" in credits and "diddy-boy" in credits and "Jacobo" in credits
    assert client.get("/help/nope").status_code == 404


def test_autostart_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert "--no-browser" in autostart.launch_command()
    assert "Exec=" in autostart.linux_entry()
    assert "<key>RunAtLoad</key>" in autostart.mac_entry()
    if sys.platform.startswith("linux"):
        assert not autostart.is_enabled()
        autostart.set_enabled(True)
        assert autostart.is_enabled() and autostart.linux_file().exists()
        autostart.set_enabled(False)
        assert not autostart.is_enabled()


def test_program_page_autostart_toggle(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(autostart, "is_enabled", lambda: calls[-1] if calls else False)
    calls = []
    monkeypatch.setattr(autostart, "set_enabled", calls.append)
    assert "Start when I sign in" in client.get("/settings/program").text
    r = client.post("/settings/program", data={"_csrf": csrf(client), "action": "autostart",
                                                "enabled": "1"})
    assert r.status_code == 302 and calls == [True]
    assert "Don’t start when I sign in" in client.get("/settings/program").text


def test_tray_icon_image():
    import pytest

    pytest.importorskip("PIL")
    from shacklog import tray

    img = tray.icon_image(32)
    assert img.size == (32, 32) and img.getpixel((16, 2))[3] == 255
