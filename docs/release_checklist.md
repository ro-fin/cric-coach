# Release Checklist & Audit (Phase 7 — DoD sweep, UAT index, hardware register)

The campaign's closing audit for the backlog in `cricket-ai-coach-user-stories.md`.
Three parts: (1) the Definition-of-Done sweep per epic, (2) the UAT script index,
(3) the **real-hardware milestone register** — every benchmark that cannot pass on
this build machine, named honestly and marked DEPLOY-BLOCKED.

The per-story inventory lives in code, not prose: `cricai_data.release_manifest`
lists every `US-*` id with status (`shipped` / `shipped-partial` / `shipped-as-seam`
/ `deferred`) and a pointer; `packages/data/tests/test_release_manifest.py` fails the
gate if the manifest and the backlog ever diverge.

## Release gates (run all; nothing waivable)

GitHub Actions cannot run on this account (billing `startup_failure` on private
repos), so **the merge gate is the identical local suite**:

```
make lint typecheck test-unit     # ruff + mypy strict + per-package 100% line+branch
make web-lint web-test web-build  # pnpm lint/typecheck, vitest 100%, next build
make test-integration             # real temp PostgreSQL 16 + redis + ffmpeg
make safety                       # SAF suite — release-gating, never waivable
make golden                       # golden demo digest + T5 golden scenarios
make contract                     # Phase 8: checked-in OpenAPI dump is current (web contract test pins to it)
make web-e2e                      # Phase 8: Playwright UAT journeys, tablet + desktop, production build
```

- [ ] All of the above green at the release commit
- [ ] Alembic migrations at head on a fresh temp cluster
- [ ] `packages/data/tests/test_golden_demo.py` digest unchanged, or changed
      intentionally in the same commit (DoD: golden fixtures updated intentionally)
- [ ] `scripts/verify_deploy.py` exits 0 against the deployed stack
- [ ] UAT scripts executed and logged (`docs/uat_scripts.md` results table)
- [ ] `make web-e2e` green at the release commit (26 journeys: sign-in, UAT-P1, UAT-P2,
      UAT-PA degraded session, UAT-C2 review queue; `apps/web/e2e`)

**Two honest caveats about what the gates do and do not cover:**

- **`scripts/` is outside every coverage gate.** `make test-unit` measures
  100% line+branch only over the five `cricai_*` packages (`Makefile` `COV_MODS`);
  it never runs with `--cov=scripts`. So `scripts/verify_deploy.py` (measured at
  98% by its unit tests) and `scripts/run_scheduled.py` have real but **unenforced**
  coverage — a new untested smoke leg or option would not fail any gate. mypy
  strict does still typecheck `scripts/`.
- **`make web-e2e` exercises the dev stack, not the deployed stack.** The journeys run
  the real API code on in-memory SQLite with the seeded demo and a production build of
  the dashboard; PostgreSQL, Redis, media serving and the LAN proxy configuration are
  still only covered by `scripts/verify_deploy.py` and the hand-run UAT.
- **The live process-stack deploy is never machine-executed.** No gate, Makefile
  target, or CI step runs `scripts/deploy_local.sh` or boots a live stack;
  `test_verify_deploy.py` is unit-only (in-process `TestClient` + mocked web leg)
  and the compose config test is YAML-lint only. "All gates green" therefore
  attests nothing about the deploy path executing — the mandatory
  `verify_deploy.py exits 0` checkbox above is a **manual** step run on the rig,
  and UAT-PA1 is the first real rig-from-doc exercise of `deploy_local.sh`.

## 1. Definition-of-Done sweep per epic

Per the backlog's DoD: ACs checked · tests at every listed level passing · docs
updated · golden fixtures intentional · demoed to Parent/Coach · no waived SAF/SEC.
Software-level ACs are DONE for every epic below; hardware/model-quality ACs are on
the register in §3. Story-level residues are flagged inline (and in the manifest).

### Epic A — Facility, Cameras & Capture (Phase 1)
- Stories: A1–A5 shipped (software surfaces). Tests: `apps/api/tests/test_cameras.py`,
  `test_lifecycle_api.py`, `test_health_checks_api.py`, `test_checklists.py`;
  `packages/vision/tests/test_capture_qc.py`. Coverage: 100% (make test-unit).
- Docs: `docs/camera_setup.md` (rig layout, roles, field checklists).
- Register items: real 120fps flicker-free sync capture, camera-sync ≤1 frame (§3).

### Epic B — Session Ingestion & Data Management (Phase 1)
- Stories: B1–B6 shipped (B5 subsumed by K1). Tests: `apps/api/tests` (sessions,
  videos, blocks, tags, storage_admin), `packages/data/tests` (retention, probe).
- Docs: `docs/runbooks/backup_restore.md` (quarterly restore drill = UAT-PA4).

### Epic C — Calibration & Pitch Mapping (Phase 2)
- Stories: C1–C6 shipped. Tests: `packages/vision/tests` (intrinsics, extrinsics,
  drift, zones, heatmap), `apps/api/tests` (calibration, bounce, heatmap).
- Docs: `docs/camera_setup.md` calibration ritual; `scripts/calibrate_cameras.py`.

### Epic D — Ball Events & Clips (Phase 3)
- Stories: D1–D3 shipped; **D4 shipped-partial** — corrections API complete
  (accept/adjust/reject/add/bulk, stable renumbering, ground-truth export), the
  drag-to-adjust web review UI never shipped. Tests: `apps/worker/tests`
  (detect_events, cut_clips, fuse_contacts), `apps/api/tests/test_events_api.py`.
- Register items: event recall/precision and release-timing gates (§3).

### Epic E — Pose & Batting Metrics (Phase 3)
- Stories: E1–E4 shipped; **E5 shipped-partial** — overlay renderer + reference-ball
  library shipped, the before/after comparison web view never shipped.
- Tests: `packages/vision/tests` (pose, overlay), `packages/coaching/tests`
  (pre_release, contact_metrics, decision), `apps/api/tests/test_ball_metrics_api.py`.
- Docs: `docs/coaching_metrics.md` (coach sign-off = UAT-C4).

### Epic F — Detection & Tracking (Phase 4)
- Stories: F1–F6 shipped (full benchmark harness: frozen session-disjoint splits,
  eval reports, promotion gates). Tests: `packages/vision/tests` (detect, track,
  trajectory, bounce_estimate, train, triangulate), `apps/api/tests` (labels,
  datasets, models, tracks). Docs: `docs/labeling_guide.md`.
- Register items: mAP/bounce-MAE/surveyed-rig — every model-quality AC (§3).

### Epic G — Coaching Reports (Phase 5)
- Stories: G1–G6 shipped. Tests: `packages/coaching/tests` (rules, report,
  llm_writer incl. injection red-team SAF, evidence), `packages/data/tests`
  (ballrecord), `apps/worker/tests` (generate_report, rollup_reports),
  `apps/api/tests` (reports, rules). Docs: `docs/coaching_rules.md`,
  `docs/coaching_metrics.md`.

### Epic H — Workload & Safety (cross-cutting)
- Stories: H1–H5 shipped. The US-H2 plan-vs-actual AC ships on a real surface: a
  batting/MIXED daily report with tagged blocks carries the additive
  `batting_split` block (`cricai_worker.generate_report` reconciles the day's
  blocks against the governing safety config; >25% deviation flagged, fun block
  protected), rendered in the server HTML and the web ReportView. Also:
  server-side safety state, verbatim hash-verified safety text, planner rejection
  of unsafe plans. Tests: the SAF marker suite (`make safety` — workload BDD,
  red-team injection, content lints, planner constraints; never waivable) plus
  `apps/worker/tests/test_generate_report.py` (US-H2 wiring) and
  `packages/coaching/tests/test_report.py` (split block + render).
  Docs: `docs/safety_workload.md`.

### Epic I — Leg-Spin Module (Phase 6)
- Stories: I1, I2, I4–I7 shipped (honest-measurement rule: geometric
  turn/flight/dip only, no RPM/seam-axis claims anywhere). **I3 shipped-partial**
  — release-frame detection, per-ball consistency, and the release-scatter
  block + web render ship (pinning the honest empty-but-correct contract), but
  production `run_bowling_action_stage` wires no `px_per_cm`/stereo seam, so
  release heights are null-with-reason and the scatter carries zero numeric
  POINTS on any deployment; the numeric scatter and its seam wiring are
  DEPLOY-BLOCKED on register R3 (§3). Tests: `apps/worker/tests` (bowling_action,
  bowling_flight, classify_variations, full bowling DAG in
  `test_stage_adapters_pg_integration.py`), `packages/vision/tests` (release,
  variation), `packages/coaching/tests` (checkpoints, bowling_analysis/report).

### Epic J — Agent Orchestration (Phases 5–6)
- Stories: J1–J5 shipped (J5 review-queue UI landed Phase 7: `apps/web/app/review`).
- Known web-surface gap: the review queue lists DRAFT reports only, so a gate-BLOCKED
  publish (409) leaves the queue — its reasons persist in the `report_publish_blocked`
  audit rows, the report stays readable at `GET API/reports/{id}`, and republish is
  `POST API/reports/{id}/publish` (curl); recorded in `docs/uat_scripts.md` gaps.
- Tests: `apps/worker/tests` (agent_stages, stage_adapters, review_sweep),
  `packages/coaching/tests` (contracts, agents, review_gate), safety-supremacy SAF.
- Docs: the sweep + rollup cadence and the canonical crontab that wires them live
  in `docs/runbooks/deploy_lan.md` §5 (the single source for cron wiring).

### Epic K — Dashboard & Review UI (Phase 6)
- Stories: K1, K2, K4, K5 shipped; **K3 shipped-partial** — text notes complete,
  voice-note capture/transcription never shipped. Tests: `apps/web` vitest at 100%
  thresholds (`pnpm test`), `apps/api/tests/test_notes_api.py`.
- Note: the backlog's Playwright ST flows ride the T5 goldens + vitest component
  tests; no separate Playwright suite shipped in Phase 6. **Phase 8 ships one**
  (`apps/web/e2e`, `make web-e2e`): the UAT-P1/P2/PA/C2 journeys run in Chromium on a
  tablet and a desktop viewport against the real API (in-memory SQLite, seeded demo)
  and a production build of the dashboard. It is a release gate here; in CI it is a
  separate job, not yet branch protection.

### Phase 8 — Dashboard UI rebuild (Epic K re-delivered, product-grade)
- Plan and decisions: `docs/specs/phase-8-plan.md`; team protocol and status:
  `docs/specs/phase-8-team.md`, `docs/specs/phase-8-status/`.
- Auth: the API token no longer ships in the JS bundle. `/login` (role + token) sets an
  httpOnly `cricai_session` cookie; `app/api/cricai/[...path]` proxies to
  `CRICAI_API_BASE_URL` server-side. `NEXT_PUBLIC_API_TOKEN` is gone; one build serves
  every role. Tests: `apps/web/app/api`, `apps/web/app/login`, `apps/web/lib/auth`,
  e2e `auth.spec.ts` (cookie httpOnly, token never in browser JavaScript).
- Design system: Tailwind v4 tokens (light/dark/print), `components/ui` primitives,
  `components/shell` (sidebar >= 1024px, tab bar below), PWA manifest, standalone image.
- Screens: Today, sessions list/detail/new, reports index + net-wall print, review, notes,
  pitch map, progress, pipeline, wellness, alerts, cameras, settings. Every data component
  shows loading/empty/error/forbidden honestly (`test/states.ts` sweeps).
- Contract: `apps/web/test/contract.test.ts` pins every `lib/api.ts` enum and response
  mirror to `apps/web/test/openapi.json` (`scripts/dump_openapi.py`, `make contract`).
- Data parity unchanged: the dashboard renders API values only. Backend untouched
  (`packages/`, `apps/api/src`, `apps/worker` identical to the Phase 7 release).
- Dev: `make dev` boots the real API on SQLite with a seeded, gate-published demo
  (`scripts/dev_stack.py`, `apps/api/tests/test_dev_stack.py`).

### Epic L — Platform & Non-Functional
- Stories: L1, L3, L4 shipped; **L2 shipped-partial** — gates complete and enforced
  locally, hosted CI blocked by account billing (this file, §Release gates);
  **L5 shipped-as-seam** — `cricai_worker.live_hooks` + allow-list + ≤30s nudge
  budget test; no streaming transport. Tests: `apps/worker/tests` (pipeline,
  drift_monitor, live_hooks), `apps/api/tests` (privacy, auth matrix, egress).
- Docs: `docs/observability.md`; deployment runbook `docs/runbooks/deploy_lan.md`.

## 2. UAT script index (T6 — run per release)

All in `docs/uat_scripts.md`, results logged in its table:

| Script | Persona | Task | Backing stories |
|---|---|---|---|
| UAT-P1 | Player | find best cover drive via timeline + filters | US-B5/K1 |
| UAT-P2 | Player | read today's report aloud | US-G3/K5 |
| UAT-P3 | Player | state tomorrow's drill | US-G3/J3 |
| UAT-P4 | Player | interpret own pitch map | US-C6/K2 |
| UAT-P5 | Player | interpret release scatter | US-I3 |
| UAT-PA1 | Parent | rig from `docs/runbooks/deploy_lan.md` | deploy |
| UAT-PA2 | Parent | session start → report walkthrough | US-B1/B2/A3/L1/G3 |
| UAT-PA3 | Parent | the five dashboard questions | US-K4/B6 |
| UAT-PA4 | Parent | restore drill, developer watching only | US-B6 |
| UAT-C1 | Coach | author a rule (`scripts/rules_io.py`) | US-G2 |
| UAT-C2 | Coach | veto a finding (evidence verdicts) | US-G6/J5 |
| UAT-C3 | Coach | edit a plan (drills PUT; unsafe edit rejected) | US-J3/H5 |
| UAT-C4 | Coach | sign off `docs/coaching_metrics.md` | US-E2/E3/I2 |
| UAT-C5 | Coach | review a month of trends vs own notes | US-G5/K3/K4 |

Golden scenario index (T5). The T5.1–T5.3/T5.5 scenario files are dual-marked
`[golden, integration]`, so both `make golden` and `make test-integration` run
them. T5.4 is split across two gates — listed honestly below so no runner
assumes `make golden` alone covers it; the mandatory gate block above runs all
of `make golden`, `make test-integration` and `make web-test`, so every part of
T5 is exercised on a compliant release:
T5.1 batting day → `apps/worker/tests/test_golden_batting_day.py` (`make golden`
+ `make test-integration`) ·
T5.2 mixed session → `test_golden_mixed_session.py` (`make golden` +
`make test-integration`) ·
T5.3 degraded → `test_golden_degraded_session.py` (`make golden` +
`make test-integration`) ·
T5.4 bowling target block → the bowling DAG in
`test_stage_adapters_pg_integration.py` (dual-marked `[golden, integration]`, so
`make golden` and `make test-integration` both run it); its release-scatter
RENDER assertions live in `apps/web/app/reports/ReportView.test.tsx` and ride
`make web-test` ·
T5.5 ceiling week → `test_golden_ceiling_week.py` (`make golden` +
`make test-integration`).

## 3. REAL-HARDWARE MILESTONE REGISTER — all DEPLOY-BLOCKED

None of these can pass on this build machine (no cameras, no rig, no GPU
workstation, no real footage). Each is **DEPLOY-BLOCKED**: it must run on the
deployed rig before its AC can be checked. The software side — harnesses, frozen
splits, promotion gates, stage timings, provenance columns — shipped in Phases 3–6,
so most studies plug in without schema or code change. **One exception is recorded
explicitly:** R3's release-height leg needs a code change first. Production
`run_bowling_action_stage` (`apps/worker/.../stage_adapters.py`) passes neither a
`px_per_cm` scale nor a stereo/`track3d_resolver` seam into the bowling-action
analysis, so `cricai_vision.release` returns null-with-reason on every ball and
the release scatter ships zero points on ANY rig, real footage included (US-I3 is
shipped-partial for exactly this — see the manifest). R3 below therefore covers
BOTH the surveyed-rig benchmark AND wiring that seam.

| # | Milestone | Target | Protocol pointer | Status |
|---|---|---|---|---|
| R1 | Release-timing benchmark @120fps | ±2 frames (register ratchet; backlog initial: ≤3-frame median, US-D1/T3) | Label 3 full sessions per `docs/labeling_guide.md` (US-B4/D4 ground truth); run the US-F2 eval harness (`routers/models.py` promotion gates, frozen session-disjoint splits) | DEPLOY-BLOCKED |
| R2 | Bounce-point MAE | ≤15 cm, ratchet to ≤10 cm (US-F4/T3) | Manual bounce marks (US-C5, `routers/bounce.py`) vs auto estimates (`cricai_worker.estimate_bounces`) on rig footage; first-month dual-run audit per phase-4 plan | DEPLOY-BLOCKED |
| R3 | Surveyed-rig triangulation + release-height wiring | 3D RMS ≤5 cm in the flight corridor; release height reproducible ±3 cm on identical machine feeds (US-F6/I3) | **PRE-STEP (code, not hardware):** wire a `px_per_cm` scale and/or stereo `track3d_resolver` into `run_bowling_action_stage` (`stage_adapters.py`) — the production DAG passes neither today, so release heights are null and the US-I3 scatter has zero points until this lands. Then: FT surveyed-target rig test (targets at known 3D positions) through `cricai_vision.triangulate` reprojection/QC report; repeatability study on fixed feeds | DEPLOY-BLOCKED |
| R4 | 4-hour processing SLO | full 4-cam 500-ball session ≤4 h on the target GPU workstation (US-L1) | Time a real session through `POST /pipeline/sessions/{id}/runs`; per-stage timings recorded on `pipeline_stages`; compare against the SLO | DEPLOY-BLOCKED |
| R5 | Soak | 7 consecutive daily sessions: SLO holds, no OOM, resource ceilings respected (US-L1 PT) | One week of real lab operation with `scripts/run_scheduled.py` cron wiring live; review stage timings + `docs/observability.md` alerts daily | DEPLOY-BLOCKED |
| R6 | Camera sync & capture quality | sync offset ≤1 frame @120fps; flicker-free exposure (US-A2/T3) | Field checklist in `docs/camera_setup.md` (sync-flash test); `cricai_vision.capture_qc` on the first real recordings | DEPLOY-BLOCKED |

The rest of the T3 model-quality table (event recall/precision, ball mAP,
line/length agreement, speed MAE, shot top-1, pose coverage, contact quality,
calibration reprojection) rides the same protocol as R1–R3: label real footage per
the labeling guide, evaluate through the frozen-split harness, ratchet per T3.
These were recorded as real-footage deferrals in the phase-3 and phase-4 plans —
none is silently claimed by this release.

## Sign-off

- [ ] Parent demo done (all epics) · [ ] Coach demo done (coaching-behavior stories)
- [ ] `docs/coaching_metrics.md` coach sign-off line filled (UAT-C4)
- [ ] Release commit tagged; this checklist archived with the results log
