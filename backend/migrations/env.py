"""Alembic environment for the `lae` schema.

URL resolution: `-x url=...` > `sqlalchemy.url` (set programmatically) > LAE_DATABASE_URL.
The URL (and its password) is never logged. Only objects in schema `lae` are managed; the
version table lives in `lae` too.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool, text

from lae.db.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata
VERSION_SCHEMA = "lae"


def _database_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("url") or config.get_main_option("sqlalchemy.url")
    if not url:
        from lae.config import Settings

        s = Settings()
        # migrations need the owner role; the app role (lae_app) cannot run DDL
        url = s.admin_url or s.database_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "schema":
        return name == VERSION_SCHEMA
    return True


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=VERSION_SCHEMA,
        include_schemas=True,
        include_name=_include_name,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        engine = create_engine(_database_url(), poolclass=pool.NullPool)
        with engine.connect() as connection:
            _run(connection)
        engine.dispose()
    else:
        _run(connectable)


def _run(connection: Any) -> None:
    # The version table lives in `lae`, so the schema must exist before Alembic touches it.
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS lae"))
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=VERSION_SCHEMA,
        include_schemas=True,
        include_name=_include_name,
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
