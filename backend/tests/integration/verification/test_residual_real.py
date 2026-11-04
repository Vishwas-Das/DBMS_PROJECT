"""RESIDUAL_VALUE_SCAN on real data: FK-only erasure is insufficient (architecture.md §12).

Experiment (seed 7, scale tiny, subject = ground-truth primary):

1. Probes are captured by the real pipeline (``discover_records`` + ``capture_residual_probes``)
   before the erasure. Only HMAC digests leave the database read.
2. Every FK-reachable row and every registered derived artifact is erased. All per-item checks
   and the lineage-based REDISCOVERY_EMPTY pass, but the lineage-independent residual scan
   (pgcrypto HMAC over every text-like column) finds the un-modelled copies in
   ``app.marketing_event_log.email``, including LOWER_TRIM variants (``'  EMAIL\\t'``, Title
   Case). The outcome is FAILED even though coverage is 1.
3. After deleting the hidden copies too, the scan passes and the outcome is VERIFIED.

The counts are pinned to the generator's ground truth (``hidden_copies``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text

from lae.contracts.enums import (
    CheckType,
    Normalization,
    VerificationOutcome,
    VerificationStatus,
)
from lae.contracts.types import ColumnRef, ResidualProbe, TableRef, VerificationReport

pytestmark = pytest.mark.integration

MARKETING_EMAIL = ColumnRef(TableRef("app", "marketing_event_log"), "email")
log = logging.getLogger(__name__)


def residual(r: VerificationReport) -> Any:
    found = [c for c in r.checks if c.check_type is CheckType.RESIDUAL_VALUE_SCAN]
    assert len(found) == 1
    return found[0]


def hidden_truth(w: Any) -> dict[str, int]:
    return {
        norm: n
        for table, col, norm, n in w.truth.subject(w.subject)["hidden_copies"]
        if (table, col) == ("app.marketing_event_log", "email")
    }


def raw_identifiers(w: Any) -> list[str]:
    """The subject's raw values, read ONLY to assert that they never appear in outputs."""
    row = w.q("SELECT full_name, email, phone FROM app.customer WHERE id = :c", c=w.subject)[0]
    return [v for v in row if v]


def per_norm(w: Any, probes: Sequence[ResidualProbe], col: ColumnRef) -> dict[str, int]:
    out: dict[str, int] = {}
    for norm in Normalization:
        digests = [p.value_hmac for p in probes if p.normalization is norm]
        if digests:
            out[norm.value] = w.reader.hmac_match_count(col, digests, norm, w.HMAC_KEY)
    return out


@pytest.fixture
def probes(world: Any) -> list[ResidualProbe]:
    found: list[ResidualProbe] = world.capture_probes()
    return found


def test_probes_are_digests_of_the_subject(world: Any, probes: list[ResidualProbe]) -> None:
    w = world
    assert probes and all(len(p.value_hmac) == 64 for p in probes)
    origins = {(p.origin.qualified(), p.normalization.value) for p in probes}
    assert ("app.customer.email", "EXACT") in origins
    assert ("app.customer.email", "LOWER_TRIM") in origins
    assert ("app.customer.phone", "DIGITS_ONLY") in origins
    email = w.q("SELECT email FROM app.customer WHERE id = :c", c=w.subject)[0][0]
    assert w.keyring.digest(email, Normalization.LOWER_TRIM) in {p.value_hmac for p in probes}
    text = repr(probes)
    for raw in raw_identifiers(w):
        assert raw not in text


def test_fk_only_erasure_leaves_hidden_copies(world: Any, probes: list[ResidualProbe]) -> None:
    w = world
    raws = raw_identifiers(w)
    scan = w.scan_columns()
    truth = hidden_truth(w)
    assert truth == {"EXACT": 3, "LOWER_TRIM": 5}  # 3 exact + 2 case/whitespace variants

    baseline = residual(w.verify(w.subject_plan(), probes=probes, scan=scan))
    plan = w.subject_plan()  # FK-reachable rows + registered artifacts, no hidden copies
    w.apply(plan)
    report = w.verify(plan, probes=probes, scan=scan, rediscover=w.rediscover)
    c = residual(report)

    # every planned item verified, lineage-based rediscovery is blind to the copies ...
    assert report.coverage == 1 and report.verified_artifacts == len(plan.items)
    redisc = [x for x in report.checks if x.check_type is CheckType.REDISCOVERY_EMPTY]
    assert redisc[0].passed and redisc[0].observed["remaining_records"] == 0
    assert [x.check_type for x in report.checks if not x.passed] == [CheckType.RESIDUAL_VALUE_SCAN]

    # ... but the residual scan finds them: table / column / count, never values
    by_norm = per_norm(w, probes, MARKETING_EMAIL)
    assert {k: v for k, v in by_norm.items() if v} == truth
    assert not c.passed
    assert c.observed["hits"] == [
        {"table": "app.marketing_event_log", "column": "email", "count": sum(truth.values())}
    ]
    assert c.observed["total_hits"] == sum(truth.values())  # 8 matches over 5 distinct rows
    assert c.observed["retained_hits"] == []
    assert c.observed["columns_scanned"] == len(scan)
    assert (report.outcome, report.status) == (
        VerificationOutcome.FAILED,
        VerificationStatus.FAILED,
    )
    rows = w.count("SELECT count(*) FROM app.marketing_event_log")
    assert rows == w.truth.meta["row_counts"]["marketing_event_log"]  # nothing was touched

    dumped = json.dumps([{"o": dict(x.observed), "e": dict(x.expected)} for x in report.checks])
    for raw in raws:
        assert raw not in dumped

    log.info(
        "residual experiment subject=%s: baseline hits=%s total=%s; after FK-only erasure "
        "hits=%s by_normalization=%s",
        w.subject,
        baseline.observed["hits"],
        baseline.observed["total_hits"],
        c.observed["hits"],
        by_norm,
    )


def test_lower_trim_is_needed_for_the_variants(world: Any, probes: list[ResidualProbe]) -> None:
    """With EXACT probes only, the scan misses the two case/whitespace variants."""
    w = world
    plan = w.subject_plan()
    w.apply(plan)
    exact = [p for p in probes if p.normalization is Normalization.EXACT]
    c_exact = residual(w.verify(plan, probes=exact, scan=[MARKETING_EMAIL]))
    assert c_exact.observed["total_hits"] == 3

    # delete only the exact copies (matched by digest inside SQL; no value leaves the DB):
    # the two variants remain and are found only through LOWER_TRIM
    email_digest = next(
        p.value_hmac for p in exact if p.origin == ColumnRef(TableRef("app", "customer"), "email")
    )
    w.conn.execute(
        text(
            "DELETE FROM app.marketing_event_log WHERE encode(hmac(convert_to(email, 'UTF8'), "
            ":k, 'sha256'), 'hex') = :d"
        ),
        {"k": w.HMAC_KEY, "d": email_digest},
    )
    assert residual(w.verify(plan, probes=exact, scan=[MARKETING_EMAIL])).passed
    after = residual(w.verify(plan, probes=probes, scan=[MARKETING_EMAIL]))
    assert not after.passed and after.observed["total_hits"] == 2
    assert per_norm(w, probes, MARKETING_EMAIL) == {"EXACT": 0, "LOWER_TRIM": 2, "DIGITS_ONLY": 0}


def test_erasing_hidden_copies_passes_and_verifies(world: Any, probes: list[ResidualProbe]) -> None:
    w = world
    plan = w.subject_plan(hidden_copies=True)
    hidden = [i for i in plan.items if i.table == MARKETING_EMAIL.table]
    assert len(hidden) == 1 and hidden[0].expected_row_count == hidden_truth(w)["LOWER_TRIM"]
    assert len(hidden[0].record_keys) == 3  # distinct spellings: exact, padded upper, title
    w.apply(plan)
    scan = w.scan_columns()
    report = w.verify(plan, probes=probes, scan=scan, rediscover=w.rediscover)
    c = residual(report)
    assert c.passed and c.observed["hits"] == [] and c.observed["total_hits"] == 0
    assert c.observed["probe_count"] == len({(p.normalization, p.value_hmac) for p in probes})
    assert all(x.passed for x in report.checks)
    assert report.outcome is VerificationOutcome.VERIFIED and report.coverage == 1
    others = w.count("SELECT count(*) FROM app.marketing_event_log")
    assert others == w.truth.meta["row_counts"]["marketing_event_log"] - 5


def test_retained_legal_hold_hits_are_reported_separately(
    world: Any, probes: list[ResidualProbe]
) -> None:
    """A copy inside a table under legal hold (RETAIN policy + RETAIN_ROWS item) is reported as
    a retained hit and does not fail the scan."""
    w = world
    # copy the subject's e-mail into a retained shipment row (the value never leaves SQL)
    w.conn.execute(
        text(
            "UPDATE app.shipment SET tracking_code = (SELECT email FROM app.customer "
            "WHERE id = :c) WHERE id = (SELECT min(s.id) FROM app.shipment s "
            "JOIN app.purchase_order o ON o.id = s.order_id WHERE o.customer_id = :c)"
        ),
        {"c": w.subject},
    )
    plan = w.subject_plan(hidden_copies=True)
    w.apply(plan)
    c = residual(w.verify(plan, probes=probes, scan=w.scan_columns()))
    assert c.passed and c.observed["hits"] == []
    assert c.observed["retained_hits"] == [
        {"table": "app.shipment", "column": "tracking_code", "count": 2}  # EXACT + LOWER_TRIM
    ]
