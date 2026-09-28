from alembic import context
from sqlalchemy import create_engine, pool

from backend.app import connections, models  # noqa: F401
from backend.app.config import get_settings
from backend.app.db import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations():
    connection = context.config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
        return
    engine = create_engine(
        get_settings().connection_url, poolclass=pool.NullPool, hide_parameters=True
    )
    with engine.connect() as connection:
        migrate(connection)
    engine.dispose()


run_migrations()
