# AI Cricket Coach — System Design & Phased Delivery Plan

**Date:** 2026-07-07 · **Status:** Approved for build (autonomous execution per owner directive)
**Source of truth for requirements:** `cricket-ai-coach-user-stories.md` (v1.0, 07 Jul 2026)

This document records the architecture decisions and the phase plan used to build the
backlog end-to-end. Where the backlog allows interpretation, the decision and its
rationale are pinned here.

---

## 1. Scope interpretation

The backlog spans software, physical hardware, and field procedures. Delivery is split
accordingly:

| Surface | Delivery in this build |
|---|---|
| Software (APIs, pipeline, agents, dashboard, safety engine, CV math) | Fully implemented, tested, 100% line+branch coverage, deployed |
| Hardware-coupled CV (MediaPipe pose, YOLO detectors) | Full pipeline code behind **provider adapters**; deterministic fake providers + synthetic fixtures satisfy every testable AC; real-model providers included but activated only when real footage exists |
| Field tests (FT) & model validation vs. real ground truth (MV) | Delivered as **executable runbooks + checklists** (`docs/runbooks/`) with result-recording schemas, because they require the physical lab; the backlog itself marks these as physical-world activities |
| UAT | Delivered as scripted UAT protocols per release (T6) |

Rationale: US-L2 explicitly requires "seed scripts create a demo session (synthetic) so
every story is testable without the physical lab." The synthetic-first design is the
backlog's own testability strategy, not a shortcut.

## 2. Architecture (per backlog §Context, non-negotiable)

```
Cameras → Capture/Upload → Object store (MinIO-compatible)
        → Processing DAG (probe → calibrate-check → events → clips → pose → detect/track → metrics)
        → Per-ball canonical records (PostgreSQL, versioned JSON Schema)
        → Agent pipeline: Ingest → Events → (Batting|LegSpin) → Progress → DrillPlanner → **Safety (final gate)** → ReportWriter
        → Dashboard (Next.js) / PDF exports
```

The LLM layer only ever sees structured findings JSON — never raw video, never
unvalidated free text without untrusted-input flagging (US-G4, US-K3).

## 3. Tech stack & repo layout

Per US-L2 / plan §8-9:

```
apps/api        FastAPI + SQLAlchemy 2 + Alembic (PostgreSQL)
apps/worker     RQ workers (Redis) running the processing DAG
apps/web        Next.js (App Router, TypeScript) dashboard
packages/data      canonical schemas, DB models, storage adapters, provenance
packages/vision    calibration, events, pose, tracking, triangulation, overlays
packages/coaching  rules engine, metrics, workload/safety, agents, reports
models/         model registry metadata (dataset/version/promotion records)
datasets/       dataset version manifests (labels live in object storage)
scripts/        CLI entry points (calibrate_cameras.py, detect_events.py, seed_demo.py …)
docs/           camera_setup.md, coaching_metrics.md, safety_workload.md, runbooks/, specs/
```

Decisions:
- **Python 3.12 via uv workspace** (single lockfile, per-package pyproject).
- **pnpm workspace** for `apps/web`.
- **Heavy CV deps are optional extras** (`[cv]`: opencv; `[pose]`: mediapipe; `[detect]`:
  ultralytics/torch). Core logic is pure-Python/NumPy testable; adapters isolate the
  heavy imports. OpenCV is a standard dependency of `packages/vision` (needed for
  calibration math and is CI-friendly); mediapipe/torch are behind providers.
- **Storage adapter**: S3 protocol interface with two implementations — MinIO/S3 client
  (production) and filesystem-backed store (dev/tests) using identical key layout
  `sessions/{session_id}/{camera_id}/{filename}`.
- **Queue**: RQ (simpler than Celery, satisfies US-L1); DAG runner with per-stage
  status, retry/backoff, resume-from-failure implemented as our own thin layer over RQ
  jobs (testable in-process with fakeredis-style synchronous runner + real Redis IT).
- **LLM adapter**: provider interface with (a) Anthropic client implementation and (b)
  deterministic fake for tests; strict payload allow-list per US-L3.

## 4. Test & coverage strategy (owner directive: 100% at every stage)

- Python: `pytest --cov ... --cov-branch --cov-fail-under=100` per package, enforced in
  CI. `# pragma: no cover` is allowed **only** on hardware/network adapter innards that
  cannot execute in CI (real MinIO client calls, real MediaPipe inference, real LLM
  HTTP), each with a paired contract test against the fake + an IT marker that runs
  when the real service is available. A CI lint counts and caps pragma usage.
- Web: vitest `--coverage` with 100% thresholds on lib/components; Playwright system
  tests for the ST-level ACs (K1, B5, D4, E5 flows) against a seeded dev server.
- Integration tests use **real local PostgreSQL 16** via a per-run temp cluster
  (`initdb` into a temp dir — real Postgres without Docker, since the Docker daemon is
  unavailable on the build machine) and **real redis-server** for queue IT, real
  **ffmpeg** for clip IT.
- SAF suite (workload BDD from the Gherkin ACs, injection red-team, content lints,
  planner constraints) is a separate pytest marker, release-gating, never waivable.
- Golden scenarios T5.1–T5.5 are automated E2E over synthetic sessions.

## 5. Delivery phases (each: plan → implement → test → verify → commit → PR → merge)

| Phase | Content | Backlog stories |
|---|---|---|
| 0 | Repo, CI, quality gates, docs skeleton, seed script | L2 |
| 1 | Capture & ingestion core: sessions/blocks/videos/tags APIs, storage, lifecycle, health checks, safety checklist, RBAC | A1–A5 (software), B1–B4, B6, L3 |
| 2 | Calibration & pitch mapping | C1–C6 |
| 3 | Events, clips, pose, batting metrics | D1–D4, E1–E5 |
| 4 | Detection & tracking | F1–F6 |
| 5 | Coaching reports, workload safety, agents, DAG infra, observability | G1–G6, H1–H5, J1–J4, L1, L4 |
| 6 | Leg-spin module, coach review, full dashboard, real-time hooks | I1–I7, J5, K1–K5, B5, L5 |
| 7 | Golden scenarios, deployment (compose + LAN runbook + verified local deploy), release | T5, DoD audit |
| 8 | Dashboard UI rebuild: design system, app shell, server-side auth proxy, test harness, missing surfaces ([plan](phase-8-plan.md)) | K1–K5, B5, dashboard halves of A4/H3/J2/L1/L3 |

Branching: `phase-N-<slug>` off `main`; PR per phase (or per epic within large phases);
merge only with CI green. Remote: private GitHub repo (child-data project ⇒ private).

## 6. Key cross-cutting invariants (enforced by tests from Phase 1 onward)

1. **Safety supremacy**: Workload/Safety agent is topologically last; its text is
   hash-verified in final artifacts; LLM cannot alter it (H5, G4).
2. **Nullable-with-reason**: no metric is ever silently zero/fabricated (G1, E2).
3. **Provenance everywhere**: every metric field carries `manual | auto_<ver> | proxy`
   and confidence where applicable (G1).
4. **Min-sample gating**: no finding from < configurable N balls (G2); trend claims
   need ≥ 3 sessions & ≥ 30 balls/point (G5).
5. **Age-appropriate content lints**: banned-phrase lists for kid-facing copy, wellness
   language, and honest-limits claims (no RPM/spin-axis) run in CI (G2, H4, I5, K4).
6. **Local-first privacy**: LAN default, egress allow-list, role-based access,
   deletion round-trip (L3).
7. **Idempotency**: detectors, uploads, DAG stages re-run without duplication (D1, B2, L1).

## 7. Autonomous decisions log (owner may override any)

| # | Decision | Rationale |
|---|---|---|
| 1 | Private GitHub repo `cricAI` under `dineshkumares` | "push/merge" directive needs a remote; minor's data ⇒ private |
| 2 | uv + Python 3.12, pnpm, RQ over Celery | modern, simpler, fully satisfies ACs |
| 3 | Temp-cluster Postgres + local Redis for IT instead of testcontainers | Docker daemon unavailable on build machine; still real services |
| 4 | Provider-adapter pattern for MediaPipe/YOLO/LLM with deterministic fakes | 100% coverage + testability without lab footage; real providers pluggable |
| 5 | FT/MV-vs-real-footage ACs delivered as runbooks + recording schemas | physically impossible in software; backlog structure anticipates this |
| 6 | docker-compose authored for LAN deploy; verified deploy on build machine is process-based | Docker daemon unavailable locally; compose validated by config lint + CI |
| 7 | Dashboard talks to the API through a Next.js server-side proxy; role token lives in an httpOnly cookie | `NEXT_PUBLIC_API_TOKEN` was inlined into the bundle and extractable by any LAN device; proxy also removes the need for API CORS and lets one build serve all roles |
| 8 | Tailwind v4 + in-repo accessible primitives, no component-library runtime | full control, 100% testable, small bundle for the lab tablet |
| 9 | Windows build workspace in `C:\CricAi` with portable Node/pnpm and local caches | owner's machine; nothing installed system-wide |
