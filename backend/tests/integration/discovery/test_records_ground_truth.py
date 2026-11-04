"""Record discovery through the real PgTargetReader vs the generator's ground truth.

The generator computes its oracle from its own bookkeeping (never from FKs), so this is an
independent check. Metrics use `lae.verification.metrics` (exact Fractions).

Truth for discovery = ground-truth ``owned`` (FK-reachable, SUBJECT_ROOT/OWNED) + ``derived_rows``
(key-preserving artifact rows: matview, CTAS, embedding via registered mapping, cache).
Discovered = records with role SUBJECT_ROOT or OWNED. SHARED and REFERENCING_OTHER_SUBJECT are
compared separately and exactly. Hidden copies (marketing_event_log) have neither PK nor FK;
FK/lineage discovery must NOT find them, so recall over owned + derived + hidden is < 1 for every
subject that has hidden copies (that gap is what the residual scan closes).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import pytest

from lae.contracts.enums import RecordRole, Traceability
from lae.contracts.types import DiscoveryLimits, TableRef
from lae.discovery import discover_records, record_set_fingerprint
from lae.errors import DiscoveryLimitExceeded, SubjectNotFoundError
from lae.verification.metrics import confusion, f1, precision, recall

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)
APP = "app"
OWNED_ROLES = {RecordRole.SUBJECT_ROOT, RecordRole.OWNED}
Row = tuple[TableRef, tuple[Any, ...]]


def t(name: str) -> TableRef:
    return TableRef(APP, name)


def _rows(entries: list[list[Any]]) -> set[Row]:
    return {(TableRef.parse(e[0]), tuple(e[1])) for e in entries}


@dataclass(frozen=True)
class Score:
    subject: int
    tp: int
    fp: int
    fn: int
    precision: Fraction | None
    recall: Fraction | None
    f1: Fraction | None
    hidden_rows: int
    recall_with_hidden: Fraction | None


def _evaluate(pipeline: Any, subject: int) -> tuple[Score, Any]:
    truth = pipeline.truth.subject(subject)
    result = discover_records(
        pipeline.graph, pipeline.root, (subject,), pipeline.reader, DiscoveryLimits()
    )
    nodes = result.record_graph.nodes()
    discovered = {(k.table, k.values) for k, a in nodes if a.role in OWNED_ROLES}
    expected = _rows(truth["owned"]) | _rows(truth["derived_rows"])
    tp, fp, fn = confusion(discovered, expected)
    p, r = precision(tp, fp), recall(tp, fn)
    # hidden copies: LOWER_TRIM counts include the EXACT matches (FORMAT in generator.py)
    hidden = max((h[3] for h in truth["hidden_copies"] if h[2] == "LOWER_TRIM"), default=0)
    hidden_set = {(t("marketing_event_log"), ("hidden", i)) for i in range(hidden)}
    tp_h, _fp_h, fn_h = confusion(discovered, expected | hidden_set)
    score = Score(subject, tp, fp, fn, p, r, f1(p, r), hidden, recall(tp_h, fn_h))
    return score, result


def _assert_subject(pipeline: Any, subject: int) -> Score:
    score, result = _evaluate(pipeline, subject)
    truth = pipeline.truth.subject(subject)
    nodes = result.record_graph.nodes()
    roles = {(k.table, k.values): a.role for k, a in nodes}

    assert score.precision == 1, (subject, score)
    assert score.recall == 1, (subject, score)
    assert score.f1 == 1

    shared = {(TableRef.parse(e[0]), tuple(e[1])) for e in truth["shared"]}
    assert {k for k, r in roles.items() if r is RecordRole.SHARED} == shared
    referencing = _rows(truth["referencing_other"])
    got_ref = {k for k, r in roles.items() if r is RecordRole.REFERENCING_OTHER_SUBJECT}
    assert got_ref == referencing
    # unrelated customers never appear: only the subject and the customers it referred
    customers = {k[1][0] for k in roles if k[0] == t("customer")}
    assert customers == {subject} | {k[1][0] for k in referencing}
    # downstream only: upstream reference data and hidden copies are never records
    tables = {k[0] for k in roles}
    assert t("product") not in tables
    assert t("marketing_event_log") not in tables
    assert t("daily_revenue") not in tables
    if score.hidden_rows:
        assert score.recall_with_hidden is not None and score.recall_with_hidden < 1

    # AGGREGATE-only artifacts (no row in the record graph) = ground-truth aggregates
    aggregate_only = {
        a
        for a, tr in result.affected_artifacts.items()
        if tr is Traceability.AGGREGATE and a not in tables
    }
    assert aggregate_only == {TableRef.parse(a) for a in truth["aggregates"]}
    for table, _ in _rows(truth["derived_rows"]):
        assert table in result.affected_artifacts
    assert not any(tr is Traceability.OPAQUE for tr in result.affected_artifacts.values())
    return score


def _report(scores: list[Score]) -> None:
    def f(x: Fraction | None) -> str:
        return "n/a" if x is None else f"{float(x):.3f}"

    lines = ["subject  tp  fp  fn  precision  recall  f1     hidden  recall(+hidden)"]
    for s in scores:
        lines.append(
            f"{s.subject:>7} {s.tp:>3} {s.fp:>3} {s.fn:>3}  {f(s.precision):>9}  "
            f"{f(s.recall):>6}  {f(s.f1):>5}  {s.hidden_rows:>6}  {f(s.recall_with_hidden):>15}"
        )
    _log.warning("discovery vs ground truth\n%s", "\n".join(lines))


def _sample(customers: int, exclude: set[int], n: int, seed: int) -> list[int]:
    pool = [c for c in range(1, customers + 1) if c not in exclude]
    return sorted(random.Random(seed).sample(pool, n))


# ------------------------------------------------------------------ known subjects (tiny/7)


def test_primary_subject_roles(pipeline: Any) -> None:
    known = pipeline.truth.meta["known_subjects"]
    primary = known["primary"]
    assert primary == 7
    score = _assert_subject(pipeline, primary)
    _, result = _evaluate(pipeline, primary)
    roles = {(k.table, k.values): a.role for k, a in result.record_graph.nodes()}
    for referred in known["referred_by_primary"]:  # 15, 16
        assert roles[(t("customer"), (referred,))] is RecordRole.REFERENCING_OTHER_SUBJECT
    assert (t("customer"), (known["primary_referrer"],)) not in roles  # 3 is upstream
    assert roles[(t("customer"), (primary,))] is RecordRole.SUBJECT_ROOT
    shared = [k for k, r in roles.items() if r is RecordRole.SHARED]
    assert shared and all(k[0] == t("ticket_message") for k in shared)
    assert roles[(t("customer_ltv"), (primary,))] is RecordRole.OWNED  # predicate key (no PK)
    assert roles[(t("customer_segment"), (primary,))] is RecordRole.OWNED
    assert score.hidden_rows == 5  # 3 exact + upper/whitespace + title-case copies
    assert score.recall_with_hidden is not None and score.recall_with_hidden < 1


def test_secondary_subject_shares_the_same_messages(pipeline: Any) -> None:
    known = pipeline.truth.meta["known_subjects"]
    primary, secondary = known["primary"], known["secondary"]
    _assert_subject(pipeline, secondary)
    a = {k for k, r in _roles(pipeline, primary).items() if r is RecordRole.SHARED}
    b = {k for k, r in _roles(pipeline, secondary).items() if r is RecordRole.SHARED}
    assert a == b  # message by 8 on 7's ticket and by 7 on 8's ticket are shared by both


def _roles(pipeline: Any, subject: int) -> dict[Row, RecordRole]:
    result = discover_records(
        pipeline.graph, pipeline.root, (subject,), pipeline.reader, DiscoveryLimits()
    )
    return {(k.table, k.values): a.role for k, a in result.record_graph.nodes()}


@pytest.mark.parametrize("subject", ["referred_by_primary", "primary_referrer"])
def test_other_known_subjects(pipeline: Any, subject: str) -> None:
    known = pipeline.truth.meta["known_subjects"]
    ids = known[subject] if isinstance(known[subject], list) else [known[subject]]
    for cid in ids:
        _assert_subject(pipeline, cid)
    if subject == "primary_referrer":
        roles = _roles(pipeline, known["primary_referrer"])
        assert roles[(t("customer"), (known["primary"],))] is (RecordRole.REFERENCING_OTHER_SUBJECT)
        # the referred customer's own data is NOT traversed (never follow past another subject)
        primary_owned = _rows(pipeline.truth.subject(known["primary"])["owned"])
        assert set(roles) & primary_owned == {(t("customer"), (known["primary"],))}


def test_all_tiny_subjects_and_random_sample(pipeline: Any) -> None:
    meta = pipeline.truth.meta
    customers = meta["scale"]["customers"]
    known = meta["known_subjects"]
    special = {known["primary"], known["secondary"], known["primary_referrer"]}
    special |= set(known["referred_by_primary"])
    sample = _sample(customers, special, 10, seed=2026)
    assert len(sample) == 10
    scores = [_assert_subject(pipeline, c) for c in sorted(special) + sample]
    _report(scores)
    # every subject at tiny scale (20) is covered by the union of known + sample + the rest
    rest = [c for c in range(1, customers + 1) if c not in special and c not in sample]
    for c in rest:
        _assert_subject(pipeline, c)


# ------------------------------------------------------------------ small scale (100 customers)


@pytest.mark.parametrize("seeded_db", [{"scale": "small", "seed": 1}], indirect=True)
def test_small_scale_known_and_random_subjects(pipeline: Any) -> None:
    meta = pipeline.truth.meta
    assert meta["scale_name"] == "small"
    known = meta["known_subjects"]
    assert known["primary"] == 42
    special = {known["primary"], known["secondary"], known["primary_referrer"]}
    special |= set(known["referred_by_primary"])
    sample = _sample(meta["scale"]["customers"], special, 15, seed=42)
    scores = [_assert_subject(pipeline, c) for c in sorted(special) + sample]
    _report(scores)
    assert sum(s.hidden_rows > 0 for s in scores) >= 1


@pytest.mark.parametrize("seeded_db", [{"scale": "default", "seed": 42}], indirect=True)
def test_default_scale_random_subjects(pipeline: Any) -> None:
    meta = pipeline.truth.meta
    assert meta["scale"]["customers"] == 1000
    known = meta["known_subjects"]
    special = {known["primary"], known["secondary"], known["primary_referrer"]}
    special |= set(known["referred_by_primary"])
    sample = _sample(meta["scale"]["customers"], special, 20, seed=7)
    scores = [_assert_subject(pipeline, c) for c in sorted(special) + sample]
    _report(scores)
    assert sum(s.tp for s in scores) > 10 * len(scores)  # non-trivial subjects


# ------------------------------------------------------------------ mechanics on the real DB


def test_batching_and_determinism(pipeline: Any) -> None:
    primary = pipeline.truth.meta["known_subjects"]["primary"]
    limits = DiscoveryLimits()
    a = discover_records(pipeline.graph, pipeline.root, (primary,), pipeline.reader, limits)
    b = discover_records(
        pipeline.graph, pipeline.root, (primary,), pipeline.reader, DiscoveryLimits(batch_size=1)
    )
    assert a.record_graph.record_set_hash() == b.record_graph.record_set_hash()
    assert a.record_graph.nodes() == b.record_graph.nodes()
    assert a.record_graph.edges() == b.record_graph.edges()
    assert a.affected_artifacts == b.affected_artifacts
    fp = record_set_fingerprint(pipeline.graph, pipeline.root, (primary,), pipeline.reader, limits)
    assert fp == a.record_graph.record_set_hash()


def test_string_key_is_accepted_for_bigint_root(pipeline: Any) -> None:
    """Str-normalised keys bind with a cast to the column type (CR-1 §6, Lead decision 6)."""
    primary = pipeline.truth.meta["known_subjects"]["primary"]
    as_int = discover_records(
        pipeline.graph, pipeline.root, (primary,), pipeline.reader, DiscoveryLimits()
    )
    as_str = discover_records(
        pipeline.graph, pipeline.root, (str(primary),), pipeline.reader, DiscoveryLimits()
    )
    tables_int = sorted((k.table, len(k.values)) for k, _ in as_int.record_graph.nodes())
    tables_str = sorted((k.table, len(k.values)) for k, _ in as_str.record_graph.nodes())
    assert tables_int == tables_str


def test_limits_enforced_without_leaking_values(pipeline: Any) -> None:
    primary = pipeline.truth.meta["known_subjects"]["primary"]
    with pytest.raises(DiscoveryLimitExceeded) as exc:
        discover_records(
            pipeline.graph,
            pipeline.root,
            (primary,),
            pipeline.reader,
            DiscoveryLimits(max_records=5),
        )
    assert exc.value.details["limit"] == "max_records"
    assert set(exc.value.details) == {"limit", "value", "table"}
    with pytest.raises(DiscoveryLimitExceeded) as exc2:
        discover_records(
            pipeline.graph, pipeline.root, (primary,), pipeline.reader, DiscoveryLimits(max_depth=1)
        )
    assert exc2.value.details["limit"] == "max_depth"


def test_unknown_subject(pipeline: Any) -> None:
    with pytest.raises(SubjectNotFoundError) as exc:
        discover_records(
            pipeline.graph, pipeline.root, (987_654,), pipeline.reader, DiscoveryLimits()
        )
    assert "987654" not in str(exc.value) and "987654" not in repr(exc.value.details)
