DC := docker compose
PSQL := $(DC) exec -T db psql -U parallax -d parallax

.PHONY: help setup db.up db.down db.migrate db.psql db.wait audit crawl crawl.one rollup health test lint

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
	SELECT outlet, \
	       max(started_at) AS last_run, \
	       sum(items_new) FILTER (WHERE started_at > now() - interval '24 hours') AS new_24h, \
	       count(*) FILTER (WHERE NOT ok AND started_at > now() - interval '24 hours') AS failures_24h \
	FROM crawl_runs GROUP BY outlet ORDER BY outlet;"

test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts
