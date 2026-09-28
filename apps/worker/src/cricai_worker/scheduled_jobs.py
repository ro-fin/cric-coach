"""Production scheduler entrypoints (US-J4/G5/L4/J5) — what an operator crons.

Cron wiring itself stays deploy-phase, but the callables a schedule invokes
are PRODUCTION code, not a test-module composition (findings [3/33/49/78]):

- :func:`run_nightly` — US-J4 frozen baselines for every non-guest player;
- :func:`run_weekly` — the US-L4 drift/quality monitor over the ISO week that
  just ended, then the US-G5 all-player weekly/monthly rollups (which read
  the baselines the nightly job wrote — ordering matters: nightly before
  weekly on the shared anchor day);
- :func:`run_review_sweep` — the US-J5 timeout sweep composed with the REAL
  publish gate (``cricai_api``'s ``publish_reasons`` + ``default_recompute``),
  so a timeout default-publish runs the exact hash/claims/coverage/evidence/
  live-safety validation a manual publish runs — never a hand-rolled gate.

Cadence, ordering and example crontab lines live in
``docs/runbooks/scheduled_jobs.md``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from cricai_data.models import Player, Report
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext
from cricai_worker.drift_monitor import DriftMonitorSummary, run_weekly_drift_monitor
from cricai_worker.nightly_baselines import BaselineSummary, compute_nightly_baselines
from cricai_worker.review_sweep import SweepSummary, sweep_due_reports
from cricai_worker.rollup_reports import RollupSweepSummary, rollup_all_players, week_period

__all__ = ["WeeklyRunSummary", "run_nightly", "run_review_sweep", "run_weekly"]


@dataclass(frozen=True)
class WeeklyRunSummary:
    """What one weekly run did: the drift monitor's summary plus the all-player
    rollup sweep (its per-player summaries and any skipped players)."""

    drift: DriftMonitorSummary
    rollups: RollupSweepSummary


def _non_guest_player_ids(ctx: WorkerContext) -> list[uuid.UUID]:
    """Every non-guest player, stable order (guests never accrue longitudinal
    baselines, reports or milestones — their footage is short-lived by policy)."""
    with ctx.session_factory() as db:
        return list(
            db.scalars(select(Player.id).where(Player.is_guest.is_(False)).order_by(Player.id))
        )


def run_nightly(ctx: WorkerContext, *, as_of: date | None = None) -> list[BaselineSummary]:
    """US-J4 nightly cron: recompute frozen baselines for the whole squad.

    One :func:`~cricai_worker.nightly_baselines.compute_nightly_baselines`
    run per non-guest player; each is idempotent and convergent, so a
    crash-resume is a plain re-run.
    """
    return [
        compute_nightly_baselines(ctx, player_id, as_of=as_of)
        for player_id in _non_guest_player_ids(ctx)
    ]


def run_weekly(ctx: WorkerContext, *, as_of: date | None = None) -> WeeklyRunSummary:
    """US-L4 + US-G5 weekly cron: drift monitor, then all-player rollups.

    The drift monitor runs over the ISO week that JUST ENDED relative to
    ``as_of`` (default today, UTC) — its tag-then-compare loop needs the
    completed week — while the rollups run as of ``as_of`` so the current
    week's report reflects everything logged so far.
    """
    effective = as_of if as_of is not None else datetime.now(tz=UTC).date()
    ended_week_start = week_period(effective - timedelta(days=7))[0]
    drift = run_weekly_drift_monitor(ctx, ended_week_start)
    return WeeklyRunSummary(drift=drift, rollups=rollup_all_players(ctx, as_of=effective))


def run_review_sweep(ctx: WorkerContext, *, now: datetime | None = None) -> SweepSummary:
    """US-J5 sweep cron: timeout default-publish THROUGH the real publish gate.

    Composes ``cricai_api.routers.reports.publish_reasons`` with
    ``default_recompute`` into :func:`~cricai_worker.review_sweep.sweep_due_reports`
    — the same validation path a manual ``POST /reports/{id}/publish`` runs, so
    a report failing the gate at timeout lands in BLOCKED with reasons audited,
    never silently published (findings [3/33/49]).

    The import is deferred: ``cricai_worker`` carries no hard ``cricai_api``
    dependency (the sweep's gate stays injectable); this entrypoint runs where
    both packages are installed — the lab box's workspace environment.
    """
    from cricai_api.routers.reports import (  # noqa: PLC0415  (deferred: see docstring)
        default_recompute,
        publish_reasons,
    )

    def real_gate(db: OrmSession, report: Report) -> list[str]:
        return publish_reasons(db, report, default_recompute)

    return sweep_due_reports(ctx, real_gate, now=now)
