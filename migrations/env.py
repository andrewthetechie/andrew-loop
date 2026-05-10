"""Alembic environment — sync SQLite runner for orch migrations."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from orch.db import Base

# Alembic Config object
alembic_config = context.config

# Set up Python logging from alembic.ini if present
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    """Resolve the database URL.

    Priority:
    1. -x db_path=<path> CLI option passed to alembic
    2. ORCH_DB_PATH environment variable
    3. sqlalchemy.url from alembic.ini
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_path" in x_args:
        return f"sqlite:///{x_args['db_path']}"

    env_path = os.environ.get("ORCH_DB_PATH")
    if env_path:
        return f"sqlite:///{env_path}"

    return alembic_config.get_main_option("sqlalchemy.url", "sqlite:///.orchestra/state.db")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL script without a DB connection)."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    cfg_section = alembic_config.get_section(alembic_config.config_ini_section, {})
    cfg_section["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
