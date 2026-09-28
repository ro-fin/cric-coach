# LAN Deployment Runbook (Phase 7 — design milestones 6/7; US-L3)

How the Parent deploys the whole cricAI stack — API, worker, dashboard,
PostgreSQL, Redis — on the lab box, **from this document alone** (the T6
parent UAT is literally "rig-from-doc"). Everything stays on the LAN: no
cloud egress, no port forwarding, ever (US-L3).

Two paths ship:

| Path | Status | When |
|---|---|---|
| **Process path** — `scripts/deploy_local.sh` | **Verified by unit tests + runbook walkthrough**; first live rig run is UAT-PA1 | Default. Local Docker is broken there (design milestone 6). |
| **Compose path** — `deploy/docker-compose.yaml` | Authored + config-linted (`packages/data/tests/test_compose_config.py`), **not yet run** | Once Docker works on the lab box. |

Whichever path you take, finish with the smoke: `uv run scripts/verify_deploy.py`.

## 1. Prerequisites

- The repo checked out on the lab box (assumed at `~/cricai` below).
- [uv](https://docs.astral.sh/uv/), Node 22 + pnpm, ffmpeg on `PATH`.
- Process path only: PostgreSQL 16 and Redis 7 running locally
  (`brew services start postgresql@16 redis` or distro equivalent).
- Compose path only: Docker Engine + the compose plugin.
- The lab box reachable from LAN devices by a stable name/address
  (`lab.local` below — substitute yours).

## 2. Environment file (both paths)

```sh
cd ~/cricai
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env` (it is gitignored — never commit it):

1. **Role tokens** (US-L3) — generate three distinct tokens:
   `openssl rand -hex 32`. An empty token means that role cannot log in;
   `deploy_local.sh` refuses to start without a parent token.
2. **Database** — process path: create the role **with a password** and point
   `CRICAI_DATABASE_URL` at it — the password in the URL must match the role,
   or password auth fails on a default Debian/Ubuntu cluster:

   ```sh
   # Run as the postgres superuser (sudo -u postgres) on a default
   # Debian/Ubuntu cluster, where local peer auth maps your OS user to a role
   # that does not exist; the plain form (drop the prefix) works on a Homebrew
   # install where your OS user is already a superuser.
   sudo -u postgres createuser -P cricai        # prompts for the password
   sudo -u postgres createdb -O cricai cricai
   # then set CRICAI_DATABASE_URL=postgresql+psycopg://cricai:THAT_PASSWORD@localhost:5432/cricai
   ```

   Use a strong password (`openssl rand -hex 24`); `deploy_local.sh` warns
   loudly if `CRICAI_DATABASE_URL` is unset (it would fall back to the weak
   `cricai:cricai`) or still carries a placeholder. Compose path: set
   `CRICAI_POSTGRES_PASSWORD` instead (the in-network URL is derived).
3. **`NEXT_PUBLIC_*`** — the dashboard's API origin, bearer token and media
   base. Use the **LAN address** (`http://lab.local:8000`), not localhost,
   or other devices' browsers will call themselves. `NEXT_PUBLIC_API_TOKEN`
   is **inlined into the JS bundle** and is therefore extractable by any
   device that loads the dashboard — set it to the **least-privileged**
   `CRICAI_PLAYER_TOKEN`, never the parent/coach admin tokens. Build a
   separate, higher-privilege dashboard only for a device that stays in
   parent/coach hands.
4. **Pose model (optional)** — `CRICAI_POSE_MODEL_ASSET` points the pose
   stage at a MediaPipe pose-landmarker `.task` bundle. Leave it empty to run
   the deterministic `FakePoseProvider`: analysis and reports still work, but
   on seeded synthetic skeletons rather than the real footage. The fallback
   is stamped onto every `pose_tracks` row (its `model_name`), so it is
   visible in the data — never silent — but a rig-from-doc deploy that wants
   real pose tracking must set this (and install the `pose` extra).

> **Build-time inlining caveat.** Next.js inlines `NEXT_PUBLIC_*` into the
> JS bundle when `pnpm build` runs. Changing any of them requires a
> **rebuild**: process path — redo step 3.2 then restart; compose path —
> `docker compose -f deploy/docker-compose.yaml up -d --force-recreate web`
> (the web container rebuilds on every start). One build serves one
> role/device class; build again with a different `NEXT_PUBLIC_API_TOKEN`
> for a different role (see `apps/web/README.md`).

## 3. Process path (default; unit-verified, first live run is UAT-PA1)

### 3.1 Install

```sh
cd ~/cricai
uv sync --all-packages
cd apps/web && pnpm install && cd ../..
```

### 3.2 Build the dashboard (inlines NEXT_PUBLIC_*)

```sh
set -a; . deploy/.env; set +a
cd apps/web && pnpm build && cd ../..
```

### 3.3 Start / stop / status

```sh
scripts/deploy_local.sh start    # alembic upgrade head, then api+worker+web
scripts/deploy_local.sh status
scripts/deploy_local.sh stop
```

`start` writes a pidfile plus per-process logs under `.cricai-run/`
(`api.log`, `worker.log`, `web.log`) and fails loudly if any process dies
within its first two seconds. It starts, in order:

- **api** — `uvicorn cricai_api.app:create_app --factory` on
  `CRICAI_API_PORT` (default 8000), after running migrations;
- **worker** — `rq worker` on the `CRICAI_RQ_QUEUE` queue (default
  `cricai`) against `CRICAI_REDIS_URL`. Note: `POST /pipeline/...` runs the
  DAG synchronously in the API today, and **nothing enqueues jobs in-code yet**
  (`cricai_worker.pipeline.enqueue_session_pipeline` has no production caller) —
  the worker is forward wiring (and, on the compose path, the `exec` host the
  cron lines target);
- **web** — `next start` on `CRICAI_WEB_PORT` (default 3000), serving the
  build from 3.2.

### 3.4 Verify (black-box smoke)

```sh
set -a; . deploy/.env; set +a
uv run scripts/verify_deploy.py \
  --api-base http://localhost:8000 --web-base http://localhost:3000
```

Checks, in order: API `/health`; tokenless requests rejected (auth is ON);
parent token accepted; an active camera exists (on a **fresh** deploy the
smoke registers C1 at lab defaults — a later real C1 registration simply
starts era 2); a guest-player session runs create → start → stop; the report
list answers; the dashboard root loads. Exit 0 = deployment good; any FAIL
line names the broken leg. The smoke's only footprint is one guest player +
one captured session **per run** — repeated smokes accrete one more of each,
but they are inert (guests never accrue baselines/reports/milestones).

The HTTP legs stay green even if the **rq worker process is dead** (the API
accepts sessions; nothing drains the queue). Add `--check-worker` to also
assert redis is reachable and a worker is registered — the one leg that needs
`redis`/`rq` importable (the workspace env has them), so it is opt-in:

```sh
uv run scripts/verify_deploy.py --check-worker \
  --api-base http://localhost:8000 --web-base http://localhost:3000 \
  --redis-url "$CRICAI_REDIS_URL" --rq-queue "${CRICAI_RQ_QUEUE:-cricai}"
```

Prefer `CRICAI_PARENT_TOKEN` (sourced above) or `--parent-token-file` over
`--parent-token`; the flag warns because it exposes the admin token to `ps`
and shell history.

Then do the cross-device check the UAT scripts use: from another LAN device,
open `http://lab.local:3000` and confirm the dashboard loads data.

## 4. Compose path (authored; run it once Docker is fixed)

```sh
cd ~/cricai
docker compose -f deploy/docker-compose.yaml config   # interpolation check
docker compose -f deploy/docker-compose.yaml build     # build once, WHILE ONLINE
docker compose -f deploy/docker-compose.yaml up -d
docker compose -f deploy/docker-compose.yaml ps       # wait for healthy
```

Notes:

- `deploy/.env` is loaded automatically (the project directory is
  `deploy/`); required values fail interpolation loudly.
- **Small provisioning images are built** (`deploy/Dockerfile.{api,worker,web}`):
  they bake the SYSTEM binaries at build time — `ffmpeg` in **both** api and
  worker (the api runs upload probing and the pipeline DAG in-process, so it
  needs `ffmpeg` exactly like the worker), and a pinned `pnpm` in web. Run
  `docker compose … build` once while online; after that, recreating a
  container on an offline LAN box never hits `apt`/`corepack` and so cannot
  crash-loop. The repo itself is still bind-mounted; api/worker keep their
  container venvs in named volumes (`UV_PROJECT_ENVIRONMENT=/opt/venv`, host
  `.venv` untouched) and web keeps `node_modules`/`.next` in named volumes,
  so the start-time `uv sync --frozen` / `pnpm install` are offline no-ops
  once those volumes are warm. First boot is still slow (uv sync + next
  build) — healthchecks allow a 10-minute start period.
- Persistent state: `cricai-pgdata` (PostgreSQL) and `cricai-storage`
  (object store, mounted at `/data/storage` in api **and** worker).
- Migrations run in the api container before uvicorn starts.
- Smoke it the same way (add `--check-worker --redis-url redis://lab.local:6379/0`
  once redis is reachable from the smoke host):
  `uv run scripts/verify_deploy.py --api-base http://lab.local:8000 --web-base http://lab.local:3000`.

## 5. Scheduled jobs (crontab)

The periodic jobs (US-J4 nightly baselines, US-L4+G5 weekly drift/rollups,
US-J5 review sweep) dispatch through one entrypoint,
`scripts/run_scheduled.py`. Ordering and cadence rationale:
`docs/runbooks/scheduled_jobs.md`. Process-path crontab (`crontab -e`):

Three things bite here, so the lines below handle all of them:

1. **`uv` is not on cron's PATH.** Cron runs with a bare `PATH` (`/usr/bin:/bin`),
   so a bare `uv` fails with "command not found". Set an explicit `PATH=` at
   the top of the crontab — cron does **not** expand `~`/`$HOME` there, so
   write the literal directory. Find it with `command -v uv` (typically
   `~/.local/bin/uv`; adjust the line to your home).
2. **The log directory may not exist.** `.cricai-run/` is created by
   `deploy_local.sh start`; a box that only ever crons would have no such
   dir and the redirect would fail — each line `mkdir -p` it first.
3. **Overlapping runs.** The every-15-minutes sweep can outlast its interval;
   `flock -n` skips a run whose predecessor still holds the per-job lock.

Cron does not read `deploy/.env` either, so each line sources it (`set -a`
marks every sourced variable for export). Process-path crontab (`crontab -e`):

```cron
# Cron has a minimal PATH — point it at uv's dir (literal path; no ~ / $HOME).
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin

# cricAI scheduled jobs (order matters: nightly before weekly on Mondays)
0 2 * * *    cd ~/cricai && mkdir -p .cricai-run && flock -n .cricai-run/nightly.lock      sh -c 'set -a && . deploy/.env && set +a && uv run scripts/run_scheduled.py nightly'      >> ~/cricai/.cricai-run/cron.log 2>&1
30 2 * * 1   cd ~/cricai && mkdir -p .cricai-run && flock -n .cricai-run/weekly.lock       sh -c 'set -a && . deploy/.env && set +a && uv run scripts/run_scheduled.py weekly'       >> ~/cricai/.cricai-run/cron.log 2>&1
*/15 * * * * cd ~/cricai && mkdir -p .cricai-run && flock -n .cricai-run/review-sweep.lock sh -c 'set -a && . deploy/.env && set +a && uv run scripts/run_scheduled.py review-sweep' >> ~/cricai/.cricai-run/cron.log 2>&1
```

After lab-box downtime, catch up with
`uv run scripts/run_scheduled.py nightly --as-of YYYY-MM-DD` (then weekly).

Compose path: same three fixes, but the work runs inside the worker
container (which already has `uv` on PATH and `deploy/.env` in its env), so
only `flock` + `mkdir` wrap the host side:

```cron
0 2 * * * cd ~/cricai && mkdir -p .cricai-run && flock -n .cricai-run/nightly.lock docker compose -f deploy/docker-compose.yaml exec -T worker uv run python scripts/run_scheduled.py nightly >> ~/cricai/.cricai-run/cron.log 2>&1
```

## 6. Backups

The nightly backup manifest, `pg_dump` + object-store sync ordering, and the
quarterly **restore drill** (the T6 parent UAT includes performing it) are
specified in [`docs/runbooks/backup_restore.md`](backup_restore.md). Add its
manifest cron line alongside the ones above, **after** your dump/rsync steps.

## 7. Clip media serving (dashboard playback)

`NEXT_PUBLIC_CRICAI_MEDIA_BASE` must serve the object-store tree over HTTP
(the API stores files under `CRICAI_STORAGE_ROOT`; the dashboard's clip
player fetches them directly). Any LAN static file server over that
directory works (MinIO pointed at it, or nginx/caddy `root` + autoindex
off). This is deliberately not one of the five compose services — point the
variable wherever you serve the tree, and rebuild web when it changes.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `deploy_local: ERROR: CRICAI_PARENT_TOKEN is empty` | Fill the tokens in `deploy/.env` (§2). The API has no auth-off mode. |
| `apps/web/.next missing` | Run the dashboard build (§3.2) — it must happen **after** `NEXT_PUBLIC_*` are final. |
| `verify_deploy` FAIL `auth-enforced` (tokenless got 200) | You are not talking to cricAI (wrong port/proxy). The API always 401s tokenless requests. |
| `verify_deploy` FAIL `auth-parent-token` | Token mismatch between `deploy/.env` and the running API — restart after edits (`stop` then `start`). |
| `verify_deploy` FAIL `session-lifecycle` with 409 | Machine sessions need a safety-checklist ack (US-A5); the smoke uses a coach throwdown, so a 409 here means lifecycle state corruption — check `.cricai-run/api.log`. |
| Dashboard loads but every panel errors | `NEXT_PUBLIC_API_BASE_URL`/`NEXT_PUBLIC_API_TOKEN` were wrong **at build time** — fix `deploy/.env`, rebuild (§3.2), restart. Browser devtools will show calls to the wrong origin or 401s. |
| Dashboard fine on the lab box, dead from other devices | `NEXT_PUBLIC_API_BASE_URL` says `localhost`. Rebuild with the LAN address. Also check the OS firewall allows 3000/8000 on the LAN. |
| Clips don't play | `NEXT_PUBLIC_CRICAI_MEDIA_BASE` not serving `CRICAI_STORAGE_ROOT` (§7). |
| `address already in use` in `.cricai-run/*.log` | Change `CRICAI_API_PORT`/`CRICAI_WEB_PORT` in `deploy/.env`, or stop the squatter. |
| `pidfile … exists — already running?` after a crash | `scripts/deploy_local.sh stop` clears the stale pidfile, then `start`. `stop` is idempotent and only kills PIDs whose command still looks like ours — a PID the OS recycled onto an unrelated process is reported and left alone, never killed. |
| rq worker exits immediately | Redis not running / wrong `CRICAI_REDIS_URL` — see `.cricai-run/worker.log`. |
| Alembic `connection refused` on start | PostgreSQL not running or `CRICAI_DATABASE_URL` wrong. |
| Compose `up` hangs "waiting" | Watch `docker compose … logs -f api web`; first boot legitimately takes minutes (start period 600 s). |
| Weekly rollups look empty | Nightly must run before weekly on the anchor day (§5); re-run `nightly` then `weekly` with `--as-of`. |
