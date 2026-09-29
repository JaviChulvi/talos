from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    return create_engine(get_settings().connection_url, pool_pre_ping=True, hide_parameters=True)


def session_factory():
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with session_factory()() as session:
        yield session


Database = Annotated[Session, Depends(get_db)]
