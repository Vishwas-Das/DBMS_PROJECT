"""verify_execution (module_contracts.md §8.1, architecture.md §12, CR lead-1 §3)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from lae.audit.hmac_keys import HmacKeyring
from lae.contracts.enums import ExecutionMode, ExecutionStatus
from lae.contracts.ports import TargetReader
from lae.contracts.types import (
    ArtifactDefinition,
    CheckResult,
    ColumnRef,
    ErasurePlan,
    ExecutionResult,
    PolicySet,
    ResidualProbe,
    TableRef,
    VerificationReport,
)
from lae.errors import ConfigurationError, VerificationError
from lae.lineage.results import RecordDiscoveryResult
from lae.verification.checks import check_embeddings, checks_for_item
from lae.verification.outcome import build_report
from lae.verification.rediscovery import check_rediscovery
from lae.verification.residual import check_residual

logger = logging.getLogger(__name__)


def _check_sort_key(c: CheckResult) -> tuple[Any, ...]:
    return (
        c.step_order is None,
        c.step_order or 0,
        c.check_type.value,
        c.table.qualified() if c.table is not None else "",
        c.column or "",
    )


def _artifacts_by_target(
    artifacts: Sequence[ArtifactDefinition],
) -> dict[TableRef, ArtifactDefinition]:
    out: dict[TableRef, ArtifactDefinition] = {}
    for a in artifacts:
        if a.target in out:
            raise VerificationError(
                "two registered artifacts share a target table",
                code="AMBIGUOUS_ARTIFACT",
                details={"table": a.target.qualified()},
            )
        out[a.target] = a
    return out


def verify_execution(
    plan: ErasurePlan,
    execution: ExecutionResult,
    reader: TargetReader,
    rediscover_records: Callable[[TargetReader], RecordDiscoveryResult],
    artifacts: Sequence[ArtifactDefinition],
    probes: Sequence[ResidualProbe],
    scan_columns: Sequence[ColumnRef],
    hmac_key: bytes,
    policies: PolicySet,
) -> VerificationReport:
    """Independently verify a committed LIVE execution of ``plan``.

    Runs, in order: the checks bound to each plan item (by ``step_order``), EMBEDDING_ABSENT for
    every embedding artifact, REDISCOVERY_EMPTY and the lineage-independent
    RESIDUAL_VALUE_SCAN. Failing checks are data (``passed=False``). ``VerificationError`` is
    raised only if a check cannot run (reader/DB error, malformed probe, missing artifact
    definition, a key-based action at TABLE level, a non-committed execution or a bad key);
    the service then records status ``ERROR``.

    Checks are returned sorted by (step_order, check_type, table, column), global checks last.
    """
    if (
        execution.mode is not ExecutionMode.LIVE
        or execution.status is not ExecutionStatus.COMMITTED
    ):
        raise VerificationError(
            "only a committed LIVE execution can be verified",
            code="NOT_VERIFIABLE",
            details={"mode": execution.mode.value, "status": execution.status.value},
        )
    try:
        HmacKeyring(hmac_key)  # validates type and length; the key itself is never stored here
    except ConfigurationError as exc:
        raise VerificationError(
            "HMAC key is unusable", code="INVALID_HMAC_KEY", details=exc.details
        ) from None

    by_target = _artifacts_by_target(artifacts)
    checks: list[CheckResult] = []
    for item in sorted(plan.items, key=lambda it: it.step_order):
        checks.extend(checks_for_item(reader, item, plan, by_target))
    checks.extend(check_embeddings(reader, plan, artifacts))
    checks.append(check_rediscovery(reader, plan, rediscover_records))
    checks.append(check_residual(reader, plan, probes, scan_columns, hmac_key, policies))
    checks.sort(key=_check_sort_key)

    report = build_report(plan, checks)
    logger.info(
        "verification of plan %s: %d checks, %d failed, outcome %s, %d/%d items verified",
        plan.plan_id,
        len(checks),
        sum(1 for c in checks if not c.passed),
        report.outcome.value,
        report.verified_artifacts,
        report.required_artifacts,
    )
    return report
