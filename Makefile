DC := docker compose
PSQL := $(DC) exec -T db psql -U parallax -d parallax

.PHONY: social threads.refresh sched.install sched.uninstall sched.install.launchd sched.install.systemd sched.uninstall.launchd sched.uninstall.systemd db.dump db.restore ops.check help setup db.up db.down db.migrate db.psql db.wait audit crawl crawl.one rollup health test lint enrich reextract stance stance.posts stance.eval label dedup label.pairs dedup.eval framing report ui

help:
	@echo "setup      install deps into .venv via uv"
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
	@echo "stance.eval  score the classifier against the gold set (ARGS=--classify spends quota)"
	@echo "dedup      rebuild near-duplicate clusters + propagation order (Q3); ARGS=--keyword X narrows"
	@echo "label.pairs  hand-label candidate pairs into eval/dup_gold.csv (blind)"
	@echo "dedup.eval   precision/recall of the clusterer against the pair gold set"
	@echo "framing    per-cluster shared core + what each member added/dropped; ARGS=--summarize spends quota"
	@echo "report     Q1-Q4 for one keyword as text, e.g. make report KEYWORD=沈伯洋 ARGS=\"--since 2026-08-20\""
	@echo "ui         the same page in a browser (Streamlit, http://localhost:8501)"
	@echo "test       pytest"
	@echo "sched.install  schedule crawl+rollup on this host: launchd on macOS, systemd on Linux"
	@echo "ops.check  verify the systemd units + the Linux runtime in Docker (no VPS needed)"

setup: dict
	uv sync

# jieba's PyPI wheel omits the traditional-Chinese dictionary; without it
# segmentation falls back to a simplified-oriented one and search quality drops.
dict:
	@test -f config/dict.txt.big || curl -sSL -o config/dict.txt.big \
		https://raw.githubusercontent.com/fxsjy/jieba/master/extra_dict/dict.txt.big
	@echo "dict.txt.big ready ($$(wc -l < config/dict.txt.big | tr -d ' ') entries)"

resegment:
	uv run python -m parallax.jobs.resegment

db.up:
	$(DC) up -d db

db.down:
	$(DC) down

# Compose reports healthy via pg_isready; block until then so `make db.up db.migrate`
# in one line does not race the container's startup.
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
db.migrate: db.wait
	@echo "applying db/schema.sql"
	@$(PSQL) -v ON_ERROR_STOP=1 -q < db/schema.sql
	@for f in $$(ls db/migrations/*.sql 2>/dev/null | sort); do \
		echo "applying $$f"; \
		$(PSQL) -v ON_ERROR_STOP=1 -q < "$$f" || exit 1; \
	done
	@echo "schema + migrations applied"

db.psql:
	$(DC) exec -it db psql -U parallax -d parallax

audit:
	uv run python scripts/audit_feeds.py

crawl:
	uv run python -m parallax.jobs.crawl_listing

crawl.one:
	uv run python -m parallax.jobs.crawl_listing --outlet $(OUTLET) --verbose

rollup:
	uv run python -m parallax.jobs.rollup_daily

# ---- tier 2 + Q1 ---------------------------------------------------------
# All keyword-scoped: nothing here runs over the whole index. `stance` and
# `stance.eval --classify` spend API quota; everything else is local.
social:
	uv run python -m parallax.jobs.social --keyword "$(KEYWORD)" $(ARGS)

threads.refresh:
	uv run python -m parallax.jobs.social --refresh-token

enrich:
	uv run python -m parallax.jobs.enrich --keyword "$(KEYWORD)" $(ARGS)

# The raw HTML cache exists so a parser fix never re-hits an outlet; this is
# how the fix reaches every stored body. Follow with `make dedup && make framing`.
reextract:
	uv run python -m parallax.jobs.reextract $(ARGS)

stance:
	uv run python -m parallax.jobs.stance --keyword "$(KEYWORD)" $(ARGS)

# Q4. Same quota as `stance` and the same cache-once rule, over posts instead
# of articles; needs `make social KEYWORD=...` to have run first.
stance.posts:
	uv run python -m parallax.jobs.stance_social --keyword "$(KEYWORD)" $(ARGS)

label:
	uv run python scripts/label_stance.py --keyword "$(KEYWORD)" $(ARGS)

label.posts:
	uv run python scripts/label_posts.py --keyword "$(KEYWORD)" $(ARGS)

stance.eval:
	uv run python -m parallax.jobs.eval_stance $(ARGS)

# ---- Q3: dedup -----------------------------------------------------------
dedup:
	uv run python -m parallax.jobs.dedup $(ARGS)

label.pairs:
	uv run python scripts/label_pairs.py $(ARGS)

dedup.eval:
	uv run python -m parallax.jobs.eval_dedup $(ARGS)

# Runs after dedup. Deterministic and local unless ARGS=--summarize, which is
# the one model call in Q3: one line per member with a non-empty delta, cached
# on the row until its deltas change.
framing:
	uv run python -m parallax.jobs.framing $(ARGS)

# ---- Q1-Q3 in one page: the incident report --------------------------------
# Reads only. Weights appear solely for outlet-days the rollup marked complete,
# so run `make rollup` first if the denominator looks stale.
report:
	uv run python -m parallax.jobs.report --keyword "$(KEYWORD)" $(ARGS)

# Same report object, rendered. The `ui` extra pulls in streamlit and its
# pandas/pyarrow tail, so it stays optional and is installed on first use.
ui:
	uv run --extra ui streamlit run src/parallax/ui/app.py

# The query that answers "is tier-1 still working?". A zero or a stale last_run
# here means data is being lost right now and cannot be backfilled.
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
	@echo "-- largest_gap is the number that matters: the crawl runs every 20 min, so"
	@echo "-- anything past ~1h is coverage this project can never get back."
	@$(PSQL) -tc "SELECT count(*) FILTER (WHERE NOT ok) || ' failed runs in 24h' FROM crawl_runs WHERE started_at > now() - interval '24 hours';"
	@uv run python -m parallax.jobs.social --status

# One entry point per host kind. macOS: launchd (re-runs a job missed during
# sleep). Linux: systemd timers with Persistent=true, the same property. The
# recipes below are unchanged from before the split; only the dispatch is new.
UNAME := $(shell uname -s)

sched.install:
ifeq ($(UNAME),Darwin)
	@$(MAKE) --no-print-directory sched.install.launchd
else
	@$(MAKE) --no-print-directory sched.install.systemd
endif

sched.uninstall:
ifeq ($(UNAME),Darwin)
	@$(MAKE) --no-print-directory sched.uninstall.launchd
else
	@$(MAKE) --no-print-directory sched.uninstall.systemd
endif

# Install the launchd agents and remove the cron entries, so the two can never
# double-run.
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

sched.uninstall.systemd:
	@sudo systemctl disable --now $(SYSTEMD_TIMERS) 2>/dev/null || true
	@for u in $(SYSTEMD_UNITS); do sudo rm -f $(SYSTEMD_DIR)/$$u; done
	@sudo systemctl daemon-reload
	@echo "systemd units removed"

# ---- backups / migration ----------------------------------------------------
# Tier-1 rows cannot be re-fetched, so the database is the only copy of the
# denominator. `-Fc` is compressed and restorable table-by-table. The VPS
# backup timer runs exactly this target.
BACKUP_DIR := backups

db.dump:
	@mkdir -p $(BACKUP_DIR)
	@f=$(BACKUP_DIR)/parallax-$$(date -u +%Y%m%dT%H%M%SZ).dump; \
	$(DC) exec -T db pg_dump -U parallax -Fc parallax > $$f && ls -la $$f

# Destructive on the target database: --clean drops every object in the dump
# before recreating it. Point it at a throwaway project to rehearse:
#   PARALLAX_DB_PORT=5434 docker compose -p pxdrill up -d db
#   make db.restore DC="docker compose -p pxdrill" FILE=backups/parallax-….dump
db.restore: db.wait
	@test -n "$(FILE)" || { echo "usage: make db.restore FILE=backups/parallax-….dump" >&2; exit 1; }
	@$(DC) exec -T db pg_restore -U parallax -d parallax --clean --if-exists --no-owner < "$(FILE)"
	@$(DC) exec -T db psql -U parallax -d parallax -tAc \
		"SELECT count(*) || ' article_index rows, ' || (SELECT count(*) FROM outlet_daily_totals WHERE complete) || ' complete outlet-days' FROM article_index;"

# ---- ops.check: verify the Linux packaging without a Linux host ----------
# This Mac has no systemd, so the rendered units are checked by systemd's own
# parser inside ubuntu:24.04 (with stub executables and the service user in
# place, because `systemd-analyze verify` resolves both). Then the runtime:
# uv sync from the lockfile and a real dry-run crawl of cna inside the
# official uv image, proving the Linux wheels and the feeds work from a
# container -- the closest thing to the VPS that runs on a laptop.
OPS_CHECK := .ops-check

ops.check:
	@rm -rf $(OPS_CHECK) && mkdir -p $(OPS_CHECK)/units
	@for u in $(SYSTEMD_UNITS); do \
		sed -e "s#@@ROOT@@#/srv/parallax#g" -e "s#@@UV@@#/usr/local/bin/uv#g" -e "s#@@USER@@#parallax#g" \
			ops/systemd/$$u > $(OPS_CHECK)/units/$$u; \
	done
	@echo "-- systemd-analyze verify (ubuntu:24.04)"
	@docker run --rm -v "$(CURDIR)/$(OPS_CHECK)/units:/units:ro" ubuntu:24.04 bash -euc '\
		export DEBIAN_FRONTEND=noninteractive; \
		apt-get update -qq >/dev/null && apt-get install -y -qq systemd make >/dev/null; \
		useradd -r parallax; mkdir -p /srv/parallax/backups; \
		install -m755 /dev/null /usr/local/bin/uv; \
		cp /units/* /etc/systemd/system/; \
		systemd-analyze verify /etc/systemd/system/parallax-*.service /etc/systemd/system/parallax-*.timer; \
		echo "6 units verified (verify prints nothing when clean)"; \
		systemd-analyze calendar "*:0/20" "*-*-* 00:20:00 Asia/Taipei" "*-*-* 03:00:00 Asia/Taipei" | grep -E "Normalized|Next elapse"'
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

test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts
