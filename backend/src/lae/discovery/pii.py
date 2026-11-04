"""Heuristic PII suggestions from column names and types (module_contracts.md §5).

Suggestions are always unconfirmed (source HEURISTIC) and are never used for residual scans
until a human confirms them. Only base tables are scanned: view / matview columns are derived.
"""

from __future__ import annotations

import re

from lae.contracts.enums import ErasureStrategy, PiiCategory, TableKind
from lae.contracts.types import CatalogSnapshot, ColumnInfo, ColumnRef, PiiColumn

__all__ = ["classify_column", "suggest_pii"]

_DIRECT = re.compile(
    r"(^|_)(e?mail|email_address|phone|phone_number|mobile|telephone|tel|fax|ssn|"
    r"passport(_no|_number)?|national_id|tax_id|iban|full_name|first_name|last_name|"
    r"given_name|family_name|surname|username|login)($|_)"
)
_QUASI = re.compile(
    r"(^|_)(address|street|address_line\d?|city|postcode|postal_code|zip|zip_code|"
    r"birth_date|date_of_birth|dob|birthday|gender|ip|ip_address|latitude|longitude|geo)($|_)"
)
_SENSITIVE = re.compile(
    r"(^|_)(body|note|notes|comment|comments|message|description|free_text|payload|"
    r"health|diagnosis|religion|ethnicity)($|_)"
)
_TEXTUAL = ("text", "character", "varchar", "char", "citext", "json")


def _is_textual(data_type: str) -> bool:
    return data_type.lower().startswith(_TEXTUAL)


def classify_column(column: ColumnInfo) -> PiiCategory | None:
    """Best-guess category for a column, or None if nothing matches."""
    name = column.name.lower()
    dtype = column.data_type.lower()
    if dtype.startswith("vector"):
        return PiiCategory.EMBEDDING
    if _DIRECT.search(name) and _is_textual(dtype):
        return PiiCategory.DIRECT_IDENTIFIER
    if _QUASI.search(name):
        return PiiCategory.QUASI_IDENTIFIER
    if _SENSITIVE.search(name) and _is_textual(dtype):
        return PiiCategory.SENSITIVE
    return None


def suggest_pii(catalog: CatalogSnapshot) -> list[PiiColumn]:
    """Unconfirmed suggestions for base-table columns, sorted by column.

    Strategy: NOT NULL columns are PSEUDONYMIZE (they cannot be nulled), the rest NULLIFY.
    """
    out: list[PiiColumn] = []
    for ref in sorted(catalog.tables):
        info = catalog.tables[ref]
        if info.kind is not TableKind.TABLE:
            continue
        for col in sorted(info.columns, key=lambda c: c.ordinal):
            if col.name in info.primary_key:
                continue
            category = classify_column(col)
            if category is None:
                continue
            strategy = ErasureStrategy.NULLIFY if col.is_nullable else ErasureStrategy.PSEUDONYMIZE
            out.append(
                PiiColumn(
                    column=ColumnRef(ref, col.name),
                    category=category,
                    strategy=strategy,
                    is_confirmed=False,
                    use_for_residual_scan=False,
                )
            )
    return out
