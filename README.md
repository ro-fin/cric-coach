# cricAI — Home AI Cricket Lab

A netted home cricket lab that captures every ball on 4–8 cameras, converts video into
structured per-ball measurements, and delivers coach-like feedback — **one main
correction, one drill, one measurable goal per session** — while actively protecting a
growing 11–12-year-old from overload.

**Architecture (non-negotiable):** Cameras → Video pipeline → CV models → Structured
per-ball metrics → AI coaching agents → Dashboard. The LLM never analyzes raw video.

- Backlog: [`cricket-ai-coach-user-stories.md`](cricket-ai-coach-user-stories.md)
- Design & phase plan: [`docs/specs/2026-07-07-cricket-ai-coach-design.md`](docs/specs/2026-07-07-cricket-ai-coach-design.md)
- Safety policy: [`docs/safety_workload.md`](docs/safety_workload.md)

**Deploy & release (Phase 7):**
- Deploy runbook (rig-from-doc, troubleshooting, cron wiring §5): [`docs/runbooks/deploy_lan.md`](docs/runbooks/deploy_lan.md)
- UAT scripts (player/parent/coach, results log): [`docs/uat_scripts.md`](docs/uat_scripts.md)
- Release checklist, hardware register & the per-story audit: [`docs/release_checklist.md`](docs/release_checklist.md) (inventory in `cricai_data.release_manifest`)
- Deploy smoke: [`scripts/verify_deploy.py`](scripts/verify_deploy.py) · scheduled jobs: [`scripts/run_scheduled.py`](scripts/run_scheduled.py)

**Dashboard UI rebuild (Phase 8, in progress):** plan, decisions and test architecture in
[`docs/specs/phase-8-plan.md`](docs/specs/phase-8-plan.md). On the Windows build machine the
workspace is `C:\CricAi` (see *Windows build machine* below).

## Layout

```
apps/api        FastAPI service (sessions, videos, tags, reports)
apps/worker     RQ processing DAG (probe → events → clips → pose → metrics → agents)
apps/web        Next.js dashboard
packages/data      canonical schemas, enums, synthetic data, storage adapters
packages/vision    calibration, events, pose, tracking, overlays
packages/coaching  rules engine, workload safety, agents, reports
scripts/        CLI entry points     docs/       specs, metric defs, runbooks
models/         model registry       datasets/   dataset version manifests
```

## Development

Requirements: [uv](https://docs.astral.sh/uv/), pnpm, ffmpeg, PostgreSQL 16, Redis.

```bash
make setup        # install python workspace + web deps
make check        # lint + typecheck + unit tests (100% coverage gates) + web
make test-integration   # real postgres/redis/ffmpeg tests
make safety       # SAF suite — release-gating, never waivable
make seed         # generate the synthetic demo session
make dev          # dashboard against a seeded in-memory API (scripts/dev_stack.py)
make contract     # the checked-in OpenAPI dump is current (web contract test pins to it)
make web-e2e      # Playwright UAT journeys, tablet + desktop, production build
```

Every Python package is gated at **100% line+branch coverage**; the web app is gated
at 100% vitest coverage. The SAF (safety) suite and golden-session snapshots gate
every release.

### Windows build machine

Everything lives under `C:\CricAi`: this repo in `cricAI\`, portable Node 22 and pnpm 10 in
`tools\node\`, pnpm/npm/uv/Playwright caches in `cache\`, gate logs in `logs\`, and a live
status board in `C:\CricAi\README.md`. `make` is not installed there, so run the Makefile's
underlying commands after loading the environment in Git Bash:

```bash
source /c/CricAi/tools/env.sh   # PATH + caches, cd into the repo
uv sync --all-packages && pnpm install --frozen-lockfile
```

## Dashboard (`apps/web`)

Next.js app rendering ONLY what the API returns (US-K4 data-parity — no
client-side metric computation). Phase 8 rebuilt it on a design system (Tailwind
tokens, light/dark/print, tablet-first shell) and moved sign-in server-side. Routes:

| Route | View | Roles |
|---|---|---|
| `/login` | choose a role and enter its token; sets the httpOnly session cookie | public |
| `/` | Today: the published daily report (one correction, drill, goal) and workload | all |
| `/sessions`, `/sessions/[id]`, `/sessions/new` | session list; ball-by-ball timeline + multi-cam player (US-K1); guided create → cameras → checklist → start/stop | all; `new` parent, coach |
| `/pitchmap/[sessionId]` | pitch map + zone analytics (US-K2) | all |
| `/progress` | trends, milestones, workload (US-K4/G5) | all |
| `/reports` | report list, report view, PDF/PNG export, net-wall print (US-K5) | all |
| `/wellness` | wellness check-in and history (US-H3) | all |
| `/review` | coach review queue: publish gate-held drafts (US-J5) | coach |
| `/notes` | coach notes panel (US-K3) | coach, parent |
| `/pipeline` | pipeline runs and stage traces (US-L1) | parent, coach |
| `/alerts` | alerts inbox (US-J2) | parent, coach |
| `/cameras`, `/settings` | camera registry and health checks; versioned settings | parent |

**Configuration (Phase 8).** The browser never holds an API token. Pages call the
same-origin proxy `/api/cricai/*`, which forwards to the API server-side with the
bearer from the `cricai_session` cookie set at `/login`. One build serves every role.

```bash
CRICAI_API_BASE_URL=http://<api-host>:8000            # server-only, read at runtime
NEXT_PUBLIC_CRICAI_MEDIA_BASE=http://<store>:9000/cricai   # clip media, inlined at build
```

```bash
make dev                  # seeded in-memory API + dashboard (scripts/dev_stack.py)
cd apps/web && pnpm dev   # dashboard alone against a running API
pnpm build && pnpm start  # production build (what the lab tablet is served)
```

**LAN deploy note (US-L3, local-first):** only `NEXT_PUBLIC_CRICAI_MEDIA_BASE` is
inlined at build time; point it at the LAN address of the store, not localhost.
Never expose the API or the dashboard beyond the LAN — no raw video or report
data leaves the lab.

For the full deployment — the verified process-based path
(`scripts/deploy_local.sh`), the post-deploy smoke (`scripts/verify_deploy.py`),
and the scheduled-jobs crontab — follow the runbook
[`docs/runbooks/deploy_lan.md`](docs/runbooks/deploy_lan.md); its §5 is the single
source for cron wiring. Do NOT hand-wire cron from elsewhere.

## Privacy

Local-first by design (US-L3): LAN deployment, role-based access, explicit-action
sharing only, no raw video ever leaves the lab. LLM calls receive structured findings
only, through an allow-listed payload schema.
