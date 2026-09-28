# Scheduled Jobs (US-J4 / US-G5 / US-L4 / US-J5)

The periodic jobs ship as production entrypoints in
`cricai_worker.scheduled_jobs`, dispatched through one cron entrypoint,
`scripts/run_scheduled.py`. This page is the **why** (what each job does and
why the order matters); the crontab itself — PATH, env sourcing, `flock`,
log dir, and the compose variant — lives in **one place**,
[`deploy_lan.md` §5](deploy_lan.md), so the two cannot drift. Operators wire
cron from that section; run a job by hand with
`uv run scripts/run_scheduled.py <nightly|weekly|review-sweep>` in the
workspace environment (`uv sync --all-packages`).

| Entrypoint | Cadence | What it does |
|---|---|---|
| `run_nightly(ctx)` | nightly (e.g. 02:00) | US-J4 frozen `metric_baselines` for every non-guest player (idempotent, convergent) |
| `run_weekly(ctx)` | weekly, Monday after the nightly run | US-L4 drift/quality monitor over the ISO week just ended, then US-G5 weekly/monthly rollups for every non-guest player |
| `run_review_sweep(ctx)` | frequently (e.g. every 15 min) | US-J5 timeout sweep: default-publish gate-held drafts past `review_due_at` THROUGH the real publish gate (block + audit on failure, never silent) |

**Ordering matters:** `run_nightly` → `run_weekly` (rollups read the baselines
the nightly job wrote) → `run_review_sweep` (sweeps whatever drafts are due,
rollups included in `coach_gate` mode). The sweep is safe to run at any
frequency — a decided report leaves DRAFT and is never touched again.

The runnable crontab (with the PATH, env-sourcing, `flock` and log-dir
handling the lab box actually needs) is in [`deploy_lan.md` §5](deploy_lan.md)
— the single source, so it cannot drift from this rationale. Catch-up after
downtime uses `--as-of` on `nightly`/`weekly` (documented there too).

Notes:

- `run_review_sweep` composes the API package's publish validation
  (`_publish_reasons` + `default_recompute`) into the worker sweep — it must
  run in an environment where `cricai_api` is importable (the workspace env
  is; the worker package alone is not enough).
- All three return summary dataclasses; pipe them to logs if you want a
  paper trail beyond the `audit_log` rows the jobs already write.
- The drift monitor's runbook loop (sample → tag → next run compares) is
  documented in `docs/observability.md`.
