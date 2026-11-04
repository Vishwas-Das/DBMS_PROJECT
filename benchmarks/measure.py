"""Independent measurement by direct SQL. Never asks the engine what it did.

* ``take_snapshot``: md5 of every row of every key-addressable `app` table (+ a multiset for the
  key-less marketing log) so deletions and modifications can be attributed to a subject.
* ``erasure_coverage``: per ground-truth unit, is the row gone / correctly anonymized / retained?
* ``residual_scan``: an HMAC scan implemented here in SQL (pgcrypto), independent of
  ``lae.verification``, plus a substring scan for free-text copies.
* ``damage``: rows changed that do not belong to the subject.

Raw identifier values only ever travel as bound parameters and live in local variables. They are
never logged or written to results (results hold table names and counts only).
"""

from __future__ import annotations

import hashlib
import hmac
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import psycopg
from lae.verification import metrics
from psycopg import sql

WS = " \t\n\r\f\v"
SCHEMA = "app"
NO_PK_TABLE = "app.marketing_event_log"
# primary keys of the key-addressable app relations that appear in the ground truth
TRUTH_PK: dict[str, tuple[str, ...]] = {
    "app.customer": ("id",),
    "app.customer_address": ("id",),
    "app.loyalty_account": ("customer_id",),
    "app.purchase_order": ("id",),
    "app.order_line": ("order_id", "line_no"),
    "app.payment": ("id",),
    "app.shipment": ("id",),
    "app.support_ticket": ("id",),
    "app.ticket_message": ("id",),
    "app.document": ("id",),
    "app.document_embedding": ("id",),
    "app.customer_profile_cache": ("customer_id",),
    "app.customer_ltv": ("customer_id",),
    "app.customer_segment": ("customer_id",),
}
# retention ANONYMIZE: these columns must no longer carry the original value (or the reference)
ANONYMIZE_COLUMNS: dict[str, tuple[str, ...]] = {
    "app.purchase_order": ("shipping_name", "shipping_address"),
}
ANONYMIZE_REFERENCE: dict[str, str] = {"app.purchase_order": "customer_id"}
TEXT_TYPES = ("text", "character varying", "character", "jsonb", "json", "citext")

Unit = tuple[str, tuple[str, ...]]


def unit(table: str, key: Iterable[Any]) -> Unit:
    return (table, tuple(str(v) for v in key))


def ascii_lower_trim(value: str) -> str:
    stripped = value.strip(WS)
    return stripped.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _ident(qualified: str) -> sql.Identifier:
    schema, name = qualified.split(".", 1)
    return sql.Identifier(schema, name)


# ------------------------------------------------------------------ subject identifiers


@dataclass(frozen=True)
class SubjectIdentity:
    """Raw identifiers, in memory only (used as bound parameters)."""

    customer_id: int
    email_norm: str
    phone_norm: str | None

    def __repr__(self) -> str:  # never print raw identifiers
        return f"SubjectIdentity(customer_id=<{self.customer_id}>)"


def load_identity(conn: psycopg.Connection[Any], customer_id: int) -> SubjectIdentity:
    row = conn.execute(
        "SELECT email, phone FROM app.customer WHERE id = %s", (customer_id,)
    ).fetchone()
    if row is None:
        raise LookupError("subject not found in app.customer")
    email, phone = row
    return SubjectIdentity(
        customer_id,
        ascii_lower_trim(str(email)),
        ascii_lower_trim(phone) if phone else None,
    )


# ------------------------------------------------------------------ row snapshots


@dataclass
class Snapshot:
    rows: dict[str, dict[tuple[str, ...], str]] = field(default_factory=dict)
    marketing: Counter[tuple[str, bool]] = field(default_factory=Counter)
    subject_marketing_ctids: frozenset[str] = frozenset()
    anonymize_values: dict[Unit, tuple[Any, ...]] = field(default_factory=dict)
    referrer: dict[str, Any] = field(default_factory=dict)


def take_snapshot(
    conn: psycopg.Connection[Any],
    ident: SubjectIdentity,
    anonymize_units: Iterable[Unit] = (),
    referencing_units: Iterable[Unit] = (),
) -> Snapshot:
    snap = Snapshot()
    for table, pk in TRUTH_PK.items():
        q = sql.SQL("SELECT {pk}, md5(t::text) FROM {t} AS t").format(
            pk=sql.SQL(", ").join(sql.Identifier("t", c) for c in pk), t=_ident(table)
        )
        snap.rows[table] = {
            tuple(str(v) for v in r[:-1]): str(r[-1]) for r in conn.execute(q).fetchall()
        }
    q = sql.SQL(
        "SELECT md5(t::text), lower(btrim(t.email, %s)) = %s, t.ctid::text FROM {t} AS t"
    ).format(t=_ident(NO_PK_TABLE))
    ctids: list[str] = []
    for digest, is_subject, ctid in conn.execute(q, (WS, ident.email_norm)).fetchall():
        snap.marketing[(str(digest), bool(is_subject))] += 1
        if is_subject:
            ctids.append(str(ctid))
    snap.subject_marketing_ctids = frozenset(ctids)
    for table, key in anonymize_units:
        ref = ANONYMIZE_REFERENCE[table]
        cols = (ref, *ANONYMIZE_COLUMNS[table])
        pk = TRUTH_PK[table]
        q = sql.SQL("SELECT {cols} FROM {t} WHERE {cond}").format(
            cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
            t=_ident(table),
            cond=sql.SQL(" AND ").join(
                sql.SQL("{}::text = %s").format(sql.Identifier(c)) for c in pk
            ),
        )
        row = conn.execute(q, key).fetchone()
        if row is not None:
            snap.anonymize_values[(table, key)] = tuple(row)
    ids = [k[0] for _t, k in referencing_units]
    if ids:
        for cid, ref in conn.execute(
            "SELECT id::text, referrer_id FROM app.customer WHERE id::text = ANY(%s)",
            (ids,),
        ).fetchall():
            snap.referrer[str(cid)] = ref
    return snap


# ------------------------------------------------------------------ ground truth helpers


@dataclass(frozen=True)
class SubjectTruth:
    owned: frozenset[Unit]
    retained: Mapping[Unit, str]  # unit -> ANONYMIZE | RETAIN
    shared: frozenset[Unit]
    referencing_other: frozenset[Unit]
    derived: frozenset[Unit]
    hidden_lower_trim: int
    hidden_exact: int

    @classmethod
    def from_truth(cls, gt: Mapping[str, Any]) -> SubjectTruth:
        hidden = {h[2]: int(h[3]) for h in gt["hidden_copies"]}
        return cls(
            owned=frozenset(unit(t, k) for t, k in gt["owned"]),
            retained={unit(t, k): str(a) for t, k, a in gt["retained"]},
            shared=frozenset(unit(t, k) for t, k, _o in gt["shared"]),
            referencing_other=frozenset(unit(t, k) for t, k in gt["referencing_other"]),
            derived=frozenset(unit(t, k) for t, k in gt["derived_rows"]),
            hidden_lower_trim=hidden.get("LOWER_TRIM", 0),
            hidden_exact=hidden.get("EXACT", 0),
        )

    def anonymize_units(self) -> list[Unit]:
        return sorted(u for u, a in self.retained.items() if a == "ANONYMIZE")


def hidden_units(snap: Snapshot) -> frozenset[Unit]:
    return frozenset((NO_PK_TABLE, ("ctid", c)) for c in snap.subject_marketing_ctids)


# ------------------------------------------------------------------ discovery quality


def _fraction_out(f: Fraction | None) -> dict[str, Any] | None:
    if f is None:
        return None
    return {"exact": f"{f.numerator}/{f.denominator}", "value": float(f)}


def discovery_quality(
    discovered: frozenset[Unit], truth: SubjectTruth, before: Snapshot
) -> dict[str, Any]:
    full = truth.owned | truth.derived | hidden_units(before)
    tp, fp, fn = metrics.confusion(discovered, full)
    p, r = metrics.precision(tp, fp), metrics.recall(tp, fn)
    fk_tp, _fk_fp, fk_fn = metrics.confusion(discovered, truth.owned)
    return {
        "discovered": len(discovered),
        "truth": len(full),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "fk_truth": len(truth.owned),
        "fk_tp": fk_tp,
        "fk_fn": fk_fn,
        "precision": _fraction_out(p),
        "recall": _fraction_out(r),
        "f1": _fraction_out(metrics.f1(p, r)),
        "recall_fk_truth": _fraction_out(metrics.recall(fk_tp, fk_fn)),
    }


def resolve_record_graph(
    conn: psycopg.Connection[Any], nodes: Iterable[tuple[Any, Any]], roles: set[str]
) -> frozenset[Unit]:
    """RecordKeys (lae.contracts.types.RecordKey) of the given roles -> ground-truth units.

    Predicate keys (rows of key-less tables, or derived rows addressed by a non-PK key) are
    resolved by querying the rows they address, exactly as the executor would.
    """
    out: set[Unit] = set()
    for key, attrs in nodes:
        if attrs.role.value not in roles:
            continue
        table = key.table.qualified()
        pk = TRUTH_PK.get(table)
        if pk is not None and tuple(key.columns) == pk and not key.is_predicate:
            out.add(unit(table, key.values))
            continue
        cond = sql.SQL(" AND ").join(
            sql.SQL("{}::text = %s").format(sql.Identifier(c)) for c in key.columns
        )
        params = [str(v) for v in key.values]
        if table == NO_PK_TABLE:
            q = sql.SQL("SELECT ctid::text FROM {t} WHERE {c}").format(t=_ident(table), c=cond)
            out.update((table, ("ctid", str(r[0]))) for r in conn.execute(q, params).fetchall())
        elif pk is not None:
            q = sql.SQL("SELECT {pk} FROM {t} WHERE {c}").format(
                pk=sql.SQL(", ").join(map(sql.Identifier, pk)), t=_ident(table), c=cond
            )
            out.update(unit(table, r) for r in conn.execute(q, params).fetchall())
        else:  # a table outside the ground-truth vocabulary: counted as discovered (FP)
            out.add(unit(table, ("pred", *params)))
    return frozenset(out)


# ------------------------------------------------------------------ erasure coverage


def erasure_coverage(
    conn: psycopg.Connection[Any],
    ident: SubjectIdentity,
    truth: SubjectTruth,
    before: Snapshot,
    after: Snapshot,
) -> dict[str, Any]:
    """Fraction of ground-truth personal-data units handled correctly, measured by SQL."""
    cats: dict[str, list[int]] = {
        k: [0, 0]
        for k in (
            "owned_deleted",
            "anonymized",
            "retained",
            "derived",
            "hidden",
            "references",
        )
    }
    for u in sorted(truth.owned):
        table, key = u
        exists_after = key in after.rows.get(table, {})
        action = truth.retained.get(u)
        if action is None:
            c = cats["owned_deleted"]
            c[0] += not exists_after
        elif action == "RETAIN":
            c = cats["retained"]
            c[0] += exists_after and after.rows[table][key] == before.rows[table].get(key)
        else:  # ANONYMIZE
            c = cats["anonymized"]
            c[0] += exists_after and _anonymized_ok(conn, u, ident, before)
        c[1] += 1
    for table, key in sorted(truth.derived):
        c = cats["derived"]
        c[0] += key not in after.rows.get(table, {})
        c[1] += 1
    remaining_hidden = sum(n for (_d, subj), n in after.marketing.items() if subj)
    before_hidden = len(before.subject_marketing_ctids)
    cats["hidden"] = [max(before_hidden - remaining_hidden, 0), before_hidden]
    for _table, key in sorted(truth.referencing_other):
        c = cats["references"]
        if key in after.rows["app.customer"]:
            ref = conn.execute(
                "SELECT referrer_id FROM app.customer WHERE id::text = %s", (key[0],)
            ).fetchone()
            c[0] += ref is not None and ref[0] != ident.customer_id
        c[1] += 1
    correct = sum(v[0] for v in cats.values())
    total = sum(v[1] for v in cats.values())
    cov = metrics.coverage(correct, total)
    return {
        "units_correct": correct,
        "units_total": total,
        "coverage": _fraction_out(cov),
        "by_category": {k: {"correct": v[0], "total": v[1]} for k, v in cats.items()},
        "hidden_before_matches_truth": before_hidden == truth.hidden_lower_trim,
        "retention_violations": (cats["retained"][1] - cats["retained"][0])
        + sum(
            1
            for u, a in truth.retained.items()
            if a == "ANONYMIZE" and u[1] not in after.rows[u[0]]
        ),
    }


def _anonymized_ok(
    conn: psycopg.Connection[Any], u: Unit, ident: SubjectIdentity, before: Snapshot
) -> bool:
    table, key = u
    ref = ANONYMIZE_REFERENCE[table]
    cols = (ref, *ANONYMIZE_COLUMNS[table])
    pk = TRUTH_PK[table]
    q = sql.SQL("SELECT {cols} FROM {t} WHERE {cond}").format(
        cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
        t=_ident(table),
        cond=sql.SQL(" AND ").join(sql.SQL("{}::text = %s").format(sql.Identifier(c)) for c in pk),
    )
    row = conn.execute(q, key).fetchone()
    old = before.anonymize_values.get(u)
    if row is None or old is None:
        return False
    if row[0] is not None and row[0] == ident.customer_id:
        return False  # still linked to the subject
    return all(new is None or new != prev for new, prev in zip(row[1:], old[1:], strict=True))


# ------------------------------------------------------------------ damage


def damage(truth: SubjectTruth, ident: SubjectIdentity, before: Snapshot, after: Snapshot) -> dict:
    allowed = truth.owned | truth.derived | truth.shared | truth.referencing_other
    unrelated_deleted = unrelated_modified = shared_deleted = shared_modified = inserted = 0
    other_customers_deleted = 0
    for table, rows_before in before.rows.items():
        rows_after = after.rows.get(table, {})
        inserted += len(rows_after.keys() - rows_before.keys())
        for key, digest in rows_before.items():
            u = (table, key)
            gone = key not in rows_after
            changed = not gone and rows_after[key] != digest
            if not (gone or changed):
                continue
            if u in truth.shared:
                shared_deleted += gone
                shared_modified += changed
            elif u not in allowed:
                unrelated_deleted += gone
                unrelated_modified += changed
                if gone and table == "app.customer" and key[0] != str(ident.customer_id):
                    other_customers_deleted += 1
    before_other = Counter({k: n for k, n in before.marketing.items() if not k[1]})
    after_other = Counter({k: n for k, n in after.marketing.items() if not k[1]})
    marketing_unrelated = sum((before_other - after_other).values())
    return {
        "unrelated_rows_deleted": unrelated_deleted + marketing_unrelated,
        "unrelated_rows_modified": unrelated_modified,
        "unrelated_damage": unrelated_deleted + unrelated_modified + marketing_unrelated,
        "other_customers_deleted": other_customers_deleted,
        "shared_rows_deleted": shared_deleted,
        "shared_rows_modified": shared_modified,
        "rows_inserted": inserted,
    }


# ------------------------------------------------------------------ residual scan


def text_columns(conn: psycopg.Connection[Any]) -> dict[str, list[str]]:
    """Text-like columns of every table and materialized view in schema app (pg_catalog)."""
    q = """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'm') AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
    """
    out: dict[str, list[str]] = {}
    for rel, col, typ in conn.execute(q, (SCHEMA,)).fetchall():
        base = str(typ).split("(")[0].strip()
        if base in TEXT_TYPES:
            out.setdefault(f"{SCHEMA}.{rel}", []).append(str(col))
    return out


def residual_scan(
    conn: psycopg.Connection[Any],
    ident: SubjectIdentity,
    hmac_key: str,
    columns: Mapping[str, list[str]],
) -> dict[str, Any]:
    """Rows still holding the subject's email/phone.

    hmac: whole-value match of lower/trim(value) by HMAC-SHA256 computed in PostgreSQL
    (pgcrypto) against digests computed here. substring: the email occurs anywhere in the
    lower-cased text (free text, JSON payloads). Phone matches in other customers' own rows are
    excluded (synthetic phones can collide).
    """
    email_d = hmac.new(hmac_key.encode(), ident.email_norm.encode(), hashlib.sha256).hexdigest()
    phone_d = (
        hmac.new(hmac_key.encode(), ident.phone_norm.encode(), hashlib.sha256).hexdigest()
        if ident.phone_norm
        else None
    )
    per_table: dict[str, dict[str, int]] = {}
    total_h = total_s = 0
    for table, cols in sorted(columns.items()):

        def hm(c: str) -> sql.Composable:
            return sql.SQL(
                "encode(hmac(lower(btrim({c}::text, %(ws)s)), %(k)s, 'sha256'), 'hex')"
            ).format(c=sql.Identifier(c))

        email_hit = sql.SQL(" OR ").join(sql.SQL("{} = %(e)s").format(hm(c)) for c in cols)
        sub_hit = sql.SQL(" OR ").join(
            sql.SQL("strpos(lower({}::text), %(en)s) > 0").format(sql.Identifier(c)) for c in cols
        )
        phone_hit: sql.Composable = sql.SQL("false")
        if phone_d is not None:
            phone_hit = sql.SQL(" OR ").join(sql.SQL("{} = %(p)s").format(hm(c)) for c in cols)
            if table == "app.customer":
                phone_hit = sql.SQL("(({}) AND id = %(cid)s)").format(phone_hit)
        q = sql.SQL(
            "SELECT count(*) FILTER (WHERE ({e}) OR ({p})), "
            "count(*) FILTER (WHERE ({s}) OR ({p})) FROM {t}"
        ).format(e=email_hit, p=phone_hit, s=sub_hit, t=_ident(table))
        params = {
            "ws": WS,
            "k": hmac_key,
            "e": email_d,
            "p": phone_d,
            "en": ident.email_norm,
            "cid": ident.customer_id,
        }
        row = conn.execute(q, params).fetchone()
        h, s = (int(row[0]), int(row[1])) if row else (0, 0)
        if h or s:
            per_table[table] = {"hmac_rows": h, "substring_rows": s}
        total_h += h
        total_s += s
    return {"hmac_rows": total_h, "substring_rows": total_s, "by_table": per_table}


# ------------------------------------------------------------------ lae overhead


def lae_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'lae' ORDER BY tablename"
        ).fetchall()
    ]
    out: dict[str, int] = {}
    for t in tables:
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("lae", t))
        ).fetchone()
        out[t] = int(row[0]) if row else 0
    return out


def lae_size_bytes(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'lae' AND c.relkind IN ('r', 'm')"
    ).fetchone()
    return int(row[0]) if row else 0
