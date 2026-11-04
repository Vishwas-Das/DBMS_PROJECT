"""FastAPI application (CR lead-1 §4). Routers are thin: validate → service → response model."""

# NOTE: no `from __future__ import annotations` here. FastAPI must resolve the locally defined
# Annotated dependency aliases (Actor, Svc) at runtime.

import hmac
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from lae.api.schemas import (
    AuditEventOut,
    AuditOut,
    BenchmarkRunOut,
    ChainVerificationOut,
    ConflictAcknowledge,
    CustomerOut,
    DiscoveryOut,
    EdgeStatusUpdate,
    ErasureRequestCreate,
    ErasureRequestOut,
    ErrorEnvelope,
    ExecutionOut,
    GraphEdgeOut,
    HealthOut,
    LineageOut,
    PlanApprove,
    PlanExecute,
    PlanGenerate,
    PlanOut,
    SchemaGraphOut,
    StatsOut,
    VerificationOut,
)
from lae.errors import LaeError
from lae.services.facade import LaeServicesProtocol

log = logging.getLogger("lae.api")

STATUS_BY_CODE: dict[str, int] = {
    "NOT_FOUND": 404,
    "SUBJECT_NOT_FOUND": 404,
    "INVALID_INPUT": 422,
    "UNKNOWN_TABLE": 422,
    "UNKNOWN_COLUMN": 422,
    "CANONICALIZATION": 422,
    "INVALID_STATE": 409,
    "PLAN_BLOCKED": 409,
    "PLAN_HASH_MISMATCH": 409,
    "PLAN": 409,
    "STALE_PLAN": 409,
    "ALREADY_EXECUTED": 409,
    "ROW_COUNT_MISMATCH": 409,
    "DISCOVERY_LIMIT": 413,
    "SERIALIZATION_CONFLICT": 503,
    "UNAUTHORIZED": 401,
}

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    s: {"model": ErrorEnvelope} for s in (401, 404, 409, 413, 422, 500, 503)
}


def _envelope(
    status: int, code: str, message: str, details: dict[str, object] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _status_for_class(exc: LaeError) -> int:
    """Fallback for codes not listed above, by exception family (never 500 for domain refusals)."""
    from lae.errors import ExecutionError, NotFoundError, PlanError, ValidationError

    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ValidationError):
        return 422
    if isinstance(exc, PlanError | ExecutionError):
        return 409
    return 500


class AuthError(LaeError):
    default_code = "UNAUTHORIZED"


def create_app(
    services_factory: Callable[[], LaeServicesProtocol],
    *,
    api_token: str | None,
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173"),
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver"),
) -> FastAPI:
    """Build the app. `services_factory` is called lazily once, on the first request."""
    app = FastAPI(
        title="Lineage-Aware Erasure Engine API",
        version="0.1.0",
        description=(
            "Research prototype. Mutating endpoints require the `X-LAE-Token` header. "
            "Responses never contain raw subject keys or identifier values."
        ),
    )
    # review H2: refuse requests whose Host header is not local (mitigates DNS rebinding)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-LAE-Token", "X-LAE-Actor"],
    )

    _cache: list[LaeServicesProtocol] = []

    def svc() -> LaeServicesProtocol:
        if not _cache:
            _cache.append(services_factory())
        return _cache[0]

    def require_token(
        x_lae_token: Annotated[str | None, Header()] = None,
        x_lae_actor: Annotated[str | None, Header()] = None,
    ) -> str:
        """Local-prototype auth: shared token, constant-time compare. Returns the actor name."""
        if not api_token:
            raise AuthError("server has no LAE_API_TOKEN configured; mutations are disabled")
        # compare bytes: str compare_digest raises TypeError on non-ASCII input (review L4)
        if x_lae_token is None or not hmac.compare_digest(
            x_lae_token.encode("utf-8"), api_token.encode("utf-8")
        ):
            raise AuthError("missing or invalid X-LAE-Token")
        actor = (x_lae_actor or "local-operator").strip()[:64]
        return actor or "local-operator"

    Actor = Annotated[str, Depends(require_token)]  # noqa: N806 - type alias
    Svc = Annotated[LaeServicesProtocol, Depends(svc)]  # noqa: N806 - type alias

    # --------------------------------------------------------------- middleware / errors
    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        response = await call_next(request)
        # structured, PII-free: method, path template, status, duration. Never bodies/queries.
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        log.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(LaeError)
    async def lae_error(_: Request, exc: LaeError) -> JSONResponse:
        status = STATUS_BY_CODE.get(exc.code) or _status_for_class(exc)
        if status >= 500:
            log.error("lae_error", extra={"code": exc.code})
        return _envelope(status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Report field locations and messages only — never echo the submitted values.
        errs = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return _envelope(422, "INVALID_INPUT", "request validation failed", {"errors": errs})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", extra={"type": type(exc).__name__})
        return _envelope(500, "INTERNAL", "internal error")

    # --------------------------------------------------------------- routes
    public = APIRouter(prefix="/api", responses=ERROR_RESPONSES)
    # review H2: every route except /api/health requires the token (reads included)
    r = APIRouter(prefix="/api", responses=ERROR_RESPONSES, dependencies=[Depends(require_token)])

    @public.get("/health", response_model=HealthOut)
    def health(s: Svc) -> HealthOut:
        return s.health()

    @r.get("/stats", response_model=StatsOut)
    def stats(s: Svc) -> StatsOut:
        return s.stats()

    @r.get("/customers", response_model=list[CustomerOut])
    def customers(
        s: Svc,
        q: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> list[CustomerOut]:
        return s.search_customers(q, limit)

    @r.post("/erasure-requests", response_model=ErasureRequestOut, status_code=201)
    def create_request(body: ErasureRequestCreate, s: Svc, actor: Actor) -> ErasureRequestOut:
        return s.create_request(body, actor)

    @r.get("/erasure-requests", response_model=list[ErasureRequestOut])
    def list_requests(
        s: Svc, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[ErasureRequestOut]:
        return s.list_requests(limit)

    @r.get("/erasure-requests/{request_id}", response_model=ErasureRequestOut)
    def get_request(request_id: UUID, s: Svc) -> ErasureRequestOut:
        return s.get_request(request_id)

    @r.post("/discovery/{request_id}", response_model=DiscoveryOut)
    def discovery(request_id: UUID, s: Svc, actor: Actor) -> DiscoveryOut:
        return s.run_discovery(request_id, actor)

    @r.get("/discovery/{request_id}", response_model=DiscoveryOut)
    def latest_discovery(request_id: UUID, s: Svc) -> DiscoveryOut:
        return s.latest_discovery(request_id)

    @r.post("/lineage-edges/{edge_id}/status", response_model=GraphEdgeOut)
    def set_edge_status(
        edge_id: UUID, body: EdgeStatusUpdate, s: Svc, actor: Actor
    ) -> GraphEdgeOut:
        return s.set_edge_status(edge_id, body, actor)

    @r.get("/executions", response_model=list[ExecutionOut])
    def list_executions(s: Svc, request_id: UUID) -> list[ExecutionOut]:
        return s.list_executions(request_id)

    @r.get("/lineage/schema", response_model=SchemaGraphOut)
    def schema_graph(s: Svc, level: Literal["TABLE", "COLUMN"] = "TABLE") -> SchemaGraphOut:
        return s.schema_graph(level)

    @r.get("/lineage/{request_id}", response_model=LineageOut)
    def lineage(
        request_id: UUID, s: Svc, level: Literal["TABLE", "COLUMN"] = "TABLE"
    ) -> LineageOut:
        return s.lineage(request_id, level)

    @r.post("/erasure-plans/{request_id}/generate", response_model=PlanOut, status_code=201)
    def generate(
        request_id: UUID, s: Svc, actor: Actor, body: PlanGenerate | None = None
    ) -> PlanOut:
        return s.generate_plan(request_id, body or PlanGenerate(), actor)

    @r.get("/erasure-plans/{plan_id}", response_model=PlanOut)
    def get_plan(plan_id: UUID, s: Svc) -> PlanOut:
        return s.get_plan(plan_id)

    @r.post("/erasure-plans/{plan_id}/conflicts/{conflict_id}/acknowledge", response_model=PlanOut)
    def acknowledge(
        plan_id: UUID,
        conflict_id: UUID,
        s: Svc,
        actor: Actor,
        body: ConflictAcknowledge | None = None,
    ) -> PlanOut:
        return s.acknowledge_conflict(plan_id, conflict_id, actor, body.note if body else None)

    @r.post("/erasure-plans/{plan_id}/approve", response_model=PlanOut)
    def approve(plan_id: UUID, body: PlanApprove, s: Svc, actor: Actor) -> PlanOut:
        return s.approve_plan(plan_id, body.plan_hash, actor)

    @r.post("/erasure-plans/{plan_id}/execute", response_model=ExecutionOut)
    def execute(plan_id: UUID, body: PlanExecute, s: Svc, actor: Actor) -> ExecutionOut:
        return s.execute_plan(plan_id, body, actor)

    @r.get("/verification/{request_id}", response_model=VerificationOut)
    def get_verification(request_id: UUID, s: Svc) -> VerificationOut:
        return s.get_verification(request_id)

    @r.post("/verification/{request_id}/run", response_model=VerificationOut)
    def run_verification(request_id: UUID, s: Svc, actor: Actor) -> VerificationOut:
        return s.run_verification(request_id, actor)

    @r.get("/audit/events", response_model=list[AuditEventOut])
    def audit_events(
        s: Svc,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> list[AuditEventOut]:
        return s.audit_events(after_seq, limit)

    @r.post("/audit/verify-chain", response_model=ChainVerificationOut)
    def verify_chain(s: Svc) -> ChainVerificationOut:
        return s.verify_chain()

    @r.get("/audit/{request_id}", response_model=AuditOut)
    def audit_for_request(request_id: UUID, s: Svc) -> AuditOut:
        return s.audit_for_request(request_id)

    @r.get("/evidence/{request_id}/download")
    def evidence_download(request_id: UUID, s: Svc) -> Response:
        return Response(
            s.evidence_download(request_id),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="evidence-{request_id}.json"'},
        )

    @r.get("/evidence/{request_id}/leaves")
    def evidence_leaves(request_id: UUID, s: Svc) -> Response:
        return Response(
            s.evidence_leaves(request_id),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="leaves-{request_id}.json"'},
        )

    @r.get("/benchmarks", response_model=list[BenchmarkRunOut])
    def benchmarks(
        s: Svc, scenario: Annotated[str | None, Query(max_length=16)] = None
    ) -> list[BenchmarkRunOut]:
        return s.benchmark_runs(scenario)

    app.include_router(public)
    app.include_router(r)
    return app
