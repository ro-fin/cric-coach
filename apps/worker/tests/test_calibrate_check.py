"""Unit tests for the ``calibrate_check`` stage (US-J1): report calibration state, never crash."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from cricai_data.db import create_all
from cricai_data.enums import BowlerSource, CalibrationKind, SessionType
from cricai_data.models import Calibration, Player
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.calibrate_check import check_session_calibration
from cricai_worker.context import WorkerContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

SESSION_DATE = date(2026, 7, 7)


@pytest.fixture
def ctx(tmp_path: Any) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        store=FsObjectStore(tmp_path / "store"),
    )


def _add_calibration(ctx: WorkerContext, *, created_at: datetime, valid: bool) -> uuid.UUID:
    with ctx.session_factory() as db:
        calibration = Calibration(
            camera_id="C1",
            era_no=1,
            kind=CalibrationKind.INTRINSIC,
            params={},
            valid=valid,
            created_at=created_at,
        )
        db.add(calibration)
        db.commit()
        return calibration.id


def _seed_session(
    ctx: WorkerContext,
    *,
    calibration_id: uuid.UUID | None,
    calibration_suspect: bool = False,
) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            calibration_id=calibration_id,
            calibration_suspect=calibration_suspect,
        )
        db.add_all([player, session])
        db.commit()
        return session.id


def test_missing_session_raises_lookup_error(ctx: WorkerContext) -> None:
    with pytest.raises(LookupError, match="not found"):
        check_session_calibration(ctx, uuid.uuid4())


def test_no_calibration_attached_is_reported_not_raised(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx, calibration_id=None, calibration_suspect=True)
    out = check_session_calibration(ctx, session_id)
    assert out == {
        "calibration_id": None,
        "has_calibration": False,
        "valid": None,
        "calibration_suspect": True,
        "age_days": None,
    }


def test_dangling_calibration_id_is_treated_as_absent(ctx: WorkerContext) -> None:
    """A calibration_id pointing at a deleted row: honest 'no calibration', never a crash."""
    calibration_id = _add_calibration(ctx, created_at=datetime(2026, 6, 1, tzinfo=UTC), valid=True)
    session_id = _seed_session(ctx, calibration_id=calibration_id)
    with ctx.session_factory() as db:
        db.delete(db.get(Calibration, calibration_id))
        db.commit()
    out = check_session_calibration(ctx, session_id)
    assert out["has_calibration"] is False
    assert out["age_days"] is None
    assert out["valid"] is None


def test_fresh_calibration_reports_age_and_validity(ctx: WorkerContext) -> None:
    calibration_id = _add_calibration(ctx, created_at=datetime(2026, 6, 27, tzinfo=UTC), valid=True)
    session_id = _seed_session(ctx, calibration_id=calibration_id)
    out = check_session_calibration(ctx, session_id)
    assert out["has_calibration"] is True
    assert out["calibration_id"] == str(calibration_id)
    assert out["valid"] is True
    assert out["age_days"] == 10.0  # 2026-07-07 minus 2026-06-27


def test_future_calibration_age_clamps_to_zero(ctx: WorkerContext) -> None:
    """A calibration dated after the session clamps to age 0 rather than going negative."""
    calibration_id = _add_calibration(
        ctx, created_at=datetime(2026, 7, 20, tzinfo=UTC), valid=False
    )
    session_id = _seed_session(ctx, calibration_id=calibration_id)
    out = check_session_calibration(ctx, session_id)
    assert out["age_days"] == 0.0
    assert out["valid"] is False
