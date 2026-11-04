-- Runs once on first container start (docker-entrypoint-initdb.d), as lae_owner.
-- Creates the dev database `lae` (already created by POSTGRES_DB) and the test database `lae_test`,
-- each with extensions, schemas and the read-only role grants.
-- Dev-only passwords. Never reuse them outside a local machine.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_reader') THEN
    CREATE ROLE lae_reader LOGIN PASSWORD 'lae_reader_dev';
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

-- ---------------------------------------------------------------- database: lae_test
\connect lae_test
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS lae AUTHORIZATION lae_owner;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION lae_owner;
GRANT USAGE ON SCHEMA lae, app TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA app GRANT SELECT ON TABLES TO lae_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE lae_owner IN SCHEMA lae GRANT SELECT ON TABLES TO lae_reader;
