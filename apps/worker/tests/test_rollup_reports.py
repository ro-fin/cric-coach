"""US-G5/US-K4 rollup job: weekly/monthly reports, milestones, regression alerts.

Every acceptance criterion is pinned here: trend series carry per-point n and
the qualification guardrails (US-G5), regression alerts are context-normalized
to the same zone (worse technique vs harder ball mix), the milestone log is
guardrailed and idempotent, and the emitted bodies satisfy the pinned
contract-#3 shape with number-free wording (the existing publish gate's
claims-coverage rule, proven end-to-end in the API suite).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import cricai_worker.rollup_reports as rollup_module
import pytest
from cricai_coaching.content_lint import (
    BANNED_COACHING_PHRASES,
    BANNED_MEDICAL_PHRASES,
    BANNED_SPIN_CLAIMS,
    find_banned_phrases,
)
from cricai_coaching.report import validate_claims_coverage
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    AlertAudience,
    BowlerSource,
    DeliveryIntensity,
    MilestoneKind,
    ReportKind,
    ReportStatus,
    SessionType,
)
from cricai_data.models import (
    Alert,
    AppSetting,
    AuditLog,
    BowlingLedgerEntry,
    MetricBaseline,
    Milestone,
    Player,
    Report,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.nightly_baselines import WINDOW_SESSION, ZONE_ALL
from cricai_worker.rollup_reports import (
    HONESTY_NO_TRENDS,
    POSITIVE_BY_KIND,
    PROGRESS_REGRESSION_CODE,
    STREAK_METRIC,
    VOLUME_METRIC,
    RollupSummary,
    assemble_rollup_body,
    month_period,
    rollup_all_players,
    rollup_reports,
    week_period,
)
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

#: A Wednesday; its ISO week is 2026-06-15 (Mon) .. 2026-06-21 (Sun).
AS_OF = date(2026, 6, 17)


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


def _add_baseline(
    ctx: WorkerContext,
    player_id: uuid.UUID,
    metric: str,
    snapshot_date: date,
    value: float,
    *,
    zone_key: str = ZONE_ALL,
    n: int = 40,
) -> None:
    with ctx.session_factory() as db:
        db.add(
            MetricBaseline(
                player_id=player_id,
                metric=metric,
                zone_key=zone_key,
                window=WINDOW_SESSION,
                snapshot_date=snapshot_date,
                value=value,
                n=n,
                payload={"session_id": None, "session_ids": []},
            )
        )
        db.commit()


def _seed_trend(  # helper: three qualified points ending inside the AS_OF week
    ctx: WorkerContext,
    player_id: uuid.UUID,
    values: tuple[float, float, float],
    *,
    metric: str = "control_pct",
    zone_key: str = ZONE_ALL,
    n: int = 40,
) -> list[date]:
    dates = [date(2026, 6, 3), date(2026, 6, 10), date(2026, 6, 17)]
    for day, value in zip(dates, values, strict=True):
        _add_baseline(ctx, player_id, metric, day, value, zone_key=zone_key, n=n)
    return dates


def _add_session_on(ctx: WorkerContext, player_id: uuid.UUID, day: date) -> None:
    with ctx.session_factory() as db:
        db.add(
            SessionRow(
                player_id=player_id,
                session_date=day,
                session_type=SessionType.BATTING,
                bowler_source=BowlerSource.MACHINE,
            )
        )
        db.commit()


def _add_ledger(ctx: WorkerContext, player_id: uuid.UUID, day: date, balls: int) -> None:
    with ctx.session_factory() as db:
        db.add(
            BowlingLedgerEntry(
                player_id=player_id,
                entry_date=day,
                balls=balls,
                intensity=DeliveryIntensity.SPIN,
                created_by="parent",
            )
        )
        db.commit()


def _milestones(ctx: WorkerContext, player_id: uuid.UUID) -> list[Milestone]:
    with ctx.session_factory() as db:
        return list(
            db.scalars(
                select(Milestone)
                .where(Milestone.player_id == player_id)
                .order_by(Milestone.achieved_on, Milestone.kind, Milestone.metric)
            )
        )


def _reports(ctx: WorkerContext, player_id: uuid.UUID) -> dict[str, Report]:
    with ctx.session_factory() as db:
        rows = db.scalars(select(Report).where(Report.player_id == player_id)).all()
        return {row.kind.value: row for row in rows}


def _alerts(ctx: WorkerContext) -> list[Alert]:
    with ctx.session_factory() as db:
        return list(db.scalars(select(Alert).where(Alert.code == PROGRESS_REGRESSION_CODE)))


# ------------------------------------------------------------------ periods


def test_week_period_is_monday_to_sunday() -> None:
    assert week_period(AS_OF) == (date(2026, 6, 15), date(2026, 6, 21))
    assert week_period(date(2026, 6, 15)) == (date(2026, 6, 15), date(2026, 6, 21))
    assert week_period(date(2026, 6, 21)) == (date(2026, 6, 15), date(2026, 6, 21))


def test_month_period_spans_the_calendar_month() -> None:
    assert month_period(AS_OF) == (date(2026, 6, 1), date(2026, 6, 30))
    assert month_period(date(2028, 2, 10)) == (date(2028, 2, 1), date(2028, 2, 29))  # leap


# ------------------------------------------------------------- report bodies


def test_unknown_player_raises(ctx: WorkerContext) -> None:
    with pytest.raises(ValueError, match="player not found"):
        rollup_reports(ctx, uuid.uuid4(), as_of=AS_OF)


def test_weekly_and_monthly_bodies_follow_contract_3(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    dates = _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.trend_count == 1

    reports = _reports(ctx, player_id)
    assert set(reports) == {"weekly", "monthly"}
    for kind, report in reports.items():
        body = report.body
        assert report.status is ReportStatus.DRAFT
        assert report.session_id is None
        assert body["kind"] == kind
        assert body["main_correction"] is None
        assert body["drill"] is None
        assert body["goal"] is None
        assert body["secondary"] == []
        assert body["positive"] == POSITIVE_BY_KIND[kind]
        assert body["safety"] is None
        assert body["honesty_banner"] is None  # a qualified trend exists
        # Contract #3: fatigue_note/coverage_note present-null; every structured
        # number ships a recompute claim (finding [24]) — three trend points plus
        # the personal-best milestone, all recomputable at publish.
        assert body["fatigue_note"] is None
        assert body["coverage_note"] is None
        assert body["claims"] == [
            {
                "value": value,
                "metric": "control_pct",
                "recompute_key": (
                    f"baseline:control_pct:{ZONE_ALL}:{WINDOW_SESSION}:{day.isoformat()}"
                ),
            }
            for day, value in zip(dates, (0.5, 0.6, 0.7), strict=True)
        ] + [
            {
                "value": 0.7,
                "metric": "control_pct",
                "recompute_key": f"milestone:personal_best:control_pct:{dates[-1].isoformat()}",
            }
        ]
        (trend,) = body["trends"]
        assert trend == {
            "metric": "control_pct",
            "zone_key": ZONE_ALL,
            "points": [
                {"date": day.isoformat(), "value": value, "n": 40}
                for day, value in zip(dates, (0.5, 0.6, 0.7), strict=True)
            ],
            "direction": "improving",
            "qualified": True,
        }
        assert validate_claims_coverage(body) == []
    assert reports["weekly"].period_start == date(2026, 6, 15)
    assert reports["weekly"].period_end == date(2026, 6, 21)
    assert reports["monthly"].period_start == date(2026, 6, 1)
    assert reports["monthly"].period_end == date(2026, 6, 30)


def test_honesty_banner_when_no_trend_qualifies(ctx: WorkerContext) -> None:
    """US-G5: a trend claim requires >=3 sessions and >=30 balls per point."""
    player_id = _add_player(ctx)
    _add_baseline(ctx, player_id, "control_pct", date(2026, 6, 10), 0.5, n=10)
    _add_baseline(ctx, player_id, "control_pct", date(2026, 6, 17), 0.7, n=10)
    rollup_reports(ctx, player_id, as_of=AS_OF)
    body = _reports(ctx, player_id)["weekly"].body
    assert body["honesty_banner"] == HONESTY_NO_TRENDS
    (trend,) = body["trends"]
    assert trend["qualified"] is False  # shown, honestly flagged


def test_as_of_freezes_the_trend_view(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    _add_baseline(ctx, player_id, "control_pct", date(2026, 6, 24), 0.9)  # after as_of
    rollup_reports(ctx, player_id, as_of=AS_OF)
    (trend,) = _reports(ctx, player_id)["weekly"].body["trends"]
    assert [point["date"] for point in trend["points"]] == [
        "2026-06-03",
        "2026-06-10",
        "2026-06-17",
    ]


def test_default_as_of_is_today_utc(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    rollup_reports(ctx, player_id)
    today = datetime.now(tz=UTC).date()
    report = _reports(ctx, player_id)["weekly"]
    assert (report.period_start, report.period_end) == week_period(today)


def test_rerun_is_idempotent_and_relands_in_draft(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    first = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert (first.weekly_created, first.monthly_created) == (True, True)

    with ctx.session_factory() as db:  # a coach published the weekly report
        report = db.get(Report, uuid.UUID(first.weekly_report_id))
        assert report is not None
        report.status = ReportStatus.PUBLISHED
        db.commit()

    second = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert (second.weekly_created, second.monthly_created) == (False, False)
    assert second.weekly_report_id == first.weekly_report_id
    assert second.monthly_report_id == first.monthly_report_id
    reports = _reports(ctx, player_id)
    assert reports["weekly"].status is ReportStatus.DRAFT  # demotion is visible
    with ctx.session_factory() as db:
        audit = db.scalars(select(AuditLog).where(AuditLog.action == "report_regenerated")).one()
        assert audit.entity_id == first.weekly_report_id


def test_assemble_body_rejects_wording_with_uncovered_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-check that keeps rollup bodies publishable by the existing gate."""
    monkeypatch.setitem(POSITIVE_BY_KIND, ReportKind.WEEKLY.value, "Scored 99 this week!")
    with pytest.raises(ValueError, match="no claim covers"):
        assemble_rollup_body(
            kind=ReportKind.WEEKLY.value,
            period_start=date(2026, 6, 15),
            period_end=date(2026, 6, 21),
            snapshots=[],
            milestones=[],
        )


# ------------------------------------------------------------------ milestones


def test_personal_best_milestone_written_once(ctx: WorkerContext) -> None:
    """US-J4/K4: progress_agent.personal_best finally gets its caller."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.milestones_written == 1

    (milestone,) = _milestones(ctx, player_id)
    assert milestone.kind is MilestoneKind.PERSONAL_BEST
    assert milestone.metric == "control_pct"
    assert milestone.value == pytest.approx(0.7)
    assert milestone.achieved_on == date(2026, 6, 17)
    assert milestone.context == {"zone_key": ZONE_ALL, "window": WINDOW_SESSION, "n": 40}

    again = rollup_reports(ctx, player_id, as_of=AS_OF)  # idempotent on the uq
    assert again.milestones_written == 0
    assert len(_milestones(ctx, player_id)) == 1


def test_unqualified_snapshot_never_celebrates(ctx: WorkerContext) -> None:
    """US-J4 guardrail: no personal best without the sample behind it."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7), n=10)  # < 30 balls per point
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.milestones_written == 0
    assert _milestones(ctx, player_id) == []


def test_zone_slices_do_not_write_their_own_personal_bests(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7), zone_key="off/good")
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.milestones_written == 0


def test_flat_latest_point_is_not_a_personal_best(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.7, 0.7))  # ties history, no strict beat
    assert rollup_reports(ctx, player_id, as_of=AS_OF).milestones_written == 0


def test_volume_landmarks_from_the_bowling_ledger(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _add_ledger(ctx, player_id, date(2026, 6, 1), 60)
    _add_ledger(ctx, player_id, date(2026, 6, 8), 50)  # cumulative 110 crosses 100
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.milestones_written == 1

    (milestone,) = _milestones(ctx, player_id)
    assert milestone.kind is MilestoneKind.VOLUME
    assert milestone.metric == VOLUME_METRIC
    assert milestone.value == pytest.approx(100.0)
    assert milestone.achieved_on == date(2026, 6, 8)
    assert milestone.context == {"landmarks": [100], "cumulative_balls": 110}

    assert rollup_reports(ctx, player_id, as_of=AS_OF).milestones_written == 0


def test_one_day_crossing_several_landmarks_records_the_highest(ctx: WorkerContext) -> None:
    """The milestone unique key is per day: one row, highest landmark wins."""
    player_id = _add_player(ctx)
    _add_ledger(ctx, player_id, date(2026, 6, 1), 300)  # crosses 100 and 250 at once
    rollup_reports(ctx, player_id, as_of=AS_OF)
    (row,) = [m for m in _milestones(ctx, player_id) if m.kind is MilestoneKind.VOLUME]
    assert row.value == pytest.approx(250.0)
    assert row.achieved_on == date(2026, 6, 1)
    assert row.context == {"landmarks": [100, 250], "cumulative_balls": 300}


def test_ledger_entries_after_as_of_do_not_count(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _add_ledger(ctx, player_id, date(2026, 6, 25), 500)  # after the run's horizon
    assert rollup_reports(ctx, player_id, as_of=AS_OF).milestones_written == 0


def test_streak_landmarks_and_gap_reset(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    for day in (1, 2, 3, 4, 5, 8, 9):  # five consecutive days, gap, two more
        _add_session_on(ctx, player_id, date(2026, 6, day))
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.milestones_written == 2  # streak 3 (Jun 3) and streak 5 (Jun 5)

    rows = [m for m in _milestones(ctx, player_id) if m.kind is MilestoneKind.STREAK]
    assert [(m.value, m.achieved_on) for m in rows] == [
        (3.0, date(2026, 6, 3)),
        (5.0, date(2026, 6, 5)),
    ]
    assert all(m.metric == STREAK_METRIC for m in rows)
    assert all(m.context == {"streak_start": "2026-06-01"} for m in rows)

    assert rollup_reports(ctx, player_id, as_of=AS_OF).milestones_written == 0


def test_a_fortnight_run_crosses_every_streak_landmark(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    for day in range(1, 15):  # 14 consecutive practice days
        _add_session_on(ctx, player_id, date(2026, 6, day))
    rollup_reports(ctx, player_id, as_of=AS_OF)
    rows = [m for m in _milestones(ctx, player_id) if m.kind is MilestoneKind.STREAK]
    assert [(m.value, m.achieved_on) for m in rows] == [
        (3.0, date(2026, 6, 3)),
        (5.0, date(2026, 6, 5)),
        (7.0, date(2026, 6, 7)),
        (14.0, date(2026, 6, 14)),
    ]


def test_two_sessions_one_day_count_as_one_practice_day(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    for day in (1, 1, 2, 3):  # doubled first day must not inflate the streak
        _add_session_on(ctx, player_id, date(2026, 6, day))
    rollup_reports(ctx, player_id, as_of=AS_OF)
    rows = [m for m in _milestones(ctx, player_id) if m.kind is MilestoneKind.STREAK]
    assert [(m.value, m.achieved_on) for m in rows] == [(3.0, date(2026, 6, 3))]


def test_report_milestones_key_lists_only_the_period(ctx: WorkerContext) -> None:
    """Weekly body carries the week's milestones; monthly the month's."""
    player_id = _add_player(ctx)
    _add_ledger(ctx, player_id, date(2026, 6, 1), 120)  # volume 100, outside the week
    for day in (15, 16, 17):  # streak 3 inside the week
        _add_session_on(ctx, player_id, date(2026, 6, day))
    rollup_reports(ctx, player_id, as_of=AS_OF)

    reports = _reports(ctx, player_id)
    assert reports["weekly"].body["milestones"] == [
        {
            "kind": "streak",
            "metric": STREAK_METRIC,
            "value": 3.0,
            "achieved_on": "2026-06-17",
        }
    ]
    assert reports["monthly"].body["milestones"] == [
        {
            "kind": "volume",
            "metric": VOLUME_METRIC,
            "value": 100.0,
            "achieved_on": "2026-06-01",
        },
        {
            "kind": "streak",
            "metric": STREAK_METRIC,
            "value": 3.0,
            "achieved_on": "2026-06-17",
        },
    ]


# ------------------------------------------------------------------- alerts


def test_same_zone_regression_raises_a_parent_alert(ctx: WorkerContext) -> None:
    """US-G5 AC: worse technique = the SAME zone regressing, qualified."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.8, 0.6, 0.4), zone_key="off/good")
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.alerts_written == 1

    (alert,) = _alerts(ctx)
    assert alert.audience is AlertAudience.PARENT
    assert alert.severity == "warning"
    assert alert.detail["metric"] == "control_pct"
    assert alert.detail["zone_key"] == "off/good"
    assert alert.detail["player_id"] == str(player_id)
    assert alert.detail["latest_date"] == "2026-06-17"

    assert rollup_reports(ctx, player_id, as_of=AS_OF).alerts_written == 0  # dedupe
    assert len(_alerts(ctx)) == 1


def test_mix_shift_never_alerts(ctx: WorkerContext) -> None:
    """The ``all`` slice regressing while the zone holds = harder ball mix."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.8, 0.6, 0.4), zone_key=ZONE_ALL)
    _seed_trend(ctx, player_id, (0.7, 0.7, 0.7), zone_key="off/good")
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.alerts_written == 0
    assert _alerts(ctx) == []


def test_unqualified_zone_regression_never_alerts(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.8, 0.6, 0.4), zone_key="off/good", n=10)
    assert rollup_reports(ctx, player_id, as_of=AS_OF).alerts_written == 0


def test_new_trend_point_raises_a_fresh_alert(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.8, 0.6, 0.4), zone_key="off/good")
    rollup_reports(ctx, player_id, as_of=AS_OF)
    _add_baseline(ctx, player_id, "control_pct", date(2026, 6, 24), 0.3, zone_key="off/good")
    summary = rollup_reports(ctx, player_id, as_of=date(2026, 6, 24))
    assert summary.alerts_written == 1
    assert len(_alerts(ctx)) == 2


# -------------------------------------------------------------------- sweep


def test_rollup_all_players_skips_guests(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _add_player(ctx, is_guest=True)
    sweep = rollup_all_players(ctx, as_of=AS_OF)
    assert [summary.player_id for summary in sweep.summaries] == [str(player_id)]
    assert all(isinstance(summary, RollupSummary) for summary in sweep.summaries)
    assert sweep.skipped == []  # nothing collided: no skipped players recorded


# ---------------------------------------------------------------------- SAF


@pytest.mark.safety
def test_rollup_copy_is_kid_safe_and_claim_free() -> None:
    """SAF: rollup wording passes the US-G2/H4 lint and the banned-claim lint
    (no rpm/revs/spin-rate language anywhere bowling-facing, US-I5)."""
    banned = BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES + BANNED_SPIN_CLAIMS
    for text in [*POSITIVE_BY_KIND.values(), HONESTY_NO_TRENDS]:
        assert find_banned_phrases(text, banned) == []


def _summary_fields(summary: RollupSummary) -> dict[str, Any]:
    return {
        "milestones": summary.milestones_written,
        "alerts": summary.alerts_written,
        "trends": summary.trend_count,
    }


def test_summary_counts_are_coherent(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))  # personal best
    _seed_trend(ctx, player_id, (0.8, 0.6, 0.4), zone_key="off/good")  # regression
    _add_ledger(ctx, player_id, date(2026, 6, 1), 120)  # volume 100
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert _summary_fields(summary) == {"milestones": 2, "alerts": 1, "trends": 2}


# ------------------------------------------------- review gate (US-J5, [2/11/28/36/52/75])


def _enable_coach_gate(ctx: WorkerContext, *, timeout_hours: int = 24) -> None:
    with ctx.session_factory() as db:
        db.add(
            AppSetting(
                version=2,
                settings={
                    "report_review": {"mode": "coach_gate", "timeout_hours": timeout_hours},
                    "live_mode": {"enabled": False, "allowlist": []},
                },
                approved_by="coach",
                reason="test",
            )
        )
        db.commit()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def test_coach_gate_rollup_drafts_carry_the_review_deadline(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-J5 finding [2/11/28/36/52/75]: rollup drafts enter the review gate
    exactly like generate_report drafts — held with review_due_at, so the
    coach queue lists them and the timeout sweep decides them."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    _enable_coach_gate(ctx)
    now = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: now)

    rollup_reports(ctx, player_id, as_of=AS_OF)

    reports = _reports(ctx, player_id)
    for kind in ("weekly", "monthly"):
        due = reports[kind].review_due_at
        assert due is not None, f"{kind} rollup draft carries no review deadline"
        assert _as_utc(due) == now + timedelta(hours=24)


def test_auto_publish_mode_leaves_rollup_drafts_deadline_free(ctx: WorkerContext) -> None:
    """A None deadline means 'never gate-held' to the queue and the sweep."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    rollup_reports(ctx, player_id, as_of=AS_OF)  # unseeded settings: auto_publish
    reports = _reports(ctx, player_id)
    assert reports["weekly"].review_due_at is None
    assert reports["monthly"].review_due_at is None


def test_regenerating_a_held_rollup_draft_preserves_its_deadline(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-J5 finding [14/30/38]: re-runs must not creep the coach's timeout."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    _enable_coach_gate(ctx)
    first_now = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: first_now)
    rollup_reports(ctx, player_id, as_of=AS_OF)

    # 23h later — one hour before the deadline — the rollup re-runs.
    monkeypatch.setattr(
        "cricai_coaching.review_gate.utcnow", lambda: first_now + timedelta(hours=23)
    )
    rollup_reports(ctx, player_id, as_of=AS_OF)

    reports = _reports(ctx, player_id)
    for kind in ("weekly", "monthly"):
        due = reports[kind].review_due_at
        assert due is not None
        assert _as_utc(due) == first_now + timedelta(hours=24)  # unchanged, no creep


def test_demoting_a_published_rollup_takes_a_fresh_deadline(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New body after a publish = new review window (the demotion is audited)."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    _enable_coach_gate(ctx)
    first_now = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: first_now)
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)

    with ctx.session_factory() as db:  # a coach published the weekly report
        report = db.get(Report, uuid.UUID(summary.weekly_report_id))
        assert report is not None
        report.status = ReportStatus.PUBLISHED
        db.commit()

    second_now = first_now + timedelta(hours=5)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: second_now)
    rollup_reports(ctx, player_id, as_of=AS_OF)

    weekly = _reports(ctx, player_id)["weekly"]
    assert weekly.status is ReportStatus.DRAFT
    assert weekly.review_due_at is not None
    assert _as_utc(weekly.review_due_at) == second_now + timedelta(hours=24)


# ------------------------------------- milestone convergence (findings [9/37/41])


def _volume_rows(ctx: WorkerContext, player_id: uuid.UUID) -> list[tuple[float, date, Any]]:
    return [
        (m.value, m.achieved_on, m.context)
        for m in _milestones(ctx, player_id)
        if m.kind is MilestoneKind.VOLUME
    ]


def test_backdated_ledger_entry_converges_the_volume_log(ctx: WorkerContext) -> None:
    """Finding [9/41]: a backfilled entry moves the landmark, never duplicates it."""
    player_id = _add_player(ctx)
    _add_ledger(ctx, player_id, date(2026, 6, 8), 50)
    _add_ledger(ctx, player_id, date(2026, 6, 15), 50)  # cumulative 100 on 6/15
    rollup_reports(ctx, player_id, as_of=AS_OF)
    assert _volume_rows(ctx, player_id) == [
        (100.0, date(2026, 6, 15), {"landmarks": [100], "cumulative_balls": 100})
    ]

    _add_ledger(ctx, player_id, date(2026, 6, 1), 60)  # backfilled earlier session
    rollup_reports(ctx, player_id, as_of=AS_OF)

    assert _volume_rows(ctx, player_id) == [  # ONE row, moved to the true crossing
        (100.0, date(2026, 6, 8), {"landmarks": [100], "cumulative_balls": 110})
    ]


def test_same_day_growth_upgrades_the_days_landmark(ctx: WorkerContext) -> None:
    """Finding [9]: more same-day balls crossing a higher landmark update the row."""
    player_id = _add_player(ctx)
    _add_ledger(ctx, player_id, date(2026, 6, 15), 120)
    rollup_reports(ctx, player_id, as_of=AS_OF)
    assert _volume_rows(ctx, player_id) == [
        (100.0, date(2026, 6, 15), {"landmarks": [100], "cumulative_balls": 120})
    ]

    _add_ledger(ctx, player_id, date(2026, 6, 15), 200)  # same-day extra block
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)

    assert summary.milestones_written == 1  # the corrected row counts as written
    assert _volume_rows(ctx, player_id) == [
        (250.0, date(2026, 6, 15), {"landmarks": [100, 250], "cumulative_balls": 320})
    ]


def test_privacy_deleted_ledger_rows_converge_the_volume_log(ctx: WorkerContext) -> None:
    """Finding [37]: deleting a session's ledger rows (privacy delete) must not
    leave a landmark row whose evidence no longer exists."""
    player_id = _add_player(ctx)
    _add_ledger(ctx, player_id, date(2026, 6, 1), 60)
    _add_ledger(ctx, player_id, date(2026, 6, 8), 60)  # 100 crossed on 6/8
    rollup_reports(ctx, player_id, as_of=AS_OF)
    assert [(v, d) for v, d, _ in _volume_rows(ctx, player_id)] == [(100.0, date(2026, 6, 8))]

    with ctx.session_factory() as db:  # the privacy.py delete: the 6/1 session's rows go
        for entry in db.scalars(
            select(BowlingLedgerEntry).where(BowlingLedgerEntry.entry_date == date(2026, 6, 1))
        ):
            db.delete(entry)
        db.commit()
    _add_ledger(ctx, player_id, date(2026, 6, 15), 50)  # 60 + 50 = 110 on 6/15
    rollup_reports(ctx, player_id, as_of=AS_OF)

    assert [(v, d) for v, d, _ in _volume_rows(ctx, player_id)] == [(100.0, date(2026, 6, 15))]


def test_backdated_session_converges_the_streak_log(ctx: WorkerContext) -> None:
    """Finding [41]: a late-logged practice day merges runs; the log converges."""
    player_id = _add_player(ctx)
    for day in (15, 16, 18, 19, 20):
        _add_session_on(ctx, player_id, date(2026, 6, day))
    rollup_reports(ctx, player_id, as_of=date(2026, 6, 20))
    streaks = [
        (m.value, m.achieved_on)
        for m in _milestones(ctx, player_id)
        if m.kind is MilestoneKind.STREAK
    ]
    assert streaks == [(3.0, date(2026, 6, 20))]

    _add_session_on(ctx, player_id, date(2026, 6, 17))  # guardian logs Wednesday late
    rollup_reports(ctx, player_id, as_of=date(2026, 6, 21))

    streaks = [
        (m.value, m.achieved_on)
        for m in _milestones(ctx, player_id)
        if m.kind is MilestoneKind.STREAK
    ]
    assert streaks == [(3.0, date(2026, 6, 17)), (5.0, date(2026, 6, 19))]  # no duplicates


def test_personal_bests_are_events_and_never_reconciled_away(ctx: WorkerContext) -> None:
    """PERSONAL_BEST rows are append-only history, not machine re-derivations."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    rollup_reports(ctx, player_id, as_of=AS_OF)
    assert len(_milestones(ctx, player_id)) == 1

    with ctx.session_factory() as db:  # later history changes must not delete it
        for row in db.scalars(select(MetricBaseline)):
            db.delete(row)
        db.commit()
    rollup_reports(ctx, player_id, as_of=AS_OF)
    (milestone,) = _milestones(ctx, player_id)
    assert milestone.kind is MilestoneKind.PERSONAL_BEST


# ------------------------------------------ metric directionality ([7/74/80])


def test_lower_is_better_zone_worsening_raises_the_alert(ctx: WorkerContext) -> None:
    """Finding [7/74]: falling away MORE (up-trend) is the regression."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (4.0, 8.0, 12.0), metric="falling_away_deg", zone_key="off/good")
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.alerts_written == 1
    (alert,) = _alerts(ctx)
    assert alert.detail["metric"] == "falling_away_deg"


def test_lower_is_better_zone_improvement_never_alerts(ctx: WorkerContext) -> None:
    """Finding [7/74]: a kid whose lean shrinks 12->4 deg must never alarm a parent."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (12.0, 8.0, 4.0), metric="falling_away_deg", zone_key="off/good")
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.alerts_written == 0
    assert _alerts(ctx) == []


def test_lower_is_better_all_time_low_is_the_personal_best(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (12.0, 8.0, 4.0), metric="falling_away_deg")
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.milestones_written == 1
    (milestone,) = _milestones(ctx, player_id)
    assert milestone.kind is MilestoneKind.PERSONAL_BEST
    assert milestone.value == pytest.approx(4.0)


def test_lower_is_better_worsening_is_never_a_personal_best(ctx: WorkerContext) -> None:
    """Finding [74]: the WORST lean must never celebrate in the kid feed."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (4.0, 8.0, 12.0), metric="falling_away_deg")
    assert rollup_reports(ctx, player_id, as_of=AS_OF).milestones_written == 0
    assert _milestones(ctx, player_id) == []


def test_untrended_primitives_ship_no_trend_or_milestone(ctx: WorkerContext) -> None:
    """Finding [7]: release_ms means are internal primitives, never a trend."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (100.0, 200.0, 300.0), metric="release_ms")
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    assert summary.trend_count == 0
    assert summary.milestones_written == 0
    assert _reports(ctx, player_id)["weekly"].body["trends"] == []


def test_personal_best_scans_all_history_not_the_render_window(ctx: WorkerContext) -> None:
    """Finding [80]: an aged-out true best blocks a window-local 'new best'."""
    player_id = _add_player(ctx)
    start = date(2026, 5, 1)
    values = [0.82] + [0.6 + 0.01 * i for i in range(14)] + [0.78]
    for offset, value in enumerate(values):
        _add_baseline(ctx, player_id, "control_pct", start + timedelta(days=offset), value)
    assert rollup_reports(ctx, player_id, as_of=AS_OF).milestones_written == 0
    assert _milestones(ctx, player_id) == []


# --------------------------------- daily-report linkage (finding [79])


def _add_daily_report(ctx: WorkerContext, player_id: uuid.UUID, day: date) -> str:
    with ctx.session_factory() as db:
        report = Report(
            player_id=player_id,
            kind=ReportKind.DAILY,
            period_start=day,
            period_end=day,
            body={"kind": "daily"},
        )
        db.add(report)
        db.commit()
        return str(report.id)


def test_rollup_bodies_link_the_periods_daily_reports(ctx: WorkerContext) -> None:
    """US-G5 finding [79]: weekly/monthly bodies trace the daily reports they
    summarize (the plan's 'from daily reports' input, minimally)."""
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    in_week = _add_daily_report(ctx, player_id, date(2026, 6, 16))
    in_month = _add_daily_report(ctx, player_id, date(2026, 6, 3))
    _add_daily_report(ctx, player_id, date(2026, 5, 20))  # outside both periods
    rollup_reports(ctx, player_id, as_of=AS_OF)

    reports = _reports(ctx, player_id)
    assert reports["weekly"].body["source_daily_report_ids"] == [in_week]
    assert reports["monthly"].body["source_daily_report_ids"] == [in_month, in_week]


def test_rollup_bodies_with_no_daily_reports_link_nothing(ctx: WorkerContext) -> None:
    player_id = _add_player(ctx)
    _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))
    rollup_reports(ctx, player_id, as_of=AS_OF)
    assert _reports(ctx, player_id)["weekly"].body["source_daily_report_ids"] == []


# ------------------------------ concurrent-run isolation (finding [15])


def test_concurrent_duplicate_insert_skips_that_player_not_the_sweep(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding [15]: a unique-key collision with a concurrent run rolls back
    that player's rollup and the all-players sweep continues."""
    first = _add_player(ctx)
    second = _add_player(ctx)
    victim, other = sorted((first, second))  # rollup_all_players orders by id
    for player_id in (victim, other):
        _seed_trend(ctx, player_id, (0.5, 0.6, 0.7))

    real_add = rollup_module._add_milestone
    fired = False

    def racing_add(db: Any, player_id: uuid.UUID, *args: Any, **kw: Any) -> int:
        nonlocal fired
        written = real_add(db, player_id, *args, **kw)
        if written and player_id == victim and not fired:
            fired = True  # a concurrent run commits the identical row mid-window
            with ctx.session_factory() as db2:
                db2.add(
                    Milestone(
                        player_id=player_id,
                        kind=args[0],
                        metric=args[1],
                        value=args[2],
                        context=dict(kw.get("context", {}) or (args[5] if len(args) > 5 else {})),
                        achieved_on=args[3],
                    )
                )
                db2.commit()
        return written

    monkeypatch.setattr(rollup_module, "_add_milestone", racing_add)

    sweep = rollup_all_players(ctx, as_of=AS_OF)  # must not raise

    assert [summary.player_id for summary in sweep.summaries] == [str(other)]
    # The skipped player is now VISIBLE in the sweep summary, not silently
    # dropped: a persistent (not one-off) failure would recur here every run.
    assert sweep.skipped == [(str(victim), "IntegrityError")]
    assert set(_reports(ctx, other)) == {"weekly", "monthly"}
    assert _reports(ctx, victim) == {}  # the loser's work rolled back whole
    assert len(_milestones(ctx, victim)) == 1  # the concurrent run's row stands
