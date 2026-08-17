DC := docker compose
PSQL := $(DC) exec -T db psql -U parallax -d parallax

.PHONY: sched.install sched.uninstall help setup db.up db.down db.migrate db.psql db.wait audit crawl crawl.one rollup health test lint

help:
	@echo "setup      install deps into .venv via uv"
	@echo "db.up      start Postgres (docker compose)"
	@echo "db.migrate apply db/schema.sql (idempotent)"
	@echo "audit      T-002: probe outlet feeds + robots.txt  [no DB needed]"
	@echo "crawl      run the tier-1 listing crawl once"
	@echo "crawl.one  run one outlet, e.g. make crawl.one OUTLET=cna"
	@echo "health     per-outlet crawl health for the last 24h"
	@echo "test       pytest"

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

# Install the launchd agents and remove the cron entries, so the two can never
# double-run. launchd is used because it re-runs a job missed during sleep.
sched.install:
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
			grep -v 'parallax.jobs' /tmp/parallax-crontab.current | crontab -; \
			echo "removed parallax cron entries (backup: ~/.parallax-crontab.backup)"; \
		else \
			echo "no parallax cron entries present; crontab left untouched"; \
		fi; \
	else \
		echo "no crontab for this user; nothing to remove"; \
	fi
	@rm -f /tmp/parallax-crontab.current
	@launchctl list | grep parallax || true

sched.uninstall:
	@for j in crawl rollup; do \
		launchctl unload ~/Library/LaunchAgents/com.parallax.$$j.plist 2>/dev/null || true; \
		rm -f ~/Library/LaunchAgents/com.parallax.$$j.plist; \
	done
	@echo "launchd agents removed"

test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts
