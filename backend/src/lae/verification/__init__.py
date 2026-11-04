"""lae.verification: independent post-execution verification (module_contracts.md §8.1)."""

from __future__ import annotations

from lae.verification.checks import PSEUDONYM_PATTERN, is_anonymized
from lae.verification.engine import verify_execution
from lae.verification.metrics import confusion, coverage, f1, precision, recall
from lae.verification.outcome import build_report, item_verified

__all__ = [
    "PSEUDONYM_PATTERN",
    "build_report",
    "confusion",
    "coverage",
    "f1",
    "is_anonymized",
    "item_verified",
    "precision",
    "recall",
    "verify_execution",
]
