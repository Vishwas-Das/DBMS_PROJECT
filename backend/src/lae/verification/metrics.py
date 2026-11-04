"""Exact evaluation metrics used by benchmarks and the API.

All ratios are returned as :class:`fractions.Fraction`, so nothing that is hashed or compared
ever contains a float. Callers convert to ``float`` only for display or CSV output.

**Undefined ratios.** A ratio whose denominator is zero (``0/0``) is *undefined* and returned
as ``None`` rather than being silently mapped to 0 or 1, because both choices would bias
averages across benchmark runs:

- ``precision(0, 0)`` (nothing discovered) is ``None``;
- ``recall(0, 0)`` (nothing to find) is ``None``;
- ``f1(p, r)`` is ``None`` if ``p`` or ``r`` is ``None``, or if ``p + r == 0`` (``2pr/(p+r)``
  is ``0/0``);
- ``coverage(0, 0)`` (no required artifacts) is ``None``.

Note: ``VerificationReport.coverage`` (contracts, CR lead-1 §3) defines coverage with no
required artifacts as ``1`` because it decides the outcome ``VERIFIED``. ``coverage`` here is
the *reporting* metric and keeps the undefined case visible.
"""

from __future__ import annotations

from collections.abc import Hashable, Set
from fractions import Fraction

from lae.errors import ValidationError

__all__ = ["confusion", "coverage", "f1", "precision", "recall"]


def _count(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a non-negative int")
    return value


def _ratio(num: int, den: int) -> Fraction | None:
    return None if den == 0 else Fraction(num, den)


def precision(tp: int, fp: int) -> Fraction | None:
    """``tp / (tp + fp)``; ``None`` when nothing was discovered."""
    tp, fp = _count("tp", tp), _count("fp", fp)
    return _ratio(tp, tp + fp)


def recall(tp: int, fn: int) -> Fraction | None:
    """``tp / (tp + fn)``; ``None`` when the ground truth is empty."""
    tp, fn = _count("tp", tp), _count("fn", fn)
    return _ratio(tp, tp + fn)


def f1(p: Fraction | None, r: Fraction | None) -> Fraction | None:
    """Harmonic mean ``2pr / (p + r)``; ``None`` if either input is ``None`` or ``p + r == 0``."""
    if p is None or r is None:
        return None
    for name, v in (("precision", p), ("recall", r)):
        if not isinstance(v, Fraction) or not 0 <= v <= 1:
            raise ValidationError(f"{name} must be a Fraction in [0, 1]")
    if p + r == 0:
        return None
    return 2 * p * r / (p + r)


def coverage(verified: int, required: int) -> Fraction | None:
    """``verified / required``; ``None`` when nothing is required."""
    verified, required = _count("verified", verified), _count("required", required)
    if verified > required:
        raise ValidationError("verified cannot exceed required")
    return _ratio(verified, required)


def confusion(discovered: Set[Hashable], truth: Set[Hashable]) -> tuple[int, int, int]:
    """``(tp, fp, fn)`` = ``(|D & T|, |D - T|, |T - D|)``."""
    return len(discovered & truth), len(discovered - truth), len(truth - discovered)
