"""RESIDUAL_VALUE_SCAN: lineage-independent search for leftover copies of the subject's values.

For every column in ``scan_columns`` (all text-like columns of all target tables, computed by
the service from the catalog, *not* from lineage), and for every normalization used by the
probes, ``reader.hmac_match_count`` counts the rows whose normalized HMAC equals one of the
probe digests (pgcrypto in the database). Hits are reported as ``{table, column, count}``;
values never leave the database.

Hits in a table whose policy is ``RETAIN`` *and* for which the plan has a ``RETAIN_ROWS`` item
are expected (legal hold keeps the row untouched) and are reported separately as
``retained_hits``; they do not fail the check. A count cannot tell which row matched, so this
exemption is per table (documented limitation).
"""

from __future__ import annotations

from collections.abc import Sequence

from lae.contracts.enums import CheckType, Normalization, PlanAction, RetentionAction
from lae.contracts.ports import TargetReader
from lae.contracts.types import CheckResult, ColumnRef, ErasurePlan, PolicySet, ResidualProbe
from lae.errors import VerificationError
from lae.verification._common import as_count, guarded, is_hex64

_CT = CheckType.RESIDUAL_VALUE_SCAN


def group_probes(probes: Sequence[ResidualProbe]) -> dict[Normalization, tuple[str, ...]]:
    """Probe digests grouped by normalization, de-duplicated and sorted."""
    groups: dict[Normalization, set[str]] = {}
    for p in probes:
        if not is_hex64(p.value_hmac):
            raise VerificationError(
                "residual probe digest must be 64 lowercase hex characters",
                code="INVALID_PROBE",
                details={"origin": p.origin.qualified(), "normalization": p.normalization.value},
            )
        groups.setdefault(Normalization(p.normalization), set()).add(p.value_hmac)
    return {n: tuple(sorted(groups[n])) for n in sorted(groups)}


def _match_count(
    reader: TargetReader,
    col: ColumnRef,
    digests: tuple[str, ...],
    norm: Normalization,
    hmac_key: bytes,
) -> int:
    return as_count(
        guarded(_CT, col.table, lambda: reader.hmac_match_count(col, digests, norm, hmac_key)),
        _CT,
        col.table,
    )


def check_residual(
    reader: TargetReader,
    plan: ErasurePlan,
    probes: Sequence[ResidualProbe],
    scan_columns: Sequence[ColumnRef],
    hmac_key: bytes,
    policies: PolicySet,
) -> CheckResult:
    groups = group_probes(probes)
    columns = sorted(set(scan_columns))
    legal_hold = {
        it.table
        for it in plan.items
        if it.action is PlanAction.RETAIN_ROWS
        and policies.action_for(it.table) is RetentionAction.RETAIN
    }
    hits: list[dict[str, object]] = []
    retained_hits: list[dict[str, object]] = []
    total = 0
    if groups:
        for col in columns:
            count = 0
            for norm, digests in groups.items():
                count += _match_count(reader, col, digests, norm, hmac_key)
            if count:
                entry: dict[str, object] = {
                    "table": col.table.qualified(),
                    "column": col.column,
                    "count": count,
                }
                if col.table in legal_hold:
                    retained_hits.append(entry)
                else:
                    hits.append(entry)
                    total += count
    return CheckResult(
        _CT,
        None,
        None,
        None,
        total == 0,
        observed={
            "hits": hits,
            "retained_hits": retained_hits,
            "total_hits": total,
            "columns_scanned": len(columns) if groups else 0,
            "probe_count": sum(len(d) for d in groups.values()),
        },
        expected={"total_hits": 0},
    )
