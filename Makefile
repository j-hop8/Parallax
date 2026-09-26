DC := docker compose
PSQL := $(DC) exec -T db psql -U parallax -d parallax

.PHONY: help
help:
	@echo "saturation feed-window pressure, ARGS=\"--days 30\" for a longer window"
	@echo "setup      full workstation: deps + llm + ui extras + dictionary"
	@echo "setup.crawl  crawl host only: base deps + dictionary, no model or UI"
	@echo "db.up      start Postgres (docker compose)"
	@echo "db.migrate apply db/schema.sql (idempotent)"
	@echo "db.dump    pg_dump -Fc into backups/ (the migration + the daily VPS backup)"
	@echo "db.restore restore a dump: make db.restore FILE=backups/parallax-….dump"
	@echo "audit      T-002: probe outlet feeds + robots.txt  [no DB needed]"
	@echo "crawl      run the tier-1 listing crawl once"
	@echo "crawl.one  run one outlet, e.g. make crawl.one OUTLET=cna"
	@echo "health     per-outlet crawl health for the last 24h"
	@echo "social     fetch Threads posts for a keyword, e.g. make social KEYWORD=沈伯洋"
	@echo "threads.refresh  print a refreshed Threads token and expiry"
	@echo "enrich     tier-2 body fetch for a keyword, e.g. make enrich KEYWORD=沈伯洋"
	@echo "reextract  re-run body extraction over the raw HTML cache (no fetch) after a parser fix"
	@echo "stance     classify a keyword's enriched articles (Q1), ARGS=--dry-run to count first"
	@echo "stance.posts classify a keyword's Threads posts (Q4), ARGS=--dry-run to count first"
	@echo "label      hand-label a keyword's articles into eval/stance_gold.csv (blind)"
	@echo "posts.eval   score post stance against the post gold set (ARGS=--classify spends quota)"
	@echo "stance.eval  score the classifier against the gold set (ARGS=--classify spends quota)"
	@echo "gold.remap  re-key eval/stance_gold.csv to a rebuilt db by URL (ARGS=--write)"
	@echo "label.validate  relabel another annotator's rows blind, to measure agreement"
	@echo "stance.agreement  pairwise annotator kappa; no database, no quota"
	@echo "dedup      rebuild near-duplicate clusters + propagation order (Q3); ARGS=--keyword X narrows"
	@echo "label.pairs  hand-label candidate pairs into eval/dup_gold.csv (blind)"
	@echo "dedup.eval   precision/recall of the clusterer against the pair gold set"
	@echo "framing    per-cluster shared core + what each member added/dropped; ARGS=--summarize spends quota"
	@echo "report     Q1-Q4 for one keyword as text, e.g. make report KEYWORD=沈伯洋 ARGS=\"--since 2026-08-20\""
	@echo "ui         the same page in a browser (Streamlit, http://localhost:8501)"
	@echo "test       pytest"
	@echo "sched.install  schedule crawl+rollup on this host: launchd on macOS, systemd on Linux"
	@echo "ops.check  verify the systemd units + the Linux runtime in Docker (no VPS needed)"
	@echo "setup.demo   demo host: crawl deps + the ui extra"
	@echo "demo.install  public demo page on this Linux host: Streamlit + Caddy (ops/README.md §10)"
	@echo "demo.uninstall  remove the demo page unit; the crawl is untouched"
	@echo "oci.firewall  Oracle VM only: open 80/443 in the image's own iptables rules"
	@echo "backup.pull  copy the VM's dumps here, HOST=parallax@<ip> or PARALLAX_BACKUP_HOST in .env"
	@echo "backup.pull.install  pull daily at 04:00 via launchd (macOS); independent of sched.install"

# The full workstation: crawl, classify (llm) and the Streamlit page (ui).
# `nlp` is deliberately NOT here -- it drags in torch, and nothing in the
# current pipeline uses it.
.PHONY: setup
setup: dict
	uv sync --extra llm --extra ui

# The always-on crawl host (ops/README.md) runs tier 1: no model key lives
# there. Base deps only -- but still the dictionary, because the listing crawl
# segments every title it stores and a wrong dictionary silently degrades
# search and dedup. The public demo page is opt-in on top (setup.demo below).
.PHONY: setup.crawl
setup.crawl: dict
	uv sync

# jieba's PyPI wheel omits the traditional-Chinese dictionary; without it
# segmentation falls back to a simplified-oriented one and search quality drops.
dict:
	@test -f config/dict.txt.big || curl -sSL -o config/dict.txt.big \
		https://raw.githubusercontent.com/fxsjy/jieba/master/extra_dict/dict.txt.big
	@echo "dict.txt.big ready ($$(wc -l < config/dict.txt.big | tr -d ' ') entries)"

.PHONY: resegment
resegment:
	uv run python -m parallax.jobs.resegment

.PHONY: db.up
db.up:
	$(DC) up -d db

.PHONY: db.down
db.down:
	$(DC) down

# Compose reports healthy via pg_isready; block until then so `make db.up db.migrate`
# in one line does not race the container's startup.
.PHONY: db.wait
db.wait:
	@echo "waiting for postgres..."
	@for i in $$(seq 1 30); do \
		$(DC) exec -T db pg_isready -U parallax -d parallax >/dev/null 2>&1 && echo "ready" && exit 0; \
		sleep 2; \
	done; echo "timed out waiting for postgres" >&2; exit 1

# schema.sql is CREATE TABLE IF NOT EXISTS, so on an EXISTING database it adds
# nothing -- new columns arrive only through db/migrations/. Running the schema
# alone left such a database without outlet_daily_totals.complete, and the rollup
# then failed on a column that appeared to exist in the committed schema.
# Migrations are applied in filename order after it; each must be idempotent.
# SQL is piped over stdin rather than read from a bind mount. Docker Desktop on
# macOS silently presents an unshared bind path as an EMPTY directory instead of
# failing, so `-f /schema/schema.sql` died with "No such file or directory" even
# though `docker inspect` showed the mount. stdin depends on nothing but the host
# filesystem.
.PHONY: db.migrate
db.migrate: db.wait
	@echo "applying db/schema.sql"
	@$(PSQL) -v ON_ERROR_STOP=1 -q < db/schema.sql
	@for f in $$(ls db/migrations/*.sql 2>/dev/null | sort); do \
		echo "applying $$f"; \
		$(PSQL) -v ON_ERROR_STOP=1 -q < "$$f" || exit 1; \
	done
	@echo "schema + migrations applied"

.PHONY: db.psql
db.psql:
	$(DC) exec -it db psql -U parallax -d parallax

.PHONY: audit
audit:
	uv run python scripts/audit_feeds.py

.PHONY: crawl
crawl:
	uv run python -m parallax.jobs.crawl_listing

.PHONY: crawl.one
crawl.one:
	uv run python -m parallax.jobs.crawl_listing --outlet $(OUTLET) --verbose

.PHONY: rollup
rollup:
	uv run python -m parallax.jobs.rollup_daily

# ---- tier 2 + Q1 ---------------------------------------------------------
# All keyword-scoped: nothing here runs over the whole index. `stance` and
# `stance.eval --classify` spend API quota; everything else is local.
.PHONY: social
social:
	uv run python -m parallax.jobs.social --keyword "$(KEYWORD)" $(ARGS)

.PHONY: threads.refresh
threads.refresh:
	uv run python -m parallax.jobs.social --refresh-token

.PHONY: enrich
enrich:
	uv run python -m parallax.jobs.enrich --keyword "$(KEYWORD)" $(ARGS)

# The raw HTML cache exists so a parser fix never re-hits an outlet; this is
# how the fix reaches every stored body. Follow with `make dedup && make framing`.
.PHONY: reextract
reextract:
	uv run python -m parallax.jobs.reextract $(ARGS)

.PHONY: stance
stance:
	uv run python -m parallax.jobs.stance --keyword "$(KEYWORD)" $(ARGS)

# Q4. Same quota as `stance` and the same cache-once rule, over posts instead
# of articles; needs `make social KEYWORD=...` to have run first.
.PHONY: stance.posts
stance.posts:
	uv run python -m parallax.jobs.stance_social --keyword "$(KEYWORD)" $(ARGS)

.PHONY: label
label:
	uv run python scripts/label_stance.py --keyword "$(KEYWORD)" $(ARGS)

# T-020. Every gold row in this repo was written by claude-opus-5, so the F1
# measures two models agreeing. These two close that gap: relabel a stratified
# sample of someone else's rows blind, then compare with kappa.
# Re-key the stance gold set after a restore or re-crawl: ids belong to the
# database, the labelled URL does not. Dry run unless ARGS=--write.
.PHONY: gold.remap
gold.remap:
	uv run python scripts/remap_gold.py $(ARGS)

.PHONY: label.validate
label.validate:
	uv run python scripts/label_stance.py --keyword "$(KEYWORD)" --validate $(ARGS)

.PHONY: stance.agreement
stance.agreement:
	uv run python -m parallax.jobs.eval_stance --agreement $(ARGS)

.PHONY: label.posts
label.posts:
	uv run python scripts/label_posts.py --keyword "$(KEYWORD)" $(ARGS)

.PHONY: posts.eval
posts.eval:
	uv run python -m parallax.jobs.eval_posts $(ARGS)

.PHONY: stance.eval
stance.eval:
	uv run python -m parallax.jobs.eval_stance $(ARGS)

# ---- Q3: dedup -----------------------------------------------------------
.PHONY: dedup
dedup:
	uv run python -m parallax.jobs.dedup $(ARGS)

.PHONY: label.pairs
label.pairs:
	uv run python scripts/label_pairs.py $(ARGS)

.PHONY: dedup.eval
dedup.eval:
	uv run python -m parallax.jobs.eval_dedup $(ARGS)

# Runs after dedup. Deterministic and local unless ARGS=--summarize, which is
# the one model call in Q3: one line per member with a non-empty delta, cached
# on the row until its deltas change.
.PHONY: framing
framing:
	uv run python -m parallax.jobs.framing $(ARGS)

# ---- Q1-Q3 in one page: the incident report --------------------------------
# Reads only. Weights appear solely for outlet-days the rollup marked complete,
# so run `make rollup` first if the denominator looks stale.
.PHONY: report
report:
	uv run python -m parallax.jobs.report --keyword "$(KEYWORD)" $(ARGS)

# Same report object, rendered. The `ui` extra pulls in streamlit and its
# pandas/pyarrow tail, so it stays optional and is installed on first use.
.PHONY: ui
ui:
	uv run --extra ui streamlit run src/parallax/ui/app.py

# The query that answers "is tier-1 still working?". A zero or a stale last_run
# here means data is being lost right now and cannot be backfilled.
.PHONY: health
health:
	@$(PSQL) -c "\
	WITH r AS ( \
	  SELECT outlet, started_at, \
	         started_at - lag(started_at) OVER (PARTITION BY outlet ORDER BY started_at) AS gap \
	  FROM crawl_runs WHERE ok AND started_at > now() - interval '24 hours') \
	SELECT outlet, \
	       to_char(max(started_at) AT TIME ZONE 'Asia/Taipei','MM-DD HH24:MI') AS last_ok, \
	       count(*) AS ok_runs, \
	       coalesce(max(gap), interval '0') AS largest_gap \
	FROM r GROUP BY outlet ORDER BY largest_gap DESC NULLS LAST;"
	@echo "-- largest_gap is the number that matters: the crawl runs every 10 min, so"
	@echo "-- anything past ~1h is coverage this project can never get back."
	@$(PSQL) -tc "SELECT count(*) FILTER (WHERE NOT ok) || ' failed runs in 24h' FROM crawl_runs WHERE started_at > now() - interval '24 hours';"
	@uv run python -m parallax.jobs.social --status

# One entry point per host kind. macOS: launchd (re-runs a job missed during
# sleep). Linux: systemd timers with Persistent=true, the same property. The
# recipes below are unchanged from before the split; only the dispatch is new.
UNAME := $(shell uname -s)

.PHONY: sched.install
sched.install:
ifeq ($(UNAME),Darwin)
	@$(MAKE) --no-print-directory sched.install.launchd
else
	@$(MAKE) --no-print-directory sched.install.systemd
endif

.PHONY: sched.uninstall
sched.uninstall:
ifeq ($(UNAME),Darwin)
	@$(MAKE) --no-print-directory sched.uninstall.launchd
else
	@$(MAKE) --no-print-directory sched.uninstall.systemd
endif

# Install the launchd agents and remove the cron entries, so the two can never
# double-run.
.PHONY: sched.install.launchd
sched.install.launchd:
	@mkdir -p ~/Library/LaunchAgents logs
	@UV=$$(command -v uv); \
	if [ -z "$$UV" ]; then \
		echo "uv not found on PATH -- refusing to write a plist that cannot run" >&2; \
		exit 1; \
	fi; \
	for j in crawl rollup; do \
		sed -e "s#@@ROOT@@#$(CURDIR)#g" -e "s#@@UV@@#$$UV#g" \
			ops/com.parallax.$$j.plist.template > ~/Library/LaunchAgents/com.parallax.$$j.plist; \
		plutil -lint ~/Library/LaunchAgents/com.parallax.$$j.plist >/dev/null || exit 1; \
		launchctl unload ~/Library/LaunchAgents/com.parallax.$$j.plist 2>/dev/null || true; \
		launchctl load ~/Library/LaunchAgents/com.parallax.$$j.plist || exit 1; \
		echo "loaded com.parallax.$$j"; \
	done
	@# Editing the user's crontab is destructive, so: only touch it when a
	@# parallax entry actually exists, back it up first, and never write from a
	@# failed read -- piping the output of a failed `crontab -l` would install an
	@# EMPTY crontab and silently destroy unrelated jobs.
	@if crontab -l > /tmp/parallax-crontab.current 2>/dev/null; then \
		if grep -q 'parallax.jobs' /tmp/parallax-crontab.current; then \
			cp /tmp/parallax-crontab.current $$HOME/.parallax-crontab.backup; \
			grep -v -e 'parallax.jobs' -e 'Parallax tier-1' -e 'permanently unrecoverable' \
				-e 'Daily totals (Asia/Taipei' /tmp/parallax-crontab.current | crontab -; \
			echo "removed parallax cron entries (backup: ~/.parallax-crontab.backup)"; \
		else \
			echo "no parallax cron entries present; crontab left untouched"; \
		fi; \
	else \
		echo "no crontab for this user; nothing to remove"; \
	fi
	@rm -f /tmp/parallax-crontab.current
	@launchctl list | grep parallax || true

.PHONY: sched.uninstall.launchd
sched.uninstall.launchd:
	@for j in crawl rollup; do \
		launchctl unload ~/Library/LaunchAgents/com.parallax.$$j.plist 2>/dev/null || true; \
		rm -f ~/Library/LaunchAgents/com.parallax.$$j.plist; \
	done
	@echo "launchd agents removed"

# Linux (the VPS). Renders ops/systemd/* with absolute paths and the invoking
# user, installs them system-wide (sudo for the copy and systemctl only; the
# jobs themselves run as $(id -un), who must be in the docker group), and
# enables the three timers. Idempotent: re-run after `git pull` if a unit
# changed. See ops/README.md for the cutover order.
SYSTEMD_DIR := /etc/systemd/system
SYSTEMD_UNITS := $(notdir $(wildcard ops/systemd/parallax-*.service ops/systemd/parallax-*.timer))
SYSTEMD_TIMERS := parallax-crawl.timer parallax-rollup.timer parallax-backup.timer

.PHONY: sched.install.systemd
sched.install.systemd:
	@UV=$$(command -v uv); \
	if [ -z "$$UV" ]; then \
		echo "uv not found on PATH -- refusing to install a unit that cannot run" >&2; \
		exit 1; \
	fi; \
	mkdir -p backups; \
	for u in $(SYSTEMD_UNITS); do \
		sed -e "s#@@ROOT@@#$(CURDIR)#g" -e "s#@@UV@@#$$UV#g" -e "s#@@USER@@#$$(id -un)#g" \
			ops/systemd/$$u | sudo tee $(SYSTEMD_DIR)/$$u >/dev/null || exit 1; \
		echo "installed $(SYSTEMD_DIR)/$$u"; \
	done
	@sudo systemctl daemon-reload
	@sudo systemctl enable --now $(SYSTEMD_TIMERS)
	@systemctl list-timers 'parallax-*' --no-pager

.PHONY: sched.uninstall.systemd
sched.uninstall.systemd:
	@sudo systemctl disable --now $(SYSTEMD_TIMERS) 2>/dev/null || true
	@for u in $(SYSTEMD_UNITS); do sudo rm -f $(SYSTEMD_DIR)/$$u; done
	@sudo systemctl daemon-reload
	@echo "systemd units removed"

# ---- public demo page (T-029) ------------------------------------------------
# The Streamlit page on the crawl host, behind Caddy, as its own unit: kept out
# of sched.install so scheduling the crawl never installs a web server, and
# capped (ops/demo/parallax-ui.service) so the page can never starve the crawl.
# Linux only. Needs Caddy installed and PARALLAX_PUBLIC_HOST in .env
# (ops/README.md §10). Re-runnable: every run rotates the parallax_ro password,
# rewrites .env.ui and restarts the page with it.
# The port is read back from compose, not assumed: PARALLAX_DB_PORT may be set
# in .env, which compose reads and this shell does not.
DEMO_UNIT := parallax-ui.service

# `uv sync` alone is exact and would remove streamlit again; on the demo host
# every deploy syncs with the extra (ops/README.md, "Deploy a change").
.PHONY: setup.demo
setup.demo: dict
	uv sync --extra ui

.PHONY: demo.install
demo.install: db.migrate
	@test "$$(uname -s)" = Linux || { echo "demo.install is Linux-only (systemd + Caddy)" >&2; exit 1; }
	@HOST=$$(sed -n 's/^PARALLAX_PUBLIC_HOST=//p' .env 2>/dev/null | tr -d "\"' "); \
	test -n "$$HOST" || { echo "set PARALLAX_PUBLIC_HOST in .env, e.g. 203-0-113-5.sslip.io" >&2; exit 1; }; \
	.venv/bin/python -c "import streamlit" 2>/dev/null \
		|| { echo "no streamlit in .venv -- run make setup.demo first" >&2; exit 1; }; \
	command -v caddy >/dev/null || { echo "caddy not installed -- see ops/README.md §10" >&2; exit 1; }; \
	PORT=$$($(DC) port db 5432 | head -1 | sed 's/.*://'); \
	test -n "$$PORT" || { echo "cannot resolve the published Postgres port (make db.up?)" >&2; exit 1; }; \
	chmod 600 .env || exit 1; \
	PW=$$(openssl rand -hex 24); \
	printf "ALTER ROLE parallax_ro PASSWORD '%s';\n" "$$PW" | $(PSQL) -v ON_ERROR_STOP=1 -q || exit 1; \
	( umask 077; printf 'PARALLAX_DATABASE_URL=postgresql://parallax_ro:%s@127.0.0.1:%s/parallax\n' \
		"$$PW" "$$PORT" > .env.ui ) || exit 1; \
	sed -e "s#@@ROOT@@#$(CURDIR)#g" -e "s#@@GROUP@@#$$(id -gn)#g" \
		ops/demo/$(DEMO_UNIT) | sudo tee $(SYSTEMD_DIR)/$(DEMO_UNIT) >/dev/null || exit 1; \
	sed -e "s#@@HOST@@#$$HOST#g" ops/demo/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null || exit 1; \
	caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile || exit 1; \
	sudo systemctl daemon-reload && sudo systemctl enable $(DEMO_UNIT) && sudo systemctl restart $(DEMO_UNIT) \
		&& sudo systemctl reload caddy || exit 1; \
	echo "demo page: https://$$HOST  (first visit may wait a few seconds for the certificate)"

.PHONY: demo.uninstall
demo.uninstall:
	@sudo systemctl disable --now $(DEMO_UNIT) 2>/dev/null || true
	@sudo rm -f $(SYSTEMD_DIR)/$(DEMO_UNIT) && sudo systemctl daemon-reload
	@printf "ALTER ROLE parallax_ro PASSWORD NULL;\n" | $(PSQL) -q || true
	@rm -f .env.ui
	@echo "demo page removed; parallax_ro can no longer log in. Caddy still holds 443:"
	@echo "sudo systemctl stop caddy  (or point /etc/caddy/Caddyfile elsewhere)"

# ---- Oracle Always Free host (T-032) ----------------------------------------
# Oracle's Ubuntu images ship their own iptables rules (netfilter-persistent,
# /etc/iptables/rules.v4) that REJECT everything but SSH, and ufw layered on
# top opens nothing. So on that image: no ufw; insert one ACCEPT for 80/443
# ahead of the REJECT and persist it. Idempotent (-C checks first). The VCN
# security list must allow the same two ports (ops/README.md, Oracle block).
OCI_RULE := INPUT -p tcp -m multiport --dports 80,443 -m conntrack --ctstate NEW -j ACCEPT

.PHONY: oci.firewall
oci.firewall:
	@test "$$(uname -s)" = Linux || { echo "oci.firewall runs on the Oracle VM" >&2; exit 1; }
	@command -v netfilter-persistent >/dev/null || { \
		echo "no netfilter-persistent: not Oracle's image -- use ufw (ops/README.md §10)" >&2; exit 1; }
	@sudo iptables -C $(OCI_RULE) 2>/dev/null || sudo iptables -I $(OCI_RULE)
	@sudo netfilter-persistent save >/dev/null
	@sudo iptables -L INPUT -n --line-numbers | head -8

# ---- backups / migration ----------------------------------------------------
# Tier-1 rows cannot be re-fetched, so the database is the only copy of the
# denominator. `-Fc` is compressed and restorable table-by-table. The VPS
# backup timer runs exactly this target.
BACKUP_DIR := backups

.PHONY: db.dump
db.dump:
	@mkdir -p $(BACKUP_DIR)
	@f=$(BACKUP_DIR)/parallax-$$(date -u +%Y%m%dT%H%M%SZ).dump; \
	$(DC) exec -T db pg_dump -U parallax -Fc parallax > $$f && ls -la $$f

# Destructive on the target database: --clean drops every object in the dump
# before recreating it. Point it at a throwaway project to rehearse:
#   PARALLAX_DB_PORT=5434 docker compose -p pxdrill up -d db
#   make db.restore DC="docker compose -p pxdrill" FILE=backups/parallax-….dump
.PHONY: db.restore
db.restore: db.wait
	@test -n "$(FILE)" || { echo "usage: make db.restore FILE=backups/parallax-….dump" >&2; exit 1; }
	@$(DC) exec -T db pg_restore -U parallax -d parallax --clean --if-exists --no-owner < "$(FILE)"
	@$(DC) exec -T db psql -U parallax -d parallax -tAc \
		"SELECT count(*) || ' article_index rows, ' || (SELECT count(*) FROM outlet_daily_totals WHERE complete) || ' complete outlet-days' FROM article_index;"

# Off-host copies (T-032). The VM keeps 14 days of dumps on its own disk; an
# Always Free VM can be reclaimed or lost with that disk, so the workstation
# pulls them. --ignore-existing: a dump is immutable once written. Keeps the
# newest $(BACKUP_KEEP), and exits non-zero when the newest one is over 36h
# old -- the VM's backup timer (or the VM) has stopped, and launchd's log
# should say so rather than keep copying the same old file.
BACKUP_PULL_DIR := $(BACKUP_DIR)/vm
BACKUP_KEEP := 30
BACKUP_PLIST := $(HOME)/Library/LaunchAgents/com.parallax.backup-pull.plist

.PHONY: backup.pull
backup.pull:
	@HOST="$(HOST)"; \
	[ -n "$$HOST" ] || HOST=$$(sed -n 's/^PARALLAX_BACKUP_HOST=//p' .env 2>/dev/null | tr -d "\"' "); \
	test -n "$$HOST" || { echo "usage: make backup.pull HOST=parallax@<vm-ip>  (or PARALLAX_BACKUP_HOST in .env)" >&2; exit 1; }; \
	mkdir -p $(BACKUP_PULL_DIR); \
	rsync -a --ignore-existing -e "ssh -o BatchMode=yes -o ConnectTimeout=20" \
		"$$HOST:parallax/$(BACKUP_DIR)/" $(BACKUP_PULL_DIR)/ || exit 1; \
	ls -1t $(BACKUP_PULL_DIR)/parallax-*.dump 2>/dev/null | tail -n +$$(( $(BACKUP_KEEP) + 1 )) | xargs rm -f; \
	NEWEST=$$(ls -1t $(BACKUP_PULL_DIR)/parallax-*.dump 2>/dev/null | head -1); \
	test -n "$$NEWEST" || { echo "$$(date '+%F %T') no dumps pulled from $$HOST" >&2; exit 1; }; \
	if [ -n "$$(find "$$NEWEST" -mmin +2160)" ]; then \
		echo "$$(date '+%F %T') STALE: newest dump $$NEWEST is over 36h old -- check the VM" >&2; exit 1; \
	fi; \
	echo "$$(date '+%F %T') ok: $$(ls $(BACKUP_PULL_DIR)/parallax-*.dump | wc -l | tr -d ' ') dumps, newest $$NEWEST"

.PHONY: backup.pull.install
backup.pull.install:
	@test "$$(uname -s)" = Darwin || { echo "backup.pull.install schedules launchd (macOS)" >&2; exit 1; }
	@$(MAKE) --no-print-directory backup.pull
	@mkdir -p $(dir $(BACKUP_PLIST)) logs
	@sed -e "s#@@ROOT@@#$(CURDIR)#g" ops/com.parallax.backup-pull.plist.template > $(BACKUP_PLIST)
	@plutil -lint $(BACKUP_PLIST) >/dev/null
	@launchctl unload $(BACKUP_PLIST) 2>/dev/null || true
	@launchctl load $(BACKUP_PLIST)
	@echo "loaded com.parallax.backup-pull (daily 04:00, log: logs/backup-pull.log)"

.PHONY: backup.pull.uninstall
backup.pull.uninstall:
	@launchctl unload $(BACKUP_PLIST) 2>/dev/null || true
	@rm -f $(BACKUP_PLIST)
	@echo "backup pull removed; dumps already in $(BACKUP_PULL_DIR) are kept"

# ---- ops.check: verify the Linux packaging without a Linux host ----------
# This Mac has no systemd, so the rendered units are checked by systemd's own
# parser inside ubuntu:24.04 (with stub executables and the service user in
# place, because `systemd-analyze verify` resolves both). Then the runtime:
# uv sync from the lockfile and a real dry-run crawl of cna inside the
# official uv image, proving the Linux wheels and the feeds work from a
# container -- the closest thing to the VPS that runs on a laptop.
OPS_CHECK := .ops-check

.PHONY: ops.check
ops.check:
	@rm -rf $(OPS_CHECK) && mkdir -p $(OPS_CHECK)/units
	@for u in $(addprefix ops/systemd/,$(SYSTEMD_UNITS)) ops/demo/$(DEMO_UNIT); do \
		sed -e "s#@@ROOT@@#/srv/parallax#g" -e "s#@@UV@@#/usr/local/bin/uv#g" -e "s#@@USER@@#parallax#g" \
			-e "s#@@GROUP@@#parallax#g" $$u > $(OPS_CHECK)/units/$$(basename $$u); \
	done
	@sed -e "s#@@HOST@@#203-0-113-5.sslip.io#g" ops/demo/Caddyfile > $(OPS_CHECK)/Caddyfile
	@echo "-- systemd-analyze verify (ubuntu:24.04)"
	@docker run --rm -v "$(CURDIR)/$(OPS_CHECK)/units:/units:ro" ubuntu:24.04 bash -euc '\
		export DEBIAN_FRONTEND=noninteractive; \
		apt-get update -qq >/dev/null && apt-get install -y -qq systemd make >/dev/null; \
		useradd -r parallax; mkdir -p /srv/parallax/backups; \
		install -m755 /dev/null /usr/local/bin/uv; \
		mkdir -p /srv/parallax/.venv/bin && install -m755 /dev/null /srv/parallax/.venv/bin/python; \
		cp /units/* /etc/systemd/system/; \
		systemd-analyze verify /etc/systemd/system/parallax-*.service /etc/systemd/system/parallax-*.timer; \
		echo "$$(ls /units | wc -l) units verified (verify prints nothing when clean)"; \
		grep -h "^OnCalendar=" /etc/systemd/system/parallax-*.timer | cut -d= -f2- \
			| while IFS= read -r c; do systemd-analyze calendar "$$c"; done | grep -E "Normalized|Next elapse"'
	@echo "-- caddy validate (caddy:2), the demo proxy rendered for a sample sslip.io host"
	@docker run --rm -v "$(CURDIR)/$(OPS_CHECK)/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 \
		caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile > $(OPS_CHECK)/caddy.log 2>&1 \
		|| { cat $(OPS_CHECK)/caddy.log >&2; exit 1; }
	@tail -1 $(OPS_CHECK)/caddy.log
	@echo "-- Linux runtime: uv sync + dry-run crawl of cna (ghcr.io/astral-sh/uv:python3.12-bookworm-slim)"
	@docker run --rm -v "$(CURDIR):/src:ro" -w /work ghcr.io/astral-sh/uv:python3.12-bookworm-slim bash -euc '\
		tar -C /src --exclude=.venv --exclude=raw --exclude=logs --exclude=backups --exclude=graphify-out \
			--exclude=$(OPS_CHECK) --exclude=.git -cf - . | tar -xf -; \
		uv sync --frozen --no-dev -q; \
		uv run --no-sync python -m parallax.jobs.crawl_listing --outlet cna --dry-run --wait-network 0 2>&1 \
			| tee /tmp/dryrun.log; \
		grep -Eq "cna +fetched=[1-9]" /tmp/dryrun.log || { echo "dry run fetched nothing for cna" >&2; exit 1; }'
	@rm -rf $(OPS_CHECK)
	@echo "ops.check OK"

.PHONY: test
test:
	uv run pytest -q

.PHONY: lint
lint:
	uv run ruff check src tests scripts

# Read-only feed-window pressure report.
.PHONY: saturation
saturation:
	uv run python -m parallax.jobs.saturation $(ARGS)
