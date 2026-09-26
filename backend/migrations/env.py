from alembic import context
from sqlalchemy import create_engine, pool

from backend.app.config import get_settings
from backend.app.db import Base


def run_migrations():
    engine = create_engine(
        get_settings().connection_url, poolclass=pool.NullPool, hide_parameters=True
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
