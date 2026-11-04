"""ArtifactRepo: registered derived artifacts (derived_artifact)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from lae.contracts.enums import ArtifactKind, RefreshStrategy
from lae.contracts.types import ArtifactDefinition, KeyMapping, TableRef
from lae.db.models import DerivedArtifact
from lae.db.records import ArtifactRecord
from lae.db.repositories._base import Repo


def mappings_to_json(mappings: Sequence[KeyMapping]) -> list[dict[str, Any]] | None:
    if not mappings:
        return None
    return [
        {
            "source": m.source.qualified(),
            "source_columns": list(m.source_columns),
            "target_columns": list(m.target_columns),
        }
        for m in mappings
    ]


def mappings_from_json(value: Any) -> tuple[KeyMapping, ...]:
    if not value:
        return ()
    return tuple(
        KeyMapping(
            TableRef.parse(m["source"]), tuple(m["source_columns"]), tuple(m["target_columns"])
        )
        for m in value
    )


def _record(m: DerivedArtifact) -> ArtifactRecord:
    return ArtifactRecord(
        definition=ArtifactDefinition(
            artifact_id=m.id,
            name=m.name,
            kind=ArtifactKind(m.kind),
            target=TableRef(m.schema_name, m.table_name),
            definition_sql=m.definition_sql,
            refresh_strategy=RefreshStrategy(m.refresh_strategy),
            key_mappings=mappings_from_json(m.source_key_mapping),
        ),
        is_auto_registered=m.is_auto_registered,
        is_active=m.is_active,
        is_inspectable=m.is_inspectable,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


class ArtifactRepo(Repo):
    """Methods:
    add(definition, *, is_auto_registered=False, is_inspectable=None, is_active=True)
        -> ArtifactRecord   (is_inspectable defaults to kind != EXTERNAL)
    update(definition, *, is_inspectable=None, is_active=None) -> ArtifactRecord
    get(artifact_id) -> ArtifactRecord
    get_by_name(name) -> ArtifactRecord | None
    find_by_target(table) -> ArtifactRecord | None
    list_artifacts(include_inactive=False) -> list[ArtifactRecord]             (by name)
    active_definitions() -> list[ArtifactDefinition]                 (by name)
    deactivate(artifact_id) -> ArtifactRecord                        (soft delete)
    """

    def add(
        self,
        definition: ArtifactDefinition,
        *,
        is_auto_registered: bool = False,
        is_inspectable: bool | None = None,
        is_active: bool = True,
    ) -> ArtifactRecord:
        kind = ArtifactKind(definition.kind)
        m = DerivedArtifact(
            id=definition.artifact_id,
            name=definition.name,
            kind=kind.value,
            schema_name=definition.target.schema,
            table_name=definition.target.name,
            definition_sql=definition.definition_sql,
            refresh_strategy=RefreshStrategy(definition.refresh_strategy).value,
            source_key_mapping=mappings_to_json(definition.key_mappings),
            is_auto_registered=is_auto_registered,
            is_active=is_active,
            is_inspectable=(kind is not ArtifactKind.EXTERNAL)
            if is_inspectable is None
            else is_inspectable,
        )
        self.session.add(m)
        self.session.flush()
        self.session.refresh(m)
        return _record(m)

    def update(
        self,
        definition: ArtifactDefinition,
        *,
        is_inspectable: bool | None = None,
        is_active: bool | None = None,
    ) -> ArtifactRecord:
        m = self._get(DerivedArtifact, definition.artifact_id)
        m.name = definition.name
        m.kind = ArtifactKind(definition.kind).value
        m.schema_name = definition.target.schema
        m.table_name = definition.target.name
        m.definition_sql = definition.definition_sql
        m.refresh_strategy = RefreshStrategy(definition.refresh_strategy).value
        m.source_key_mapping = mappings_to_json(definition.key_mappings)
        if is_inspectable is not None:
            m.is_inspectable = is_inspectable
        if is_active is not None:
            m.is_active = is_active
        m.updated_at = func.now()
        self.session.flush()
        self.session.refresh(m)
        return _record(m)

    def get(self, artifact_id: UUID) -> ArtifactRecord:
        return _record(self._get(DerivedArtifact, artifact_id))

    def get_by_name(self, name: str) -> ArtifactRecord | None:
        m = self.session.scalars(
            select(DerivedArtifact).where(DerivedArtifact.name == name)
        ).first()
        return None if m is None else _record(m)

    def find_by_target(self, table: TableRef) -> ArtifactRecord | None:
        m = self.session.scalars(
            select(DerivedArtifact).where(
                DerivedArtifact.schema_name == table.schema,
                DerivedArtifact.table_name == table.name,
            )
        ).first()
        return None if m is None else _record(m)

    def list_artifacts(self, include_inactive: bool = False) -> list[ArtifactRecord]:
        q = select(DerivedArtifact).order_by(DerivedArtifact.name)
        if not include_inactive:
            q = q.where(DerivedArtifact.is_active.is_(True))
        return [_record(m) for m in self.session.scalars(q)]

    def active_definitions(self) -> list[ArtifactDefinition]:
        return [r.definition for r in self.list_artifacts()]

    def deactivate(self, artifact_id: UUID) -> ArtifactRecord:
        m = self._get(DerivedArtifact, artifact_id)
        m.is_active = False
        m.updated_at = func.now()
        self.session.flush()
        self.session.refresh(m)
        return _record(m)
