"""Signed evidence bundles and the offline verifier (architecture.md §13).

Bundle (canonical JSON, ``bundle_version`` "1")::

    {
      "bundle_version": "1",
      "plan_id": "<uuid>",
      "plan_hash": "<hex64>",
      "snapshot_fingerprint": "<hex64>",
      "record_set_hash": "<hex64>",
      "execution": {"run_id": "<uuid>", "mode": "LIVE", "committed_at": "<iso Z>",
                    "step_counts": {"APPLIED": n, "FAILED": n, "SKIPPED": n}},
      "verification": {"status": "PASSED", "checks": [<check>, ...]},
      "merkle_root": "<hex64>",
      "leaf_count": n,
      "chain_anchor": {"seq": n, "event_hash": "<hex64>"},
      "issued_at": "<iso Z>",
      "signing_key_id": "<hex16>"
    }

where ``<check>`` is ``{"check_type", "table" (qualified name or null), "column", "step_order",
"passed", "observed", "expected"}``, and checks are sorted by their canonical bytes.

The signature is Ed25519 over ``canonical_json(bundle)``. The **export file** (input to
:func:`verify_evidence_offline`) is the canonical JSON object ``{"bundle": {...}, "signature":
"<base64>"}``. The **leaves file** is the JSON array ``[leaf.as_canonical(), ...]`` in bundle
order (``EvidenceBundle.leaves_canonical``). The **chain export** is a JSON array of
``AuditEventRecord``-like objects in ``seq`` order; ``seq``, ``event_type``,
``payload_canonical``, ``prev_hash`` and ``event_hash`` are required, ``entity_type``,
``entity_id``, ``actor`` and ``occurred_at`` are optional (they are not covered by the hash).

Neither the bundle nor the leaves contain personal data: records appear only as HMACs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from lae.audit.canonical import canonical_json
from lae.audit.hashchain import is_hex64, verify_chain
from lae.audit.hmac_keys import HmacKeyring
from lae.audit.merkle import MerkleTree
from lae.audit.signing import Signer, public_key_id, verify_signature
from lae.contracts.enums import (
    UNVERIFIABLE_CATEGORIES,
    CheckType,
    ExecutionMode,
    ExecutionStatus,
    LineageLevel,
    PlanAction,
    StepStatus,
    VerificationStatus,
)
from lae.contracts.types import (
    AuditEventRecord,
    CheckResult,
    ErasurePlan,
    EvidenceLeaf,
    ExecutionResult,
    PlanItem,
    RecordKey,
    VerificationReport,
)
from lae.errors import SignatureError, ValidationError

BUNDLE_VERSION = "1"
BUNDLE_FIELDS = frozenset(
    {
        "bundle_version",
        "plan_id",
        "plan_hash",
        "snapshot_fingerprint",
        "record_set_hash",
        "execution",
        "verification",
        "merkle_root",
        "leaf_count",
        "chain_anchor",
        "issued_at",
        "signing_key_id",
    }
)
EXPORT_FIELDS = frozenset({"bundle", "signature"})
LEAF_FIELDS = frozenset({"table", "key_hmac", "action", "check_types", "passed"})
_EVENT_REQUIRED = ("seq", "event_type", "payload_canonical", "prev_hash", "event_hash")
_ENVELOPE_FIELDS = ("entity_type", "entity_id", "actor", "occurred_at")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ------------------------------------------------------------------ inputs


@dataclass(frozen=True, slots=True)
class ChainAnchor:
    """The audit event the bundle is anchored to (normally the latest event at issuance)."""

    seq: int
    event_hash: str

    @classmethod
    def from_event(cls, event: AuditEventRecord) -> ChainAnchor:
        return cls(seq=event.seq, event_hash=event.event_hash)

    def as_canonical(self) -> dict[str, Any]:
        return {"seq": self.seq, "event_hash": self.event_hash}


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """The committed execution run: ``ExecutionResult`` plus its persisted id and commit time
    (``execution_run.id`` / ``committed_at``), which the contract result type does not carry."""

    run_id: UUID
    committed_at: datetime
    result: ExecutionResult


# ------------------------------------------------------------------ outputs


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """A signed bundle. ``bundle`` is the JSON-native dict whose canonical form was signed."""

    bundle: Mapping[str, Any]
    bundle_canonical: str
    signature: str
    leaves: tuple[EvidenceLeaf, ...]
    leaves_canonical: str
    merkle_root: str
    leaf_count: int
    chain_anchor: ChainAnchor
    signing_key_id: str
    issued_at: datetime

    def export_json(self) -> str:
        """The export file: canonical ``{"bundle": ..., "signature": ...}``."""
        bundle = json.loads(self.bundle_canonical)
        return canonical_json({"bundle": bundle, "signature": self.signature}).decode("utf-8")


@dataclass(frozen=True, slots=True)
class EvidenceVerification:
    valid: bool
    signature_ok: bool
    merkle_ok: bool | None
    chain_ok: bool | None
    errors: tuple[str, ...]


# ------------------------------------------------------------------ building


def _require_hex64(name: str, value: object) -> None:
    if not is_hex64(value):
        raise ValidationError(f"{name} must be 64 lowercase hex characters")


def _require_aware(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be a timezone-aware datetime")


def _check_as_canonical(check: CheckResult) -> dict[str, Any]:
    return {
        "check_type": check.check_type.value,
        "table": check.table.qualified() if check.table is not None else None,
        "column": check.column,
        "step_order": check.step_order,
        "passed": check.passed,
        "observed": dict(check.observed),
        "expected": dict(check.expected),
    }


def _sorted_by_canonical(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=canonical_json)


def build_evidence(
    plan: ErasurePlan,
    execution: ExecutionEvidence,
    verification: VerificationReport,
    leaves: Sequence[EvidenceLeaf],
    chain_anchor: ChainAnchor,
    signer: Signer,
    issued_at: datetime,
) -> EvidenceBundle:
    """Build and sign the evidence bundle for an executed and verified plan.

    Inputs:
    - ``plan``: the executed ``ErasurePlan``; ``plan.schema_fingerprint`` is recorded as
      ``snapshot_fingerprint``. ``plan_hash``, ``record_set_hash`` and the fingerprint must be
      64-hex.
    - ``execution``: :class:`ExecutionEvidence`; the result must be ``LIVE`` and ``COMMITTED``.
    - ``verification``: the final ``VerificationReport`` (status must not be ``RUNNING``).
      ``observed`` / ``expected`` must be canonicalizable (no floats) and, by contract, hold
      counts and hashes only.
    - ``leaves``: one ``EvidenceLeaf`` per record / artifact outcome. ``key_hmac`` must be
      64-hex (guards against a raw key slipping into evidence). Leaves are sorted by their
      canonical bytes before the Merkle tree is built; the sorted order is the bundle order.
    - ``chain_anchor``: :class:`ChainAnchor` of an existing audit event.
    - ``signer``: the active :class:`Signer`.
    - ``issued_at``: timezone-aware issuance time.

    Raises ``ValidationError`` for invalid inputs and ``CanonicalizationError`` if any value
    cannot be canonicalized.
    """
    for name, value in (
        ("plan_hash", plan.plan_hash),
        ("record_set_hash", plan.record_set_hash),
        ("schema_fingerprint", plan.schema_fingerprint),
        ("chain_anchor.event_hash", chain_anchor.event_hash),
    ):
        _require_hex64(name, value)
    if (
        isinstance(chain_anchor.seq, bool)
        or not isinstance(chain_anchor.seq, int)
        or chain_anchor.seq < 1
    ):
        raise ValidationError("chain_anchor.seq must be a positive int")
    _require_aware("issued_at", issued_at)
    _require_aware("execution.committed_at", execution.committed_at)
    result = execution.result
    if result.mode is not ExecutionMode.LIVE or result.status is not ExecutionStatus.COMMITTED:
        raise ValidationError("evidence can only be issued for a committed LIVE execution")
    if verification.status is VerificationStatus.RUNNING:
        raise ValidationError("verification is still running")
    for leaf in leaves:
        _require_hex64("leaf.key_hmac", leaf.key_hmac)

    ordered = sorted(leaves, key=lambda lf: canonical_json(lf.as_canonical()))
    tree = MerkleTree.build(ordered)
    step_counts = {s.value: 0 for s in StepStatus}
    for step in result.steps:
        step_counts[step.status.value] += 1

    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "snapshot_fingerprint": plan.schema_fingerprint,
        "record_set_hash": plan.record_set_hash,
        "execution": {
            "run_id": execution.run_id,
            "mode": result.mode,
            "committed_at": execution.committed_at,
            "step_counts": step_counts,
        },
        "verification": {
            "status": verification.status,
            "checks": _sorted_by_canonical([_check_as_canonical(c) for c in verification.checks]),
        },
        "merkle_root": tree.root,
        "leaf_count": tree.size,
        "chain_anchor": chain_anchor.as_canonical(),
        "issued_at": issued_at,
        "signing_key_id": signer.key_id,
    }
    canonical = canonical_json(bundle)
    signature = signer.sign(canonical)
    return EvidenceBundle(
        bundle=MappingProxyType(json.loads(canonical)),
        bundle_canonical=canonical.decode("utf-8"),
        signature=signature,
        leaves=tuple(ordered),
        leaves_canonical=canonical_json([lf.as_canonical() for lf in ordered]).decode("utf-8"),
        merkle_root=tree.root,
        leaf_count=tree.size,
        chain_anchor=chain_anchor,
        signing_key_id=signer.key_id,
        issued_at=issued_at,
    )


# ------------------------------------------------------------------ leaves


def _item_verified(item: PlanItem, bound: Sequence[CheckResult]) -> bool:
    # Same rule as lae.verification.outcome.item_verified (CR lead-1 §3); audit must not import
    # lae.verification, so it is restated here and kept equal by a unit test.
    if item.category in UNVERIFIABLE_CATEGORIES:
        return False
    return bool(bound) and all(c.passed for c in bound)


def leaves_from_verification(
    plan: ErasurePlan, report: VerificationReport, keyring: HmacKeyring
) -> list[EvidenceLeaf]:
    """Evidence leaves for every plan item, sorted by canonical bytes (the bundle order).

    - RECORD-level item: one leaf per record key, ``key_hmac = keyring.key_digest(RecordKey(
      item.table, item.key_columns, key))``. ``PlanItem`` does not carry ``is_predicate``, so
      keys of tables without a primary key are digested with ``is_predicate=False``.
    - TABLE-level item: a single leaf with ``key_hmac = keyring.digest(item.table.qualified())``
      (EXACT normalization).
    - ``check_types``: the distinct check types bound to the item (``step_order``);
      ``passed``: the item counts as verified (verifiable category, at least one bound check,
      all bound checks passed). SCHEDULE / MANUAL_REVIEW / UNRESOLVED items are never passed.

    Raw key values are consumed only inside the HMAC; nothing else of them reaches a leaf.
    """
    leaves: list[EvidenceLeaf] = []
    for item in plan.items:
        bound = [c for c in report.checks if c.step_order == item.step_order]
        types = tuple(sorted({c.check_type for c in bound}, key=lambda t: t.value))
        passed = _item_verified(item, bound)
        table = item.table.qualified()
        if item.lineage_level is LineageLevel.TABLE:
            digests = [keyring.digest(table)]
        else:
            digests = [
                keyring.key_digest(RecordKey(item.table, item.key_columns, key))
                for key in item.record_keys
            ]
        leaves.extend(EvidenceLeaf(table, d, item.action, types, passed) for d in digests)
    return sorted(leaves, key=lambda lf: canonical_json(lf.as_canonical()))


# ------------------------------------------------------------------ offline verification


class _InputError(Exception):
    """Internal: malformed verifier input (message is safe to report)."""


def _reject_float(text: str) -> Any:
    raise _InputError("floats are not allowed")


def _reject_constant(text: str) -> Any:
    raise _InputError(f"non-finite number {text} is not allowed")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _InputError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _strict_loads(text: str, what: str) -> Any:
    """json.loads that rejects floats, NaN/Infinity and duplicate object keys."""
    try:
        return json.loads(
            text,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except _InputError as exc:
        raise _InputError(f"{what}: {exc}") from None
    except (ValueError, TypeError) as exc:
        raise _InputError(f"{what}: not valid JSON") from exc


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_leaves(text: str) -> list[EvidenceLeaf]:
    raw = _strict_loads(text, "leaves")
    if not isinstance(raw, list):
        raise _InputError("leaves: expected a JSON array")
    leaves: list[EvidenceLeaf] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != LEAF_FIELDS:
            raise _InputError(f"leaves[{i}]: expected keys {sorted(LEAF_FIELDS)}")
        try:
            if not (
                isinstance(item["table"], str)
                and isinstance(item["key_hmac"], str)
                and isinstance(item["passed"], bool)
                and isinstance(item["check_types"], list)
            ):
                raise TypeError
            leaf = EvidenceLeaf(
                table=item["table"],
                key_hmac=item["key_hmac"],
                action=PlanAction(item["action"]),
                check_types=tuple(CheckType(c) for c in item["check_types"]),
                passed=item["passed"],
            )
        except (TypeError, ValueError):
            raise _InputError(f"leaves[{i}]: malformed leaf") from None
        if leaf.as_canonical() != item:
            # e.g. unsorted check_types: the file is not the canonical form that was hashed
            raise _InputError(f"leaves[{i}]: not in canonical form")
        leaves.append(leaf)
    return leaves


def _same_instant(a: object, b: object) -> bool:
    """Both ``None``, or both aware timestamps (``datetime`` or ISO-8601 text) of one instant."""
    if a is None or b is None:
        return a is None and b is None
    try:
        da = a if isinstance(a, datetime) else datetime.fromisoformat(a)  # type: ignore[arg-type]
        db = b if isinstance(b, datetime) else datetime.fromisoformat(b)  # type: ignore[arg-type]
        if da.utcoffset() is None or db.utcoffset() is None:
            return False
        return da == db
    except (TypeError, ValueError):
        return False


def _same_uuid(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return UUID(str(a)) == UUID(str(b))
    except ValueError:
        return False


def envelope_mismatch(columns: Mapping[str, Any], payload_canonical: str) -> bool:
    """True iff an exported envelope column disagrees with the *hashed* envelope.

    ``entity_type``, ``entity_id``, ``actor`` and ``occurred_at`` are not bound by the event
    hash directly. The DB writer (CR verification-crypto-engineer-1 §1) also stores them as
    ``{"envelope": {...}, "data": ...}`` inside ``payload_canonical``, which *is* hashed. A
    column that is present in ``columns`` and in the envelope must therefore agree with it;
    otherwise the stored column was rewritten. Payloads without an envelope (other writers) and
    columns absent from either side are not compared. Never raises.
    """
    try:
        payload = json.loads(payload_canonical)
    except (TypeError, ValueError):
        return False  # a corrupt payload is reported by the hash check
    env = payload.get("envelope") if isinstance(payload, dict) else None
    if not isinstance(env, dict):
        return False
    for name in _ENVELOPE_FIELDS:
        if name not in columns or name not in env:
            continue
        a, b = columns[name], env[name]
        if name == "occurred_at":
            same = _same_instant(a, b)
        elif name == "entity_id":
            same = _same_uuid(a, b)
        else:
            same = a == b
        if not same:
            return True
    return False


def first_envelope_mismatch(events: Sequence[AuditEventRecord]) -> int | None:
    """``seq`` of the first record whose envelope columns disagree with its hashed envelope."""
    for ev in events:
        columns = {
            "entity_type": ev.entity_type,
            "entity_id": ev.entity_id,
            "actor": ev.actor,
            "occurred_at": ev.occurred_at,
        }
        if envelope_mismatch(columns, ev.payload_canonical):
            return ev.seq
    return None


def chain_segment_error(
    events: Sequence[AuditEventRecord],
    envelope_bad_seq: int | None,
    anchor_seq: object,
    anchor_hash: object,
    *,
    not_found: str = "chain_anchor not found in the audit chain export",
) -> str | None:
    """Verify an exported chain segment and that it contains ``(anchor_seq, anchor_hash)``.

    Returns ``None`` if everything holds, else one error naming the *first* bad ``seq``
    (hash-chain failure or envelope mismatch, whichever comes first).
    """
    result = verify_chain(events, expected_start_prev=events[0].prev_hash)
    if envelope_bad_seq is not None and (
        result.valid or (result.first_invalid_seq or 0) > envelope_bad_seq
    ):
        return f"audit chain invalid at seq {envelope_bad_seq} (envelope_mismatch)"
    if not result.valid:
        return f"audit chain invalid at seq {result.first_invalid_seq} ({result.reason})"
    if not any(ev.seq == anchor_seq and ev.event_hash == anchor_hash for ev in events):
        return not_found
    return None


def _parse_events_checked(text: str) -> tuple[list[AuditEventRecord], int | None]:
    """Parse a chain export; also return the first ``seq`` whose envelope columns mismatch."""
    raw = _strict_loads(text, "chain")
    events = _parse_events_raw(raw)
    bad: int | None = None
    for item, ev in zip(raw, events, strict=True):
        if envelope_mismatch(item, ev.payload_canonical):
            bad = ev.seq
            break
    return events, bad


def _parse_events(text: str) -> list[AuditEventRecord]:
    return _parse_events_raw(_strict_loads(text, "chain"))


def _parse_events_raw(raw: Any) -> list[AuditEventRecord]:
    if not isinstance(raw, list):
        raise _InputError("chain: expected a JSON array")
    events: list[AuditEventRecord] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or any(k not in item for k in _EVENT_REQUIRED):
            raise _InputError(f"chain[{i}]: missing required fields")
        try:
            if not (
                _is_int(item["seq"])
                and isinstance(item["event_type"], str)
                and isinstance(item["payload_canonical"], str)
                and isinstance(item["prev_hash"], str)
                and isinstance(item["event_hash"], str)
            ):
                raise TypeError
            entity_id = item.get("entity_id")
            occurred_at = item.get("occurred_at")
            events.append(
                AuditEventRecord(
                    seq=item["seq"],
                    event_type=item["event_type"],
                    entity_type=str(item.get("entity_type", "")),
                    entity_id=UUID(entity_id) if entity_id is not None else None,
                    actor=str(item.get("actor", "")),
                    occurred_at=(
                        datetime.fromisoformat(occurred_at) if occurred_at is not None else _EPOCH
                    ),
                    payload_canonical=item["payload_canonical"],
                    prev_hash=item["prev_hash"],
                    event_hash=item["event_hash"],
                )
            )
        except (TypeError, ValueError, AttributeError):
            raise _InputError(f"chain[{i}]: malformed event") from None
    return events


def _structural_errors(bundle: Mapping[str, Any], public_key_pem: str) -> list[str]:
    errors: list[str] = []
    keys = set(bundle)
    if keys != BUNDLE_FIELDS:
        missing = sorted(BUNDLE_FIELDS - keys)
        extra = sorted(keys - BUNDLE_FIELDS)
        errors.append(f"bundle fields mismatch (missing={missing}, unexpected={extra})")
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        errors.append("unsupported bundle_version")
    if not is_hex64(bundle.get("merkle_root")):
        errors.append("merkle_root is not 64-hex")
    leaf_count = bundle.get("leaf_count")
    if not isinstance(leaf_count, int) or isinstance(leaf_count, bool) or leaf_count < 0:
        errors.append("leaf_count is not a non-negative int")
    anchor = bundle.get("chain_anchor")
    if (
        not isinstance(anchor, dict)
        or set(anchor) != {"seq", "event_hash"}
        or not _is_int(anchor["seq"])
        or not is_hex64(anchor["event_hash"])
    ):
        errors.append("chain_anchor is malformed")
    try:
        if bundle.get("signing_key_id") != public_key_id(public_key_pem):
            errors.append("signing_key_id does not match the public key")
    except SignatureError:
        pass  # reported by the signature step
    return errors


def verify_evidence_offline(
    bundle_json: str,
    public_key_pem: str,
    leaves_json: str | None = None,
    chain_json: str | None = None,
) -> EvidenceVerification:
    """Verify an exported evidence bundle with **no database and no network**. Never raises.

    1. Parse the export file (strict JSON: no floats, no duplicate keys), re-canonicalize
       ``bundle`` and check the Ed25519 ``signature`` with ``public_key_pem``.
    2. Check the bundle structure: exact field set, ``bundle_version``, hex fields, and that
       ``signing_key_id`` is the id of ``public_key_pem``.
    3. If ``leaves_json`` is given: recompute the Merkle root and compare it and the leaf count
       with the bundle (``merkle_ok``); otherwise ``merkle_ok`` is ``None``.
    4. If ``chain_json`` is given: verify the exported chain segment (starting from its first
       event's ``prev_hash``), require every exported envelope column (``entity_type``,
       ``entity_id``, ``actor``, ``occurred_at``) to agree with the hashed envelope inside
       ``payload_canonical`` (see :func:`envelope_mismatch`), and require the bundle's
       ``chain_anchor`` (seq and event_hash) to be one of its events (``chain_ok``); otherwise
       ``chain_ok`` is ``None``. A failure names the first bad ``seq``.

    ``valid`` is true only if the signature is good, there are no errors, and neither optional
    check failed.
    """
    errors: list[str] = []
    signature_ok = False
    merkle_ok: bool | None = None
    chain_ok: bool | None = None
    bundle: dict[str, Any] | None = None

    # 1. export file + signature
    try:
        export = _strict_loads(bundle_json, "bundle file")
        if not isinstance(export, dict) or set(export) != EXPORT_FIELDS:
            raise _InputError("bundle file: expected an object with keys 'bundle', 'signature'")
        if not isinstance(export["bundle"], dict) or not isinstance(export["signature"], str):
            raise _InputError("bundle file: 'bundle' must be an object, 'signature' a string")
        bundle = export["bundle"]
        canonical = canonical_json(bundle)
        try:
            signature_ok = verify_signature(public_key_pem, canonical, export["signature"])
            if not signature_ok:
                errors.append("signature is invalid")
        except SignatureError:
            errors.append("public key could not be loaded")
    except _InputError as exc:
        errors.append(str(exc))
    except Exception as exc:  # pragma: no cover - defensive: the verifier must never raise
        errors.append(f"bundle could not be processed ({type(exc).__name__})")

    if bundle is not None:
        # 2. structure
        try:
            errors.extend(_structural_errors(bundle, public_key_pem))
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"bundle structure could not be checked ({type(exc).__name__})")

        # 3. Merkle
        if leaves_json is not None:
            merkle_ok = False
            try:
                leaves = _parse_leaves(leaves_json)
                tree = MerkleTree.build(leaves)
                if tree.root != bundle.get("merkle_root"):
                    errors.append("merkle root does not match the leaves")
                elif tree.size != bundle.get("leaf_count"):
                    errors.append("leaf_count does not match the number of leaves")
                else:
                    merkle_ok = True
            except _InputError as exc:
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"leaves could not be processed ({type(exc).__name__})")

        # 4. chain segment + anchor
        if chain_json is not None:
            chain_ok = False
            try:
                events, envelope_bad = _parse_events_checked(chain_json)
                if not events:
                    raise _InputError("chain: export is empty")
                anchor = bundle.get("chain_anchor")
                if not isinstance(anchor, dict):
                    anchor = {}
                error = chain_segment_error(
                    events, envelope_bad, anchor.get("seq"), anchor.get("event_hash")
                )
                if error is not None:
                    errors.append(error)
                else:
                    chain_ok = True
            except _InputError as exc:
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"chain could not be processed ({type(exc).__name__})")
    else:
        if leaves_json is not None:
            merkle_ok = False
        if chain_json is not None:
            chain_ok = False

    valid = signature_ok and not errors and merkle_ok is not False and chain_ok is not False
    return EvidenceVerification(valid, signature_ok, merkle_ok, chain_ok, tuple(errors))
