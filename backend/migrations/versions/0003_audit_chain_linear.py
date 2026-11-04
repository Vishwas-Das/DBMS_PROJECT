"""Make audit-chain forks impossible (QA defect D1, 2026-11-05). Lead-owned.

Two concurrent SERIALIZABLE transactions can both read the same chain head, because the
transaction snapshot is taken before the advisory-lock wait in PgAuditSink.append returns.
Both then append an event with the same prev_hash, which forks the chain.
A UNIQUE index on prev_hash makes the second append fail (unique_violation). Its whole
transaction (including any erasure DML) rolls back, so the chain stays strictly linear.

Revision ID: 0003_audit_chain_linear
Revises: 0002_security_hardening
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_audit_chain_linear"
down_revision: str | None = "0002_security_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE UNIQUE INDEX uq_audit_event_prev_hash ON lae.audit_event (prev_hash)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS lae.uq_audit_event_prev_hash")
