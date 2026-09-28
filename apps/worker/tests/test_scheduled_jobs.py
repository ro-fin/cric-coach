"""US-J4/G5/L4/J5 production scheduler entrypoints (findings [3/33/49/78]).

The three cron-callable compositions live in ``cricai_worker.scheduled_jobs``
— production code, not a test-module composition: ``run_nightly`` (baselines),
``run_weekly`` (drift monitor + all-player rollups) and ``run_review_sweep``
(the timeout sweep THROUGH the real API publish gate).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import ReportKind, ReportStatus
from cricai_data.models import AuditLog, Player, Report
from cricai_data.storage import FsObjectStore
from cricai_worker import scheduled_jobs
from cricai_worker.context import WorkerContext
from cricai_worker.rollup_reports import RollupSweepSummary
from cricai_worker.scheduled_jobs import (
    WeeklyRunSummary,
    run_nightly,
    run_review_sweep,
    run_weekly,
)
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

AS_OF = date(2026, 6, 17)  # a Wednesday


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


def _add_player(ctx: WorkerContext, *, is_guest: bool = False) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20), is_guest=is_guest)
        db.add(player)
        db.commit()
        return player.id


# ------------------------------------------------------------------ nightly


def test_run_nightly_covers_every_non_guest_player(ctx: WorkerContext) -> None:
    """US-J4: the nightly cron recomputes baselines for the whole squad."""
    players = sorted([_add_player(ctx), _add_player(ctx)])
    _add_player(ctx, is_guest=True)  # guests never accrue longitudinal data

    summaries = run_nightly(ctx, as_of=AS_OF)

    assert [summary.player_id for summary in summaries] == [str(pid) for pid in players]


def test_run_nightly_defaults_to_no_horizon(ctx: WorkerContext) -> None:
    _add_player(ctx)
    (summary,) = run_nightly(ctx)
    assert summary.values == 0  # no data seeded; the job still runs cleanly


# ------------------------------------------------------------------- weekly


def test_run_weekly_runs_drift_then_rollups_for_the_week_just_ended(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-L4 + US-G5 finding [3/78]: one weekly entrypoint composes the drift
    monitor (over the ISO week that just ended) and the all-player rollups."""
    calls: list[tuple[str, Any]] = []
    drift_sentinel = object()

    def fake_drift(_ctx: WorkerContext, week_start: date) -> Any:
        calls.append(("drift", week_start))
        return drift_sentinel

    def fake_rollups(_ctx: WorkerContext, *, as_of: date | None = None) -> RollupSweepSummary:
        calls.append(("rollups", as_of))
        return RollupSweepSummary(summaries=[], skipped=[])

    monkeypatch.setattr(scheduled_jobs, "run_weekly_drift_monitor", fake_drift)
    monkeypatch.setattr(scheduled_jobs, "rollup_all_players", fake_rollups)

    summary = run_weekly(ctx, as_of=AS_OF)

    # The week just ended relative to Wednesday 6/17 is Mon 6/8 .. Sun 6/14.
    assert calls == [("drift", date(2026, 6, 8)), ("rollups", AS_OF)]
    assert isinstance(summary, WeeklyRunSummary)
    assert summary.drift is drift_sentinel
    assert summary.rollups.summaries == []
    assert summary.rollups.skipped == []


def test_run_weekly_defaults_to_today_utc(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        scheduled_jobs,
        "run_weekly_drift_monitor",
        lambda _ctx, week_start: seen.setdefault("week_start", week_start),
    )
    monkeypatch.setattr(
        scheduled_jobs,
        "rollup_all_players",
        lambda _ctx, *, as_of=None: (
            seen.setdefault("as_of", as_of) and RollupSweepSummary(summaries=[], skipped=[])
        ),
    )
    run_weekly(ctx)
    today = datetime.now(tz=UTC).date()
    assert seen["as_of"] == today
    last_week_anchor = today - timedelta(days=7)
    assert seen["week_start"] == last_week_anchor - timedelta(days=last_week_anchor.weekday())


# ------------------------------------------------------------- review sweep


def _seed_due_report(
    ctx: WorkerContext, *, body: dict[str, Any] | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    player_id = _add_player(ctx)
    with ctx.session_factory() as db:
        report = Report(
            player_id=player_id,
            kind=ReportKind.DAILY,
            period_start=date(2026, 7, 10),
            period_end=date(2026, 7, 10),
            status=ReportStatus.DRAFT,
            body=body if body is not None else {"kind": "daily"},
            review_due_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        db.add(report)
        db.commit()
        return player_id, report.id


def test_run_review_sweep_publishes_through_the_real_gate(ctx: WorkerContext) -> None:
    """US-J5 finding [3/33/49]: the production sweep entrypoint composes the
    API's _publish_reasons + default_recompute — no hand-rolled gate."""
    _player_id, report_id = _seed_due_report(ctx)

    summary = run_review_sweep(ctx)

    assert summary.published == 1
    assert summary.blocked == 0
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        assert report.status is ReportStatus.PUBLISHED
        (audit,) = db.scalars(select(AuditLog).where(AuditLog.action == "report_published"))
        assert audit.actor == "worker:review_sweep"
        assert audit.detail is not None
        assert audit.detail["timeout"] is True


def test_run_review_sweep_blocks_a_tampered_report(ctx: WorkerContext) -> None:
    """A fabricated claim fails the REAL recompute at timeout: BLOCKED, audited."""
    _player_id, report_id = _seed_due_report(
        ctx,
        body={
            "kind": "daily",
            "claims": [
                {"value": 99.0, "metric": "made_up", "recompute_key": f"finding:{uuid.uuid4()}:n"}
            ],
        },
    )

    summary = run_review_sweep(ctx)

    assert summary.published == 0
    assert summary.blocked == 1
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        assert report.status is ReportStatus.BLOCKED


def test_run_review_sweep_passes_now_through(ctx: WorkerContext) -> None:
    _player_id, report_id = _seed_due_report(ctx)
    early = datetime(2019, 1, 1, tzinfo=UTC)  # before the deadline: nothing due
    assert run_review_sweep(ctx, now=early).due == 0
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        assert report.status is ReportStatus.DRAFT
