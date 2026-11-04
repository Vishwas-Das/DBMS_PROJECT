"""E2E / security fixtures (QA-owned). REAL PostgreSQL through the HTTP API.

Reuses the Database Engineer's integration fixtures (``migrated_db``, ``seeded_db`` ...) from
``tests/integration/conftest.py`` by loading that module, so there is one implementation of
the reset / migrate / seed logic. The same guard applies: only ``lae_test`` or
``lae_test_<suffix>`` databases (set ``LAE_TEST_DATABASE_URL``, e.g. ``.../lae_test_qa``).
If PostgreSQL is unreachable every test here is SKIPPED with the reason (never passes).

``api_env`` builds a Runtime with ALL role URLs pointing at the test database and the repo
``.env`` disabled (``_env_file=None``): otherwise ``LAE_READER_URL`` from a developer's ``.env``
would silently point the reader engine at the dev database.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

_INTEGRATION_CONFTEST = Path(__file__).resolve().parents[1] / "integration" / "conftest.py"


def _load_integration_fixtures() -> Any:
    name = "lae_tests_integration_conftest_for_e2e"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _INTEGRATION_CONFTEST)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_fx = _load_integration_fixtures()
migrated_url = _fx.migrated_url
migrated_db = _fx.migrated_db
generator = _fx.generator
_dataset_state = _fx._dataset_state
mark_dataset_dirty = _fx.mark_dataset_dirty
seeded_db = _fx.seeded_db

TOKEN = "e2e-security-token-" + "q" * 24
APPROVER = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "qa-approver"}
OPERATOR = {"X-LAE-Token": TOKEN, "X-LAE-Actor": "qa-operator"}


@pytest.fixture
def fresh_db(
    request: pytest.FixtureRequest,
    migrated_db: sa.Engine,
    migrated_url: str,
    generator: Any,
    mark_dataset_dirty: Any,
) -> tuple[sa.Engine, Any]:
    """Per test: drop + re-migrate `lae` AND re-seed `app` (default tiny/7).

    Security tests commit erasures, open requests and audit events; the engine allows one open
    request per subject, so `lae` state must not leak between tests. Override the dataset with
    ``@pytest.mark.parametrize("fresh_db", [{"scale": "small", "seed": 42}], indirect=True)``.
    """
    params = getattr(request, "param", None) or {}
    with migrated_db.begin() as conn:
        for stmt in _fx.RESET_SQL:
            conn.execute(sa.text(stmt))
    _fx.run_alembic(migrated_url)
    truth = generator.seed_database(
        migrated_db,
        params.get("scale", _fx.DEFAULT_SCALE),
        int(params.get("seed", _fx.DEFAULT_SEED)),
    )
    mark_dataset_dirty()  # integration fixtures in the same session must re-seed
    return migrated_db, truth


@pytest.fixture
def api_env(
    fresh_db: tuple[sa.Engine, Any], migrated_url: str, mark_dataset_dirty: Any, tmp_path: Path
) -> Iterator[dict[str, Any]]:
    """Fresh database per test, own signing key, real PostgreSQL."""
    from fastapi.testclient import TestClient

    from lae.api.app import create_app
    from lae.audit.signing import Signer
    from lae.config import Settings
    from lae.services.impl import LaeServices
    from lae.services.runtime import Runtime

    engine, truth = fresh_db
    Signer.generate().save(tmp_path / "signing.pem")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=migrated_url,
        reader_url=migrated_url,
        executor_url=migrated_url,
        hmac_key="e2e-hmac-key-" + "k" * 32,
        signing_key_path=tmp_path / "signing.pem",
        target_schemas=["app"],
        api_token=TOKEN,
    )
    rt = Runtime.from_settings(settings)
    rt.anchor_dir = tmp_path / "anchors"
    client = TestClient(create_app(lambda: LaeServices(rt), api_token=TOKEN))
    try:
        yield {"engine": engine, "truth": truth, "rt": rt, "client": client}
    finally:
        client.close()
        rt.dispose()
        mark_dataset_dirty()


# ------------------------------------------------------------------ helpers (as a fixture)

APP_TABLES = (
    "app.customer", "app.customer_address", "app.loyalty_account", "app.product",
    "app.purchase_order", "app.order_line", "app.shipment", "app.payment", "app.support_ticket",
    "app.ticket_message", "app.document", "app.document_embedding", "app.customer_profile_cache",
    "app.marketing_event_log", "app.customer_ltv", "app.daily_revenue", "app.customer_segment",
)  # fmt: skip


class Helpers:
    """Direct-SQL oracles, independent of the engine, plus API shortcuts."""

    TOKEN = TOKEN
    APPROVER = APPROVER
    OPERATOR = OPERATOR

    def __init__(self, engine: sa.Engine, client: Any) -> None:
        self.engine = engine
        self.client = client

    def plan_for(self, subject: Any) -> dict[str, Any]:
        r = self.client.post(
            "/api/erasure-requests", json={"subject_key": [subject]}, headers=OPERATOR
        )
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        r = self.client.post(f"/api/erasure-plans/{rid}/generate", headers=OPERATOR)
        assert r.status_code == 201, r.text
        return {"request_id": rid, "plan": r.json()}

    def approve(self, plan: dict[str, Any]) -> Any:
        return self.client.post(
            f"/api/erasure-plans/{plan['id']}/approve",
            json={"plan_hash": plan["plan_hash"]},
            headers=APPROVER,
        )

    def execute(self, plan_id: str, mode: str = "LIVE") -> Any:
        return self.client.post(
            f"/api/erasure-plans/{plan_id}/execute", json={"mode": mode}, headers=OPERATOR
        )

    def app_fingerprint(self) -> dict[str, Any]:
        """Row count + order-independent content hash of every app relation + object count."""
        out: dict[str, Any] = {}
        with self.engine.connect() as c:
            for t in APP_TABLES:
                q = self.render(
                    "SELECT count(*), md5(coalesce(string_agg(md5(x::text), '' "
                    "ORDER BY md5(x::text)), '')) FROM {} x",
                    *t.split("."),
                )
                out[t] = tuple(c.execute(sa.text(q)).one())
            out["_relations"] = c.execute(
                sa.text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'app' AND c.relkind IN ('r', 'm', 'v')"
                )
            ).scalar_one()
        return out

    def lae_counts(self) -> dict[str, int]:
        with self.engine.connect() as c:
            names = [
                r[0]
                for r in c.execute(
                    sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'lae' ORDER BY 1")
                )
            ]
            return {
                n: int(
                    c.execute(
                        sa.text(self.render("SELECT count(*) FROM {}", "lae", n))
                    ).scalar_one()
                )
                for n in names
            }

    def render(self, template: str, *qualified: str) -> str:
        """`template` with ONE `{}` replaced by the psycopg-quoted identifier `qualified`."""
        from psycopg import sql

        raw = self.engine.raw_connection()
        try:
            return (
                sql.SQL(template)
                .format(sql.Identifier(*qualified))
                .as_string(raw.driver_connection)  # type: ignore[arg-type]
            )
        finally:
            raw.close()

    def scalar(self, q: str, **params: Any) -> Any:
        with self.engine.connect() as c:
            return c.execute(sa.text(q), params).scalar()

    @staticmethod
    def subjects(
        truth: Any, *, shared: bool | None = False, hidden: bool | None = None,
        referencing: bool | None = False,
    ) -> list[int]:  # fmt: skip
        out = []
        for sid, v in sorted(truth.subjects.items()):
            if shared is not None and bool(v["shared"]) != shared:
                continue
            if hidden is not None and bool(v["hidden_copies"]) != hidden:
                continue
            if referencing is not None and bool(v["referencing_other"]) != referencing:
                continue
            out.append(sid)
        return out


@pytest.fixture
def helpers(api_env: dict[str, Any]) -> Helpers:
    return Helpers(api_env["engine"], api_env["client"])
