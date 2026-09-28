"""US-J1/L1 pipeline: real-PostgreSQL fault matrix, lock contention, wiring; golden trace.

Integration tests exercise the parts SQLite cannot: real ``pg_advisory_lock``
contention between two connections, and resume-from-failure against the
enforced schema. The wiring test resolves every production dotted path — the
five CV media stages now resolve to their Phase-6 call-adapters
(``cricai_worker.stage_adapters``); the full real-media DAG run lives in
``test_stage_adapters_pg_integration.py``. The golden test pins the persisted
stage sequence by the rows' own execution timestamps.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import BowlerSource, SessionType, StageStatus
from cricai_data.models import PipelineStage, Player
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import (
    DEFAULT_STAGE_PATHS,
    STAGES,
    AdvisoryLock,
    RunPolicy,
    advisory_lock_key,
    resolve_stage,
    run_pipeline,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"

_CALLS: dict[str, int] = {}


def ok_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    return {"ok": True}


def flaky_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    _CALLS["flaky"] = _CALLS.get("flaky", 0) + 1
    if _CALLS["flaky"] == 1:
        raise RuntimeError("first attempt fails")
    return {"attempt": _CALLS["flaky"]}


def _path(name: str) -> str:
    return f"{__name__}:{name}"


def _all_ok(**overrides: str) -> dict[str, str]:
    registry = {stage: _path("ok_stage") for stage in STAGES}
    registry.update(overrides)
    return registry


def _seed(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.commit()
        return session.id


# ------------------------------------------------------------- integration


@pytest.mark.integration
def test_advisory_lock_contention_on_postgres(pg_url: str) -> None:
    """Two workers, one session: the second holder is denied until the first releases."""
    engine = make_engine(pg_url)
    key = advisory_lock_key(uuid.uuid4())
    first = AdvisoryLock(engine=engine, key=key)
    second = AdvisoryLock(engine=engine, key=key)
    assert first.acquire() is True
    assert second.acquire() is False  # held by `first`
    first.release()
    assert second.acquire() is True  # now free
    second.release()


@pytest.mark.integration
def test_resume_from_failure_on_postgres(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent-session-safe resume: the first run loses `pose`, the rerun reuses the rest."""
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    ctx = WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )
    session_id = _seed(ctx)
    _CALLS.clear()
    registry = _all_ok(pose=_path("flaky_stage"))
    policy = RunPolicy(max_attempts=1)

    first = run_pipeline(ctx, session_id, registry=registry, policy=policy)
    assert first.status == "failed"
    assert next(s for s in first.stages if s.stage == "pose").status is StageStatus.FAILED

    second = run_pipeline(ctx, session_id, registry=registry, policy=policy)
    fate = {s.stage: s for s in second.stages}
    assert second.status == "succeeded"
    assert fate["probe"].resumed is False  # probe re-executes (fresh footage inventory)
    assert fate["events"].resumed is True  # upstream media work reused
    assert fate["pose"].resumed is False
    assert fate["report"].status is StageStatus.SUCCEEDED


@pytest.mark.integration
def test_all_default_stage_paths_resolve() -> None:
    """Production wiring check: every dotted path must import to a callable, so
    a module/function rename fails loudly here. Every stage — including the
    five CV media stages via their Phase-6 call-adapters — is a real
    ``fn(ctx, session_id)`` callable; the full real-media run is proven in
    ``test_stage_adapters_pg_integration.py``."""
    unresolved: dict[str, str] = {}
    for stage, path in DEFAULT_STAGE_PATHS.items():
        try:
            resolve_stage(path)
        except Exception as exc:  # report every miswire at once, not just the first
            unresolved[stage] = f"{path} ({type(exc).__name__})"
    assert not unresolved, f"unresolvable stage paths: {unresolved}"


# -------------------------------------------------------------- golden trace


@pytest.mark.golden
def test_golden_stage_sequence(tmp_path: Path) -> None:
    """The persisted trace for a synthetic session pins the EXECUTED stage order (US-J1 ST).

    Rows are ordered by their own ``started_at`` — the runner's record of when
    each stage actually ran — never by the expected order itself (sorting by
    ``STAGES.index`` would make the sequence assertions tautologically true for
    any execution order). A runner regression that reorders or parallelizes
    stages, or skips/fails one, now fails this golden.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    ctx = WorkerContext(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        store=FsObjectStore(tmp_path / "store"),
    )
    session_id = _seed(ctx)
    outcome = run_pipeline(ctx, session_id, registry=_all_ok())
    assert outcome.status == "succeeded"

    with ctx.session_factory() as db:
        rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == outcome.run_id)).all()
    assert all(r.status is StageStatus.SUCCEEDED for r in rows)
    assert all(r.started_at is not None and r.finished_at is not None for r in rows)

    def _started(row: PipelineStage) -> datetime:
        assert row.started_at is not None
        return row.started_at

    sequence = [r.stage for r in sorted(rows, key=_started)]
    assert sequence == list(STAGES)  # each stage exactly once, in executed order
    # US-H5 supremacy end to end: the report only started after safety finished.
    safety = next(r for r in rows if r.stage == "safety")
    report = next(r for r in rows if r.stage == "report")
    assert safety.finished_at is not None and report.started_at is not None
    assert safety.finished_at <= report.started_at
