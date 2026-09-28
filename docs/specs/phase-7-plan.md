# Phase 7 Plan — Golden Scenarios, Deployment & Release (T5/T6, design milestone 7)

**Scope:** the five T5 end-to-end golden scenarios (automated, release-gating), T6 UAT
scripts, LAN deployment (docker-compose authored + config-validated, process-based deploy
verified on this machine, ops runbook, scheduled-job wiring), the review-queue UI deferred
from Phase 6, and the final release audit (Definition of Done sweep, real-hardware milestone
register). · **Branch:** `phase-7-golden-deploy`

Scope decisions (recorded as autonomous decisions; owner may override):

- **Golden scenarios are `-m golden` integration tests on real temp PG** driving the FULL
  production surface: seed via synthetic generators + API calls, run the real DAG
  (stage_adapters), assert through the API exactly what the story ACs name. Each pins a
  content digest only where the Phase-1 golden does (intentional-update discipline);
  everything else asserts semantically (fault named, counts exact, banners present).
- **T5 #4 (bowling target block) is already covered** by the Phase-6 bowling full-DAG test;
  Phase 7 extends it with the release-scatter render assertion (report body scatter block +
  web ReportView render) rather than duplicating it.
- **Deploy is docker-compose authored + linted, process-based verified** (design milestone 6:
  local Docker broken): compose file for api/worker/web/postgres/redis with healthchecks,
  validated by `docker compose config` in a test; the VERIFIED path is `scripts/deploy_local.sh`
  (process supervisor-less: uvicorn + rq worker + next start + existing local PG/redis) plus
  `scripts/verify_deploy.py` (black-box smoke: health, auth, session lifecycle, report fetch,
  dashboard reachable). Cron wiring for `cricai_worker.scheduled_jobs` ships as documented
  crontab lines in the runbook + a `scripts/run_scheduled.py` entrypoint.
- **Review-queue UI (US-J5 residue)**: one `apps/web/app/review/**` page over
  `GET /settings/review-queue` + publish/block actions (reports router), coach-role only.
- **Release audit is a doc + a test**: `docs/release_checklist.md` (DoD per epic, UAT scripts,
  real-hardware milestone register naming release ±2f / bounce MAE / surveyed-rig / 4h SLO /
  soak as DEPLOY-BLOCKED benchmarks with their protocols) and a `test_release_manifest.py`
  asserting every backlog story ID appears in exactly one status bucket (shipped/deferred
  with pointer) — the campaign's honest closing inventory.

## Fan-out (worktree agents; disjoint files)

| Agent | Owns |
|---|---|
| g1 | T5 #1 machine batting day golden (`apps/worker/tests/test_golden_batting_day.py`): 120 synthetic balls, planted 40% straight-front-foot fault on full outside-off → full DAG → report names that fault with exact counts, ≥2 clips evidence, one drill, one goal; publish passes. |
| g2 | T5 #2 mixed session golden (`test_golden_mixed_session.py`): machine + throwdown + player-bowling blocks → block attribution, H2 split reconciliation, workload ledger exact (incl. throwdown weight 0). |
| g3 | T5 #3 degraded + #5 ceiling week goldens (`test_golden_degraded_session.py`, `test_golden_ceiling_week.py`): dead camera + missing audio → completes, honesty banners, zero fabricated metrics; 7-day at-limit history → planner zero-bowling day, verbatim H1 warning text hash-verified. |
| g4 | T5 #4 extension (release-scatter render in the bowling golden + ReportView scatter test), `apps/web/app/review/**` review-queue UI (list, publish, block; coach-only; vitest 100%). |
| d1 | `deploy/docker-compose.yaml` + `.env.example` + compose-config lint test; `scripts/deploy_local.sh` + `scripts/verify_deploy.py` (black-box smoke, **UNIT-tested in-process** via `TestClient` + a mocked web leg — see the amendment below); `scripts/run_scheduled.py`; `docs/runbooks/deploy_lan.md` (rig-from-doc for the parent UAT, crontab lines, backup/restore pointer). |
| u1 | `docs/uat_scripts.md` (T6 player/parent/coach scripts, step-by-step against shipped surfaces); `docs/release_checklist.md` (DoD sweep, hardware-milestone register); `packages/data/tests/test_release_manifest.py` (story-ID inventory test). |

**Amendment (d1 deviation, recorded honestly).** The `d1` row originally promised
`verify_deploy.py` "exercised in an integration test against a live process
stack". That was overridden: the shipped `apps/api/tests/test_verify_deploy.py`
is **unit-only** (in-process `fastapi.TestClient`; the web leg is an
`httpx.MockTransport`), and **no gate — Makefile, CI, or otherwise — ever runs
`scripts/deploy_local.sh` or boots a live stack.** The deploy path is therefore
verified two ways only: (1) `verify_deploy.py`'s unit tests, and (2) the runbook
smoke run by hand on the rig (`docs/runbooks/deploy_lan.md`, and UAT-PA1 — the
first real rig-from-doc exercise of `deploy_local.sh`). A machine-executed
**live-stack integration gate is DEPLOY-BLOCKED** (it needs a built `apps/web`
and a running PG/redis the build machine does not have); it is not part of the
merge gate. The `docs/release_checklist.md` "Two honest caveats" note records the
same fact for release runners.

Gates, review, audit, PR: identical to Phases 5–6 (per-package 100%, mypy, ruff, vitest 100%,
integration/safety/golden, adversarial review → fix wave → fix-verification audit → merge).
