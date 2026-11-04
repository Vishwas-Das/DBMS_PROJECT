"""Security hardening (independent review, 2026-11-04). Lead-owned.

1. M5: `ck_erasure_plan_approval_consistent` made SUPERSEDED/FAILED plans with approval history
   impossible, so a stale APPROVED plan could never be re-planned. New rule: approved statuses
   need approved_at; DRAFT/BLOCKED must not have it; SUPERSEDED/FAILED may keep their history.
2. M2: non-superuser application role `lae_app`.
   * DML on `lae` tables, but NO UPDATE/DELETE/TRUNCATE on lae.audit_event and nothing on
     lae.alembic_version. It is not the owner of audit_event, so it cannot DISABLE the
     append-only trigger.
   * Owns the `app` schema objects (PostgreSQL 16 needs ownership for REFRESH MATERIALIZED VIEW).
   * `lae.grant_app_role()` re-applies ownership/grants after the synthetic data is (re)seeded.
   The role is created NOLOGIN here. db/init / scripts give it a LOGIN password for local dev.

Revision ID: 0002_security_hardening
Revises: 0001_baseline
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_security_hardening"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPROVED = "'APPROVED','EXECUTED','VERIFIED'"

GRANT_FN = r"""
CREATE OR REPLACE FUNCTION lae.grant_app_role() RETURNS void
LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_app') THEN
    RETURN;
  END IF;
  EXECUTE 'ALTER SCHEMA app OWNER TO lae_app';
  FOR r IN SELECT c.relname, c.relkind FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'app' AND c.relkind IN ('r','p','v','m')
  LOOP
    EXECUTE format('ALTER %s app.%I OWNER TO lae_app',
                   CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                  ELSE 'TABLE' END, r.relname);
  END LOOP;
  EXECUTE 'GRANT USAGE ON SCHEMA app TO lae_reader';
  EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA app TO lae_reader';
  EXECUTE 'GRANT USAGE ON SCHEMA lae TO lae_app';
  EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA lae TO lae_app';
  EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA lae TO lae_app';
  EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON lae.audit_event FROM lae_app';
  EXECUTE 'REVOKE ALL ON lae.alembic_version FROM lae_app';
  EXECUTE 'GRANT SELECT ON lae.alembic_version TO lae_app';
END
$fn$;
"""


def upgrade() -> None:
    op.execute("ALTER TABLE lae.erasure_plan DROP CONSTRAINT ck_erasure_plan_approval_consistent")
    op.execute(
        "ALTER TABLE lae.erasure_plan ADD CONSTRAINT ck_erasure_plan_approval_consistent CHECK ("
        f"CASE WHEN status IN ({APPROVED}) THEN approved_at IS NOT NULL "
        "WHEN status IN ('DRAFT','BLOCKED') THEN approved_at IS NULL ELSE true END)"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_app') "
        "THEN CREATE ROLE lae_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; END $$"
    )
    op.execute(GRANT_FN)
    op.execute("SELECT lae.grant_app_role()")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS lae.grant_app_role()")
    op.execute("ALTER TABLE lae.erasure_plan DROP CONSTRAINT ck_erasure_plan_approval_consistent")
    op.execute(
        "ALTER TABLE lae.erasure_plan ADD CONSTRAINT ck_erasure_plan_approval_consistent CHECK ("
        f"(status IN ({APPROVED})) = (approved_at IS NOT NULL)) NOT VALID"
    )  # NOT VALID: SUPERSEDED plans that legitimately kept approval history stay intact
