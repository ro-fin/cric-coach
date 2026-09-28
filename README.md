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
```

Every Python package is gated at **100% line+branch coverage**; the web app is gated
at 100% vitest coverage. The SAF (safety) suite and golden-session snapshots gate
every release.

## Dashboard (`apps/web`)

Next.js app rendering ONLY what the API returns (US-K4 data-parity — no
client-side metric computation). Routes:

| Route | View |
|---|---|
| `/` | home |
| `/sessions`, `/sessions/[id]` | session list; ball-by-ball timeline + multi-cam clip player (US-K1) |
| `/pitchmap/[sessionId]` | pitch map + zone analytics (US-K2) |
| `/notes` | coach notes panel (US-K3) |
| `/reports` | session/weekly/monthly report surface + PDF/PNG export (US-K5) |
| `/progress` | trends, milestones, workload (US-K4/G5) |
| `/review` | coach review queue: publish/block gate-held drafts (US-J5) |

Configuration is exactly **two** environment variables — where the API is and
which bearer token to send:

```bash
NEXT_PUBLIC_API_BASE_URL=http://<api-host>:8000   # default http://localhost:8000
NEXT_PUBLIC_API_TOKEN=<role-token>                # parent/coach/player token
```

Clip playback can additionally override the object-store base with
`NEXT_PUBLIC_CRICAI_MEDIA_BASE` (defaults to the local MinIO).

```bash
cd apps/web
pnpm dev     # local development against a running API
pnpm build   # production build (then: pnpm start)
```

**LAN deploy note (US-L3, local-first):** `NEXT_PUBLIC_*` values are inlined
at **build time** — set both variables before `pnpm build`, pointing at the
LAN address of the API box (not localhost) so other devices on the LAN can
use the dashboard. Never expose the API or the dashboard beyond the LAN — no
raw video or report data leaves the lab.

For the full deployment — the verified process-based path
(`scripts/deploy_local.sh`), the post-deploy smoke (`scripts/verify_deploy.py`),
and the scheduled-jobs crontab — follow the runbook
[`docs/runbooks/deploy_lan.md`](docs/runbooks/deploy_lan.md); its §5 is the single
source for cron wiring. Do NOT hand-wire cron from elsewhere.

## Privacy

Local-first by design (US-L3): LAN deployment, role-based access, explicit-action
sharing only, no raw video ever leaves the lab. LLM calls receive structured findings
only, through an allow-listed payload schema.
