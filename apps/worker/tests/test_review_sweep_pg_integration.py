"""IT (US-J5): review-gate hold -> timeout sweep on real PostgreSQL.

Runs the migrated schema (including the seeded ``app_settings`` v1): the seed
governs (auto_publish -> no hold), a coach_gate version holds a draft with a
tz-aware ``review_due_at`` that round-trips intact, and the sweep publishes it
at timeout with the audit trail complete.
"""

import datetime
import uuid
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cricai_coaching.app_settings import APP_SETTINGS_SEED_VERSION, DEFAULT_APP_SETTINGS
from cricai_coaching.review_gate import review_due_at_for
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import BowlerSource, ReportKind, ReportStatus, SessionType
from cricai_data.models import AppSetting, AuditLog, Player, Report, Session
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.review_sweep import AUDIT_ACTOR, sweep_due_reports
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

pytestmark = pytest.mark.integration

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"

NOW = datetime.datetime(2026, 7, 11, 10, 0, tzinfo=UTC)


def _ctx(pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerContext:
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    return WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed_player_report(db: OrmSession, review_due_at: datetime.datetime) -> uuid.UUID:
    player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
    session = Session(
        player=player,
        session_date=datetime.date(2026, 7, 10),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add_all([player, session])
    db.flush()
    report = Report(
        player_id=player.id,
        session_id=session.id,
        kind=ReportKind.DAILY,
        period_start=session.session_date,
        period_end=session.session_date,
        status=ReportStatus.DRAFT,
        body={"kind": "daily"},
        review_due_at=review_due_at,
    )
    db.add(report)
    db.commit()
    return report.id


def test_hold_and_timeout_sweep_on_postgres(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(pg_url, tmp_path, monkeypatch)

    with ctx.session_factory() as db:
        # Finding [27]: the seed ROW must exist — `review_due_at_for is None`
        # alone is indistinguishable from the unseeded-table fallback, so a
        # dropped op.bulk_insert would otherwise pass every gate.
        seeded = db.scalar(
            select(AppSetting).where(AppSetting.version == APP_SETTINGS_SEED_VERSION)
        )
        assert seeded is not None, "Phase-6 migration must seed app_settings v1"
        assert seeded.settings == DEFAULT_APP_SETTINGS
        assert seeded.approved_by is not None

        # The migration's seeded v1 governs: auto_publish means no hold.
        assert review_due_at_for(db, now=NOW) is None

        # A coach-approved coach_gate version flips the gate on.
        db.add(
            AppSetting(
                version=2,
                settings={
                    "report_review": {"mode": "coach_gate", "timeout_hours": 24},
                    "live_mode": {"enabled": False, "allowlist": ["ball_count"]},
                },
                approved_by="coach",
                reason="review before the player sees reports",
            )
        )
        db.commit()
        due = review_due_at_for(db, now=NOW)
        assert due == NOW + timedelta(hours=24)
        report_id = _seed_player_report(db, due)

    with ctx.session_factory() as db:
        stored = db.get(Report, report_id)
        assert stored is not None
        assert stored.review_due_at is not None
        # PG keeps the timestamp tz-aware and exact across the round-trip.
        assert stored.review_due_at.tzinfo is not None
        assert stored.review_due_at == due

    # Before the deadline: the coach's review window is respected.
    early = sweep_due_reports(ctx, lambda _db, _report: [], now=due - timedelta(minutes=1))
    assert early.due == 0

    # At timeout: default-publish with notice, audited.
    summary = sweep_due_reports(ctx, lambda _db, _report: [], now=due)
    assert summary.published == 1
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        assert report.status is ReportStatus.PUBLISHED
        (audit,) = db.scalars(select(AuditLog).where(AuditLog.action == "report_published"))
        assert audit.actor == AUDIT_ACTOR
        assert audit.detail is not None
        assert audit.detail["timeout"] is True
        assert datetime.datetime.fromisoformat(audit.detail["review_due_at"]) == due
