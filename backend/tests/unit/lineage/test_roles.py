"""RecordGraph.set_role (post-BFS re-classification) and RecordDiscoveryResult identity."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from lae.contracts.enums import RecordRole, Traceability
from lae.contracts.types import RecordKey, TableRef
from lae.errors import GraphIntegrityError
from lae.lineage import RecordAttrs, RecordDiscoveryResult, RecordGraph

pytestmark = pytest.mark.unit

CUST = TableRef("app", "customer")
THREAD = TableRef("app", "message_thread")
FK_A = UUID(int=1)
FK_B = UUID(int=2)


def cust(i: int) -> RecordKey:
    return RecordKey(CUST, ("id",), (i,))


def thread(i: int) -> RecordKey:
    return RecordKey(THREAD, ("id",), (i,))


def shared_case() -> RecordGraph:
    """customer 42 -> thread 9 (OWNED during BFS) and customer 42 -> customer 77 (referrer)."""
    rg = RecordGraph()
    rg.add_root(cust(42))
    rg.add_dependency(cust(42), thread(9), FK_A, RecordRole.OWNED)
    rg.add_dependency(cust(42), cust(77), FK_B, RecordRole.REFERENCING_OTHER_SUBJECT)
    return rg


def test_set_role_reclassifies_after_bfs() -> None:
    rg = shared_case()
    before = rg.attrs(thread(9))
    rg.set_role(thread(9), RecordRole.SHARED)
    assert rg.attrs(thread(9)) == RecordAttrs(RecordRole.SHARED, before.depth, before.via_edge_ids)
    # edges, ordering and record-set hash are unaffected by roles
    assert rg.edges() == shared_case().edges()
    assert rg.record_set_hash() == shared_case().record_set_hash()
    roles = {n["role"] for n in rg.to_node_link(lambda k: repr(k.sort_key()))["nodes"]}
    assert roles == {"SUBJECT_ROOT", "SHARED", "REFERENCING_OTHER_SUBJECT"}


def test_set_role_is_an_explicit_override() -> None:
    """Unlike add_dependency, set_role may lower the role (e.g. a reviewer's decision)."""
    rg = shared_case()
    rg.set_role(cust(77), RecordRole.SHARED)
    assert rg.attrs(cust(77)).role is RecordRole.SHARED
    rg.set_role(cust(77), RecordRole.OWNED)
    assert rg.attrs(cust(77)).role is RecordRole.OWNED
    rg.set_role(cust(77), RecordRole.OWNED)  # idempotent
    assert rg.attrs(cust(77)).role is RecordRole.OWNED


def test_add_dependency_after_set_role_keeps_precedence() -> None:
    rg = shared_case()
    rg.set_role(thread(9), RecordRole.SHARED)
    rg.add_dependency(cust(42), thread(9), FK_B, RecordRole.OWNED)
    a = rg.attrs(thread(9))
    assert a.role is RecordRole.SHARED
    assert a.via_edge_ids == (FK_A, FK_B)


def test_set_role_errors_leak_no_values() -> None:
    rg = shared_case()
    with pytest.raises(GraphIntegrityError) as ei:
        rg.set_role(thread(12345), RecordRole.SHARED)
    assert ei.value.code == "UNKNOWN_RECORD"
    assert "12345" not in json.dumps(ei.value.details)
    with pytest.raises(GraphIntegrityError) as ei:
        rg.set_role(thread(9), RecordRole.SUBJECT_ROOT)
    assert ei.value.code == "INVALID_ROLE"
    with pytest.raises(GraphIntegrityError) as ei:
        rg.set_role(cust(42), RecordRole.SHARED)
    assert ei.value.code == "INVALID_ROLE"
    assert "42" not in json.dumps(ei.value.details)
    assert rg.attrs(cust(42)).role is RecordRole.SUBJECT_ROOT
    assert rg.attrs(thread(9)).role is RecordRole.OWNED


def test_record_discovery_result_identity_equality() -> None:
    rg = shared_case()
    a = RecordDiscoveryResult(rg, {CUST: Traceability.KEY_PRESERVING})
    b = RecordDiscoveryResult(rg, {CUST: Traceability.KEY_PRESERVING})
    assert a == a
    assert a != b  # eq=False: identity, not field equality
    assert len({a, b}) == 2  # hashable by identity (RecordGraph itself is unhashable)
