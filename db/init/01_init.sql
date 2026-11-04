-- Runs once on first container start (docker-entrypoint-initdb.d), as lae_owner.
-- Creates the dev database `lae` (already created by POSTGRES_DB) and the test database `lae_test`,
-- each with extensions, schemas and the read-only role grants.
-- Dev-only passwords. Never reuse them outside a local machine.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_reader') THEN
    CREATE ROLE lae_reader LOGIN PASSWORD 'lae_reader_dev';
  END IF;
  -- non-superuser application role (security review M2); grants come from migration 0002
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_app') THEN
    CREATE ROLE lae_app LOGIN PASSWORD 'lae_app_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
  -- executor role: DML on the target schema only (architecture §2.1)
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_executor') THEN
    CREATE ROLE lae_executor LOGIN PASSWORD 'lae_executor_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;

CREATE DATABASE lae_test OWNER lae_owner;

-- ---------------------------------------------------------------- database: lae
\connect lae
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
GRANT USAGE ON SCHEMA lae, app TO lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;

-- ---------------------------------------------------------------- database: lae_test
\connect lae_test
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;

-- Per-workstream integration-test databases (parallel agents must not share a schema they drop).
-- Created on a fresh volume. On an existing volume, run scripts/create_test_dbs.py (same result,
-- idempotent). Keep this list in sync with DEFAULT_SUFFIXES in that script.
\connect lae
SELECT format('CREATE DATABASE %I OWNER lae_owner', 'lae_test_' || s)
FROM unnest(ARRAY['qa', 'bench', 'disc', 'erasure', 'lead', 'verif']) AS s
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'lae_test_' || s)
\gexec

\connect lae_test_qa
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;

\connect lae_test_bench
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;

\connect lae_test_disc
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;

\connect lae_test_erasure
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;

\connect lae_test_lead
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;

\connect lae_test_verif
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader, lae_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lae_executor;
