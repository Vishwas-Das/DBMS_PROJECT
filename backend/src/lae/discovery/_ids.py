"""Deterministic identifiers for discovered edges and artifacts (uuid5, fixed namespaces)."""

from __future__ import annotations

from uuid import UUID, uuid5

# Fixed namespaces. Changing them changes every edge id, so they are frozen.
NS_FK = UUID("6f1c2a55-3d0e-5a3c-9b1e-1c5e0f7a0001")
NS_SQL = UUID("6f1c2a55-3d0e-5a3c-9b1e-1c5e0f7a0002")
NS_INFERRED = UUID("6f1c2a55-3d0e-5a3c-9b1e-1c5e0f7a0003")
NS_ARTIFACT = UUID("6f1c2a55-3d0e-5a3c-9b1e-1c5e0f7a0004")


def stable_id(namespace: UUID, *parts: object) -> UUID:
    """uuid5 over the parts joined by U+001F (a separator that never occurs in identifiers)."""
    return uuid5(namespace, "\x1f".join(str(p) for p in parts))


def cols(columns: tuple[str, ...]) -> str:
    return ",".join(columns)
