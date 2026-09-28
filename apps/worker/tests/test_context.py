"""WorkerContext bootstrap tests: env-driven wiring for job modules."""

from pathlib import Path

import pytest
from cricai_data.storage import FsObjectStore
from cricai_worker.context import ENV_DATABASE_URL, ENV_STORAGE_ROOT, WorkerContext


def test_from_env_reads_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DATABASE_URL, "sqlite://")
    monkeypatch.setenv(ENV_STORAGE_ROOT, str(tmp_path / "store"))
    context = WorkerContext.from_env()
    assert isinstance(context.store, FsObjectStore)
    with context.session_factory() as session:
        assert session.get_bind().dialect.name == "sqlite"
    context.store.put("probe/liveness", b"ok")
    assert context.store.get("probe/liveness") == b"ok"


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_DATABASE_URL, raising=False)
    monkeypatch.delenv(ENV_STORAGE_ROOT, raising=False)
    monkeypatch.chdir(tmp_path)
    context = WorkerContext.from_env()
    engine = context.session_factory.kw.get("bind")
    assert engine is not None
    assert engine.dialect.name == "postgresql"
    assert isinstance(context.store, FsObjectStore)
