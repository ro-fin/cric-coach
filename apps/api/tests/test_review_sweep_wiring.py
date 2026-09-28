"""US-J5 ST: the timeout sweep runs THROUGH the real publish validation path.

The production composition lives in ``cricai_worker.scheduled_jobs.run_review_sweep``
(findings [3/33/49]): the API's ``_publish_reasons`` + the default claim
recomputer compose straight into the worker sweep. This suite proves that
production entrypoint over one shared database — a generated draft past its
deadline publishes when the gate holds, a tampered one lands in BLOCKED
exactly as a manual publish would, and a coach_gate WEEKLY/MONTHLY rollup
draft flows queue -> sweep -> player visibility end-to-end (findings
[2/11/28/36/52/75]).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from cricai_data.db import make_session_factory
from cricai_data.enums import BowlerSource, ReportStatus, SessionType
from cricai_data.models import AppSetting, AuditLog, MetricBaseline, Player, Report
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.generate_report import generate_report
from cricai_worker.review_sweep import AUDIT_ACTOR
from cricai_worker.rollup_reports import rollup_reports

# The production scheduler entrypoint IS the wiring under test: it composes
# the reports router's _publish_reasons + default_recompute into the sweep.
from cricai_worker.scheduled_jobs import run_review_sweep
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from cricai_testing.apptest import (
    COACH_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)

SESSION_DATE = date(2026, 7, 10)
AS_OF = date(2026, 6, 17)


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def ctx(engine: Engine, tmp_path: Path) -> WorkerContext:
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


def _seed_session(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = SessionRow(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.commit()
        return session.id


def _hold_in_the_past(ctx: WorkerContext, report_id: uuid.UUID) -> datetime:
    """Backdate the gate hold (the i5 integrator wires review_due_at into
    generate_report itself via cricai_coaching.review_gate.review_due_at_for)."""
    due = datetime.now(tz=UTC) - timedelta(hours=1)
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        report.review_due_at = due
        db.commit()
    return due


def _report_status(ctx: WorkerContext, report_id: uuid.UUID) -> ReportStatus:
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        return report.status


def test_expired_hold_publishes_through_the_real_gate(ctx: WorkerContext) -> None:
    """Generate -> hold -> timeout -> sweep: the real gate passes an untampered
    honest report and the audit trail records a timeout publish (US-J5)."""
    session_id = _seed_session(ctx)
    result = generate_report(ctx, session_id)
    _hold_in_the_past(ctx, result.report_id)

    summary = run_review_sweep(ctx)

    assert summary.published == 1
    assert summary.blocked == 0
    assert _report_status(ctx, result.report_id) is ReportStatus.PUBLISHED
    with ctx.session_factory() as db:
        (audit,) = db.scalars(select(AuditLog).where(AuditLog.action == "report_published"))
        assert audit.actor == AUDIT_ACTOR
        assert audit.detail is not None
        assert audit.detail["timeout"] is True


def test_expired_hold_failing_the_real_gate_is_blocked(ctx: WorkerContext) -> None:
    """A tampered claim fails the SAME recompute check a manual publish runs:
    the report lands in BLOCKED at timeout, never silently published (US-J5/H5)."""
    session_id = _seed_session(ctx)
    result = generate_report(ctx, session_id)
    _hold_in_the_past(ctx, result.report_id)
    with ctx.session_factory() as db:
        report = db.get(Report, result.report_id)
        assert report is not None
        body = dict(report.body)
        body["claims"] = [
            {"value": 99.0, "metric": "made_up", "recompute_key": f"finding:{uuid.uuid4()}:n"}
        ]
        report.body = body
        db.commit()

    summary = run_review_sweep(ctx)

    assert summary.published == 0
    assert summary.blocked == 1
    assert _report_status(ctx, result.report_id) is ReportStatus.BLOCKED
    with ctx.session_factory() as db:
        (audit,) = db.scalars(select(AuditLog).where(AuditLog.action == "report_publish_blocked"))
        assert audit.actor == AUDIT_ACTOR
        assert audit.detail is not None
        assert any("not recomputable" in reason for reason in audit.detail["reasons"])


def test_coach_gate_rollup_flows_queue_to_sweep_to_player(
    ctx: WorkerContext, client: TestClient, engine: Engine
) -> None:
    """US-J5 x US-G5 (findings [2/11/28/36/52/75]): under coach_gate a rollup
    draft appears in the coach review queue and — with no coach action — the
    production sweep default-publishes it through the REAL gate at timeout,
    making it player-visible."""
    with ctx.session_factory() as db:
        db.add(
            AppSetting(
                version=2,
                settings={
                    "report_review": {"mode": "coach_gate", "timeout_hours": 24},
                    "live_mode": {"enabled": False, "allowlist": []},
                },
                approved_by="coach",
                reason="review before the player sees reports",
            )
        )
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.commit()
        player_id = player.id

    with ctx.session_factory() as db:
        for day, value in ((3, 0.5), (10, 0.6), (17, 0.7)):
            db.add(
                MetricBaseline(
                    player_id=player_id,
                    metric="control_pct",
                    zone_key="all",
                    window="session",
                    snapshot_date=date(2026, 6, day),
                    value=value,
                    n=40,
                    payload={"session_id": None, "session_ids": []},
                )
            )
        db.commit()

    summary = rollup_reports(ctx, player_id, as_of=AS_OF)

    queue = client.get("/settings/review-queue", headers=auth(COACH_TOKEN))
    assert queue.status_code == 200
    queued_ids = {item["id"] for item in queue.json()}
    assert {summary.weekly_report_id, summary.monthly_report_id} <= queued_ids

    hidden = client.get(f"/reports/{summary.weekly_report_id}", headers=auth(PLAYER_TOKEN))
    assert hidden.status_code == 404  # held drafts never reach the child

    swept = run_review_sweep(ctx, now=datetime.now(tz=UTC) + timedelta(hours=25))
    assert swept.published == 2

    for report_id in (summary.weekly_report_id, summary.monthly_report_id):
        shown = client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN))
        assert shown.status_code == 200
        assert shown.json()["status"] == "published"
