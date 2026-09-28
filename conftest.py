"""Root fixtures: one real-PostgreSQL server per test session, one fresh DB per test."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, make_url, text

from cricai_testing.pg_temp import TempPostgres, start_temp_postgres


@pytest.fixture(scope="session")
def pg_server() -> Iterator[TempPostgres]:
    pg = start_temp_postgres()
    yield pg
    pg.stop()


@pytest.fixture
def pg_url(pg_server: TempPostgres) -> Iterator[str]:
    """URL of a brand-new database, dropped after the test (full isolation)."""
    admin = create_engine(pg_server.url, isolation_level="AUTOCOMMIT")
    name = f"cricai_t_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield str(make_url(pg_server.url).set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()
