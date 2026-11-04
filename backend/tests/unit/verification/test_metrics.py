from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lae.errors import ValidationError
from lae.verification.metrics import confusion, coverage, f1, precision, recall

pytestmark = pytest.mark.unit


def test_precision_recall_exact() -> None:
    assert precision(2, 1) == Fraction(2, 3)
    assert recall(2, 2) == Fraction(1, 2)
    assert isinstance(precision(1, 2), Fraction)
    assert precision(0, 5) == 0
    assert recall(5, 0) == 1


def test_zero_over_zero_is_none() -> None:
    assert precision(0, 0) is None
    assert recall(0, 0) is None
    assert coverage(0, 0) is None
    assert f1(None, Fraction(1)) is None
    assert f1(Fraction(1), None) is None
    assert f1(Fraction(0), Fraction(0)) is None


def test_f1() -> None:
    assert f1(Fraction(1), Fraction(1)) == 1
    assert f1(Fraction(1, 2), Fraction(1)) == Fraction(2, 3)
    assert f1(Fraction(0), Fraction(1)) == 0


def test_coverage() -> None:
    assert coverage(9, 10) == Fraction(9, 10)
    assert coverage(3, 3) == 1
    with pytest.raises(ValidationError):
        coverage(4, 3)


@pytest.mark.parametrize("bad", [-1, True, 1.0, "1"])
def test_invalid_counts(bad: object) -> None:
    for fn in (precision, recall, coverage):
        with pytest.raises(ValidationError):
            fn(bad, 1)  # type: ignore[operator]
        with pytest.raises(ValidationError):
            fn(0, bad)  # type: ignore[operator]


@pytest.mark.parametrize("bad", [Fraction(3, 2), Fraction(-1, 2), 0.5])
def test_f1_rejects_out_of_range(bad: object) -> None:
    with pytest.raises(ValidationError):
        f1(bad, Fraction(1, 2))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        f1(Fraction(1, 2), bad)  # type: ignore[arg-type]


def test_confusion() -> None:
    assert confusion({1, 2, 3}, {2, 3, 4, 5}) == (2, 1, 2)
    assert confusion(set(), set()) == (0, 0, 0)
    assert confusion(frozenset({"a"}), set()) == (0, 1, 0)


@given(st.sets(st.integers(0, 30)), st.sets(st.integers(0, 30)))
def test_confusion_identities(d: set[int], t: set[int]) -> None:
    tp, fp, fn = confusion(d, t)
    assert tp + fp == len(d) and tp + fn == len(t)
    p, r = precision(tp, fp), recall(tp, fn)
    score = f1(p, r)
    if p is not None and r is not None and p + r:
        assert score == Fraction(2 * tp, 2 * tp + fp + fn)
    else:
        assert score is None
