# Phase 8 Team Protocol — four parallel terminals

Four Claude Code terminals build Phase 8 at the same time. This file is the contract
between them. Every terminal reads it first and follows it exactly. The product plan is
`phase-8-plan.md`; this file only says who owns what, how the pieces fit, and how work
reaches `develop`.

## 1. Terminals, branches, worktrees

| Terminal | Name | Branch | Working directory |
|---|---|---|---|
| T1 | Harness | `phase-8/t1-harness` | `C:\CricAi\cricAI` (the main checkout) |
| T2 | Foundation | `phase-8/t2-foundation` | `C:\CricAi\wt\t2` (git worktree) |
| T3 | Core screens | `phase-8/t3-core-screens` | `C:\CricAi\wt\t3` (git worktree) |
| T4 | Analytics and operations | `phase-8/t4-analytics-ops` | `C:\CricAi\wt\t4` (git worktree) |

Every branch is cut from `origin/develop`. `main` holds the untouched import and is never
pushed to. `develop` is the integration branch; it must always pass the full gate.

Shell setup in every terminal (Git Bash):

```bash
source /c/CricAi/tools/env.sh        # PATH, caches; cds into C:\CricAi\cricAI
cd /c/CricAi/wt/tN                   # T2, T3, T4 only
uv sync --all-packages && pnpm install --frozen-lockfile   # once per worktree
```

`make` is not installed on this machine. Run the Makefile's underlying commands.

## 2. File ownership (disjoint; never edit another terminal's files)

| Terminal | Owns |
|---|---|
| T1 | `apps/web/test/**` (except `fixtures.*.ts` feature files), `apps/web/vitest.config.ts`, `apps/web/vitest.setup.ts`, `apps/web/e2e/**`, `apps/web/playwright.config.ts`, `scripts/dump_openapi.py`, `scripts/dev_stack.py`, `apps/api/tests/test_openapi_dump.py`, `Makefile`, `.github/workflows/**`, `docs/specs/phase-8-status/t1.md`, test-only devDependencies in `apps/web/package.json` |
| T2 | `apps/web/app/globals.css`, `apps/web/app/layout.tsx` (+ test), `apps/web/app/login/**`, `apps/web/app/api/**`, `apps/web/middleware.ts`, `apps/web/components/**`, `apps/web/lib/**`, `apps/web/next.config.ts`, `apps/web/postcss.config.*`, `apps/web/public/**`, `apps/web/README.md`, `deploy/Dockerfile.web`, `deploy/.env.example`, `docs/runbooks/deploy_lan.md`, runtime dependencies in `apps/web/package.json`, `docs/specs/phase-8-status/t2.md` |
| T3 | `apps/web/app/page.tsx` (+ test), `apps/web/app/sessions/**`, `apps/web/app/reports/**`, `apps/web/app/review/**`, `apps/web/app/notes/**`, `apps/web/test/fixtures.core.ts`, `docs/specs/phase-8-status/t3.md` |
| T4 | `apps/web/app/pitchmap/**`, `apps/web/app/progress/**`, `apps/web/app/pipeline/**`, `apps/web/app/wellness/**`, `apps/web/app/alerts/**`, `apps/web/app/cameras/**`, `apps/web/app/settings/**`, `apps/web/test/fixtures.ops.ts`, `docs/specs/phase-8-status/t4.md` |

Rules:

- A change you need in someone else's file is a request, not an edit: write it in your
  status file under "Requests to Tn" and carry on with a local workaround. Never touch the
  backend (`packages/**`, `apps/api/src/**`, `apps/worker/**`); Phase 8 is web-only.
- `apps/web/package.json` and `pnpm-lock.yaml` are shared. On a rebase conflict in
  `pnpm-lock.yaml`, take `origin/develop`'s version and run `pnpm install` to regenerate.
  Conflicts in `package.json` are resolved by keeping both sides' entries.
- The shell's navigation list is T2's `components/shell/nav.ts`. T2 registers every route
  from the route table in section 3.5 up front, so T3 and T4 never edit it.

## 3. Frozen interface contract

Build against these names from the first minute. The owner delivers them by the milestone
in section 5; until then, keep the existing patterns and migrate when they land.

### 3.1 Design tokens (T2, `app/globals.css`, Tailwind v4 `@theme`)

Colour tokens, each available as `bg-*`, `text-*`, `border-*` utilities:
`bg`, `surface`, `surface-raised`, `border`, `ink`, `ink-muted`, `accent`, `accent-ink`,
`success`, `warning`, `danger`, `info`, `zone-yorker`, `zone-full`, `zone-good`,
`zone-short`, `line-outside-off`, `line-off`, `line-middle`, `line-leg`.
Light and dark values both defined; dark via `prefers-color-scheme` and `[data-theme=dark]`.
Base font size 18px. Minimum tap target 44px (`min-h-11`). Print styles under `@media print`.

### 3.2 Primitives (T2, `@/components/ui`)

| Export | Props |
|---|---|
| `Button` | `variant: "primary" \| "secondary" \| "ghost" \| "danger"`, `size?: "md" \| "lg"`, `loading?: boolean`, plus native button props |
| `Card`, `CardHeader`, `CardTitle`, `CardBody` | children, `className?` |
| `Badge` | `tone: "neutral" \| "success" \| "warning" \| "danger" \| "info"` |
| `Skeleton` | `lines?: number`, `label: string` (announced to screen readers) |
| `EmptyState` | `title`, `description?`, `action?: ReactNode` |
| `ErrorState` | `message`, `onRetry?` |
| `DegradedBanner` | `reasons: string[]` (rendered verbatim, never rephrased) |
| `PageHeader` | `title`, `description?`, `actions?: ReactNode` |
| `StatTile` | `label`, `value: string`, `unit?`, `confidence?: number`, `reason?: string \| null` (a null value shows the reason, never a dash) |
| `Tabs`, `TabList`, `Tab`, `TabPanel` | keyboard navigable |
| `Dialog` | `open`, `onOpenChange`, `title`, children |
| `useToast()` | returns `{ toast({ title, description?, tone? }) }` |

### 3.3 Shell and auth (T2)

- `components/shell/AppShell` wraps every page; `components/shell/nav.ts` exports
  `NAV_ITEMS: { href, label, icon, roles: Role[] }[]`.
- `lib/auth/role.tsx` exports `RoleProvider` and `useRole(): Role | null` where
  `Role = "parent" | "coach" | "player"`.
- The browser never holds the API token. `lib/api.ts` keeps every existing export
  (`createApiClient`, `defaultConfig`, `ApiError`, all types) but `defaultConfig()` now
  points at the same-origin proxy `/api/cricai` and sends no bearer. Route handlers under
  `app/api/cricai/[...path]/route.ts` forward to `CRICAI_API_BASE_URL` (server-only env)
  with the bearer from an httpOnly `cricai_session` cookie. `/login` takes role + token,
  verifies them against the API, and sets the cookie. The API does not report a role, so the
  cookie stores both and `GET /api/cricai/_session` returns `{ role }`.

### 3.4 Test helpers (T1, `apps/web/test`)

| Module | Exports |
|---|---|
| `fixtures.ts` | `session(o?)`, `tag(ballNo, o?)`, `event(ballNo, o?)`, `clip(ballNo, cameraId, o?)`, `video(cameraId, o?)`, `reviewItem(o?)`. Feature factories go in `fixtures.core.ts` (T3) and `fixtures.ops.ts` (T4). |
| `fakeApi.ts` | `createFakeApi(seed?)` returning an `ApiClient` plus `failWith(method, status)`, `hold(method)` which returns `release()`, and `calls` |
| `render.tsx` | `renderWithShell(ui, { role?, route? })` |
| `states.ts` | `expectHonestStates({ render, api, method })` drives loading, empty, error (500), forbidden (403) and asserts each is visible and labelled |
| `axe.ts` | `expectNoA11yViolations(container)` |

`apps/web/test/**` is excluded from the coverage threshold as test support, the same
policy as `testsupport/` in Python. Everything under `app/`, `components/`, `lib/` stays at
100% lines, branches, functions, statements.

### 3.5 Route table (all terminals; T2 registers it in the nav)

| Route | Owner | Roles |
|---|---|---|
| `/` Today | T3 | all |
| `/sessions`, `/sessions/[id]`, `/sessions/new` | T3 | all; `new` parent and coach |
| `/reports` | T3 | all |
| `/review` | T3 | coach |
| `/notes` | T3 | coach, parent |
| `/pitchmap/[sessionId]` | T4 | all |
| `/progress` | T4 | all |
| `/pipeline` | T4 | parent, coach |
| `/wellness` | T4 | all |
| `/alerts` | T4 | parent, coach |
| `/cameras` | T4 | parent |
| `/settings` | T4 | parent |
| `/login` | T2 | public |

## 4. Every screen, every time

Data parity: render only what the API returns; never compute a metric in the browser.
Four honest states on every data component: loading (skeleton with label), empty (with a
next action), error (message plus retry), degraded (API `degraded`/`missing_views` or a
metric `reason`, shown verbatim). Keyboard reachable, axe clean, works at 768px and 1280px,
light and dark, 100% coverage.

## 5. Milestones and dependencies

| When | Terminal | Milestone | Unblocks |
|---|---|---|---|
| first 30 min | T1 | Stage 0 baseline gates logged in `C:\CricAi\logs\t1\` | honest attribution of later failures |
| first 90 min | T1 | H1: fixtures, fakeApi, render, states, axe on `develop` | T3, T4 migrate tests |
| first 90 min | T2 | F1: Tailwind, tokens, primitives, shell, nav on `develop` | T3, T4 build screens |
| by hour 3 | T2 | F2: login, proxy, `lib/api.ts` change on `develop` | end-to-end runs |
| by hour 3 | T1 | H2: OpenAPI dump + contract test, `dev_stack.py`, Playwright scaffold | T3, T4 journeys |
| ongoing | T3, T4 | one screen at a time, each pushed to `develop` when its gate is green | |
| last | T1 | CI workflow updated, `make check` extended, final docs sweep | release |

While waiting for a dependency, T3 and T4 do the work that does not need it: acceptance
lists, contract pinning, feature `api.ts` modules, fixtures, view-model logic and tests.

## 6. Sync protocol (the only way code reaches `develop`)

```bash
git add -A && git commit -m "..."          # small commits, often
git fetch origin
git rebase origin/develop                  # resolve; pnpm install if the lockfile moved
pnpm --dir apps/web lint && pnpm --dir apps/web typecheck && pnpm --dir apps/web test && pnpm --dir apps/web build
git push origin HEAD                       # your branch
git push origin HEAD:develop               # fast-forward only; if rejected, fetch, rebase, re-run the gate, retry
```

**Gate amendment (T1, 19:25).** The gate also runs the dashboard copy safety test,
which lints every string literal in `apps/web` against the banned coaching, medical and
spin-claim phrase lists. It is a SAF test: release-gating and never waivable.

```bash
uv run pytest packages/coaching/tests/test_dashboard_copy_saf.py -q -p no:cacheprovider
```

To keep the gate short enough to win the fast-forward race, lint, test and typecheck may
run in parallel, but `build` must start only after `typecheck` finishes: `next build`
rewrites `.next/types`, and an overlapping `tsc` then fails with TS6053.

Never force-push. Never push a red gate to `develop`. Never push to `main`. Commit
messages end with the attribution line your session gives you. Python gates (`uv run ruff
check .`, `uv run ruff format --check .`, `uv run mypy packages apps/api/src apps/worker/src
scripts`) run whenever a Python file changed (T1 only).

## 7. Status and logs (the owner's rule)

- Every terminal keeps `C:\CricAi\status\tN.md`: first the plan for its work, then a log,
  newest entry first, timestamped in IST from the real clock (`date`), added **at least
  every 30 minutes** while working: what is running, what finished with gate results, what
  is failing and being iterated, requests to other terminals, what is next.
- On every push to `develop`, copy that file to `docs/specs/phase-8-status/tN.md` in the
  repo and include it in the commit, so the repository never lags behind the status board.
- Gate output goes to `C:\CricAi\logs\tN\<stage>-<HHMM>.log`.
- `C:\CricAi\README.md` is the index; it links to the four status files. Only T1 edits it.
- Everything related to the project stays under `C:\CricAi` (worktrees, caches, logs,
  browsers). Nothing is installed system-wide.
