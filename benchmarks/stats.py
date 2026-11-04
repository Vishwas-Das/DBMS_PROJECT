"""Small, dependency-free statistics for latency summaries."""

from __future__ import annotations

from collections.abc import Sequence


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default, 'linear')."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def describe(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "n": 0,
            "p50": None,
            "p95": None,
            "mean": None,
            "min": None,
            "max": None,
        }
    return {
        "n": len(values),
        "p50": round(percentile(values, 0.50) or 0.0, 3),
        "p95": round(percentile(values, 0.95) or 0.0, 3),
        "mean": round(sum(values) / len(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }
