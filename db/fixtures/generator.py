"""Deterministic synthetic data generator for the demo `app` schema (database_schema.md §4, §7).

    python -m db.fixtures.generator --scale default --seed 42 \
        --url postgresql://lae_owner:...@127.0.0.1:5433/lae --out db/fixtures/out/ground_truth.json

* Fully synthetic: invented names, ``@example.test`` emails, ``+1-555`` phones. No real data.
* Deterministic: one ``random.Random(seed)``, fixed base timestamp, no ``now()``. The same seed and
  scale give byte-identical rows and ground truth.
* Ground truth is computed from the generator's OWN bookkeeping while it creates rows, never by
  querying foreign keys, so it is an independent oracle for discovery / planning / verification.
* Reloads idempotently: drops and recreates the `app` demo objects (app_schema.sql,
  app_derived.sql) and upserts the seeded `lae` configuration rows. It only ever touches the
  database it is given. The `lae` schema must already be migrated (alembic upgrade head).

Hard cases created deliberately for the KNOWN subjects (see ``known_subjects``):
referrals (other customers whose referrer_id = subject), the subject's own referrer (upstream,
must not be traversed), a ticket_message authored by ANOTHER customer on the subject's ticket and
a message by the subject on another customer's ticket (shared records), marketing_event_log rows
copying the subject's email (exact, upper-case + whitespace/tab, mixed case), a profile-cache
entry, documents (name + email in the body) with embeddings, orders with lines, payments and
shipments (retained under the tax-retention policy), a loyalty account and addresses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

log = logging.getLogger("lae.fixtures.generator")

FIXTURES_DIR = Path(__file__).resolve().parent
APP_SCHEMA_SQL = FIXTURES_DIR / "app_schema.sql"
APP_DERIVED_SQL = FIXTURES_DIR / "app_derived.sql"
BASE_TIME = datetime(2025, 1, 1, tzinfo=UTC)
EMBEDDING_DIM = 384
LEGAL_BASIS = "tax record retention (synthetic)"
SCHEMA = "app"

# Must stay byte-identical to the CTAS SELECT in app_derived.sql (checked by an integration test).
SEGMENT_DEFINITION_SQL = (
    "SELECT l.customer_id, CASE WHEN l.total_spent >= 100000 THEN 'GOLD' "
    "WHEN l.total_spent >= 20000 THEN 'SILVER' ELSE 'BRONZE' END AS segment "
    "FROM app.customer_ltv l"
)

# Table load order (parents first) and their columns, exactly as in app_schema.sql.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "customer": ("id", "full_name", "email", "phone", "referrer_id", "created_at"),
    "customer_address": ("id", "customer_id", "line1", "city", "postal_code", "country"),
    "loyalty_account": ("customer_id", "points", "tier", "joined_at"),
    "product": ("id", "sku", "name", "price_cents"),
    "purchase_order": (
        "id",
        "customer_id",
        "ordered_at",
        "status",
        "shipping_name",
        "shipping_address",
        "total_cents",
    ),
    "order_line": ("order_id", "line_no", "product_id", "quantity", "unit_price_cents"),
    "shipment": ("id", "order_id", "line_no", "carrier", "tracking_code", "shipped_at"),
    "payment": ("id", "order_id", "amount_cents", "method", "paid_at"),
    "support_ticket": ("id", "customer_id", "subject", "status", "opened_at"),
    "ticket_message": ("id", "ticket_id", "author_customer_id", "body", "sent_at"),
    "document": ("id", "customer_id", "title", "body", "created_at"),
    "document_embedding": ("id", "source_document_id", "model", "embedding"),
    "customer_profile_cache": ("customer_id", "payload", "refreshed_at"),
    "marketing_event_log": ("email", "event", "occurred_at"),
}
IDENTITY_TABLES = (
    "customer",
    "customer_address",
    "product",
    "purchase_order",
    "shipment",
    "payment",
    "support_ticket",
    "ticket_message",
    "document",
    "document_embedding",
)
DERIVED_RELATIONS = ("customer_ltv", "daily_revenue", "customer_segment")

_FIRST = (
    "Avel", "Brin", "Cora", "Dax", "Elin", "Fenn", "Gala", "Hollis", "Ivo", "Juna", "Kael",
    "Lio", "Mira", "Nox", "Oren", "Pell", "Quin", "Rhea", "Soren", "Tamsin", "Ulla", "Vero",
    "Wren", "Xan", "Yara", "Zev",
)  # fmt: skip
_LAST = (
    "Ashgrove", "Bellweather", "Copperfield", "Dunmore", "Emberly", "Foxhollow", "Greywater",
    "Hartwell", "Ironwood", "Juniper", "Kestrel", "Larkspur", "Marrow", "Northcott", "Oakhurst",
    "Pemberton", "Quillon", "Ravensworth", "Stillwater", "Thornbury", "Underhill", "Valemont",
    "Whitlock", "Yarrow", "Zephyrine",
)  # fmt: skip
_CITIES = ("Northmere", "Eastvale", "Southport", "Westbrook", "Lakeshire", "Hillcrest")
_STREETS = ("Synthetic Way", "Example Road", "Fixture Lane", "Test Avenue", "Sample Street")
_WORDS = ("widget", "gadget", "sprocket", "gizmo", "doohickey", "contraption", "module")
_TICKET_SUBJECTS = ("Delivery delay", "Billing question", "Return request", "Account access")
_EVENTS = ("NEWSLETTER_OPEN", "LINK_CLICK", "UNSUBSCRIBE", "CAMPAIGN_SENT")
_CARRIERS = ("PostSim", "ParcelFake", "ShipTest")


# ------------------------------------------------------------------ parameters


@dataclass(frozen=True, slots=True)
class ScaleParams:
    """Row-count knobs. Ratios are integer percentages (no floats anywhere)."""

    customers: int = 1000
    orders: int = 3000
    tickets: int = 1000
    documents: int = 500
    embeddings: int = 500
    products: int = 200
    marketing_events: int = 3000
    max_addresses_per_customer: int = 2
    max_lines_per_order: int = 4
    max_messages_per_ticket: int = 4
    loyalty_pct: int = 60
    referral_pct: int = 10
    cache_pct: int = 30
    shipment_pct: int = 70
    shared_message_pct: int = 5
    guest_order_pct: int = 3
    anonymous_ticket_pct: int = 5
    no_phone_pct: int = 10

    def __post_init__(self) -> None:
        if self.customers < 20:
            raise ValueError("customers must be >= 20 (the hard cases need distinct ids)")
        for name, value in asdict(self).items():
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.products < 1:
            raise ValueError("products must be >= 1")


PRESETS: dict[str, ScaleParams] = {
    "tiny": ScaleParams(
        customers=20,
        orders=40,
        tickets=15,
        documents=10,
        embeddings=10,
        products=15,
        marketing_events=40,
    ),
    "small": ScaleParams(
        customers=100,
        orders=300,
        tickets=100,
        documents=50,
        embeddings=50,
        products=50,
        marketing_events=300,
    ),
    "default": ScaleParams(),
    "large": ScaleParams(
        customers=10_000,
        orders=30_000,
        tickets=10_000,
        documents=5_000,
        embeddings=5_000,
        products=1_000,
        marketing_events=30_000,
    ),
}


@dataclass(frozen=True, slots=True)
class KnownSubjects:
    primary: int  # the richly connected subject (42 at scale >= 100)
    secondary: int  # the "other customer" in the shared-record cases
    referred_by_primary: tuple[int, ...]  # customers whose referrer_id = primary
    primary_referrer: int  # upstream of primary; must never be traversed

    @classmethod
    def for_scale(cls, scale: ScaleParams) -> KnownSubjects:
        if scale.customers >= 100:
            return cls(primary=42, secondary=43, referred_by_primary=(77, 78), primary_referrer=5)
        return cls(primary=7, secondary=8, referred_by_primary=(15, 16), primary_referrer=3)

    def ids(self) -> frozenset[int]:
        return frozenset((self.primary, self.secondary))


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """Per-subject oracle. ``subjects[customer_id]`` has the keys documented in ``FORMAT``."""

    meta: dict[str, Any]
    subjects: dict[int, dict[str, Any]]

    def subject(self, customer_id: int) -> dict[str, Any]:
        return self.subjects[customer_id]

    def to_json(self) -> dict[str, Any]:
        return {"meta": self.meta, "subjects": {str(k): v for k, v in self.subjects.items()}}

    def dumps(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> GroundTruth:
        return cls(dict(data["meta"]), {int(k): v for k, v in data["subjects"].items()})


FORMAT = {
    "owned": "[[table, [pk...]], ...] rows reachable downstream from the subject root via declared "
    "FKs whose record role is SUBJECT_ROOT/OWNED (includes retained rows)",
    "retained": "[[table, [pk...], action], ...] subset of owned under a retention policy",
    "shared": "[[table, [pk...], other_customer_id], ...] rows linked to the subject and to "
    "another customer (MANUAL_REVIEW)",
    "referencing_other": "[[table, [pk...]], ...] other customers' root rows that reference "
    "the subject (referrer_id); never deleted, reference nullified",
    "derived_rows": "[[table, [pk...]], ...] key-addressable derived rows (matview, CTAS, "
    "embedding via registered mapping, cache)",
    "aggregates": "[table, ...] aggregate artifacts the subject contributes to (refresh only)",
    "hidden_copies": "[[table, column, normalization, row_count], ...] un-keyed copies found "
    "only by inference or the residual scan; LOWER_TRIM counts include EXACT matches",
}


# ------------------------------------------------------------------ in-memory dataset


@dataclass(slots=True)
class Dataset:
    rows: dict[str, list[tuple[Any, ...]]] = field(default_factory=lambda: defaultdict(list))
    customer_email: dict[int, str] = field(default_factory=dict)
    customer_name: dict[int, str] = field(default_factory=dict)
    referrer: dict[int, int | None] = field(default_factory=dict)
    addresses: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    loyalty: set[int] = field(default_factory=set)
    order_owner: dict[int, int | None] = field(default_factory=dict)
    order_lines: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    order_payments: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    line_shipments: dict[tuple[int, int], list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    ticket_owner: dict[int, int | None] = field(default_factory=dict)
    message_ticket: dict[int, int] = field(default_factory=dict)
    message_author: dict[int, int | None] = field(default_factory=dict)
    document_owner: dict[int, int] = field(default_factory=dict)
    embedding_document: dict[int, int] = field(default_factory=dict)
    cached: set[int] = field(default_factory=set)
    marketing_emails: list[str] = field(default_factory=list)


class _Gen:
    def __init__(self, scale: ScaleParams, seed: int) -> None:
        self.scale = scale
        self.seed = seed
        self.rng = random.Random(seed)  # noqa: S311 - deterministic synthetic data, not crypto
        self.known = KnownSubjects.for_scale(scale)
        self.ds = Dataset()
        self._seq: dict[str, int] = defaultdict(int)

    # ---- helpers
    def _next_id(self, table: str) -> int:
        self._seq[table] += 1
        return self._seq[table]

    def _ts(self, max_days: int = 365) -> datetime:
        return BASE_TIME + timedelta(seconds=self.rng.randrange(max_days * 86_400))

    def _pct(self, pct: int) -> bool:
        return self.rng.randrange(100) < pct

    def _random_ordinary_customer(self) -> int:
        """Any customer except the known subjects (their data is created explicitly)."""
        while True:
            c = self.rng.randint(1, self.scale.customers)
            if c not in self.known.ids():
                return c

    # ---- entities
    def customers(self) -> None:
        k = self.known
        for cid in range(1, self.scale.customers + 1):
            first = self.rng.choice(_FIRST)
            last = self.rng.choice(_LAST)
            name = f"{first} {last}"
            email = f"{first}.{last}.{cid}@example.test".lower()
            phone: str | None = (
                f"+1-555-{self.rng.randrange(1000):03d}-{self.rng.randrange(10000):04d}"
            )
            if cid not in k.ids() and self._pct(self.scale.no_phone_pct):
                phone = None
            if cid in k.referred_by_primary:
                referrer: int | None = k.primary
            elif cid == k.primary:
                referrer = k.primary_referrer
            elif cid in k.ids() or cid == 1:
                referrer = None
            elif self._pct(self.scale.referral_pct):
                cand = self.rng.randint(1, cid - 1)
                referrer = None if cand in k.ids() else cand
            else:
                referrer = None
            self.ds.customer_email[cid] = email
            self.ds.customer_name[cid] = name
            self.ds.referrer[cid] = referrer
            self.ds.rows["customer"].append((cid, name, email, phone, referrer, self._ts(30)))

    def addresses(self) -> None:
        for cid in range(1, self.scale.customers + 1):
            n = (
                2
                if cid in self.known.ids()
                else self.rng.randint(0, self.scale.max_addresses_per_customer)
            )
            for _ in range(n):
                aid = self._next_id("customer_address")
                self.ds.addresses[cid].append(aid)
                self.ds.rows["customer_address"].append(
                    (
                        aid,
                        cid,
                        f"{self.rng.randint(1, 999)} {self.rng.choice(_STREETS)}",
                        self.rng.choice(_CITIES),
                        f"{self.rng.randrange(100000):05d}",
                        "ZZ",
                    )
                )

    def loyalty(self) -> None:
        for cid in range(1, self.scale.customers + 1):
            if cid in self.known.ids() or self._pct(self.scale.loyalty_pct):
                self.ds.loyalty.add(cid)
                self.ds.rows["loyalty_account"].append(
                    (
                        cid,
                        self.rng.randrange(50_000),
                        self.rng.choice(("BRONZE", "SILVER", "GOLD")),
                        self._ts(60),
                    )
                )

    def products(self) -> list[int]:
        prices = []
        for _ in range(self.scale.products):
            pid = self._next_id("product")
            price = self.rng.randint(199, 49_999)
            prices.append(price)
            self.ds.rows["product"].append(
                (pid, f"SKU-{pid:05d}", f"{self.rng.choice(_WORDS)} {pid}", price)
            )
        return prices

    def orders(self, prices: Sequence[int]) -> None:
        explicit = [self.known.primary] * 3 + [self.known.secondary]
        n_random = max(self.scale.orders - len(explicit), 0)
        owners: list[int | None] = list(explicit)
        for _ in range(n_random):
            owners.append(
                None if self._pct(self.scale.guest_order_pct) else self._random_ordinary_customer()
            )
        for i, owner in enumerate(owners):
            # the primary's first order is always SHIPPED with >= 2 lines (composite-FK case)
            self._order(owner, prices, force_shipped=i == 0)

    def _order(
        self, owner: int | None, prices: Sequence[int], *, force_shipped: bool = False
    ) -> None:
        oid = self._next_id("purchase_order")
        if force_shipped:
            status = "SHIPPED"
        elif owner in self.known.ids():
            status = self.rng.choice(("PAID", "SHIPPED"))
        else:
            status = self.rng.choice(("PLACED", "PAID", "SHIPPED", "SHIPPED", "CANCELLED"))
        ordered_at = self._ts()
        total = 0
        n_lines = self.rng.randint(1, self.scale.max_lines_per_order)
        if force_shipped:
            n_lines = max(n_lines, 2)
        for line_no in range(1, n_lines + 1):
            pid = self.rng.randint(1, len(prices))
            qty = self.rng.randint(1, 3)
            total += qty * prices[pid - 1]
            self.ds.order_lines[oid].append(line_no)
            self.ds.rows["order_line"].append((oid, line_no, pid, qty, prices[pid - 1]))
            if status == "SHIPPED" and (
                owner in self.known.ids() or self._pct(self.scale.shipment_pct)
            ):
                sid = self._next_id("shipment")
                self.ds.line_shipments[(oid, line_no)].append(sid)
                self.ds.rows["shipment"].append(
                    (
                        sid,
                        oid,
                        line_no,
                        self.rng.choice(_CARRIERS),
                        f"TRK{self.rng.randrange(10**9):09d}",
                        ordered_at + timedelta(days=self.rng.randint(1, 5)),
                    )
                )
        if owner is None:
            ship_name: str | None = f"Guest Buyer {oid}"
            ship_addr: str | None = f"{oid} Guest Street, {self.rng.choice(_CITIES)}"
        else:
            ship_name = self.ds.customer_name[owner]
            ship_addr = f"{self.rng.randint(1, 999)} {self.rng.choice(_STREETS)}"
        self.ds.order_owner[oid] = owner
        self.ds.rows["purchase_order"].append(
            (oid, owner, ordered_at, status, ship_name, ship_addr, total)
        )
        if status in ("PAID", "SHIPPED"):
            parts = [total] if not self._pct(10) or total < 2 else [total // 2, total - total // 2]
            for amount in parts:
                pay_id = self._next_id("payment")
                self.ds.order_payments[oid].append(pay_id)
                self.ds.rows["payment"].append(
                    (
                        pay_id,
                        oid,
                        amount,
                        self.rng.choice(("CARD", "BANK_TRANSFER", "VOUCHER")),
                        ordered_at + timedelta(hours=self.rng.randint(0, 48)),
                    )
                )

    def tickets(self) -> None:
        k = self.known
        explicit: list[int | None] = [k.primary, k.primary, k.secondary]
        n_random = max(self.scale.tickets - len(explicit), 0)
        owners: list[int | None] = list(explicit)
        for _ in range(n_random):
            owners.append(
                None
                if self._pct(self.scale.anonymous_ticket_pct)
                else self._random_ordinary_customer()
            )
        primary_tickets: list[int] = []
        secondary_ticket = 0
        for owner in owners:
            tid = self._next_id("support_ticket")
            self.ds.ticket_owner[tid] = owner
            self.ds.rows["support_ticket"].append(
                (
                    tid,
                    owner,
                    self.rng.choice(_TICKET_SUBJECTS),
                    self.rng.choice(("OPEN", "PENDING", "CLOSED")),
                    self._ts(),
                )
            )
            if owner == k.primary:
                primary_tickets.append(tid)
                self._message(tid, k.primary)
                self._message(tid, None)
            elif owner == k.secondary:
                secondary_ticket = tid
                self._message(tid, k.secondary)
            else:
                for _ in range(self.rng.randint(1, self.scale.max_messages_per_ticket)):
                    if owner is not None and self._pct(self.scale.shared_message_pct):
                        other = self._random_ordinary_customer()
                        author: int | None = other if other != owner else None
                    elif owner is None or self._pct(40):
                        author = None  # support agent
                    else:
                        author = owner
                    self._message(tid, author)
        # shared hard cases: secondary writes on primary's ticket, primary on secondary's ticket
        self._message(primary_tickets[0], k.secondary)
        self._message(secondary_ticket, k.primary)

    def _message(self, ticket_id: int, author: int | None) -> None:
        mid = self._next_id("ticket_message")
        self.ds.message_ticket[mid] = ticket_id
        self.ds.message_author[mid] = author
        who = "agent" if author is None else "customer"
        self.ds.rows["ticket_message"].append(
            (mid, ticket_id, author, f"Synthetic {who} message #{mid}.", self._ts())
        )

    def documents(self) -> None:
        k = self.known
        owners = [k.primary, k.primary, k.secondary]
        owners += [
            self._random_ordinary_customer()
            for _ in range(max(self.scale.documents - len(owners), 0))
        ]
        for owner in owners:
            did = self._next_id("document")
            self.ds.document_owner[did] = owner
            name, email = self.ds.customer_name[owner], self.ds.customer_email[owner]
            self.ds.rows["document"].append(
                (
                    did,
                    owner,
                    f"Synthetic contract {did}",
                    f"Customer record for {name} <{email}>. Synthetic document {did}.",
                    self._ts(),
                )
            )

    def embeddings(self) -> None:
        docs = sorted(self.ds.document_owner)
        known_docs = [d for d in docs if self.ds.document_owner[d] in self.known.ids()]
        others = [d for d in docs if d not in known_docs]
        order = known_docs + others
        if not order:
            return
        n = max(self.scale.embeddings, len(known_docs))
        for i in range(n):
            did = order[i % len(order)]
            eid = self._next_id("document_embedding")
            self.ds.embedding_document[eid] = did
            vec = ",".join(
                f"{self.rng.randrange(-1_000_000, 1_000_001) / 1_000_000:.6f}"
                for _ in range(EMBEDDING_DIM)
            )
            self.ds.rows["document_embedding"].append((eid, did, "synthetic-384", f"[{vec}]"))

    def cache(self) -> None:
        for cid in range(1, self.scale.customers + 1):
            if cid in self.known.ids() or self._pct(self.scale.cache_pct):
                self.ds.cached.add(cid)
                payload = {
                    "customer_id": cid,
                    "full_name": self.ds.customer_name[cid],
                    "email": self.ds.customer_email[cid],
                    "version": 1,
                }
                self.ds.rows["customer_profile_cache"].append(
                    (cid, json.dumps(payload, sort_keys=True), self._ts(10))
                )

    def marketing(self) -> None:
        k = self.known
        p_email = self.ds.customer_email[k.primary]
        emails = [p_email, p_email, p_email, f"  {p_email.upper()}\t", p_email.title()]
        emails.append(self.ds.customer_email[k.secondary])
        for _ in range(max(self.scale.marketing_events - len(emails), 0)):
            emails.append(self.ds.customer_email[self._random_ordinary_customer()])
        self.rng.shuffle(emails)
        for email in emails:
            self.ds.marketing_emails.append(email)
            self.ds.rows["marketing_event_log"].append(
                (email, self.rng.choice(_EVENTS), self._ts())
            )

    def run(self) -> Dataset:
        self.customers()
        self.addresses()
        self.loyalty()
        prices = self.products()
        self.orders(prices)
        self.tickets()
        self.documents()
        self.embeddings()
        self.cache()
        self.marketing()
        return self.ds


# ------------------------------------------------------------------ ground truth (bookkeeping)


def _ascii_lower_trim(value: str) -> str:
    """CR crypto-1 §5: strip exactly ' \\t\\n\\r\\f\\v', lowercase only A-Z."""
    stripped = value.strip(" \t\n\r\f\v")
    return stripped.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def build_ground_truth(ds: Dataset, scale: ScaleParams, seed: int, name: str) -> GroundTruth:
    known = KnownSubjects.for_scale(scale)
    orders_by: dict[int, list[int]] = defaultdict(list)
    for oid, owner in ds.order_owner.items():
        if owner is not None:
            orders_by[owner].append(oid)
    tickets_by: dict[int, list[int]] = defaultdict(list)
    for tid, owner in ds.ticket_owner.items():
        if owner is not None:
            tickets_by[owner].append(tid)
    docs_by: dict[int, list[int]] = defaultdict(list)
    for did, owner in ds.document_owner.items():
        docs_by[owner].append(did)
    emb_by_doc: dict[int, list[int]] = defaultdict(list)
    for eid, did in ds.embedding_document.items():
        emb_by_doc[did].append(eid)
    referred_by: dict[int, list[int]] = defaultdict(list)
    for cid, ref in ds.referrer.items():
        if ref is not None:
            referred_by[ref].append(cid)
    messages_by_ticket: dict[int, list[int]] = defaultdict(list)
    messages_by_author: dict[int, list[int]] = defaultdict(list)
    for mid, tid in ds.message_ticket.items():
        messages_by_ticket[tid].append(mid)
        author = ds.message_author[mid]
        if author is not None:
            messages_by_author[author].append(mid)
    exact_count: dict[str, int] = defaultdict(int)
    norm_count: dict[str, int] = defaultdict(int)
    for email in ds.marketing_emails:
        exact_count[email] += 1
        norm_count[_ascii_lower_trim(email)] += 1

    subjects: dict[int, dict[str, Any]] = {}
    for c in range(1, scale.customers + 1):
        owned: list[list[Any]] = [["app.customer", [c]]]
        retained: list[list[Any]] = []
        shared: list[list[Any]] = []
        derived: list[list[Any]] = []
        owned += [["app.customer_address", [a]] for a in ds.addresses.get(c, [])]
        if c in ds.loyalty:
            owned.append(["app.loyalty_account", [c]])
        has_payment = False
        for oid in orders_by.get(c, []):
            owned.append(["app.purchase_order", [oid]])
            retained.append(["app.purchase_order", [oid], "ANONYMIZE"])
            for ln in ds.order_lines.get(oid, []):
                owned.append(["app.order_line", [oid, ln]])
                retained.append(["app.order_line", [oid, ln], "RETAIN"])
                for sid in ds.line_shipments.get((oid, ln), []):
                    owned.append(["app.shipment", [sid]])
                    retained.append(["app.shipment", [sid], "RETAIN"])
            for pay in ds.order_payments.get(oid, []):
                has_payment = True
                owned.append(["app.payment", [pay]])
                retained.append(["app.payment", [pay], "RETAIN"])
        for tid in tickets_by.get(c, []):
            owned.append(["app.support_ticket", [tid]])
            for mid in messages_by_ticket.get(tid, []):
                author = ds.message_author[mid]
                if author is None or author == c:
                    owned.append(["app.ticket_message", [mid]])
                else:
                    shared.append(["app.ticket_message", [mid], author])
        for mid in messages_by_author.get(c, []):
            owner = ds.ticket_owner[ds.message_ticket[mid]]
            if owner == c:
                continue  # already counted via the ticket
            if owner is None:
                owned.append(["app.ticket_message", [mid]])
            else:
                shared.append(["app.ticket_message", [mid], owner])
        for did in docs_by.get(c, []):
            owned.append(["app.document", [did]])
            derived += [["app.document_embedding", [e]] for e in emb_by_doc.get(did, [])]
        if c in ds.cached:
            derived.append(["app.customer_profile_cache", [c]])
        if has_payment:
            derived.append(["app.customer_ltv", [c]])
            derived.append(["app.customer_segment", [c]])
        email = ds.customer_email[c]
        hidden: list[list[Any]] = []
        if exact_count.get(email):
            hidden.append(["app.marketing_event_log", "email", "EXACT", exact_count[email]])
        n_norm = norm_count.get(_ascii_lower_trim(email), 0)
        if n_norm:
            hidden.append(["app.marketing_event_log", "email", "LOWER_TRIM", n_norm])
        subjects[c] = {
            "owned": sorted(owned),
            "retained": sorted(retained),
            "shared": sorted(shared),
            "referencing_other": sorted(["app.customer", [r]] for r in referred_by.get(c, [])),
            "derived_rows": sorted(derived),
            "aggregates": ["app.daily_revenue"] if has_payment else [],
            "hidden_copies": hidden,
        }

    meta = {
        "generator_version": 1,
        "seed": seed,
        "scale_name": name,
        "scale": asdict(scale),
        "known_subjects": {
            "primary": known.primary,
            "secondary": known.secondary,
            "referred_by_primary": list(known.referred_by_primary),
            "primary_referrer": known.primary_referrer,
        },
        "row_counts": {t: len(ds.rows.get(t, [])) for t in TABLE_COLUMNS},
        "format": FORMAT,
    }
    return GroundTruth(meta, subjects)


# ------------------------------------------------------------------ database load


def _connect(target: Any) -> tuple[psycopg.Connection[Any], bool]:
    """Return (connection, owned). Accepts a URL, a psycopg connection or a SQLAlchemy engine."""
    if isinstance(target, psycopg.Connection):
        return target, False
    if hasattr(target, "url") and hasattr(target, "dispose"):  # SQLAlchemy Engine
        target = target.url.render_as_string(hide_password=False)
    if not isinstance(target, str):
        raise TypeError("seed_database needs a URL, psycopg.Connection or SQLAlchemy Engine")
    url = target.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(url), True


def _copy(cur: psycopg.Cursor[Any], table: str, rows: Iterable[tuple[Any, ...]]) -> None:
    cols = TABLE_COLUMNS[table]
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(SCHEMA, table), sql.SQL(", ").join(map(sql.Identifier, cols))
    )
    with cur.copy(stmt) as copy:
        for row in rows:
            copy.write_row(row)


def _seed_lae_config(cur: psycopg.Cursor[Any]) -> None:
    cur.execute("SELECT to_regclass('lae.derived_artifact') IS NOT NULL")
    found = cur.fetchone()
    if not found or not found[0]:
        raise RuntimeError("schema lae is not migrated: run `alembic upgrade head` first")

    cur.execute(
        "INSERT INTO lae.subject_root (name, schema_name, table_name, key_columns, "
        "identifier_columns) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
        "schema_name = EXCLUDED.schema_name, table_name = EXCLUDED.table_name, "
        "key_columns = EXCLUDED.key_columns, identifier_columns = EXCLUDED.identifier_columns",
        ("customer", SCHEMA, "customer", ["id"], ["email", "phone"]),
    )
    retention = [
        ("purchase_order", "ANONYMIZE"),
        ("order_line", "RETAIN"),
        ("payment", "RETAIN"),
        ("shipment", "RETAIN"),
    ]
    cur.executemany(
        "INSERT INTO lae.retention_policy (schema_name, table_name, action, legal_basis) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (schema_name, table_name) DO UPDATE SET "
        "action = EXCLUDED.action, legal_basis = EXCLUDED.legal_basis",
        [(SCHEMA, t, a, LEGAL_BASIS) for t, a in retention],
    )
    pii = [
        ("customer", "full_name", "DIRECT_IDENTIFIER", "PSEUDONYMIZE", False),
        ("customer", "email", "DIRECT_IDENTIFIER", "PSEUDONYMIZE", True),
        ("customer", "phone", "DIRECT_IDENTIFIER", "NULLIFY", True),
        ("customer_address", "line1", "QUASI_IDENTIFIER", "PSEUDONYMIZE", False),
        ("customer_address", "city", "QUASI_IDENTIFIER", "PSEUDONYMIZE", False),
        ("customer_address", "postal_code", "QUASI_IDENTIFIER", "PSEUDONYMIZE", False),
        ("purchase_order", "shipping_name", "DIRECT_IDENTIFIER", "PSEUDONYMIZE", False),
        ("purchase_order", "shipping_address", "QUASI_IDENTIFIER", "NULLIFY", False),
        ("ticket_message", "body", "SENSITIVE", "PSEUDONYMIZE", False),
        ("document", "body", "SENSITIVE", "PSEUDONYMIZE", False),
    ]
    cur.executemany(
        "INSERT INTO lae.pii_classification (schema_name, table_name, column_name, category, "
        "erasure_strategy, source, is_confirmed, use_for_residual_scan) "
        "VALUES (%s, %s, %s, %s, %s, 'MANUAL', true, %s) "
        "ON CONFLICT (schema_name, table_name, column_name) DO UPDATE SET "
        "category = EXCLUDED.category, erasure_strategy = EXCLUDED.erasure_strategy, "
        "source = EXCLUDED.source, is_confirmed = EXCLUDED.is_confirmed, "
        "use_for_residual_scan = EXCLUDED.use_for_residual_scan",
        [(SCHEMA, t, c, cat, strat, scan) for t, c, cat, strat, scan in pii],
    )

    viewdefs: dict[str, str] = {}
    for mv in ("customer_ltv", "daily_revenue"):
        cur.execute("SELECT pg_get_viewdef(to_regclass(%s), true)", (f"{SCHEMA}.{mv}",))
        row = cur.fetchone()
        assert row is not None and row[0]
        viewdefs[mv] = str(row[0])

    def mapping(source: str, src_cols: list[str], tgt_cols: list[str]) -> str:
        return json.dumps(
            [{"source": source, "source_columns": src_cols, "target_columns": tgt_cols}]
        )

    artifacts: list[tuple[Any, ...]] = [
        # name, kind, schema, table, definition_sql, strategy, mapping, is_inspectable
        ("customer_ltv", "MATERIALIZED_VIEW", SCHEMA, "customer_ltv", viewdefs["customer_ltv"],
         "REFRESH_MATVIEW", None, True),
        ("daily_revenue", "MATERIALIZED_VIEW", SCHEMA, "daily_revenue",
         viewdefs["daily_revenue"], "REFRESH_MATVIEW", None, True),
        ("customer_segment", "DERIVED_TABLE", SCHEMA, "customer_segment",
         SEGMENT_DEFINITION_SQL, "RECOMPUTE_SQL", None, True),
        ("document_embedding", "EMBEDDING_TABLE", SCHEMA, "document_embedding", None,
         "DELETE_BY_KEY", mapping("app.document", ["id"], ["source_document_id"]), True),
        ("customer_profile_cache", "CACHE", SCHEMA, "customer_profile_cache", None,
         "INVALIDATE_BY_KEY", mapping("app.customer", ["id"], ["customer_id"]), True),
        ("crm_export_files", "EXTERNAL", "external", "crm_export_files", None,
         "EXTERNAL_TASK", mapping("app.customer", ["id"], ["customer_id"]), False),
    ]  # fmt: skip
    cur.execute(
        "DELETE FROM lae.derived_artifact WHERE name = ANY(%s) OR (schema_name, table_name) IN "
        "(SELECT s, t FROM unnest(%s::text[], %s::text[]) AS u(s, t))",
        ([a[0] for a in artifacts], [a[2] for a in artifacts], [a[3] for a in artifacts]),
    )
    cur.executemany(
        "INSERT INTO lae.derived_artifact (name, kind, schema_name, table_name, definition_sql, "
        "refresh_strategy, source_key_mapping, is_inspectable) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
        artifacts,
    )


def load_dataset(conn: psycopg.Connection[Any], ds: Dataset) -> None:
    """Recreate the `app` demo objects and load `ds` in the connection's current transaction."""
    with conn.cursor() as cur:
        cur.execute(APP_SCHEMA_SQL.read_text(encoding="utf-8"))
        for table in TABLE_COLUMNS:
            _copy(cur, table, ds.rows.get(table, []))
        for table in IDENTITY_TABLES:
            qualified = f"{SCHEMA}.{table}"
            cur.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    "GREATEST((SELECT coalesce(max(id), 0) FROM {t}), 1), "
                    "(SELECT count(*) > 0 FROM {t}))"
                ).format(t=sql.Identifier(SCHEMA, table)),
                (qualified,),
            )
        cur.execute(APP_DERIVED_SQL.read_text(encoding="utf-8"))
        for table in (*TABLE_COLUMNS, *DERIVED_RELATIONS):
            cur.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(SCHEMA, table)))
        _seed_lae_config(cur)


def resolve_scale(scale: str | ScaleParams) -> tuple[str, ScaleParams]:
    if isinstance(scale, ScaleParams):
        name = next((k for k, v in PRESETS.items() if v == scale), "custom")
        return name, scale
    if scale not in PRESETS:
        raise ValueError(f"unknown scale {scale!r}; choose one of {sorted(PRESETS)}")
    return scale, PRESETS[scale]


def generate(scale: str | ScaleParams, seed: int) -> tuple[Dataset, GroundTruth]:
    """Pure: build the dataset and its ground truth in memory (no database)."""
    name, params = resolve_scale(scale)
    ds = _Gen(params, seed).run()
    return ds, build_ground_truth(ds, params, seed, name)


def seed_database(
    conn_or_url: Any,
    scale: str | ScaleParams = "default",
    seed: int = 42,
    *,
    out: Path | str | None = None,
) -> GroundTruth:
    """Generate, (re)load `app` + seeded `lae` config in ONE transaction, commit, return truth.

    If `out` is given, the ground truth JSON is written there (parent directories created).
    """
    ds, truth = generate(scale, seed)
    conn, owned = _connect(conn_or_url)
    try:
        load_dataset(conn, ds)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if owned:
            conn.close()
    log.info("seeded app schema: %s", truth.meta["row_counts"])
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(truth.dumps(), encoding="utf-8")
    return truth


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the synthetic LAE demo dataset.")
    parser.add_argument("--scale", default="default", choices=sorted(PRESETS))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--url", default=os.environ.get("LAE_DATABASE_URL"))
    parser.add_argument("--out", default=str(FIXTURES_DIR / "out" / "ground_truth.json"))
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or LAE_DATABASE_URL is required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    truth = seed_database(args.url, args.scale, args.seed, out=args.out)
    counts = dict(truth.meta["row_counts"])
    print(json.dumps({"scale": args.scale, "seed": args.seed, "row_counts": counts}, indent=2))
    return 0


__all__ = [
    "PRESETS",
    "SEGMENT_DEFINITION_SQL",
    "GroundTruth",
    "KnownSubjects",
    "ScaleParams",
    "build_ground_truth",
    "generate",
    "load_dataset",
    "main",
    "resolve_scale",
    "seed_database",
]

if __name__ == "__main__":
    raise SystemExit(main())
