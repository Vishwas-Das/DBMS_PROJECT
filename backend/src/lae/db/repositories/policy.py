"""PolicyRepo: retention policies, PII classifications and subject roots (configuration)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select

from lae.contracts.enums import (
    ErasureStrategy,
    PiiCategory,
    PiiSource,
    RetentionAction,
)
from lae.contracts.types import ColumnRef, PiiColumn, PolicySet, SubjectRoot, TableRef
from lae.db.models import PiiClassification, RetentionPolicyModel, SubjectRootModel
from lae.db.records import PiiRecord, RetentionRecord, SubjectRootRecord
from lae.db.repositories._base import Repo


def _pii(m: PiiClassification) -> PiiRecord:
    return PiiRecord(
        id=m.id,
        pii=PiiColumn(
            column=ColumnRef(TableRef(m.schema_name, m.table_name), m.column_name),
            category=PiiCategory(m.category),
            strategy=ErasureStrategy(m.erasure_strategy),
            is_confirmed=m.is_confirmed,
            use_for_residual_scan=m.use_for_residual_scan,
        ),
        source=PiiSource(m.source),
    )


def _retention(m: RetentionPolicyModel) -> RetentionRecord:
    return RetentionRecord(
        m.id, TableRef(m.schema_name, m.table_name), RetentionAction(m.action), m.legal_basis
    )


def _root(m: SubjectRootModel) -> SubjectRootRecord:
    return SubjectRootRecord(
        name=m.name,
        root=SubjectRoot(
            root_id=m.id,
            table=TableRef(m.schema_name, m.table_name),
            key_columns=tuple(m.key_columns),
            identifier_columns=tuple(m.identifier_columns),
        ),
    )


class PolicyRepo(Repo):
    """Methods:
    policy_set() -> PolicySet                         (all retention rows + all PII rows)
    list_retention() -> list[RetentionRecord]
    upsert_retention(table, action, legal_basis=None) -> RetentionRecord
    delete_retention(table) -> bool
    list_pii(*, confirmed_only=False) -> list[PiiRecord]
    get_pii(pii_id) -> PiiRecord
    upsert_pii(pii, source=MANUAL) -> PiiRecord
    update_pii(pii_id, *, category, strategy, is_confirmed, use_for_residual_scan) -> PiiRecord
    add_subject_root(name, table, key_columns, identifier_columns=()) -> SubjectRootRecord
    get_subject_root(root_id) -> SubjectRootRecord
    subject_root_by_name(name) -> SubjectRootRecord | None
    list_subject_roots() -> list[SubjectRootRecord]
    """

    # ------------------------------------------------------------ policy set
    def policy_set(self) -> PolicySet:
        retention = {r.table: r.action for r in self.list_retention()}
        pii = {r.pii.column: r.pii for r in self.list_pii()}
        return PolicySet(retention=retention, pii=pii)

    # ------------------------------------------------------------ retention
    def list_retention(self) -> list[RetentionRecord]:
        q = select(RetentionPolicyModel).order_by(
            RetentionPolicyModel.schema_name, RetentionPolicyModel.table_name
        )
        return [_retention(m) for m in self.session.scalars(q)]

    def _retention_model(self, table: TableRef) -> RetentionPolicyModel | None:
        return self.session.scalars(
            select(RetentionPolicyModel).where(
                RetentionPolicyModel.schema_name == table.schema,
                RetentionPolicyModel.table_name == table.name,
            )
        ).first()

    def upsert_retention(
        self, table: TableRef, action: RetentionAction, legal_basis: str | None = None
    ) -> RetentionRecord:
        m = self._retention_model(table)
        if m is None:
            m = RetentionPolicyModel(schema_name=table.schema, table_name=table.name)
            self.session.add(m)
        m.action = RetentionAction(action).value
        m.legal_basis = legal_basis
        self.session.flush()
        return _retention(m)

    def delete_retention(self, table: TableRef) -> bool:
        m = self._retention_model(table)
        if m is None:
            return False
        self.session.delete(m)
        self.session.flush()
        return True

    # ------------------------------------------------------------ PII
    def list_pii(self, *, confirmed_only: bool = False) -> list[PiiRecord]:
        q = select(PiiClassification).order_by(
            PiiClassification.schema_name,
            PiiClassification.table_name,
            PiiClassification.column_name,
        )
        if confirmed_only:
            q = q.where(PiiClassification.is_confirmed.is_(True))
        return [_pii(m) for m in self.session.scalars(q)]

    def get_pii(self, pii_id: UUID) -> PiiRecord:
        return _pii(self._get(PiiClassification, pii_id))

    def upsert_pii(self, pii: PiiColumn, source: PiiSource = PiiSource.MANUAL) -> PiiRecord:
        ref = pii.column
        m = self.session.scalars(
            select(PiiClassification).where(
                PiiClassification.schema_name == ref.table.schema,
                PiiClassification.table_name == ref.table.name,
                PiiClassification.column_name == ref.column,
            )
        ).first()
        if m is None:
            m = PiiClassification(
                schema_name=ref.table.schema, table_name=ref.table.name, column_name=ref.column
            )
            self.session.add(m)
        m.category = PiiCategory(pii.category).value
        m.erasure_strategy = ErasureStrategy(pii.strategy).value
        m.source = PiiSource(source).value
        m.is_confirmed = pii.is_confirmed
        m.use_for_residual_scan = pii.use_for_residual_scan
        self.session.flush()
        return _pii(m)

    def update_pii(
        self,
        pii_id: UUID,
        *,
        category: PiiCategory | None = None,
        strategy: ErasureStrategy | None = None,
        is_confirmed: bool | None = None,
        use_for_residual_scan: bool | None = None,
    ) -> PiiRecord:
        m = self._get(PiiClassification, pii_id)
        if category is not None:
            m.category = PiiCategory(category).value
        if strategy is not None:
            m.erasure_strategy = ErasureStrategy(strategy).value
        if is_confirmed is not None:
            m.is_confirmed = is_confirmed
        if use_for_residual_scan is not None:
            m.use_for_residual_scan = use_for_residual_scan
        self.session.flush()
        return _pii(m)

    # ------------------------------------------------------------ subject roots
    def add_subject_root(
        self,
        name: str,
        table: TableRef,
        key_columns: Sequence[str],
        identifier_columns: Sequence[str] = (),
    ) -> SubjectRootRecord:
        m = SubjectRootModel(
            name=name,
            schema_name=table.schema,
            table_name=table.name,
            key_columns=list(key_columns),
            identifier_columns=list(identifier_columns),
        )
        self.session.add(m)
        self.session.flush()
        return _root(m)

    def get_subject_root(self, root_id: UUID) -> SubjectRootRecord:
        return _root(self._get(SubjectRootModel, root_id))

    def subject_root_by_name(self, name: str) -> SubjectRootRecord | None:
        m = self.session.scalars(
            select(SubjectRootModel).where(SubjectRootModel.name == name)
        ).first()
        return None if m is None else _root(m)

    def list_subject_roots(self) -> list[SubjectRootRecord]:
        q = select(SubjectRootModel).order_by(SubjectRootModel.name)
        return [_root(m) for m in self.session.scalars(q)]
