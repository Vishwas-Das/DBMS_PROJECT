"""Security review M1: artifact definitions must be exactly one read-only query."""

from __future__ import annotations

import pytest

from lae.db._sql import assert_single_select
from lae.errors import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM app.customer WHERE id <= %(n)s",
        "SELECT customer_id, sum(amount_cents) FROM app.payment GROUP BY customer_id",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "SELECT 1 UNION ALL SELECT 2",
    ],
)
def test_accepts_single_queries(sql: str) -> None:
    assert assert_single_select(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1) x; COMMIT; DELETE FROM app.customer; SELECT (1",
        "SELECT 1; SELECT 2",
        "DELETE FROM app.customer",
        "UPDATE app.customer SET email = NULL",
        "WITH d AS (DELETE FROM app.customer RETURNING 1) SELECT * FROM d",
        "INSERT INTO app.customer SELECT * FROM app.customer",
        "DROP TABLE app.customer",
        "SELECT * INTO app.copy FROM app.customer",
        "COMMIT",
        "not sql at all ((",
    ],
)
def test_rejects_everything_else(sql: str) -> None:
    with pytest.raises(ValidationError):
        assert_single_select(sql)
