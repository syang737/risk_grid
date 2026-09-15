"""Database engine and session handling.

Postgres in production, SQLite in tests and local dev. The models avoid
anything Postgres-specific so the two stay interchangeable, which keeps the
test suite fast and offline.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_URL = "sqlite+pysqlite:///./risk_grid.db"


def database_url() -> str:
    return os.environ.get("RISK_GRID_DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None, **kwargs) -> Engine:
    url = url or database_url()
    if url.startswith("sqlite"):
        # SQLite's default thread check rejects the connection reuse a threaded
        # web server does; the pool still serialises writes.
        kwargs.setdefault("connect_args", {"check_same_thread": False})
    else:
        kwargs.setdefault("pool_pre_ping", True)
    return create_engine(url, **kwargs)


class Database:
    """Owns an engine and hands out sessions."""

    def __init__(self, url: str | None = None, **kwargs) -> None:
        self.engine = make_engine(url, **kwargs)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create tables directly.

        For tests and first-run dev only -- deployed schema changes go through
        Alembic, because a customer's audit log cannot be dropped and recreated.
        """
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self._sessions()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        _database = Database()
    return _database


def set_database(database: Database | None) -> None:
    """Point the process at a specific database. Used by tests and the CLI."""
    global _database
    _database = database
