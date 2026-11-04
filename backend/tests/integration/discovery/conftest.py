"""Fixtures for lae.discovery integration tests (owned by the Lineage Discovery Engineer).

Everything runs against REAL PostgreSQL through the Database Engineer's adapters
(`PgCatalogReader`, `PgTargetReader`, repositories) on the seeded demo dataset.

Database isolation: parallel workstreams use separate test databases. The shared
``migrated_url`` guard only accepts ``lae_test``; this directory narrows the override to
``lae_test`` and this workstream's own ``lae_test_disc`` (never ``lae`` or another agent's DB).

The ``pipeline`` fixture wires the full §6-§8 discovery path the service layer will use:
catalog -> declared FK edges -> auto + registered artifacts -> SQL lineage with ``extra_keys`` =
FK child columns referencing the subject-root chain (CR lineage-discovery-engineer-1 §2, Lead
decision 2) -> LineageGraph -> PgTargetReader.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from lae.contracts.enums import LineageLevel
from lae.contracts.types import (
    ArtifactDefinition,
    CatalogSnapshot,
    PolicySet,
    RelationshipEdge,
    SqlLineageResult,
    SubjectRoot,
    TableRef,
)
from lae.db.catalog_queries import PgCatalogReader
from lae.db.repositories import ArtifactRepo, PolicyRepo
from lae.db.target import PgTargetReader
from lae.discovery import (
    artifacts_from_catalog,
    discover_foreign_keys,
    extract_artifacts_lineage,
    inspectable_artifacts,
    merge_registered,
    read_catalog,
)
from lae.lineage import LineageGraph, build_lineage_graph

ALLOWED_DISCOVERY_DATABASES = frozenset({"lae_test", "lae_test_disc"})
APP = "app"


@pytest.fixture(scope="session")
def migrated_url(pg_url: str) -> str:
    """Override of the shared guard: `lae_test` or this workstream's `lae_test_disc` only."""
    url = pg_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    if make_url(url).database not in ALLOWED_DISCOVERY_DATABASES:
        pytest.fail(
            f"discovery integration tests only run against {sorted(ALLOWED_DISCOVERY_DATABASES)!r}",
            pytrace=False,
        )
    return url


def t(name: str) -> TableRef:
    return TableRef(APP, name)


def subject_chain_keys(
    fk_edges: list[RelationshipEdge], graph_root: TableRef
) -> dict[TableRef, set[tuple[str, ...]]]:
    """FK child columns whose parent is the subject root or reachable from it via FKs.

    This is what the service passes as `extra_keys` (CR lineage-discovery-engineer-1 §2).
    """
    table_edges = [e for e in fk_edges if e.level is LineageLevel.TABLE]
    reachable = {graph_root}
    changed = True
    while changed:
        changed = False
        for e in table_edges:
            if e.source in reachable and e.target not in reachable:
                reachable.add(e.target)
                changed = True
    keys: dict[TableRef, set[tuple[str, ...]]] = {}
    for e in table_edges:
        if e.source in reachable:
            keys.setdefault(e.target, set()).add(tuple(e.target_columns))
    return keys


@dataclass
class Pipeline:
    conn: Connection
    session: Session
    truth: Any
    catalog: CatalogSnapshot
    fk_edges: list[RelationshipEdge]
    artifacts: list[ArtifactDefinition]
    lineage: list[SqlLineageResult]
    extra_keys: Mapping[TableRef, set[tuple[str, ...]]]
    graph: LineageGraph
    root: SubjectRoot
    policies: PolicySet
    reader: PgTargetReader

    def lineage_for(self, target: TableRef) -> SqlLineageResult:
        by_id = {a.artifact_id: a for a in self.artifacts}
        for r in self.lineage:
            if by_id[r.artifact_id].target == target:
                return r
        raise KeyError(target)


@pytest.fixture
def disc_session(app_conn: Connection) -> Iterator[Session]:
    session = Session(bind=app_conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def pipeline(
    seeded_db: tuple[Engine, Any], app_conn: Connection, disc_session: Session
) -> Pipeline:
    _engine, truth = seeded_db
    catalog = read_catalog(PgCatalogReader(app_conn), [APP])
    fk_edges = discover_foreign_keys(PgCatalogReader(app_conn), catalog)
    policy_repo = PolicyRepo(disc_session)
    root_record = policy_repo.subject_root_by_name("customer")
    assert root_record is not None
    root = root_record.root
    registered = ArtifactRepo(disc_session).active_definitions()
    artifacts = inspectable_artifacts(
        merge_registered(artifacts_from_catalog(catalog), registered), catalog
    )
    extra_keys = subject_chain_keys(fk_edges, root.table)
    lineage = extract_artifacts_lineage(artifacts, catalog, extra_keys=extra_keys)
    artifact_edges = [e for r in lineage for e in r.edges]
    graph = build_lineage_graph(
        catalog, [*fk_edges, *artifact_edges], artifacts=[a.target for a in artifacts]
    )
    return Pipeline(
        conn=app_conn,
        session=disc_session,
        truth=truth,
        catalog=catalog,
        fk_edges=fk_edges,
        artifacts=artifacts,
        lineage=lineage,
        extra_keys=extra_keys,
        graph=graph,
        root=root,
        policies=policy_repo.policy_set(),
        reader=PgTargetReader(app_conn, catalog),
    )
