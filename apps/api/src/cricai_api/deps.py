"""Request-scoped dependencies: DB session and object store from app state."""

from collections.abc import Iterator

from cricai_data.storage import FsObjectStore
from fastapi import Request
from sqlalchemy.orm import Session


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_store(request: Request) -> FsObjectStore:
    store: FsObjectStore = request.app.state.store
    return store
