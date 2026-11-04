"""PII-free, explainable sentences for plan items and conflicts.

Only table names, column names, constraint names, artifact names, enum values and counts are
ever interpolated. Record key values never are.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from lae.contracts.enums import OnDeleteAction
from lae.contracts.types import RelationshipEdge

__all__ = [
    "DEFINITION_TOKEN",
    "definition_token",
    "edge_label",
    "edge_labels",
    "join_columns",
    "rows",
]

DEFINITION_TOKEN = "definition_sha256="  # noqa: S105 - not a secret
"""Prefix binding a RECOMPUTE_TABLE item to the SHA-256 of its definition SQL (the reason is
part of the plan hash, and the executor checks the token). See CR erasure-engine-engineer-1."""

ON_DELETE_TEXT: dict[OnDeleteAction, str] = {
    OnDeleteAction.NO_ACTION: "NO ACTION",
    OnDeleteAction.RESTRICT: "RESTRICT",
    OnDeleteAction.CASCADE: "CASCADE",
    OnDeleteAction.SET_NULL: "SET NULL",
    OnDeleteAction.SET_DEFAULT: "SET DEFAULT",
}


def rows(n: int) -> str:
    return f"{n} row" if n == 1 else f"{n} rows"


def join_columns(cols: Iterable[str]) -> str:
    return ", ".join(cols)


def edge_label(e: RelationshipEdge) -> str:
    if e.on_delete is not None:
        name = e.constraint_name or f"{e.source.qualified()}->{e.target.qualified()}"
        return f"FK {name} (ON DELETE {ON_DELETE_TEXT[e.on_delete]})"
    name = e.constraint_name or f"{e.source.qualified()}->{e.target.qualified()}"
    cols = join_columns(e.target_columns)
    return f"{e.origin.value} {name} ({cols})" if cols else f"{e.origin.value} {name}"


def edge_labels(edges: Iterable[RelationshipEdge]) -> str:
    labels = sorted({edge_label(e) for e in edges})
    return ", ".join(labels) if labels else "no lineage edge"


def definition_token(definition_sql: str) -> str:
    return DEFINITION_TOKEN + hashlib.sha256(definition_sql.encode("utf-8")).hexdigest()
