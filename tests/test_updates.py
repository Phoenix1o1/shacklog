"""Automatic updates (desktop at launch, hub status file) and the instance card."""

import hashlib
import subprocess
from pathlib import Path

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
    launched = []  # (repo, keyword arguments) of each update started
    monkeypatch.setattr(system, "launch_desktop_update",
                        lambda repo, **kw: launched.append((repo, kw)))
    return origin, local, launched


def test_update_at_launch_runs_once_per_version(repos, tmp_path):
    origin, local, launched = repos
    data = tmp_path / "data"
    assert updates.update_at_launch(data, enabled=False) is False
    assert updates.update_at_launch(data, enabled=True, relaunch_args=["--data-dir", "X"])
    assert launched == [(local, {"rebuild": False, "relaunch_args": ["--data-dir", "X"]})]
    info = updates.read_status(data)
    assert info["behind"] == 1 and "two: new feature" in info["log"][0]
    # The update didn't happen (still behind): no second attempt for the same version.
    assert updates.update_at_launch(data, enabled=True) is False and len(launched) == 1


def test_stale_executable_is_rebuilt_once(repos, tmp_path, monkeypatch):
    """Pulled but not rebuilt (tests or build failed): git is current, the exe isn't."""
    origin, local, launched = repos
    _git(local, "pull", "-q")
    data = tmp_path / "data"
    monkeypatch.setattr(system, "build_commit", lambda: "")  # not a built exe
    assert updates.update_at_launch(data, enabled=True) is False and not launched
    monkeypatch.setattr(system, "build_commit", lambda: "0" * 40)  # built from another commit
    assert updates.exe_outdated(local)
    assert updates.update_at_launch(data, enabled=True) is True
    assert launched[0][1]["rebuild"] is True
    # Same guard as a normal update: no loop if the rebuild fails too.
    assert updates.update_at_launch(data, enabled=True) is False and len(launched) == 1
    head = subprocess.run(["git", "-C", str(local), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    monkeypatch.setattr(system, "build_commit", lambda: head)
    assert updates.exe_outdated(local) == ""


def test_helper_runs_from_a_copy_with_a_clean_environment(tmp_path, monkeypatch):
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging" / "update_unix.sh").write_text("echo hi\n")
    copy = system._helper_copy(tmp_path, "update_unix.sh")
    assert copy == tmp_path / "dist" / "running-update_unix.sh" and copy.read_text() == "echo hi\n"
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "C:/temp/_MEI1234")
    monkeypatch.setenv("_MEIPASS2", "C:/temp/_MEI1234")
    env = system.helper_environment(["--data-dir", "D:/Ham Radio/ShackLog"])
    assert "_PYI_APPLICATION_HOME_DIR" not in env and "_MEIPASS2" not in env
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert env["SHACKLOG_RELAUNCH_ARGS"] == '--data-dir "D:/Ham Radio/ShackLog"'
    assert "SHACKLOG_RELAUNCH_ARGS" not in system.helper_environment()
    assert system.update_wait_pid() > 0


def test_update_script_prefix_is_frozen():
    """The first 49 lines of update_windows.bat (up to and including ``git pull``) must
    never change: an older copy of the script that pulls a new one goes on reading the new
    file at the same byte offset. Change only what comes after the git pull block."""
    text = (Path(__file__).parents[1] / "packaging" / "update_windows.bat").read_text("ascii")
    prefix = "\n".join(text.splitlines()[:49])  # line endings don't matter to the check
    assert prefix.splitlines()[-5].startswith("git pull --ff-only")
    assert hashlib.sha256(prefix.encode()).hexdigest() == (
        "477a4d358554856d9986ba56b9441e39bb80ac97f47bb62dc38ec7c978150c4c")


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


def test_update_panel_fits_the_platform(client, tmp_path, monkeypatch):
    """Update now names this OS's helper; a Docker hub is told to rebuild its image."""
    from tests.conftest import build_app

    monkeypatch.setattr(system, "repo_status", lambda **kw: system.RepoStatus(
        path=tmp_path, git=True, branch="main", commit="abc1234"))
    page = client.get("/settings/program").text
    assert system.update_script_name() in page
    assert ("update_unix.sh" in page) != (system.os.name == "nt")
    monkeypatch.setattr(system, "repo_status", lambda **kw: system.RepoStatus(path=None, git=False))
    monkeypatch.setenv("SHACKLOG_RUNTIME", "docker")
    app, _db, _paths = build_app(tmp_path / "srv", mode="server")
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["logged_in"] = True
    page = c.get("/settings/program").text
    assert "docker compose -f deploy/docker/docker-compose.yml up -d --build" in page
    assert "Download the" not in page and "restart: unless-stopped" in page


def test_git_errors_are_not_shown_as_values(monkeypatch, client):
    """The hub's web service can't read root's checkout ("dubious ownership"): the page
    must say so once, not print the error as the commit and the branch."""
    message = "fatal: detected dubious ownership in repository at '/opt/shacklog'"
    monkeypatch.setattr(system, "repo_root", lambda: system.Path("/opt/shacklog"))
    monkeypatch.setattr(system, "git_executable", lambda: "git")
    monkeypatch.setattr(system, "_git", lambda repo, *args, **kw: (128, message + "\nTo add an exception…"))
    status = system.repo_status()
    assert (status.branch, status.commit, status.commit_date) == ("", "", "")
    assert status.error == message
    page = client.get("/settings/program").text
    assert page.count("dubious ownership") == 1 and "Git can't read this copy" in page


def test_git_trusts_the_program_folder(tmp_path, monkeypatch):
    """Every git call names the repository as a safe directory (hub: root-owned code)."""
    seen = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(system, "git_executable", lambda: "git")
    monkeypatch.setattr(system.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or Done())
    system._git(tmp_path, "status")
    assert seen[0][1:3] == ["-c", f"safe.directory={tmp_path.resolve().as_posix()}"]
