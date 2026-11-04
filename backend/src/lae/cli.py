"""`lae` command-line interface (Lead-owned). Shares the service pipeline with the API."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from lae.audit.evidence import verify_evidence_offline
from lae.audit.signing import Signer
from lae.contracts.enums import ExecutionMode

app = typer.Typer(help="Lineage-Aware Erasure Engine", no_args_is_help=True)
db_app = typer.Typer(help="database setup", no_args_is_help=True)
keys_app = typer.Typer(help="signing keys", no_args_is_help=True)
audit_app = typer.Typer(help="audit chain", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(keys_app, name="keys")
app.add_typer(audit_app, name="audit")

REPO = Path(__file__).resolve().parents[3]


def _rt(require_signer: bool = True):  # type: ignore[no-untyped-def]
    from lae.services.runtime import Runtime, configure_logging

    configure_logging()
    return Runtime.from_settings(require_signer=require_signer)


def _echo(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, sort_keys=True, default=str))


# ------------------------------------------------------------------ db
@db_app.command("upgrade")
def db_upgrade(url: Annotated[str | None, typer.Option(help="database URL")] = None) -> None:
    """Run Alembic migrations (schema `lae`)."""
    cmd = [sys.executable, "-m", "alembic", "-c", str(REPO / "backend" / "alembic.ini")]
    if url:
        cmd += ["-x", f"url={url}"]
    raise typer.Exit(subprocess.call([*cmd, "upgrade", "head"]))  # noqa: S603 - fixed argv


@db_app.command("seed")
def db_seed(
    scale: str = "default",
    seed: int = 42,
    url: Annotated[str | None, typer.Option()] = None,
    out: Path = Path("db/fixtures/out/ground_truth.json"),
) -> None:
    """(Re)create the synthetic `app` schema + lae configuration. Synthetic data only."""
    from lae.config import get_settings

    target = url or get_settings().database_url
    cmd = [
        sys.executable,
        "-m",
        "db.fixtures.generator",
        "--scale",
        scale,
        "--seed",
        str(seed),
        "--url",
        target,
        "--out",
        str(out),
    ]
    raise typer.Exit(subprocess.call(cmd, cwd=REPO))  # noqa: S603 - fixed argv


# ------------------------------------------------------------------ keys
@keys_app.command("generate")
def keys_generate(out: Path = Path("secrets/signing_key.pem")) -> None:
    """Generate an Ed25519 signing key (refuses to overwrite) + write the public key."""
    out.parent.mkdir(parents=True, exist_ok=True)
    signer = Signer.generate()
    signer.save(out)
    pub = out.with_suffix(".pub.pem")
    pub.write_text(signer.public_key_pem, encoding="utf-8")
    typer.echo(f"private key: {out}\npublic key:  {pub}\nkey id:      {signer.key_id}")


@keys_app.command("hmac")
def keys_hmac() -> None:
    """Print a fresh random LAE_HMAC_KEY value (put it in .env; never commit it)."""
    typer.echo(secrets.token_hex(32))


# ------------------------------------------------------------------ workflow
@app.command()
def snapshot() -> None:
    """Scan the catalog and store a lineage snapshot."""
    from lae.services import pipeline

    typer.echo(str(pipeline.create_snapshot(_rt(False), "cli")))


@app.command()
def erase(
    key: Annotated[list[str], typer.Option("--key", help="subject key value(s)")],
    root: str = "customer",
    live: bool = False,
    yes: Annotated[bool, typer.Option("--yes", help="confirm LIVE erasure")] = False,
    actor: str = "cli-operator",
) -> None:
    """Request -> discover -> plan -> dry run; with --live --yes: approve, execute, verify."""
    from lae.services import pipeline
    from lae.services.impl import LaeServices

    rt = _rt()
    svc = LaeServices(rt)
    subject = tuple(int(k) if k.lstrip("-").isdigit() else k for k in key)
    rid = pipeline.create_request(rt, root, subject, actor, "cli")
    pid = pipeline.generate_plan(rt, rid, actor)
    plan = svc.get_plan(pid)
    _echo(
        {
            "request_id": rid,
            "plan_id": pid,
            "plan_hash": plan.plan_hash,
            "status": plan.status,
            "blocked": plan.is_blocked,
            "counts_by_category": plan.counts_by_category,
            "conflicts": [(c.code, c.severity, c.table_name) for c in plan.conflicts],
        }
    )
    dry = pipeline.execute(rt, pid, ExecutionMode.DRY_RUN, actor)
    assert dry.result is not None
    _echo({"dry_run": [(s.step_order, s.status, s.rows_affected) for s in dry.result.steps]})
    if not live:
        typer.echo("dry run only (pass --live --yes to erase)")
        return
    if not yes:
        typer.echo("refusing LIVE erasure without --yes", err=True)
        raise typer.Exit(2)
    if plan.is_blocked:
        typer.echo("plan is BLOCKED; resolve conflicts first", err=True)
        raise typer.Exit(3)
    pipeline.approve(rt, pid, plan.plan_hash, actor)
    res = pipeline.execute(rt, pid, ExecutionMode.LIVE, actor)
    v = svc.get_verification(rid)
    _echo(
        {
            "execution": res.status,
            "timings_ms": res.timings_ms,
            "outcome": v.outcome,
            "coverage": f"{v.verified_artifacts}/{v.required_artifacts}",
            "residual_hits": [h.model_dump() for h in v.residual_hits],
            "evidence_id": res.evidence_id,
        }
    )


# ------------------------------------------------------------------ audit
@audit_app.command("verify")
def audit_verify() -> None:
    """Recompute the whole hash chain from the database."""
    from lae.services.impl import LaeServices

    res = LaeServices(_rt(False)).verify_chain()
    _echo(res.model_dump())
    raise typer.Exit(0 if res.valid else 1)


@audit_app.command("export-head")
def audit_export_head() -> None:
    """Write a signed chain-head anchor file outside the database."""
    from lae.services import pipeline

    typer.echo(pipeline.export_anchor(_rt(), "cli"))


@audit_app.command("export")
def audit_export(request_id: UUID, out_dir: Path = Path("evidence_out")) -> None:
    """Export evidence bundle, leaves and the full chain for offline verification."""
    from lae.db.engine import session_scope
    from lae.db.repositories import AuditRepo
    from lae.services.impl import LaeServices

    rt = _rt(False)
    svc = LaeServices(rt)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "bundle.json").write_bytes(svc.evidence_download(request_id))
    (out_dir / "leaves.json").write_bytes(svc.evidence_leaves(request_id))
    with session_scope(rt.reader, read_only=True) as s:
        chain = AuditRepo(s).export_chain()
    (out_dir / "chain.json").write_text(json.dumps(chain), encoding="utf-8")
    typer.echo(str(out_dir))


@app.command("verify-evidence")
def verify_evidence(
    bundle: Path,
    public_key: Annotated[Path, typer.Option("--public-key")],
    leaves: Annotated[Path | None, typer.Option()] = None,
    chain: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """OFFLINE verification of an evidence bundle (no database, no network)."""
    res = verify_evidence_offline(
        bundle.read_text(encoding="utf-8"),
        public_key.read_text(encoding="utf-8"),
        leaves.read_text(encoding="utf-8") if leaves else None,
        chain.read_text(encoding="utf-8") if chain else None,
    )
    _echo(
        {
            "valid": res.valid,
            "signature_ok": res.signature_ok,
            "merkle_ok": res.merkle_ok,
            "chain_ok": res.chain_ok,
            "errors": list(res.errors),
        }
    )
    raise typer.Exit(0 if res.valid else 1)


if __name__ == "__main__":  # pragma: no cover
    app()
