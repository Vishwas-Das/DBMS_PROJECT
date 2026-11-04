"""Security e2e tests through the HTTP API on REAL PostgreSQL (QA-owned).

Hostile input must give a 4xx with the error envelope, and the database must be unchanged
(checked by direct SQL: per-table row count + content hash, number of app relations, lae rows).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.e2e

INJECTION_KEYS = [
    "1; DROP TABLE app.customer",
    "1); DROP TABLE app.customer; --",
    "' OR '1'='1",
    "1' OR 1=1 --",
    "42'--",
    '"; DELETE FROM app.customer; --',
    "1 UNION SELECT email FROM app.customer",
    "$$; DROP SCHEMA app CASCADE; $$",
    "\\x27; TRUNCATE app.customer; --",
    "ｏｒ １＝１",  # noqa: RUF001 - full-width unicode on purpose
    "Ω≈ç√∫˜µ≤≥÷ 🔥💀",  # noqa: RUF001 - on purpose
    "‮; DROP TABLE app.customer",  # right-to-left override
    "1\u0000; DROP TABLE app.customer",  # NUL byte
    "9" * 400,  # bigger than bigint
    "-1",
    "",
]


def _assert_envelope(r: Any, *statuses: int) -> dict[str, Any]:
    assert r.status_code in statuses, (r.status_code, r.text[:300])
    body = r.json()
    assert set(body) == {"error"}, body
    assert {"code", "message", "details"} <= set(body["error"])
    return body["error"]


# ------------------------------------------------------------------ injection


@pytest.mark.parametrize("key", INJECTION_KEYS)
def test_injection_shaped_subject_keys_are_rejected_without_damage(
    api_env: dict[str, Any], helpers: Any, key: str
) -> None:
    c = api_env["client"]
    before_app, before_lae = helpers.app_fingerprint(), helpers.lae_counts()
    r = c.post("/api/erasure-requests", json={"subject_key": [key]}, headers=helpers.OPERATOR)
    # damage first: whatever the status, the database must be untouched
    assert helpers.app_fingerprint() == before_app
    assert helpers.lae_counts()["erasure_request"] == before_lae["erasure_request"]
    assert key not in r.text or key == ""  # hostile input is never echoed back
    # a malformed key for a bigint subject root is a client error (4xx), never a 5xx
    assert 400 <= r.status_code < 500, (r.status_code, r.text[:300])
    _assert_envelope(r, r.status_code)


@pytest.mark.parametrize("key", ["1; DROP TABLE app.customer", "' OR '1'='1"])
def test_injection_in_subject_root_name(api_env: dict[str, Any], helpers: Any, key: str) -> None:
    before = helpers.app_fingerprint()
    r = api_env["client"].post(
        "/api/erasure-requests",
        json={"subject_root": key, "subject_key": [1]},
        headers=helpers.OPERATOR,
    )
    _assert_envelope(r, 404, 422)
    assert helpers.app_fingerprint() == before


@pytest.mark.parametrize(
    "path",
    [
        "/api/erasure-requests/1;DROP TABLE app.customer",
        "/api/erasure-requests/' OR '1'='1",
        "/api/erasure-plans/1%27%20OR%201=1/approve",
        "/api/erasure-plans/..%2F..%2Fetc%2Fpasswd/execute",
        "/api/verification/%00",
        "/api/audit/00000000-0000-0000-0000-00000000000Z",
        "/api/lineage/1 UNION SELECT 1",
        "/api/evidence/;DROP/download",
    ],
)
def test_injection_shaped_path_params_are_rejected(
    api_env: dict[str, Any], helpers: Any, path: str
) -> None:
    c = api_env["client"]
    before = helpers.app_fingerprint()
    method = c.post if path.endswith(("approve", "execute")) else c.get
    kwargs: dict[str, Any] = {"headers": helpers.OPERATOR}
    if path.endswith("approve"):
        kwargs["json"] = {"plan_hash": "0" * 64}
    elif path.endswith("execute"):
        kwargs["json"] = {"mode": "LIVE"}
    r = method(path, **kwargs)
    assert 400 <= r.status_code < 500, (r.status_code, r.text[:200])
    if r.status_code != 404 or r.headers.get("content-type", "").startswith("application/json"):
        assert "error" in r.json() or "detail" in r.json()
    assert helpers.app_fingerprint() == before


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("/api/customers?q=' OR '1'='1", (200,)),
        ("/api/customers?q=1; DROP TABLE app.customer", (200,)),
        ("/api/customers?q=%25", (200,)),
        ("/api/customers?q=_", (200,)),
        ("/api/customers?q=" + "a" * 65, (422,)),
        ("/api/customers?limit=1;DROP TABLE app.customer", (422,)),
        ("/api/customers?limit=100000", (422,)),
        ("/api/audit/events?after_seq=-1", (422,)),
        ("/api/audit/events?limit=0 OR 1=1", (422,)),
        ("/api/lineage/schema?level=TABLE;DROP", (422,)),
        ("/api/erasure-requests?limit=' OR 1=1", (422,)),
        ("/api/benchmarks?scenario=' OR '1'='1", (200,)),
    ],
)
def test_injection_shaped_query_params(
    api_env: dict[str, Any], helpers: Any, path: str, ok: tuple[int, ...]
) -> None:
    before = helpers.app_fingerprint()
    r = api_env["client"].get(path, headers=helpers.OPERATOR)
    assert r.status_code in ok, (r.status_code, r.text[:300])
    if r.status_code >= 400:
        _assert_envelope(r, *ok)
    assert helpers.app_fingerprint() == before


def test_customer_search_wildcards_do_not_enumerate_and_mask_email(
    api_env: dict[str, Any], helpers: Any
) -> None:
    c = api_env["client"]
    for q in ("%", "_", "' OR '1'='1"):
        r = c.get("/api/customers", params={"q": q}, headers=helpers.OPERATOR)
        assert r.status_code == 200, r.text
        assert r.json() == [], f"q={q!r} must not match every customer"
    r = c.get("/api/customers", params={"limit": 100}, headers=helpers.OPERATOR)
    assert r.status_code == 200
    with api_env["engine"].connect() as conn:
        import sqlalchemy as sa

        emails = [e for (e,) in conn.execute(sa.text("SELECT email FROM app.customer"))]
    for e in emails:
        assert e not in r.text, "raw email returned by /api/customers"


# ------------------------------------------------------------------ payload shape


def test_oversized_and_malformed_payloads(api_env: dict[str, Any], helpers: Any) -> None:
    c, h = api_env["client"], helpers.OPERATOR
    before = helpers.app_fingerprint()
    cases: list[tuple[Any, ...]] = [
        ({"subject_key": list(range(9))},),  # > max_length 8
        ({"subject_key": []},),
        ({"subject_key": [1], "reason": "r" * 501},),
        ({"subject_key": [1], "unexpected": True},),  # extra="forbid"
        ({"subject_key": [{"nested": [1]}]},),
        ({"subject_key": [1.5]},),
        ({"subject_key": "1"},),
        ({},),
    ]
    for (body,) in cases:
        _assert_envelope(c.post("/api/erasure-requests", json=body, headers=h), 422)
    # ~4 MiB JSON document
    big = {"subject_key": [1], "reason": "x" * (4 * 1024 * 1024)}
    _assert_envelope(c.post("/api/erasure-requests", json=big, headers=h), 413, 422)
    big_list = {"subject_key": ["a" * 1000] * 5000}
    _assert_envelope(c.post("/api/erasure-requests", json=big_list, headers=h), 413, 422)
    nested: Any = 1
    for _ in range(2000):
        nested = [nested]
    _assert_envelope(
        c.post("/api/erasure-requests", content=f'{{"subject_key": {nested}}}'.replace("'", '"'),
               headers={**h, "Content-Type": "application/json"}),
        413, 422,
    )  # fmt: skip
    assert helpers.app_fingerprint() == before


@pytest.mark.parametrize(
    ("content", "ctype"),
    [
        ("subject_key=1", "application/x-www-form-urlencoded"),
        ("<x><subject_key>1</subject_key></x>", "application/xml"),
        ('{"subject_key": [1]}', "text/plain"),
        ("{not json", "application/json"),
        ("", "application/json"),
        ("\xff\xfe\x00", "application/json"),
    ],
)
def test_wrong_content_types_are_rejected(
    api_env: dict[str, Any], helpers: Any, content: str, ctype: str
) -> None:
    before = helpers.lae_counts()["erasure_request"]
    r = api_env["client"].post(
        "/api/erasure-requests",
        content=content.encode("utf-8", "surrogatepass"),
        headers={**helpers.OPERATOR, "Content-Type": ctype},
    )
    _assert_envelope(r, 415, 422)
    assert helpers.lae_counts()["erasure_request"] == before


# ------------------------------------------------------------------ authorization


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-LAE-Token": ""},
        {"X-LAE-Token": "wrong"},
        {"X-LAE-Token": "e2e-security-token-" + "q" * 23},  # one char short
        {"X-LAE-Token": "e2e-security-token-" + "q" * 24 + " "},
        {"x-lae-token": "' OR '1'='1"},
        {"Authorization": "Bearer e2e-security-token-" + "q" * 24},
    ],
)
def test_unauthorized_mutations_change_nothing(
    api_env: dict[str, Any], helpers: Any, headers: dict[str, str]
) -> None:
    c, truth = api_env["client"], api_env["truth"]
    subject = helpers.subjects(truth)[0]
    flow = helpers.plan_for(subject)
    plan = flow["plan"]
    conflict_id = (
        plan["conflicts"][0]["id"] if plan["conflicts"] else "0" * 8 + "-0000-0000-0000-" + "0" * 12
    )
    with api_env["engine"].connect() as conn:
        import sqlalchemy as sa

        edge_id = conn.execute(sa.text("SELECT id FROM lae.lineage_edge LIMIT 1")).scalar_one()
    before_app, before_lae = helpers.app_fingerprint(), helpers.lae_counts()
    calls = [
        ("/api/erasure-requests", {"subject_key": [subject]}),
        (f"/api/discovery/{flow['request_id']}", None),
        (f"/api/erasure-plans/{flow['request_id']}/generate", None),
        (f"/api/erasure-plans/{plan['id']}/approve", {"plan_hash": plan["plan_hash"]}),
        (f"/api/erasure-plans/{plan['id']}/execute", {"mode": "LIVE"}),
        (f"/api/erasure-plans/{plan['id']}/execute", {"mode": "DRY_RUN"}),
        (f"/api/erasure-plans/{plan['id']}/conflicts/{conflict_id}/acknowledge", None),
        (f"/api/lineage-edges/{edge_id}/status", {"status": "CONFIRMED"}),
        (f"/api/verification/{flow['request_id']}/run", None),
        ("/api/audit/verify-chain", None),
    ]
    for path, body in calls:
        r = c.post(path, json=body, headers=headers) if body else c.post(path, headers=headers)
        err = _assert_envelope(r, 401)
        assert err["code"] == "UNAUTHORIZED", (path, err)
    assert helpers.app_fingerprint() == before_app
    assert helpers.lae_counts() == before_lae, "an unauthorized call wrote to the lae schema"


def test_read_endpoints_require_the_token(api_env: dict[str, Any], helpers: Any) -> None:
    """Lead security fix (1): every endpoint except GET /api/health needs X-LAE-Token."""
    c = api_env["client"]
    flow = helpers.plan_for(helpers.subjects(api_env["truth"])[0])
    rid, pid = flow["request_id"], flow["plan"]["id"]
    assert c.get("/api/health").status_code == 200
    for path in [
        "/api/stats",
        "/api/customers",
        "/api/erasure-requests",
        f"/api/erasure-requests/{rid}",
        f"/api/discovery/{rid}",
        f"/api/lineage/{rid}",
        "/api/lineage/schema",
        f"/api/erasure-plans/{pid}",
        f"/api/verification/{rid}",
        f"/api/audit/{rid}",
        "/api/audit/events",
        f"/api/evidence/{rid}/download",
        f"/api/evidence/{rid}/leaves",
        "/api/benchmarks",
        f"/api/executions?request_id={rid}",
    ]:
        r = c.get(path)
        assert r.status_code == 401, (path, r.status_code)
        r = c.get(path, headers={"X-LAE-Token": "wrong"})
        assert r.status_code == 401, (path, r.status_code)


# ------------------------------------------------------------------ plan state machine


def test_acknowledge_changes_the_hash_and_the_old_hash_is_rejected(
    api_env: dict[str, Any], helpers: Any
) -> None:
    c = api_env["client"]
    flow = helpers.plan_for(helpers.subjects(api_env["truth"])[0])
    plan = flow["plan"]
    ackable = [
        x for x in plan["conflicts"] if x["code"] == "EXTERNAL_ARTIFACT" and x["resolution"] is None
    ]
    assert ackable, plan["conflicts"]
    r = c.post(
        f"/api/erasure-plans/{plan['id']}/conflicts/{ackable[0]['id']}/acknowledge",
        json={"note": "reviewed by QA"},
        headers=helpers.APPROVER,
    )
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["plan_hash"] != plan["plan_hash"]
    old = c.post(
        f"/api/erasure-plans/{plan['id']}/approve",
        json={"plan_hash": plan["plan_hash"]},
        headers=helpers.APPROVER,
    )
    assert _assert_envelope(old, 409)["code"] == "PLAN_HASH_MISMATCH"
    assert helpers.approve(new).status_code == 200


def test_superseded_plan_cannot_be_approved_or_executed(
    api_env: dict[str, Any], helpers: Any
) -> None:
    c = api_env["client"]
    flow = helpers.plan_for(helpers.subjects(api_env["truth"])[0])
    first = flow["plan"]
    assert helpers.approve(first).status_code == 200
    r = c.post(f"/api/erasure-plans/{flow['request_id']}/generate", headers=helpers.OPERATOR)
    assert r.status_code == 201, r.text
    second = r.json()
    assert second["id"] != first["id"]
    assert (
        c.get(f"/api/erasure-plans/{first['id']}", headers=helpers.OPERATOR).json()["status"]
        == "SUPERSEDED"
    )
    before = helpers.app_fingerprint()
    assert _assert_envelope(helpers.approve(first), 409)["code"] in ("INVALID_STATE",)
    for mode in ("LIVE", "DRY_RUN"):
        assert _assert_envelope(helpers.execute(first["id"], mode), 409)["code"] == "INVALID_STATE"
    assert helpers.app_fingerprint() == before


def test_unapproved_plan_cannot_be_executed(api_env: dict[str, Any], helpers: Any) -> None:
    flow = helpers.plan_for(helpers.subjects(api_env["truth"])[0])
    before = helpers.app_fingerprint()
    assert _assert_envelope(helpers.execute(flow["plan"]["id"]), 409)["code"] == "INVALID_STATE"
    assert helpers.app_fingerprint() == before


def test_four_eyes_approver_cannot_execute(api_env: dict[str, Any], helpers: Any) -> None:
    """Lead security fix (2): LIVE execute by the approving actor gives 409 INVALID_STATE."""
    c = api_env["client"]
    flow = helpers.plan_for(helpers.subjects(api_env["truth"])[0])
    assert helpers.approve(flow["plan"]).status_code == 200
    before = helpers.app_fingerprint()
    r = c.post(
        f"/api/erasure-plans/{flow['plan']['id']}/execute",
        json={"mode": "LIVE"},
        headers=helpers.APPROVER,
    )
    assert _assert_envelope(r, 409)["code"] == "INVALID_STATE"
    assert helpers.app_fingerprint() == before
    assert helpers.execute(flow["plan"]["id"]).status_code == 200


def test_double_live_execution_is_rejected(api_env: dict[str, Any], helpers: Any) -> None:
    flow = helpers.plan_for(helpers.subjects(api_env["truth"])[0])
    assert helpers.approve(flow["plan"]).status_code == 200
    r = helpers.execute(flow["plan"]["id"])
    assert r.status_code == 200 and r.json()["status"] == "COMMITTED", r.text
    before = helpers.app_fingerprint()
    again = helpers.execute(flow["plan"]["id"])
    assert _assert_envelope(again, 409)["code"] in ("ALREADY_EXECUTED", "INVALID_STATE")
    assert helpers.app_fingerprint() == before
    committed = helpers.scalar(
        "SELECT count(*) FROM lae.execution_run WHERE plan_id = :p AND mode = 'LIVE' "
        "AND status = 'COMMITTED'",
        p=flow["plan"]["id"],
    )
    assert committed == 1


def test_second_open_request_for_same_subject_is_rejected(
    api_env: dict[str, Any], helpers: Any
) -> None:
    """Lead security fix (4)."""
    c = api_env["client"]
    subject = helpers.subjects(api_env["truth"])[0]
    r1 = c.post("/api/erasure-requests", json={"subject_key": [subject]}, headers=helpers.OPERATOR)
    assert r1.status_code == 201
    r2 = c.post("/api/erasure-requests", json={"subject_key": [subject]}, headers=helpers.OPERATOR)
    _assert_envelope(r2, 409)
