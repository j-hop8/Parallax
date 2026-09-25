-- T-029: the role the public demo page reads as.
--
-- The page shares a host with tier 1 (invariant 1), so everything it can do to
-- Postgres is bounded here rather than trusted to the UI code:
--   * SELECT only, and every transaction read-only on top of that;
--   * a statement_timeout, so one expensive search cannot pin a core;
--   * a connection limit, so a crowd of visitors cannot use up max_connections
--     and lock the crawl out of its own database.
--
-- LOGIN with no password: over TCP (scram) the role cannot authenticate until
-- `make demo.install` sets one, so on a laptop this migration grants nothing
-- usable. Idempotent like every migration: re-run after a restore to re-apply
-- the grants.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'parallax_ro') THEN
        CREATE ROLE parallax_ro LOGIN;
    END IF;
END
$$;

ALTER ROLE parallax_ro NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 20;
ALTER ROLE parallax_ro SET default_transaction_read_only = on;
ALTER ROLE parallax_ro SET statement_timeout = '15s';
ALTER ROLE parallax_ro SET idle_in_transaction_session_timeout = '60s';

GRANT USAGE ON SCHEMA public TO parallax_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO parallax_ro;
-- Tables a later migration creates (as the owner running db.migrate).
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO parallax_ro;
