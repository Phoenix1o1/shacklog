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
    cmd = autostart.launch_command(tmp_path / "data")
    assert cmd[cmd.index("--data-dir") + 1] == str(tmp_path / "data")
    assert cmd[1].endswith("launcher.py")  # from source: works from any folder
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
    monkeypatch.setattr(autostart, "set_enabled", lambda on, data_dir=None: calls.append(on))
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


def test_autostart_leaves_out_the_default_data_folder(monkeypatch, tmp_path):
    from shacklog import config

    monkeypatch.setattr(config, "default_data_dir", lambda: tmp_path / "default")
    assert "--data-dir" not in autostart.launch_command(tmp_path / "default")
    assert "--data-dir" in autostart.launch_command(tmp_path / "elsewhere")
    # Windows command line: a path with spaces is quoted, a trailing backslash can't
    # swallow the closing quote.
    assert autostart._quote_windows(["C:\\Program Files\\ShackLog.exe", "--data-dir",
                                     "D:\\Ham radio\\"]) == \
        '"C:\\Program Files\\ShackLog.exe" --data-dir "D:\\Ham radio\\\\"'


def test_only_terminal_commands_borrow_a_console():
    """Desktop mode (no command, or "run") must not attach to the terminal it was typed
    in: Ctrl+C there, or closing it, would stop the ShackLog in the tray."""
    from shacklog.cli import CONSOLE_WORDS

    assert {"where", "info", "backup", "--version", "-h"} <= CONSOLE_WORDS
    assert "run" not in CONSOLE_WORDS and "--no-browser" not in CONSOLE_WORDS
