"""Input checks of the Proxmox container creator (deploy/proxmox/validate.sh)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

VALIDATE = Path(__file__).parents[1] / "deploy" / "proxmox" / "validate.sh"
# Linux-only scripts (they run on the Proxmox host). On Windows "bash" is often the WSL
# launcher with no Linux installed; and the PC updater runs these tests before every
# rebuild, so they must never fail there for that reason.
pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                                reason="Linux shell scripts")


def run(script: str) -> str:
    done = subprocess.run(["bash", "-c", f'source "{VALIDATE.as_posix()}"\n{script}'],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def check(fn: str, *args: str) -> str:
    quoted = " ".join("'" + a + "'" for a in args)
    return run(f"{fn} {quoted}")


@pytest.mark.parametrize("value", ["192.168.1.50/24", "10.0.0.5/8", "172.16.4.1/16", "10.1.2.3/32"])
def test_good_addresses(value):
    assert check("check_cidr", value) == ""


@pytest.mark.parametrize("value, says", [
    ("192.168.1.50", "prefix length"),
    ("192.168.1.256/24", "isn't an IPv4"),
    ("192.168.01.5/24", "isn't an IPv4"),
    ("192.168.1/24", "isn't an IPv4"),
    ("192.168.1.50/7", "8-32"),
    ("192.168.1.50/abc", "8-32"),
    ("192.168.1.0/24", "network address"),
    ("192.168.1.255/24", "broadcast"),
    ("127.0.0.5/8", "loopback"),
    ("230.1.1.1/24", "multicast"),
])
def test_bad_addresses(value, says):
    assert says in check("check_cidr", value)


@pytest.mark.parametrize("gateway, says", [
    ("192.168.1.1", ""), ("", "is needed"), ("192.168.1.254", ""),
    ("192.168.2.1", "isn't in"), ("192.168.1.50", "own address"),
    ("192.168.1.255", "broadcast"), ("router", "isn't an IPv4"),
])
def test_gateway(gateway, says):
    result = check("check_gateway", gateway, "192.168.1.50/24")
    assert (says in result) if says else result == ""


def test_other_values():
    assert check("check_hostname", "shacklog-hub") == ""
    assert "hyphens" in check("check_hostname", "shack_log")
    assert check("check_vlan", "") == "" and "4094" in check("check_vlan", "4095")
    assert check("check_port", "8573") == "" and "1024" in check("check_port", "80")
    assert check("check_time", "03:30") == "" and "HH:MM" in check("check_time", "24:00")
    assert "from 1 to 4" in check("check_cores", "8", "4")
    assert check("check_disk", "8") == "" and "whole number" in check("check_ram", "lots")


def test_ask_repeats_until_valid_and_explains():
    """A fake whiptail answers "abc" then "192.168.1.50/24": the second box shows why."""
    out = run(r'''
ANSWERS=$(mktemp); PROMPTS=$(mktemp)
printf '%s\n' "abc" " 192.168.1.50/24 " > "$ANSWERS"
# runs in a subshell (inside $(...)): keep the state in files
wt() { printf '%s\n' "$*" >> "$PROMPTS"; head -1 "$ANSWERS"; sed -i 1d "$ANSWERS"; }
value=$(ask "Static IP" "Address with prefix:" "" check_cidr)
echo "value=$value"
grep -c -- "--inputbox" "$PROMPTS"
grep -q "✗ Add the prefix length" "$PROMPTS" && echo explained
''').splitlines()
    assert out == ["value=192.168.1.50/24", "2", "explained"]


def test_ask_cancel_returns_1():
    assert run('wt() { return 1; }; ask T Q "" check_cidr && echo kept || echo cancelled') == "cancelled"


def test_restore_archive(tmp_path):
    import tarfile

    good = tmp_path / "shacklog-data-1.tar.gz"
    with tarfile.open(good, "w:gz") as tar:
        tar.add(VALIDATE, arcname="x")
    (tmp_path / "bad.tar.gz").write_bytes(b"not gzip")
    assert check("check_archive", good.as_posix()) == ""
    assert "No file" in check("check_archive", (tmp_path / "missing.tar.gz").as_posix())
    assert "readable" in check("check_archive", (tmp_path / "bad.tar.gz").as_posix())
    assert ".tar.gz" in check("check_archive", VALIDATE.as_posix())


def test_host_shacklog_ctl_finds_the_tagged_container(tmp_path):
    """On the Proxmox host, shacklog-ctl forwards to the container tagged "shacklog"."""
    fake = tmp_path / "pct"
    fake.write_text("""#!/bin/bash
case "$1" in
  list) printf 'VMID Status Lock Name\\n101 running pihole\\n105 running shacklog\\n';;
  config) [ "$2" = 105 ] && echo "tags: radio;shacklog" || echo "tags: dns";;
  status) echo "status: running";;
  exec) echo "ct=$2 cmd=${*:4}";;
esac
""")
    fake.chmod(0o755)
    host = VALIDATE.parent / "shacklog-ctl-host"

    def ctl(*args):
        done = subprocess.run(["bash", str(host), *args], capture_output=True, text=True,
                              env={"PATH": f"{tmp_path}:/usr/bin:/bin"}, timeout=30)
        return done.returncode, (done.stdout + done.stderr).strip()

    if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() != "0":
        assert "Run as root" in ctl("status")[1]
        return
    assert ctl("status") == (0, "ct=105 cmd=shacklog-ctl status")
    assert ctl("--ct", "101", "logs", "-f") == (0, "ct=101 cmd=shacklog-ctl logs -f")
    assert "isn't a container ID" in ctl("--ct", "x", "status")[1]
