"""US-G5/US-K4: the weekly/monthly rollup job — trends, milestones, alerts.

One callable job per player (the scheduler seam; cron wiring is deploy-phase):
:func:`rollup_reports` reads the frozen ``metric_baselines`` snapshots through
``cricai_coaching.progress_agent`` (per-point n, the >=3-sessions /
>=30-balls-per-point qualification guardrails — US-G5/US-J4), then

- writes the ``milestones`` log (US-K4/G5): ``personal_best`` (the progress
  agent's guardrailed detector — full-history, direction-aware) is an
  append-only EVENT log, while ``volume`` landmarks (US-H1 ledger) and
  ``streak`` landmarks (consecutive practice days) are machine RE-DERIVATIONS:
  each run recomputes them from history and reconciles the stored rows
  (delete-stale / update-changed / insert-new on the (player, kind, metric,
  achieved_on) unique key), so backdated entries, late-logged sessions and
  privacy deletes converge instead of duplicating (findings [9/37/41]);
- raises context-normalized regression alerts (US-G5 AC): only a QUALIFIED
  per-zone slice (same ``zone_key``, e.g. ``"off/good"``) regressing counts as
  "worse technique" — the mix-sensitive ``all`` slice regressing alone is a
  "harder ball mix" and never alerts. Direction respects each metric's
  ``progress_agent.metric_direction`` polarity (finding [7/74]). Alerts dedupe
  on their latest point;
- upserts one WEEKLY and one MONTHLY ``reports`` row (pinned contract #3):
  Report-body-v1 plus ``trends: [{metric, zone_key, points: [{date, value,
  n}], direction, qualified}]`` and ``milestones: [{kind, metric, value,
  achieved_on}]`` keys, ``fatigue_note``/``coverage_note`` present-null, and
  ``source_daily_report_ids`` tracing the period's daily reports (the plan's
  "from daily reports" input, finding [79]). Regeneration is idempotent on
  the (player, kind, period) unique key and always lands in DRAFT carrying
  the US-J5 review deadline (``review_due_at_for``) — the same gate seam as
  ``generate_report``, so coach_gate mode queues and sweeps rollups too
  (findings [2/11/28/36/52/75]); an already gate-held draft keeps its
  existing deadline (no timeout creep, finding [14/38]).

Publish-gate integrity (US-G3/H5, non-negotiable): every number the body ships
rides in the structured ``trends``/``milestones`` keys, and each one is also
listed in ``claims`` with a ``recompute_key`` the publish gate re-derives
straight from ``metric_baselines``/``milestones`` rows (the ``baseline:`` and
``milestone:`` schemes in ``cricai_api.routers.reports.default_recompute``).
So a rollup number mutated in the stored body after generation is recomputed
against the DB at publish and blocks the report instead of shipping (finding
[24]) — the same numeric-integrity guarantee finding-scheme daily reports
already carry. The wording fields stay number-free, so ``validate_claims_coverage``
(which scans wording) passes with the structured numbers covered by claims;
the assembler self-checks that invariant and refuses to emit a body whose
wording carries an uncovered number.
"""

from __future__ import annotations

import calendar
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cricai_coaching import progress_agent
from cricai_coaching.report import validate_claims_coverage
from cricai_coaching.review_gate import review_due_at_for
from cricai_data.db import session_scope
from cricai_data.enums import AlertAudience, MilestoneKind, ReportKind, ReportStatus
from cricai_data.models import (
    Alert,
    AuditLog,
    BowlingLedgerEntry,
    MetricBaseline,
    Milestone,
    Player,
    Report,
)
from cricai_data.models import Session as SessionRow
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext
from cricai_worker.nightly_baselines import ZONE_ALL

_LOG = logging.getLogger(__name__)

#: audit_log actor for rollup-written rows (mirrors ``worker:generate_report``).
AUDIT_ACTOR = "worker:rollup_reports"

#: US-G5 regression alert code; parent-audience (the guardian acts on it).
PROGRESS_REGRESSION_CODE = "progress_regression"

#: Milestone metrics this job derives (US-K4 feed vocabulary).
VOLUME_METRIC = "bowling_balls"
STREAK_METRIC = "practice_days"

#: Cumulative bowling-ball landmarks (US-H1 ledger is the source of truth).
VOLUME_LANDMARKS: tuple[int, ...] = (100, 250, 500, 1000, 2500, 5000)

#: Consecutive-practice-day landmarks celebrated in the US-K4 kid feed.
STREAK_LANDMARKS: tuple[int, ...] = (3, 5, 7, 14)

#: Number-free wording (numbers would need claims the existing publish gate
#: cannot recompute; the real numbers ride in the structured trends key).
POSITIVE_BY_KIND: dict[str, str] = {
    ReportKind.WEEKLY.value: "Another week of honest work in the bank - keep showing up.",
    ReportKind.MONTHLY.value: "A month of steady practice - the habit is the win.",
}

#: Honesty path (US-G5): no qualified trend series means no trend claim ships.
HONESTY_NO_TRENDS = "Not enough qualifying sessions for a trend claim yet - keep logging practice."


@dataclass(frozen=True)
class RollupSummary:
    """What one rollup run did (job summary, test surface)."""

    player_id: str
    weekly_report_id: str
    monthly_report_id: str
    weekly_created: bool
    monthly_created: bool
    milestones_written: int
    alerts_written: int
    trend_count: int


@dataclass(frozen=True)
class RollupSweepSummary:
    """What one all-player sweep did: each player's summary, plus every player
    a concurrent-write collision skipped (finding [15]).

    ``skipped`` carries ``(player_id, reason)`` so a PERSISTENTLY-failing
    player stays visible in the run summary (and the runbook's summary-to-logs
    paper trail) instead of silently vanishing from the report set — a
    one-off cron overlap and a recurring data bug are then distinguishable by
    whether the same id reappears run after run.
    """

    summaries: list[RollupSummary]
    skipped: list[tuple[str, str]]


def week_period(as_of: date) -> tuple[date, date]:
    """The ISO week (Monday..Sunday) containing ``as_of``."""
    start = as_of - timedelta(days=as_of.weekday())
    return start, start + timedelta(days=6)


def month_period(as_of: date) -> tuple[date, date]:
    """The calendar month containing ``as_of``."""
    last_day = calendar.monthrange(as_of.year, as_of.month)[1]
    return as_of.replace(day=1), as_of.replace(day=last_day)


def _milestone_exists(
    db: OrmSession, player_id: uuid.UUID, kind: MilestoneKind, metric: str, achieved_on: date
) -> bool:
    return (
        db.scalar(
            select(Milestone).where(
                Milestone.player_id == player_id,
                Milestone.kind == kind,
                Milestone.metric == metric,
                Milestone.achieved_on == achieved_on,
            )
        )
        is not None
    )


def _add_milestone(  # noqa: PLR0913  (one row's natural identity + payload)
    db: OrmSession,
    player_id: uuid.UUID,
    kind: MilestoneKind,
    metric: str,
    value: float,
    achieved_on: date,
    context: dict[str, Any],
) -> int:
    """Insert one milestone unless its unique key already holds it; 1 = written."""
    if _milestone_exists(db, player_id, kind, metric, achieved_on):
        return 0
    db.add(
        Milestone(
            player_id=player_id,
            kind=kind,
            metric=metric,
            value=value,
            context=context,
            achieved_on=achieved_on,
        )
    )
    return 1


def _write_personal_bests(
    db: OrmSession, player_id: uuid.UUID, snapshots: list[dict[str, Any]]
) -> int:
    """US-J4/K4: guardrailed personal bests from the overall (``all``) slices.

    :func:`cricai_coaching.progress_agent.personal_best` refuses unqualified
    snapshots, so a "new personal best" never surfaces to a child without the
    sample behind it (US-J4 AC). Per-zone slices are context for alerts, not
    separate best ledgers — one metric celebrates one best per day (the
    milestone unique key).
    """
    written = 0
    for snapshot in snapshots:
        if snapshot["zone_key"] != ZONE_ALL or not progress_agent.personal_best(snapshot):
            continue
        latest = snapshot["points"][-1]
        written += _add_milestone(
            db,
            player_id,
            MilestoneKind.PERSONAL_BEST,
            str(snapshot["metric"]),
            float(latest["value"]),
            date.fromisoformat(str(latest["date"])),
            context={
                "zone_key": snapshot["zone_key"],
                "window": snapshot["window"],
                "n": int(latest["n"]),
            },
        )
    return written


@dataclass(frozen=True)
class _DerivedMilestone:
    """One recomputed VOLUME/STREAK landmark: its row identity plus payload."""

    kind: MilestoneKind
    metric: str
    value: float
    achieved_on: date
    context: dict[str, Any]


def _fresh_volume_milestones(
    db: OrmSession, player_id: uuid.UUID, as_of: date
) -> list[_DerivedMilestone]:
    """US-H1-ledger volume landmarks: first date each cumulative total is reached.

    A day that crosses several landmarks at once derives ONE milestone (the
    unique key is per day): its ``value`` is the highest landmark reached and
    ``context.landmarks`` lists every landmark that day crossed.
    """
    entries = db.scalars(
        select(BowlingLedgerEntry)
        .where(
            BowlingLedgerEntry.player_id == player_id,
            BowlingLedgerEntry.entry_date <= as_of,
        )
        .order_by(BowlingLedgerEntry.entry_date, BowlingLedgerEntry.created_at)
    ).all()
    cumulative = 0
    pending = list(VOLUME_LANDMARKS)
    by_date: dict[date, dict[str, Any]] = {}
    for entry in entries:
        cumulative += entry.balls
        while pending and cumulative >= pending[0]:
            info = by_date.setdefault(entry.entry_date, {"landmarks": []})
            info["landmarks"].append(pending.pop(0))
            info["cumulative_balls"] = cumulative
    return [
        _DerivedMilestone(
            kind=MilestoneKind.VOLUME,
            metric=VOLUME_METRIC,
            value=float(info["landmarks"][-1]),
            achieved_on=day,
            context=info,
        )
        for day, info in sorted(by_date.items())
    ]


def _practice_dates(db: OrmSession, player_id: uuid.UUID, as_of: date) -> list[date]:
    rows = db.scalars(
        select(SessionRow.session_date)
        .where(SessionRow.player_id == player_id, SessionRow.session_date <= as_of)
        .distinct()
        .order_by(SessionRow.session_date)
    )
    return list(rows)


def _practice_runs(dates: list[date]) -> list[list[date]]:
    """Split sorted distinct practice dates into runs of consecutive days."""
    runs: list[list[date]] = []
    for day in dates:
        if runs and day - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def _fresh_streak_milestones(
    db: OrmSession, player_id: uuid.UUID, as_of: date
) -> list[_DerivedMilestone]:
    """Consecutive-practice-day landmarks: achieved the day the streak hits N."""
    fresh: list[_DerivedMilestone] = []
    for run in _practice_runs(_practice_dates(db, player_id, as_of)):
        for landmark in STREAK_LANDMARKS:
            if landmark > len(run):
                break
            fresh.append(
                _DerivedMilestone(
                    kind=MilestoneKind.STREAK,
                    metric=STREAK_METRIC,
                    value=float(landmark),
                    achieved_on=run[landmark - 1],
                    context={"streak_start": run[0].isoformat()},
                )
            )
    return fresh


def _reconcile_derived_milestones(
    db: OrmSession, player_id: uuid.UUID, fresh: list[_DerivedMilestone]
) -> int:
    """Converge the machine-derived VOLUME/STREAK log on recomputed history.

    ``achieved_on`` is DERIVED (cumulative ledger walk / consecutive-day runs),
    so it moves whenever history changes — a backdated ledger entry, a
    late-logged session or a privacy delete (findings [9/37/41]). Appending
    under the (player, kind, metric, achieved_on) unique key would duplicate
    the same landmark on two dates forever; instead each run deletes rows the
    recomputation no longer derives, updates rows whose value/context changed
    (a same-day block crossing a higher landmark) and inserts the new ones.
    PERSONAL_BEST rows are events a child was shown, never touched here.
    Returns rows inserted or corrected (a plain re-run writes 0).
    """
    fresh_by_key = {(item.kind, item.metric, item.achieved_on): item for item in fresh}
    written = 0
    existing = db.scalars(
        select(Milestone).where(
            Milestone.player_id == player_id,
            Milestone.kind.in_((MilestoneKind.VOLUME, MilestoneKind.STREAK)),
        )
    ).all()
    for row in existing:
        item = fresh_by_key.pop((row.kind, row.metric, row.achieved_on), None)
        if item is None:
            db.delete(row)  # its derivation moved: the row no longer exists
        elif row.value != item.value or dict(row.context) != item.context:
            row.value = item.value
            row.context = item.context
            written += 1
    for item in fresh_by_key.values():
        db.add(
            Milestone(
                player_id=player_id,
                kind=item.kind,
                metric=item.metric,
                value=item.value,
                context=item.context,
                achieved_on=item.achieved_on,
            )
        )
        written += 1
    return written


def _alert_exists(db: OrmSession, dedupe_key: str) -> bool:
    for existing in db.scalars(select(Alert).where(Alert.code == PROGRESS_REGRESSION_CODE)):
        if existing.detail.get("dedupe_key") == dedupe_key:
            return True
    return False


def _write_regression_alerts(
    db: OrmSession, player_id: uuid.UUID, snapshots: list[dict[str, Any]]
) -> int:
    """US-G5 AC: regression alerts are context-normalized to the same zone.

    Only a QUALIFIED per-zone slice regressing alerts — the player got worse
    on the SAME line x length. The ``all`` slice mixes zones, so it regressing
    alone just means a harder ball mix; it never alerts. Idempotent per latest
    trend point via ``dedupe_key`` (the drift-monitor idiom).
    """
    written = 0
    for snapshot in snapshots:
        if snapshot["zone_key"] == ZONE_ALL or not progress_agent.regression_alert(snapshot):
            continue
        latest_date = str(snapshot["points"][-1]["date"])
        dedupe_key = (
            f"{player_id}:{snapshot['metric']}:{snapshot['zone_key']}:"
            f"{snapshot['window']}:{latest_date}"
        )
        if _alert_exists(db, dedupe_key):
            continue
        db.add(
            Alert(
                audience=AlertAudience.PARENT,
                code=PROGRESS_REGRESSION_CODE,
                severity="warning",
                detail={
                    "player_id": str(player_id),
                    "metric": snapshot["metric"],
                    "zone_key": snapshot["zone_key"],
                    "window": snapshot["window"],
                    "latest_date": latest_date,
                    "dedupe_key": dedupe_key,
                },
            )
        )
        written += 1
    return written


def _period_milestones(
    db: OrmSession, player_id: uuid.UUID, start: date, end: date
) -> list[Milestone]:
    return list(
        db.scalars(
            select(Milestone)
            .where(
                Milestone.player_id == player_id,
                Milestone.achieved_on >= start,
                Milestone.achieved_on <= end,
            )
            .order_by(Milestone.achieved_on, Milestone.kind, Milestone.metric)
        )
    )


def _trend_entries(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Contract-#3 trends: exactly {metric, zone_key, points, direction, qualified}."""
    return [
        {
            "metric": snapshot["metric"],
            "zone_key": snapshot["zone_key"],
            "points": [
                {"date": point["date"], "value": point["value"], "n": point["n"]}
                for point in snapshot["points"]
            ],
            "direction": snapshot["direction"],
            "qualified": snapshot["qualified"],
        }
        for snapshot in snapshots
    ]


def _rollup_claims(
    snapshots: list[dict[str, Any]], milestones: list[Milestone]
) -> list[dict[str, Any]]:
    """Publish-gate claims for every structured number the rollup renders.

    US-G3/H5 (finding [24]): each trend point value and each milestone value
    ships with a ``recompute_key`` the publish gate re-derives from the DB, so
    a number mutated in the stored body after generation fails the gate rather
    than reaching the child. Keys carry the coordinates that identify the row —
    player is resolved from ``report.player_id`` at recompute time:

    - trend point -> ``baseline:<metric>:<zone_key>:<window>:<date>`` ->
      ``metric_baselines.value`` for that slice+snapshot;
    - milestone   -> ``milestone:<kind>:<metric>:<achieved_on>`` ->
      ``milestones.value`` for that landmark.

    ``zone_key`` uses ``/`` and never ``:``; ``metric``/``window`` are
    identifier-shaped and ``date``/``achieved_on`` are ISO — none carry a
    colon, so the gate's ``split(":")`` scheme dispatch is unambiguous.
    """
    claims: list[dict[str, Any]] = []
    for snapshot in snapshots:
        metric = str(snapshot["metric"])
        zone_key = str(snapshot["zone_key"])
        window = str(snapshot["window"])
        for point in snapshot["points"]:
            claims.append(
                {
                    "value": float(point["value"]),
                    "metric": metric,
                    "recompute_key": f"baseline:{metric}:{zone_key}:{window}:{point['date']}",
                }
            )
    for row in milestones:
        claims.append(
            {
                "value": float(row.value),
                "metric": row.metric,
                "recompute_key": (
                    f"milestone:{row.kind.value}:{row.metric}:{row.achieved_on.isoformat()}"
                ),
            }
        )
    return claims


def assemble_rollup_body(  # noqa: PLR0913  (public seam: the body's keyword inputs)
    *,
    kind: str,
    period_start: date,
    period_end: date,
    snapshots: list[dict[str, Any]],
    milestones: list[Milestone],
    daily_report_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Pinned contract #3: Report-body-v1 + ``trends`` and ``milestones`` keys.

    No correction/drill/goal — a rollup reports direction, it never invents an
    issue (US-G3 honesty rule carries over). ``honesty_banner`` ships when no
    trend series is qualified (US-G5: a trend claim needs >=3 sessions and
    >=30 balls per point). ``source_daily_report_ids`` (additive, finding
    [79]) traces the period's daily reports — the plan's "assembles ... from
    daily reports" input, kept minimal: linkage for readers and audits, not a
    re-summarization. Every structured number ships with a recompute claim
    (:func:`_rollup_claims`) so the publish gate re-derives it from the DB
    (finding [24]); wording stays number-free by construction and the final
    self-check enforces it, so ``validate_claims_coverage`` passes.
    """
    trends = _trend_entries(snapshots)
    body: dict[str, Any] = {
        "kind": kind,
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "main_correction": None,
        "drill": None,
        "goal": None,
        "secondary": [],
        "positive": POSITIVE_BY_KIND[kind],
        "safety": None,
        "honesty_banner": None if any(t["qualified"] for t in trends) else HONESTY_NO_TRENDS,
        "coverage_note": None,
        "claims": _rollup_claims(snapshots, milestones),
        "fatigue_note": None,
        "trends": trends,
        "milestones": [
            {
                "kind": row.kind.value,
                "metric": row.metric,
                "value": row.value,
                "achieved_on": row.achieved_on.isoformat(),
            }
            for row in milestones
        ],
        "source_daily_report_ids": list(daily_report_ids),
    }
    uncovered = validate_claims_coverage(body)
    if uncovered:
        raise ValueError(f"rollup wording carries numbers no claim covers: {uncovered}")
    return body


def _upsert_report(
    db: OrmSession,
    player_id: uuid.UUID,
    kind: ReportKind,
    period: tuple[date, date],
    body: dict[str, Any],
) -> tuple[uuid.UUID, bool]:
    """Insert-or-refresh the rollup report on its (player, kind, period) key.

    Regeneration always lands in DRAFT; demoting a PUBLISHED report is a
    visible event a coach must be able to trace (the generate_report rule), so
    it is audit-logged. The read locks the row (``FOR UPDATE``; SQLite's
    single-writer ignores it) so a concurrent coach publish cannot commit
    between this read and the write — the demotion decision is made against
    the status that actually holds, never silently overwritten (finding [13]).

    US-J5 review gate (findings [2/11/28/36/52/75]): a fresh draft carries
    ``review_due_at_for(db)`` — the same seam as ``generate_report`` — so
    coach_gate mode queues and timeout-sweeps rollups; an already gate-held
    draft keeps its existing deadline (re-runs must not creep the timeout,
    finding [14/38]).
    """
    period_start, period_end = period
    report = db.scalar(
        select(Report)
        .where(
            Report.player_id == player_id,
            Report.kind == kind,
            Report.period_start == period_start,
            Report.period_end == period_end,
        )
        .with_for_update()
    )
    created = report is None
    if report is None:
        report = Report(
            player_id=player_id,
            session_id=None,
            kind=kind,
            period_start=period_start,
            period_end=period_end,
            body=body,
        )
        db.add(report)
    else:
        if report.status is ReportStatus.PUBLISHED:
            db.add(
                AuditLog(
                    actor=AUDIT_ACTOR,
                    action="report_regenerated",
                    entity="report",
                    entity_id=str(report.id),
                    detail={
                        "old_status": report.status.value,
                        "reason": "regenerated by rollup run",
                    },
                )
            )
        report.body = body
    gate_held_draft = (
        not created and report.status is ReportStatus.DRAFT and report.review_due_at is not None
    )
    report.status = ReportStatus.DRAFT
    if not gate_held_draft:
        report.review_due_at = review_due_at_for(db)
    db.flush()
    return report.id, created


def _period_daily_report_ids(
    db: OrmSession, player_id: uuid.UUID, start: date, end: date
) -> list[str]:
    """The period's daily-report ids, stable order (body linkage, finding [79])."""
    rows = db.scalars(
        select(Report.id)
        .where(
            Report.player_id == player_id,
            Report.kind == ReportKind.DAILY,
            Report.period_start >= start,
            Report.period_end <= end,
        )
        .order_by(Report.period_start, Report.id)
    )
    return [str(row) for row in rows]


def _rollup_one(
    db: OrmSession,
    player_id: uuid.UUID,
    kind: ReportKind,
    period: tuple[date, date],
    snapshots: list[dict[str, Any]],
) -> tuple[uuid.UUID, bool]:
    body = assemble_rollup_body(
        kind=kind.value,
        period_start=period[0],
        period_end=period[1],
        snapshots=snapshots,
        milestones=_period_milestones(db, player_id, period[0], period[1]),
        daily_report_ids=_period_daily_report_ids(db, player_id, period[0], period[1]),
    )
    return _upsert_report(db, player_id, kind, period, body)


def rollup_reports(
    ctx: WorkerContext, player_id: uuid.UUID, *, as_of: date | None = None
) -> RollupSummary:
    """One player's weekly + monthly rollup (US-G5) — the schedulable job.

    Reads only the frozen nightly baselines (US-J4: report generation never
    recomputes history), freezes the trend view to ``as_of`` (default today,
    UTC), writes milestones and context-normalized regression alerts, then
    upserts the week's and month's Report rows containing the same trend
    series and the period's milestones. Re-running is a plain re-run: every
    write is idempotent on its unique key or dedupe key, and the derived
    VOLUME/STREAK milestone log is CONVERGENT — identical inputs write
    nothing, changed history reconciles to what a fresh run would produce
    (findings [9/37/41], the nightly_baselines delete-stale idiom).
    """
    effective = as_of if as_of is not None else datetime.now(tz=UTC).date()
    with session_scope(ctx.session_factory) as db:
        player = db.get(Player, player_id)
        if player is None:
            raise ValueError(f"player not found: {player_id}")
        rows = [
            progress_agent.baseline_to_mapping(row)
            for row in db.scalars(
                select(MetricBaseline).where(MetricBaseline.player_id == player_id)
            )
        ]
        snapshots = progress_agent.build_snapshots(rows, as_of=effective)
        milestones_written = _write_personal_bests(
            db, player_id, snapshots
        ) + _reconcile_derived_milestones(
            db,
            player_id,
            _fresh_volume_milestones(db, player_id, effective)
            + _fresh_streak_milestones(db, player_id, effective),
        )
        alerts_written = _write_regression_alerts(db, player_id, snapshots)
        weekly_id, weekly_created = _rollup_one(
            db, player_id, ReportKind.WEEKLY, week_period(effective), snapshots
        )
        monthly_id, monthly_created = _rollup_one(
            db, player_id, ReportKind.MONTHLY, month_period(effective), snapshots
        )
        return RollupSummary(
            player_id=str(player_id),
            weekly_report_id=str(weekly_id),
            monthly_report_id=str(monthly_id),
            weekly_created=weekly_created,
            monthly_created=monthly_created,
            milestones_written=milestones_written,
            alerts_written=alerts_written,
            trend_count=len(snapshots),
        )


def rollup_all_players(ctx: WorkerContext, *, as_of: date | None = None) -> RollupSweepSummary:
    """The scheduler entrypoint: roll up every non-guest player, stable order.

    Guests never accrue longitudinal reports or milestones (their footage is
    short-lived by policy); a guest id passed to :func:`rollup_reports`
    directly still works for tests and manual runs.

    Concurrent-run isolation (finding [15]): a unique-key collision with an
    overlapping run (cron overlap, an operator re-trigger) means the other
    run already wrote that player's rows — this player's transaction rolled
    back whole (``session_scope``), so the sweep skips the player and
    continues instead of aborting everyone after the collision. The skip is
    logged and recorded in ``RollupSweepSummary.skipped`` so a player failing
    on EVERY run (a data bug, not an overlap) is visible to the operator
    rather than silently losing its weekly/monthly reports. A direct
    :func:`rollup_reports` call still propagates the error to its caller.
    """
    with ctx.session_factory() as db:
        player_ids = list(
            db.scalars(select(Player.id).where(Player.is_guest.is_(False)).order_by(Player.id))
        )
    summaries: list[RollupSummary] = []
    skipped: list[tuple[str, str]] = []
    for player_id in player_ids:
        try:
            summaries.append(rollup_reports(ctx, player_id, as_of=as_of))
        except IntegrityError as exc:
            # A concurrent run's identical write stands; move on — but record
            # the skip so a persistent failure is not silently swallowed.
            reason = type(exc).__name__
            _LOG.warning("rollup sweep skipped player %s: %s", player_id, reason)
            skipped.append((str(player_id), reason))
    return RollupSweepSummary(summaries=summaries, skipped=skipped)
