"""API-layer unit tests with a stub service (no DB): auth, error envelope, no value echo."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from lae.api.app import create_app
from lae.errors import NotFoundError, PlanHashMismatch, StalePlanError

pytestmark = pytest.mark.unit
TOKEN = "t" * 32


class StubServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args))
            if name == "get_request":
                raise NotFoundError("erasure request not found")
            if name == "approve_plan":
                raise PlanHashMismatch("approval hash does not match plan hash")
            if name == "execute_plan":
                raise StalePlanError("plan is stale", details={"tables": ["app.purchase_order"]})
            if name == "benchmark_runs":
                return []
            raise RuntimeError("boom with secret alice@example.test")

        return method


@pytest.fixture
def stub() -> StubServices:
    return StubServices()


@pytest.fixture
def client(stub: StubServices) -> TestClient:
    return TestClient(create_app(lambda: stub, api_token=TOKEN), raise_server_exceptions=False)


def test_mutations_require_token(client: TestClient, stub: StubServices) -> None:
    r = client.post("/api/erasure-requests", json={"subject_key": [42]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    r = client.post(
        "/api/erasure-requests", json={"subject_key": [42]}, headers={"X-LAE-Token": "wrong"}
    )
    assert r.status_code == 401
    pid = uuid4()
    r = client.post(f"/api/erasure-plans/{pid}/execute", json={"mode": "LIVE"})
    assert r.status_code == 401
    assert stub.calls == []  # the service was never reached without a valid token


def test_no_token_configured_disables_mutations(stub: StubServices) -> None:
    c = TestClient(create_app(lambda: stub, api_token=None))
    r = c.post("/api/erasure-requests", json={"subject_key": [42]}, headers={"X-LAE-Token": ""})
    assert r.status_code == 401


def test_domain_errors_map_to_envelopes(client: TestClient) -> None:
    h = {"X-LAE-Token": TOKEN}
    r = client.get(f"/api/erasure-requests/{uuid4()}", headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = client.post(
        f"/api/erasure-plans/{uuid4()}/approve", json={"plan_hash": "a" * 64}, headers=h
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_HASH_MISMATCH"
    r = client.post(f"/api/erasure-plans/{uuid4()}/execute", json={"mode": "LIVE"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_PLAN"


def test_validation_errors_do_not_echo_values(client: TestClient) -> None:
    h = {"X-LAE-Token": TOKEN}
    r = client.post(
        "/api/erasure-requests", json={"subject_key": [], "evil": "bob@example.test"}, headers=h
    )
    assert r.status_code == 422
    assert "bob@example.test" not in r.text
    r = client.post(
        f"/api/erasure-plans/{uuid4()}/approve", json={"plan_hash": "NOTHEX"}, headers=h
    )
    assert r.status_code == 422 and "NOTHEX" not in r.text


def test_unhandled_errors_hide_details(client: TestClient) -> None:
    r = client.get("/api/stats", headers={"X-LAE-Token": TOKEN})
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "INTERNAL", "message": "internal error", "details": {}}}
    assert "alice" not in r.text


def test_openapi_lists_brief_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for p in [
        "/api/erasure-requests",
        "/api/erasure-requests/{request_id}",
        "/api/discovery/{request_id}",
        "/api/lineage/{request_id}",
        "/api/erasure-plans/{request_id}/generate",
        "/api/erasure-plans/{plan_id}",
        "/api/erasure-plans/{plan_id}/approve",
        "/api/erasure-plans/{plan_id}/execute",
        "/api/verification/{request_id}",
        "/api/audit/{request_id}",
        "/api/health",
    ]:
        assert p in paths, p
