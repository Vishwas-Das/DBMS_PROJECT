"""Canonical JSON + SHA-256 (architecture.md §13). FROZEN: shared by lineage, discovery, planner.

Rules: UTF-8, sorted keys, no whitespace, ensure_ascii=False; floats/NaN rejected; Decimal ->
string; UUID -> lowercase string; datetime must be tz-aware -> ISO-8601 UTC with 'Z';
StrEnum -> its value; tuple -> list; mapping keys must be str.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from lae.errors import CanonicalizationError


def _normalize(obj: Any, path: str = "$") -> Any:
    if obj is None or isinstance(obj, bool | str):
        return obj
    if isinstance(obj, Enum):
        return _normalize(obj.value, path)
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        raise CanonicalizationError(f"floats are not allowed in canonical JSON (at {path})")
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise CanonicalizationError(f"non-finite Decimal at {path}")
        return format(obj, "f")
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.utcoffset() is None:
            raise CanonicalizationError(f"naive datetime at {path}")
        return obj.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"non-string key {k!r} at {path}")
            out[k] = _normalize(v, f"{path}.{k}")
        return out
    if isinstance(obj, list | tuple):
        return [_normalize(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    raise CanonicalizationError(f"unsupported type {type(obj).__name__} at {path}")


def canonical_json(obj: Any) -> bytes:
    """Deterministic byte serialization of `obj`."""
    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj: Any) -> str:
    """sha256_hex(canonical_json(obj))."""
    return sha256_hex(canonical_json(obj))
