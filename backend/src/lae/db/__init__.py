"""lae.db: PostgreSQL adapters (module_contracts.md §4). Used only by lae.services.

* `lae.db.engine`: make_engine, session_scope
* `lae.db.catalog_queries`: PgCatalogReader (CatalogReader port)
* `lae.db.target`: PgTargetReader / PgTargetWriter (TargetReader / TargetWriter ports)
* `lae.db.audit_sink`: PgAuditSink (AuditSink port)
* `lae.db.repositories`: ORM <-> contract repositories
* `lae.db.models`: SQLAlchemy models (never leave lae.db)
"""
