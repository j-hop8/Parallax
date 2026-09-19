# Running tier 1 on an always-on host

Why this exists: CLAUDE.md invariant 1a. On the laptop the crawl produced
**2 complete outlet-days out of 116** (2026-08-11 → 09-17); the rest have
multi-hour sleep gaps, and articles published during a gap scroll out of the
feeds forever. The VPS runs **tier 1, the daily rollup, and Postgres**. The
laptop keeps everything keyword-driven (enrich, dedup, framing, stance, the
UI) and reaches the database through an SSH tunnel that lands on the same
`localhost:5433` the code already defaults to. No code changes for the move.

Target: Ubuntu 24.04 LTS, x86_64, 1 vCPU / 1 GB is plenty (the crawl is
network-bound; Postgres holds ~50 MB). Disk: 10 GB leaves years of headroom.

## 0. Before you start -- the one irreversible moment

There is a window between **stopping the laptop crawl** (step 5) and the
**VPS crawl's first confirmed run** (step 7). Anything published in that
window is lost. Keep it short: do steps 1–4 with the laptop crawl still
running, and only stop it once the VPS is one command away.

## 1. Provision

```bash
# as root on the fresh VPS
adduser parallax && usermod -aG sudo parallax
ufw default deny incoming && ufw allow OpenSSH && ufw enable
apt-get update && apt-get install -y unattended-upgrades make git curl
# docker: https://docs.docker.com/engine/install/ubuntu/  (engine + compose plugin)
usermod -aG docker parallax
```

Log in as `parallax` from here on. Copy your SSH key; disable password
login in `/etc/ssh/sshd_config` (`PasswordAuthentication no`).

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs to ~/.local/bin
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
uv --version && docker compose version
```

## 2. Clone and configure

```bash
git clone https://github.com/j-hop8/Parallax.git ~/parallax && cd ~/parallax
cp ops/env.example .env
openssl rand -hex 24     # paste as PARALLAX_DB_PASSWORD and into PARALLAX_DATABASE_URL
nano .env
```

`.env` needs only the two database lines; the Gemini key stays on the laptop
(tier 2 does not run here).

## 3. Database and dependencies

```bash
make setup                     # fetches config/dict.txt.big, uv sync
make db.up db.migrate          # compose Postgres, schema + migrations
docker compose config | grep -A3 ports   # expect host_ip: 127.0.0.1
uv run python -m parallax.jobs.crawl_listing --outlet cna --dry-run --wait-network 0
```

Expected from the dry run: one line per feed with item counts, no
tracebacks. If `items_seen` is 0 for every feed, check outbound DNS/HTTPS
before going on.

## 4. Rehearse the restore (still nothing stopped on the laptop)

On the laptop:

```bash
make db.dump                                   # backups/parallax-<stamp>.dump
ssh parallax@<vps> mkdir -p parallax/backups    # nothing has created it on the VPS yet
scp backups/parallax-*.dump parallax@<vps>:~/parallax/backups/
```

On the VPS, restore into a **throwaway** project first, so a bad dump
cannot touch the real database:

```bash
PARALLAX_DB_PORT=5434 docker compose -p pxdrill up -d db
make db.restore DC="docker compose -p pxdrill" FILE=backups/parallax-<stamp>.dump   # waits for pg_isready first
# expect: "<N> article_index rows, 16 complete outlet-days" -- same N as the laptop
docker compose -p pxdrill down -v
```

## 5. Stop the laptop crawl

On the laptop:

```bash
make sched.uninstall           # launchd agents removed
launchctl list | grep parallax || echo "clean"
```

The window from §0 is now open.

## 6. Migrate the data for real

On the laptop, a fresh dump so nothing since §4 is missed, then on the VPS
into the real database:

```bash
# laptop
make db.dump && scp backups/parallax-*.dump parallax@<vps>:~/parallax/backups/
# vps
make db.restore FILE=backups/parallax-<newest>.dump
```

## 7. Schedule and confirm

```bash
make sched.install             # sudo once; renders ops/systemd/*, enables 3 timers
systemctl list-timers 'parallax-*' --no-pager
sudo systemctl start parallax-crawl.service     # do not wait for the next slot
journalctl -u parallax-crawl -n 30 --no-pager
```

Expected: a `crawl_runs` line per outlet with `ok=True`. Then `make health`
on the VPS. After ~40 minutes it should show **2 ok runs per outlet and a
largest gap of ~20 min**. The window from §0 is closed.

## 8. Point the laptop at the VPS

```bash
make db.down                                    # frees 5433 locally
ssh -N -L 5433:127.0.0.1:5433 parallax@<vps>    # keep this terminal open (or autossh)
```

Laptop `.env` needs `PARALLAX_DATABASE_URL` with the VPS password (host
stays `127.0.0.1:5433`). Everything that goes through psycopg works
unchanged over the tunnel:

```bash
make report KEYWORD=沈伯洋
make ui
make enrich KEYWORD=…          # tier 2 still runs here; bodies land in the VPS db
```

`make health` is the exception: it runs `docker compose exec`, so it only
works on the host that owns the container. From the laptop:

```bash
ssh parallax@<vps> make -C parallax health
```

## 9. Day-after checks

- `ssh parallax@<vps> make -C parallax health`: every outlet's `largest_gap`
  under an hour, 70+ ok runs.
- `outlet_daily_totals`: the first new `complete = true` rows appear after
  the 00:20 rollup; `make report` rows move from `2 / N 天` to `3 / N 天`.
- `ls backups/` on the VPS: one dump per day from 03:00 Taipei; older than
  14 days are pruned.

## Operating

| Need | Command (on the VPS) |
|---|---|
| Is tier 1 alive? | `make health` (or from the laptop `ssh parallax@<vps> make -C parallax health`) -- `largest_gap` is the number that matters |
| Logs | `journalctl -u parallax-crawl -f`, `journalctl -u parallax-rollup -n 50` |
| Timers | `systemctl list-timers 'parallax-*'` |
| Deploy a change | `git pull && uv sync` -- timers pick it up on the next run; `make sched.install` again only if a unit file changed |
| Rollup now | `sudo systemctl start parallax-rollup.service` |
| Restore a backup | §4's drill, against the real project: `make db.restore FILE=…` (destructive: `--clean`) |
| Postgres shell | `make db.psql` |
| Stop everything | `make sched.uninstall && make db.down` |

Things that will bite:

- **`uv` not on the service PATH** -- `make sched.install` bakes the absolute
  path it found; if you reinstall uv elsewhere, re-run it.
- **Docker group** -- the jobs run as your user and call `docker compose exec`;
  `groups` must show `docker`, and you must log out/in after `usermod`.
- **Host reboot** -- timers are `Persistent=true`; a missed rollup runs at
  boot, the crawl resumes within a minute. Postgres is `restart:
  unless-stopped`, but `docker.service` must be enabled (it is by default).
- **Disk** -- `df -h` monthly. Growth is ~1 MB/day in Postgres plus dumps.
