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
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


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


def _sched(tmp_path, target, *extra):
    """Run a launchd sched target against a throwaway HOME, with launchctl,
    plutil, uv and crontab stubbed: launchctl calls are logged, nothing real
    is loaded and the user's crontab is never read."""
    home, stubs, work = tmp_path / "home", tmp_path / "bin", tmp_path / "work"
    for d in (home, stubs, work):
        d.mkdir(exist_ok=True)
    (work / "ops").mkdir(exist_ok=True)
    for j in ("crawl", "rollup"):
        src = ROOT / "ops" / f"com.parallax.{j}.plist.template"
        (work / "ops" / src.name).write_bytes(src.read_bytes())
    log = tmp_path / "launchctl.log"
    for name, body in {
        "launchctl": f'echo "$*" >> "{log}"',
        "plutil": "exit 0",
        "uv": "exit 0",
        "crontab": "exit 1",
    }.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{stubs}:{os.environ['PATH']}"}
    r = subprocess.run(
        ["make", "-f", str(MAKEFILE), "-C", str(work), "--no-print-directory", target, *extra],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    agents = home / "Library" / "LaunchAgents"
    calls = log.read_text().splitlines() if log.exists() else []
    return r, agents, calls


@needs_make
def test_sched_jobs_crawl_installs_only_the_crawl_and_unloads_a_stale_rollup(tmp_path):
    agents = tmp_path / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.parallax.rollup.plist").write_text("left from the single-host setup")
    r, agents, calls = _sched(tmp_path, "sched.install.launchd", "SCHED_JOBS=crawl")
    assert r.returncode == 0, r.stderr
    assert (agents / "com.parallax.crawl.plist").exists()
    assert not (agents / "com.parallax.rollup.plist").exists()
    assert any(c.startswith("load ") and "com.parallax.crawl.plist" in c for c in calls)
    assert not any(c.startswith("load ") and "rollup" in c for c in calls)
    assert any(c.startswith("unload ") and "com.parallax.rollup.plist" in c for c in calls)


@needs_make
def test_default_sched_jobs_still_installs_crawl_and_rollup(tmp_path):
    r, agents, _ = _sched(tmp_path, "sched.install.launchd")
    assert r.returncode == 0, r.stderr
    assert {p.name for p in agents.iterdir()} == {
        "com.parallax.crawl.plist", "com.parallax.rollup.plist"
    }


@needs_make
def test_empty_sched_jobs_is_refused_before_anything_is_unloaded(tmp_path):
    r, _, calls = _sched(tmp_path, "sched.install.launchd", "SCHED_JOBS=")
    assert r.returncode != 0 and "SCHED_JOBS is empty" in r.stderr
    assert calls == []


@needs_make
def test_sched_uninstall_removes_whatever_is_installed(tmp_path):
    r, agents, _ = _sched(tmp_path, "sched.install.launchd", "SCHED_JOBS=crawl")
    assert r.returncode == 0, r.stderr
    (agents / "com.parallax.tunnel.plist").write_text("must survive")
    r, agents, _ = _sched(tmp_path, "sched.uninstall.launchd")
    assert r.returncode == 0, r.stderr
    assert {p.name for p in agents.iterdir()} == {"com.parallax.tunnel.plist"}


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
        # A blank new name must not shadow the old one.
        ("PARALLAX_VM_HOST=\nPARALLAX_BACKUP_HOST=parallax@198.51.100.10\n",
         "parallax@198.51.100.10"),
    ],
)
def test_pull_reads_the_vm_host_from_env(tmp_path, env_file, expected):
    remote = tmp_path / "remote"
    _dumps(remote, 1, newest_age_h=1)
    r = _run_pull(tmp_path, remote, host=None, env_file=env_file)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "rsync-src").read_text().strip() == f"{expected}:parallax/backups/"
