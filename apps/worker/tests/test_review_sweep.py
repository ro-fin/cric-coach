"""US-J5 timeout sweep: default-publish through the gate, block on failure."""

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import ReportKind, ReportStatus
from cricai_data.models import AuditLog, Player, Report
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.review_sweep import AUDIT_ACTOR, _as_utc, sweep_due_reports
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
DUE = NOW - timedelta(hours=1)
NOT_DUE = NOW + timedelta(hours=1)


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _pass_gate(_db: OrmSession, _report: Report) -> list[str]:
    return []


def _fail_gate(_db: OrmSession, _report: Report) -> list[str]:
    return ["claim x not recomputable", "dead evidence link"]


def _seed_player(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.commit()
        return player.id


def _seed_report(
    ctx: WorkerContext,
    player_id: uuid.UUID,
    *,
    status: ReportStatus = ReportStatus.DRAFT,
    review_due_at: datetime | None = DUE,
    period: date = date(2026, 7, 10),
    safety_sha256: str | None = None,
    kind: ReportKind = ReportKind.DAILY,
) -> uuid.UUID:
    with ctx.session_factory() as db:
        report = Report(
            player_id=player_id,
            kind=kind,
            period_start=period,
            period_end=period,
            status=status,
            body={"kind": kind.value},
            review_due_at=review_due_at,
            safety_sha256=safety_sha256,
        )
        db.add(report)
        db.commit()
        return report.id


def _status(ctx: WorkerContext, report_id: uuid.UUID) -> ReportStatus:
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        return report.status


def _audits(ctx: WorkerContext, action: str) -> list[AuditLog]:
    with ctx.session_factory() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


class TestPublishPath:
    def test_due_draft_passing_gate_is_published_and_audited(self, ctx: WorkerContext) -> None:
        """US-J5 AC: configurable timeout with default-publish + notice."""
        player_id = _seed_player(ctx)
        report_id = _seed_report(ctx, player_id, safety_sha256="abc123")

        summary = sweep_due_reports(ctx, _pass_gate, now=NOW)

        assert summary.due == 1
        assert summary.published == 1
        assert summary.blocked == 0
        assert summary.report_ids == (report_id,)
        assert _status(ctx, report_id) is ReportStatus.PUBLISHED
        (audit,) = _audits(ctx, "report_published")
        assert audit.actor == AUDIT_ACTOR
        assert audit.entity == "report"
        assert audit.entity_id == str(report_id)
        assert audit.detail is not None
        assert audit.detail["timeout"] is True
        assert audit.detail["safety_sha256"] == "abc123"
        assert datetime.fromisoformat(audit.detail["review_due_at"]) == DUE

    def test_default_now_is_current_time(self, ctx: WorkerContext) -> None:
        player_id = _seed_player(ctx)
        report_id = _seed_report(ctx, player_id, review_due_at=datetime(2020, 1, 1, tzinfo=UTC))
        summary = sweep_due_reports(ctx, _pass_gate)
        assert summary.published == 1
        assert _status(ctx, report_id) is ReportStatus.PUBLISHED

    def test_rollup_kinds_are_swept_like_daily(self, ctx: WorkerContext) -> None:
        """US-J5/G5 finding [2/36/52]: gate-held WEEKLY/MONTHLY rollup drafts
        default-publish at timeout exactly like daily drafts."""
        player_id = _seed_player(ctx)
        weekly = _seed_report(ctx, player_id, kind=ReportKind.WEEKLY, period=date(2026, 7, 6))
        monthly = _seed_report(ctx, player_id, kind=ReportKind.MONTHLY, period=date(2026, 7, 1))

        summary = sweep_due_reports(ctx, _pass_gate, now=NOW)

        assert summary.due == 2
        assert summary.published == 2
        assert _status(ctx, weekly) is ReportStatus.PUBLISHED
        assert _status(ctx, monthly) is ReportStatus.PUBLISHED


class TestBlockedPath:
    def test_due_draft_failing_gate_is_blocked_never_published(self, ctx: WorkerContext) -> None:
        """A report failing the publish gate at timeout goes BLOCKED with its
        reasons audited — it is never silently published (US-J5/H5)."""
        player_id = _seed_player(ctx)
        report_id = _seed_report(ctx, player_id)

        summary = sweep_due_reports(ctx, _fail_gate, now=NOW)

        assert summary.due == 1
        assert summary.published == 0
        assert summary.blocked == 1
        assert _status(ctx, report_id) is ReportStatus.BLOCKED
        assert _audits(ctx, "report_published") == []
        (audit,) = _audits(ctx, "report_publish_blocked")
        assert audit.actor == AUDIT_ACTOR
        assert audit.detail is not None
        assert audit.detail["reasons"] == ["claim x not recomputable", "dead evidence link"]
        assert audit.detail["timeout"] is True


class TestSkips:
    def test_not_yet_due_draft_is_left_alone(self, ctx: WorkerContext) -> None:
        """The coach's review window is respected: no early publish."""
        player_id = _seed_player(ctx)
        report_id = _seed_report(ctx, player_id, review_due_at=NOT_DUE)
        summary = sweep_due_reports(ctx, _pass_gate, now=NOW)
        assert summary.due == 0
        assert _status(ctx, report_id) is ReportStatus.DRAFT
        assert _audits(ctx, "report_published") == []

    def test_draft_without_deadline_is_left_alone(self, ctx: WorkerContext) -> None:
        """A draft with no review_due_at was never gate-held (auto_publish
        mode): the sweep must not invent a publish decision for it."""
        player_id = _seed_player(ctx)
        report_id = _seed_report(ctx, player_id, review_due_at=None)
        summary = sweep_due_reports(ctx, _pass_gate, now=NOW)
        assert summary.due == 0
        assert _status(ctx, report_id) is ReportStatus.DRAFT

    @pytest.mark.parametrize("status", [ReportStatus.PUBLISHED, ReportStatus.BLOCKED])
    def test_coach_decided_reports_are_never_touched(
        self, ctx: WorkerContext, status: ReportStatus
    ) -> None:
        """US-J5: a coach action (publish or block) before the deadline stands;
        the sweep only decides still-DRAFT reports."""
        player_id = _seed_player(ctx)
        report_id = _seed_report(ctx, player_id, status=status)
        gate = _fail_gate if status is ReportStatus.PUBLISHED else _pass_gate
        summary = sweep_due_reports(ctx, gate, now=NOW)
        assert summary.due == 0
        assert _status(ctx, report_id) is status

    def test_sweep_is_idempotent(self, ctx: WorkerContext) -> None:
        player_id = _seed_player(ctx)
        _seed_report(ctx, player_id)
        assert sweep_due_reports(ctx, _pass_gate, now=NOW).published == 1
        again = sweep_due_reports(ctx, _pass_gate, now=NOW)
        assert again.due == 0
        assert len(_audits(ctx, "report_published")) == 1


class TestBatchAndValidation:
    def test_mixed_batch_decides_each_report_on_its_own_gate_result(
        self, ctx: WorkerContext
    ) -> None:
        player_id = _seed_player(ctx)
        good = _seed_report(ctx, player_id, period=date(2026, 7, 9))
        bad = _seed_report(ctx, player_id, period=date(2026, 7, 10))
        skipped = _seed_report(ctx, player_id, review_due_at=NOT_DUE, period=date(2026, 7, 11))

        def gate(db: OrmSession, report: Report) -> list[str]:
            return [] if report.id == good else ["safety text does not match safety_sha256"]

        summary = sweep_due_reports(ctx, gate, now=NOW)
        assert summary.due == 2
        assert summary.published == 1
        assert summary.blocked == 1
        assert set(summary.report_ids) == {good, bad}
        assert _status(ctx, good) is ReportStatus.PUBLISHED
        assert _status(ctx, bad) is ReportStatus.BLOCKED
        assert _status(ctx, skipped) is ReportStatus.DRAFT

    def test_naive_now_rejected(self, ctx: WorkerContext) -> None:
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            sweep_due_reports(ctx, _pass_gate, now=NOW.replace(tzinfo=None))

    def test_empty_table_sweeps_nothing(self, ctx: WorkerContext) -> None:
        summary = sweep_due_reports(ctx, _pass_gate, now=NOW)
        assert summary.due == 0
        assert summary.report_ids == ()


def test_as_utc_coerces_naive_and_passes_aware_through() -> None:
    """Stored deadlines are UTC; SQLite round-trips them naive (PG keeps tz)."""
    assert _as_utc(NOW.replace(tzinfo=None)) == NOW
    assert _as_utc(NOW) == NOW
