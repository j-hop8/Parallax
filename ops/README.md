# Running tier 1 on an always-on host

Why this exists: CLAUDE.md invariant 1a. On the laptop the crawl produced
**2 complete outlet-days out of 116** (2026-08-11 → 09-17); the rest have
multi-hour sleep gaps, and articles published during a gap scroll out of the
feeds forever. The VPS runs **tier 1, the daily rollup, and Postgres**. The
laptop keeps everything keyword-driven (enrich, dedup, framing, stance, the
UI) and reaches the database through an SSH tunnel that lands on the same
`localhost:5433` the code already defaults to. No code changes for the move.

Target: Ubuntu 24.04 LTS, x86_64 or arm64 (`make ops.check` on an Apple
Silicon Mac verifies the arm64 build, which is what Oracle's A1 runs). For tier 1 alone 1 vCPU / 1 GB is plenty
(the crawl is network-bound; Postgres holds ~50 MB). With the public demo page
(§10) take **2 GB or more**: the page holds jieba's dictionary (~300 MB RSS
measured) next to the crawl, which holds its own; 4 GB also leaves room for
Phase 2's Elasticsearch without moving hosts. Disk: 10 GB leaves years of
headroom. Pick a provider whose IPv4 stays fixed for the server's life -- the
demo URL is derived from it.

## 0. Before you start -- the one irreversible moment

There is a window between **stopping the laptop crawl** (step 5) and the
**VPS crawl's first confirmed run** (step 7). Anything published in that
window is lost. Keep it short: do steps 1–4 with the laptop crawl still
running, and only stop it once the VPS is one command away.

## Oracle Cloud Always Free -- read this first if that is the host

The chosen host (2026-09-26) is Oracle's Always Free tier in Tokyo: an Ampere
A1 (arm64) VM, **2 OCPU / 12 GB** total, 200 GB of block storage, 10 TB/month
outbound, $0. It changes a few steps below, and it brings one risk the other
hosts do not.

**The risk: Oracle reclaims idle Always Free instances.** Idle means, over 7
days, CPU 95th percentile < 20% **and** network < 20% **and** memory < 20%
(the memory test applies to A1 only). A metadata crawl passes the first two
easily, so the instance is only safe while memory stays at or above 20%.
What guards against it and against losing the VM for any other reason:

1. **Pay As You Go** (recommended): upgrade the account and set a **US$1
   budget alert** at once. Staying inside the Always Free limits still costs
   $0. Community reports say PAYG accounts are not idle-reclaimed and get A1
   capacity more easily; Oracle's docs do not say either way, so the next
   three guards do not depend on it.
2. **Watch memory after day one:** console → the instance → Metrics →
   *Memory Utilization*. At 12 GB, crawl + Postgres + the demo page may sit
   below 20%. If it does and the account is not PAYG, shrink the instance to
   **1 OCPU / 6 GB** (Edit → shape; a reboot) -- the same workload is then
   above the bar. No synthetic load: faking CPU use games the policy.
3. **A heartbeat** (T-033, `PARALLAX_HEARTBEAT_URL`): a stopped, reclaimed or
   broken crawl emails you within ~40 minutes.
4. **Off-host backups** (§11): the workstation pulls the VM's dumps daily, so
   losing the VM never loses the denominator.

**Creating it:**

- Sign up with home region **Japan East (Tokyo)**. Always Free resources
  exist only in the home region, and it cannot be changed later.
- Instance: shape `VM.Standard.A1.Flex`, 2 OCPU / 12 GB, image *Canonical
  Ubuntu 24.04* (aarch64), boot volume 50 GB, your SSH public key. "Out of
  host capacity" is common: retry later (Tokyo has one availability domain).
- Networking → the VCN's default Security List → add ingress TCP **80** and
  **443** from `0.0.0.0/0`. 22 is there already.
- The public IP is the instance's for its lifetime (kept across stop/start,
  released only on terminate) -- the sslip.io URL is built from it.

**How §1 and §10 differ on this image:**

- You log in as `ubuntu` (passwordless sudo), not root: prefix §1's
  commands with `sudo`. `adduser parallax` asks for a password -- set one;
  `sched.install` and `demo.install` use sudo as that user. Then give it
  your key: `sudo mkdir -p ~parallax/.ssh && sudo cp ~/.ssh/authorized_keys
  ~parallax/.ssh/ && sudo chown -R parallax: ~parallax/.ssh`.
- **Skip every `ufw` line.** The image ships its own iptables rules
  (`/etc/iptables/rules.v4`, netfilter-persistent) that reject everything but
  SSH; ufw on top of them opens nothing. `sudo iptables -L INPUT -n` shows
  the final `REJECT`. In §10, run `make oci.firewall` instead of `ufw allow`.
- Restarts need nobody: systemd timers and Docker start at boot without a
  login, and `Persistent=true` runs a missed crawl slot at once. Keep
  unattended-upgrades' default `Unattended-Upgrade::Automatic-Reboot "false"`
  (`/etc/apt/apt.conf.d/50unattended-upgrades`): security fixes install
  without a reboot. Once a month, if `/var/run/reboot-required` exists,
  `sudo reboot` while you can watch `make health` afterwards.

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
make setup.crawl               # fetches config/dict.txt.big, base deps only
make db.up db.migrate          # compose Postgres, schema + migrations
docker compose config | grep -A3 ports   # expect host_ip: 127.0.0.1
uv run python -m parallax.jobs.crawl_listing --dry-run --wait-network 0
```

Expected from the dry run: a line per outlet -- **all eight** -- with item
counts and no tracebacks. Run every outlet, not one: some sites treat
datacenter address ranges differently from a home connection, and the time
to find out is before the laptop crawl stops. If `items_seen` is 0 for every
feed, check outbound DNS/HTTPS; if it is 0 for one outlet only, that outlet
is refusing this host -- stop and decide before going on.

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
largest gap of ~10 min**. The window from §0 is closed.

`make health` also prints a Threads block (T-018). On the token-less
host it shows the runs made from the laptop, as of the last restore.

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
- Oracle: the instance's *Memory Utilization* metric (Oracle block, guard 2).

## 10. Public demo page (optional, any time after §7)

The Streamlit page, read-only, at `https://<ip-with-dashes>.sslip.io` -- no
domain needed; sslip.io resolves the name to the IP inside it, and Caddy gets
a Let's Encrypt certificate for it. The page runs as its own unit
(`ops/demo/parallax-ui.service`), never installed by `sched.install`, and is
built so it cannot hurt the crawl: a 768 MB memory cap, a positive OOM score,
the `parallax_ro` database role (read-only transactions, 15 s statement
timeout, 20 connections at most -- `db/migrations/004`), and a sandbox that
hides `.env` (the owner password) and the Docker socket from it (T-034).

```bash
# Caddy, from its official apt repository (https://caddyserver.com/docs/install#debian-ubuntu-raspbian)
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy

sudo ufw allow 80,443/tcp      # 80 for the certificate challenge and the https redirect
# Oracle image instead of the line above: make oci.firewall  (see the Oracle block)

# the hostname: this server's IPv4 with dashes
echo "PARALLAX_PUBLIC_HOST=$(curl -4s https://ifconfig.me | tr . -).sslip.io" >> .env

make setup.demo                # the ui extra on top of setup.crawl
make demo.install              # migrate, rotate the parallax_ro password, unit + Caddyfile, start
```

Expected: `demo page: https://<host>`. Then:

```bash
systemctl status parallax-ui --no-pager
curl -s https://<host>/_stcore/health      # ok
systemctl show parallax-ui -p MemoryCurrent
# the sandbox, seen from inside the page's own mount namespace -- both must fail:
PID=$(systemctl show -p MainPID --value parallax-ui)
sudo nsenter -t "$PID" -m cat "$PWD/.env"          # Permission denied
sudo nsenter -t "$PID" -m test -r /run/docker.sock || echo "docker socket hidden"
sudo systemctl stop parallax-ui && make health && sudo systemctl start parallax-ui
```

The last line is the isolation check: with the page stopped, the crawl's
next slot still runs on time. Run tier 2 from the laptop (§8) on a few
keywords so the page has something to show; the page itself never writes.

Moving to a real domain later: point an A record at the server, set
`PARALLAX_PUBLIC_HOST` to it, `make demo.install` again.

## 11. Off-host backups (the workstation pulls them)

The VM's daily dump lives on the VM's own disk. If the VM goes -- reclaimed,
terminated, a lost account -- that copy goes with it, and tier-1 rows cannot
be re-fetched. So the workstation (this Mac) pulls every dump daily at
04:00, an hour after the VM writes it, over the SSH key §8 already uses.

```bash
# workstation .env
PARALLAX_BACKUP_HOST=parallax@<vps-ip>

make backup.pull               # once by hand: proves ssh + rsync work non-interactively
make backup.pull.install       # then daily via launchd -> logs/backup-pull.log
```

It keeps the newest 30 in `backups/vm/`, and exits non-zero -- a `STALE`
line in the log -- when the newest dump is over 36 hours old: the VM's backup
timer, or the VM, has stopped. `sched.uninstall` (§5) leaves it alone; remove
it with `make backup.pull.uninstall`. Rehearse a restore from it with §4's
throwaway `pxdrill` project now and then.

## Operating

| Need | Command (on the VPS) |
|---|---|
| Is tier 1 alive? | `make health` (or from the laptop `ssh parallax@<vps> make -C parallax health`) -- `largest_gap` is the number that matters |
| Logs | `journalctl -u parallax-crawl -f`, `journalctl -u parallax-rollup -n 50` |
| Timers | `systemctl list-timers 'parallax-*'` |
| Deploy a change | `git pull && uv sync` -- timers pick it up on the next run; `make sched.install` again only if a unit file changed. **With the demo page (§10):** `git pull && uv sync --extra ui && sudo systemctl restart parallax-ui` -- a bare `uv sync` is exact and removes streamlit |
| Is the page up? | `systemctl status parallax-ui`, `journalctl -u parallax-ui -n 50`, `journalctl -u caddy -n 50` |
| Rollup now | `sudo systemctl start parallax-rollup.service` |
| Restore a backup | §4's drill, against the real project: `make db.restore FILE=…` (destructive: `--clean`) |
| Postgres shell | `make db.psql` |
| Stop the page only | `make demo.uninstall` -- the crawl is untouched; `sudo systemctl stop caddy` closes 443 |
| Stop everything | `make demo.uninstall; make sched.uninstall && make db.down` |

Things that will bite:

- **`uv` not on the service PATH** -- `make sched.install` bakes the absolute
  path it found; if you reinstall uv elsewhere, re-run it.
- **Docker group** -- the jobs run as your user and call `docker compose exec`;
  `groups` must show `docker`, and you must log out/in after `usermod`.
- **Host reboot** -- timers are `Persistent=true`; a missed rollup runs at
  boot, the crawl resumes within a minute. Postgres is `restart:
  unless-stopped`, but `docker.service` must be enabled (it is by default).
- **Disk** -- `df -h` monthly. Growth is ~1 MB/day in Postgres plus dumps.
