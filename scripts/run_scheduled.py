#!/usr/bin/env python3
"""Cron entrypoint for the production scheduled jobs (US-J4/G5/L4/J5).

A thin argparse dispatcher over :mod:`cricai_worker.scheduled_jobs` with a
real env-configured :class:`~cricai_worker.context.WorkerContext` — what the
Phase-7 deploy runbook (docs/runbooks/deploy_lan.md) crons on the lab box:

    uv run scripts/run_scheduled.py nightly        # US-J4 frozen baselines
    uv run scripts/run_scheduled.py weekly         # US-L4 drift + US-G5 rollups
    uv run scripts/run_scheduled.py review-sweep   # US-J5 timeout publish sweep

Ordering matters (docs/runbooks/scheduled_jobs.md): nightly before weekly on
the shared anchor day; the sweep is safe at any frequency. ``--as-of`` (nightly
and weekly only) re-anchors a catch-up run after lab-box downtime. Must run in
the workspace environment (``uv sync --all-packages``): the review sweep
composes the REAL ``cricai_api`` publish gate.
"""

from __future__ import annotations

import argparse
from datetime import date

from cricai_worker.context import WorkerContext
from cricai_worker.scheduled_jobs import run_nightly, run_review_sweep, run_weekly


def main(argv: list[str] | None = None, ctx: WorkerContext | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one cricAI scheduled job (US-J4/G5/L4/J5) against the configured DB."
    )
    parser.add_argument("job", choices=("nightly", "weekly", "review-sweep"))
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="anchor date (YYYY-MM-DD) for catch-up runs; nightly/weekly only",
    )
    args = parser.parse_args(argv)
    if args.job == "review-sweep" and args.as_of is not None:
        parser.error("--as-of does not apply to review-sweep (it sweeps what is due NOW)")

    context = ctx if ctx is not None else WorkerContext.from_env()
    if args.job == "nightly":
        baselines = run_nightly(context, as_of=args.as_of)
        print(f"nightly: baselines recomputed for {len(baselines)} player(s)")
    elif args.job == "weekly":
        weekly = run_weekly(context, as_of=args.as_of)
        print(
            f"weekly: drift week {weekly.drift.week_start}, "
            f"{len(weekly.rollups.summaries)} player rollup(s), "
            f"{len(weekly.rollups.skipped)} skipped"
        )
    else:
        sweep = run_review_sweep(context)
        print(
            f"review-sweep: {sweep.due} due, {sweep.published} published, {sweep.blocked} blocked"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
