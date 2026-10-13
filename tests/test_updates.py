"""Automatic updates (desktop at launch, hub status file) and the instance card."""

import json
import subprocess

import pytest

from shacklog.services import system, updates
from tests.conftest import csrf


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """An 'origin' with one commit more than the local clone."""
    origin, local = tmp_path / "origin", tmp_path / "local"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one")
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    _git(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "two: new feature")
    monkeypatch.setattr(system, "repo_root", lambda: local)
    launched = []
    monkeypatch.setattr(system, "launch_desktop_update", launched.append)
    return origin, local, launched


def test_update_at_launch_runs_once_per_version(repos, tmp_path):
    origin, local, launched = repos
    data = tmp_path / "data"
    assert updates.update_at_launch(data, enabled=False) is False
    assert updates.update_at_launch(data, enabled=True) is True and launched == [local]
    info = updates.read_status(data)
    assert info["behind"] == 1 and "two: new feature" in info["log"][0]
    # The update didn't happen (still behind): no second attempt for the same version.
    assert updates.update_at_launch(data, enabled=True) is False and len(launched) == 1


def test_update_at_launch_offline_starts_normally(repos, tmp_path):
    origin, local, launched = repos
    _git(local, "remote", "set-url", "origin", str(tmp_path / "nowhere"))
    assert updates.update_at_launch(tmp_path / "data", enabled=True) is False and not launched
    assert updates.read_status(tmp_path / "data")["error"]


def test_instance_card_and_update_badge(client, app):
    page = client.get("/").text
    assert "instance-card" in page and "Update available" not in page
    updates.write_status(app.config["PATHS"].data_dir, behind=2, current="aaa", latest="bbb",
                         log=["bbbbbbb Something new"])
    page = client.get("/settings/program").text
    assert "Update available" in page and "2 new changes" in page and "Something new" in page
    assert 'id="instance"' in page


def test_desktop_auto_update_toggle(client):
    r = client.post("/settings/program", data={"_csrf": csrf(client), "action": "auto_update", "enabled": "0"})
    assert r.status_code == 302
    assert "Update automatically at start" in client.get("/settings/program").text


def test_hub_check_and_auto_install_flag(tmp_path, monkeypatch):
    from tests.conftest import build_app

    app, _db, paths = build_app(tmp_path / "srv", mode="server")
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["logged_in"] = True
    monkeypatch.setenv("SHACKLOG_CONTROL", "systemd")
    r = c.post("/settings/program", data={"_csrf": csrf(c), "action": "check"})
    assert r.status_code == 302
    assert system.request_file(paths.data_dir).read_text().strip() == "check"
    c.post("/settings/program", data={"_csrf": csrf(c), "action": "auto_update", "enabled": "1"})
    assert updates.hub_auto_install(paths.data_dir)
    assert "Turn off automatic install" in c.get("/settings/program").text
    c.post("/settings/program", data={"_csrf": csrf(c), "action": "auto_update", "enabled": "0"})
    assert not updates.hub_auto_install(paths.data_dir)
