"""Derived-artifact registry helpers (architecture.md §7, CR lead-1 §2).

- Views and materialized views found in the catalog are auto-registered with the definition
  SQL taken from the catalog (``pg_get_viewdef``).
- Registered artifacts win over auto-registered ones for the same target table.
- EXTERNAL artifacts (and any artifact whose target is not in the catalog) are not inspectable:
  `external_dependencies` returns them so the planner can emit SCHEDULE_EXTERNAL / UNRESOLVED.
  No lineage edges are invented for them.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from lae.contracts.enums import ArtifactKind, RefreshStrategy, TableKind
from lae.contracts.types import ArtifactDefinition, CatalogSnapshot, TableRef
from lae.discovery._ids import NS_ARTIFACT, stable_id

__all__ = [
    "artifacts_from_catalog",
    "auto_artifact_id",
    "external_dependencies",
    "inspectable_artifacts",
    "merge_registered",
]

_AUTO_KINDS = {
    TableKind.VIEW: (ArtifactKind.VIEW, RefreshStrategy.NONE),
    TableKind.MATERIALIZED_VIEW: (ArtifactKind.MATERIALIZED_VIEW, RefreshStrategy.REFRESH_MATVIEW),
}


def auto_artifact_id(target: TableRef) -> UUID:
    """Deterministic artifact id of an auto-registered view / matview."""
    return stable_id(NS_ARTIFACT, "auto", target.qualified())


def artifacts_from_catalog(catalog: CatalogSnapshot) -> list[ArtifactDefinition]:
    """One artifact per VIEW / MATERIALIZED_VIEW in the catalog, sorted by target."""
    out: list[ArtifactDefinition] = []
    for ref in sorted(catalog.tables):
        info = catalog.tables[ref]
        if info.kind not in _AUTO_KINDS:
            continue
        kind, strategy = _AUTO_KINDS[info.kind]
        out.append(
            ArtifactDefinition(
                artifact_id=auto_artifact_id(ref),
                name=ref.qualified(),
                kind=kind,
                target=ref,
                definition_sql=info.definition_sql,
                refresh_strategy=strategy,
                key_mappings=(),
            )
        )
    return out


def merge_registered(
    auto: Iterable[ArtifactDefinition], registered: Iterable[ArtifactDefinition]
) -> list[ArtifactDefinition]:
    """Union keyed by target table; a registered entry replaces the auto entry. Sorted by target."""
    merged = {a.target: a for a in auto}
    for r in registered:
        merged[r.target] = r
    return [merged[t] for t in sorted(merged)]


def _is_external(artifact: ArtifactDefinition, catalog: CatalogSnapshot) -> bool:
    return artifact.kind is ArtifactKind.EXTERNAL or not catalog.has_table(artifact.target)


def external_dependencies(
    artifacts: Iterable[ArtifactDefinition], catalog: CatalogSnapshot
) -> list[ArtifactDefinition]:
    """Artifacts that cannot be inspected in the database, sorted by (target, id)."""
    return sorted(
        (a for a in artifacts if _is_external(a, catalog)),
        key=lambda a: (a.target, str(a.artifact_id)),
    )


def inspectable_artifacts(
    artifacts: Iterable[ArtifactDefinition], catalog: CatalogSnapshot
) -> list[ArtifactDefinition]:
    """The complement of `external_dependencies` (input for SQL lineage)."""
    return sorted(
        (a for a in artifacts if not _is_external(a, catalog)),
        key=lambda a: (a.target, str(a.artifact_id)),
    )
