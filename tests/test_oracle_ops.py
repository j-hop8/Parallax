"""Oracle Always Free host (T-032): the pieces that keep a reclaimable VM from
costing data. The backup pull must survive the cutover's sched.uninstall, keep
a bounded history, and fail loudly when the VM stops producing dumps; the
firewall target must open the two web ports and nothing else."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
PLIST = ROOT / "ops" / "com.parallax.backup-pull.plist.template"


def test_backup_pull_runs_daily_after_the_vm_dump():
    plist = plistlib.loads(PLIST.read_bytes().replace(b"@@ROOT@@", b"/srv/parallax"))
    assert plist["Label"] == "com.parallax.backup-pull"
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}, "an hour after 03:00"
    assert plist["ProgramArguments"][-1].endswith("make --no-print-directory backup.pull")


def test_sched_install_and_uninstall_never_touch_the_backup_pull_or_tunnel():
    """The cutover runs sched.(un)install on this Mac; the pull and the tunnel
    must outlive it, so neither may be one of the jobs those loops manage."""
    text = MAKEFILE.read_text()
    jobs = re.search(r"^LAUNCHD_JOBS := (.+)$", text, re.MULTILINE).group(1).split()
    assert jobs == ["crawl", "rollup"]
    assert re.search(r"^SCHED_JOBS \?= \$\(LAUNCHD_JOBS\)$", text, re.MULTILINE)
    loops = re.findall(r"^\s*(?:@)?for j in ([^;]+); do", text, re.MULTILINE)
    assert loops and set(loops) <= {"$(LAUNCHD_JOBS)", "$(SCHED_JOBS)"}, loops


TUNNEL = ROOT / "ops" / "com.parallax.tunnel.plist.template"


def test_tunnel_forwards_only_postgres_on_loopback_and_keeps_alive():
    raw = TUNNEL.read_bytes().replace(b"@@ROOT@@", b"/srv/p").replace(b"@@HOST@@", b"parallax@vm")
    plist = plistlib.loads(raw)
    assert plist["Label"] == "com.parallax.tunnel"
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert args[0] == "/usr/bin/ssh" and "-N" in args and args[-1] == "parallax@vm"
    assert args[args.index("-L") + 1] == "127.0.0.1:5433:127.0.0.1:5433"
    assert args.count("-L") == 1 and "-R" not in args and "-D" not in args
    for opt in ("BatchMode=yes", "ExitOnForwardFailure=yes"):
        assert opt in args


def test_db_wait_waits_on_tcp_not_the_init_socket():
    recipe = MAKEFILE.read_text().split("\ndb.wait:", 1)[1].split("\n.PHONY", 1)[0]
    assert "pg_isready -h 127.0.0.1" in recipe


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_unknown_sched_job_is_refused(tmp_path):
    r = subprocess.run(
        ["make", "-f", str(MAKEFILE), "-C", str(tmp_path), "--no-print-directory",
         "sched.install.launchd", "SCHED_JOBS=crawl bogus"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode != 0 and "unknown job 'bogus'" in r.stderr


def test_oci_firewall_opens_only_the_web_ports():
    rule = re.search(r"^OCI_RULE := (.+)$", MAKEFILE.read_text(), re.MULTILINE).group(1)
    assert re.search(r"--dports 80,443\b", rule)
    assert rule.startswith("INPUT ") and rule.endswith("-j ACCEPT")
    assert rule.count("--dport") == 1


# ---- the recipe itself, with rsync stubbed ---------------------------------


def _run_pull(
    tmp_path: Path, remote: Path, host: str | None = "parallax@203.0.113.5", env_file: str = ""
) -> subprocess.CompletedProcess:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    rsync = stub / "rsync"
    # Stand-in for `rsync -a --ignore-existing ... HOST:path/ DEST/`: copy the
    # "remote" dumps into the last argument, keeping mtimes like -a does.
    # Also records the remote spec (second-to-last argument) for host assertions.
    rsync.write_text(
        f'#!/bin/sh\nfor d; do src="$prev"; prev="$d"; done\n'
        f'echo "$src" > "{tmp_path}/rsync-src"\ncp -p "{remote}"/*.dump "$d"\n'
    )
    rsync.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / ".env").write_text(env_file)
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["make", "-f", str(MAKEFILE), "-C", str(work), "--no-print-directory", "backup.pull"]
        + ([f"HOST={host}"] if host else []),
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def _dumps(remote: Path, n: int, newest_age_h: float) -> list[Path]:
    remote.mkdir(exist_ok=True)
    now = time.time()
    out = []
    for i in range(n):
        f = remote / f"parallax-202609{i:02d}T190000Z.dump"
        f.write_bytes(b"PGDMP")
        age = (newest_age_h + (n - 1 - i) * 24) * 3600
        os.utime(f, (now - age, now - age))
        out.append(f)
    return out


needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


@needs_make
def test_pull_keeps_the_newest_thirty(tmp_path):
    remote = tmp_path / "remote"
    dumps = _dumps(remote, 32, newest_age_h=1)
    r = _run_pull(tmp_path, remote)
    assert r.returncode == 0, r.stderr
    kept = sorted(p.name for p in (tmp_path / "work" / "backups" / "vm").iterdir())
    assert kept == sorted(p.name for p in dumps[-30:])
    assert "ok: 30 dumps" in r.stdout


@needs_make
def test_pull_fails_loudly_when_the_vm_stopped_dumping(tmp_path):
    remote = tmp_path / "remote"
    _dumps(remote, 3, newest_age_h=40)
    r = _run_pull(tmp_path, remote)
    assert r.returncode != 0
    assert "STALE" in r.stderr


@needs_make
def test_pull_without_a_host_refuses(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = subprocess.run(
        ["make", "-f", str(MAKEFILE), "-C", str(work), "--no-print-directory", "backup.pull"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode != 0 and "PARALLAX_VM_HOST" in r.stderr


@needs_make
@pytest.mark.parametrize(
    ("env_file", "expected"),
    [
        ("PARALLAX_VM_HOST=parallax@198.51.100.7\n", "parallax@198.51.100.7"),
        ("PARALLAX_BACKUP_HOST=parallax@198.51.100.8\n", "parallax@198.51.100.8"),
        ("PARALLAX_BACKUP_HOST=old@x\nPARALLAX_VM_HOST=parallax@198.51.100.9\n",
         "parallax@198.51.100.9"),
    ],
)
def test_pull_reads_the_vm_host_from_env(tmp_path, env_file, expected):
    remote = tmp_path / "remote"
    _dumps(remote, 1, newest_age_h=1)
    r = _run_pull(tmp_path, remote, host=None, env_file=env_file)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "rsync-src").read_text().strip() == f"{expected}:parallax/backups/"
