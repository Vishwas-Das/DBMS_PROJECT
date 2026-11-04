"""Shared fixtures for lae.audit unit tests: golden vectors and the fixed test signing key."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from lae.audit.evidence import ChainAnchor, EvidenceBundle, ExecutionEvidence, build_evidence
from lae.audit.signing import Signer
from lae.contracts.enums import (
    CheckType,
    ExecutionMode,
    ExecutionStatus,
    PlanAction,
    StepStatus,
    VerificationStatus,
)
from lae.contracts.types import (
    CheckResult,
    ErasurePlan,
    EvidenceLeaf,
    ExecutionResult,
    StepResult,
    TableRef,
    VerificationReport,
)

VECTORS_DIR = Path(__file__).parent / "vectors"

EvidenceArgs = dict[str, Any]


def _leaf_from_dict(d: dict[str, Any]) -> EvidenceLeaf:
    return EvidenceLeaf(
        table=d["table"],
        key_hmac=d["key_hmac"],
        action=PlanAction(d["action"]),
        check_types=tuple(CheckType(c) for c in d["check_types"]),
        passed=d["passed"],
    )


def evidence_args_from_inputs(inp: dict[str, Any], signer: Signer) -> EvidenceArgs:
    """Turn the JSON ``evidence.inputs`` of vectors.json into ``build_evidence`` kwargs."""
    p, e, v = inp["plan"], inp["execution"], inp["verification"]
    return {
        "plan": ErasurePlan(
            plan_id=UUID(p["plan_id"]),
            request_id=UUID(p["request_id"]),
            schema_fingerprint=p["schema_fingerprint"],
            record_set_hash=p["record_set_hash"],
            items=(),
            conflicts=(),
            plan_hash=p["plan_hash"],
        ),
        "execution": ExecutionEvidence(
            run_id=UUID(e["run_id"]),
            committed_at=datetime.fromisoformat(e["committed_at"]),
            result=ExecutionResult(
                ExecutionMode.LIVE,
                ExecutionStatus.COMMITTED,
                tuple(
                    StepResult(
                        s["step_order"],
                        StepStatus(s["status"]),
                        s["rows_affected"],
                        s["expected_rows"],
                        s["duration_ms"],
                    )
                    for s in e["steps"]
                ),
            ),
        ),
        "verification": VerificationReport(
            VerificationStatus(v["status"]),
            tuple(
                CheckResult(
                    CheckType(c["check_type"]),
                    TableRef.parse(c["table"]) if c["table"] else None,
                    c["column"],
                    c["step_order"],
                    c["passed"],
                    c["observed"],
                    c["expected"],
                )
                for c in v["checks"]
            ),
        ),
        "leaves": [_leaf_from_dict(d) for d in inp["leaves"]],
        "chain_anchor": ChainAnchor(**inp["chain_anchor"]),
        "signer": signer,
        "issued_at": datetime.fromisoformat(inp["issued_at"]),
    }


@pytest.fixture(scope="session")
def leaf_from_dict() -> Callable[[dict[str, Any]], EvidenceLeaf]:
    return _leaf_from_dict


@pytest.fixture(scope="session")
def vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((VECTORS_DIR / "vectors.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="session")
def test_signer() -> Signer:
    return Signer.load(VECTORS_DIR / "test_signing_key.pem")


@pytest.fixture
def evidence_args(vectors: dict[str, Any], test_signer: Signer) -> EvidenceArgs:
    return evidence_args_from_inputs(vectors["evidence"]["inputs"], test_signer)


@pytest.fixture
def bundle(evidence_args: EvidenceArgs) -> EvidenceBundle:
    return build_evidence(**evidence_args)


@pytest.fixture
def chain_json(vectors: dict[str, Any]) -> str:
    return json.dumps(vectors["chain"]["events"])
