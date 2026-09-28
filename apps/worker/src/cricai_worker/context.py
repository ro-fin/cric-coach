"""Shared worker bootstrap: one place to build (db session factory, object store).

Every job module (detect_events, cut_clips, extract_pose, ...) takes a
:class:`WorkerContext` so tests inject temp-Postgres + tmp-dir stores and
production reads the same ``CRICAI_*`` environment the API uses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cricai_data.db import make_engine, make_session_factory
from cricai_data.storage import FsObjectStore, ObjectStore
from sqlalchemy.orm import Session, sessionmaker

#: Environment variables (same names/prefix as the API's pydantic settings).
ENV_DATABASE_URL = "CRICAI_DATABASE_URL"
ENV_STORAGE_ROOT = "CRICAI_STORAGE_ROOT"

DEFAULT_DATABASE_URL = "postgresql+psycopg://cricai:cricai@localhost:5432/cricai"
DEFAULT_STORAGE_ROOT = "storage"


@dataclass(frozen=True)
class WorkerContext:
    """What a job needs to touch the world; nothing else is global."""

    session_factory: sessionmaker[Session]
    store: ObjectStore

    @classmethod
    def from_env(cls) -> WorkerContext:
        database_url = os.environ.get(ENV_DATABASE_URL, DEFAULT_DATABASE_URL)
        storage_root = Path(os.environ.get(ENV_STORAGE_ROOT, DEFAULT_STORAGE_ROOT))
        return cls(
            session_factory=make_session_factory(make_engine(database_url)),
            store=FsObjectStore(storage_root),
        )
