# Phase 8 Plan — Dashboard UI Rebuild (Epic K polish, product-grade UI)

**Stories:** US-K1–K5 (re-delivered with a real design system), US-B5, the dashboard-facing
halves of US-A4 (lifecycle), US-H3 (wellness), US-J2 (alerts), US-L1 (pipeline status),
US-L3 (role access). · **Branch:** `phase-8-ui` (cut when the owner asks for a push)
**Packages:** `apps/web` only, plus one additive API dev-dependency-free script for the
OpenAPI contract dump. No backend behaviour changes.
**Live status:** `C:\CricAi\README.md` on the build machine (updated at least every 30 min).

## Why

Phases 1–7 shipped a complete, fully tested backend (33 routers, ~110 endpoints) and a
dashboard whose data logic is correct but whose presentation is bare: no stylesheet, no
design tokens, three plain nav links, raw-text loading/error states. The dashboard is the
only surface the player, parent and coach touch every day, so it is the largest remaining
gap between "works" and "is used".

## Scope decisions (autonomous; owner may override)

- **Server-side proxy (BFF) replaces the bundle-inlined token.** Today
  `NEXT_PUBLIC_API_TOKEN` is inlined into the JS bundle, so every LAN device can extract it,
  and a role change needs a rebuild. Next.js route handlers under `/api/cricai/*` forward to
  `cricai_api` server-side and attach the bearer from an httpOnly, SameSite=Strict cookie
  set by a `/login` page (role token entered once per device). Consequences:
  - the token never reaches browser JavaScript;
  - the browser only talks to its own origin, so the API needs no CORS middleware;
  - one build serves all three roles.
  Configuration becomes `CRICAI_API_BASE_URL` (server-only, runtime) and
  `NEXT_PUBLIC_CRICAI_MEDIA_BASE` (unchanged). `NEXT_PUBLIC_API_TOKEN` is removed.
  `lib/api.ts` keeps the injectable `ApiConfig` so every existing test pattern survives.
- **Data parity is unchanged and non-negotiable (US-K4).** The UI renders only API values.
  No client-side metric computation, ever. Geometry for charts remains presentation.
- **Styling stack:** Tailwind CSS v4 with semantic design tokens in `app/globals.css`
  (light and dark via `prefers-color-scheme` plus an explicit toggle), a print stylesheet
  for the net-wall report, lucide-react icons. Small accessible primitives (button, card,
  badge, dialog, tabs, toast, skeleton) are written in-repo under `components/ui` and are
  fully tested like any other code. No component-library runtime dependency.
- **Charts stay hand-rolled SVG** (deterministic, testable, dependency-free), restyled
  through tokens.
- **Tablet first.** 18px base type, 44px minimum tap targets, bottom tab bar under 1024px,
  sidebar above. The primary user is an 11–12-year-old on the lab tablet.

## Test and loop architecture

Every stage runs the same loop: slice the story (acceptance list including the four honest
states) → pin the API contract → fixtures → failing tests → implement with tokens only →
gates (lint, typecheck, vitest 100%, build) → iterate until green → update docs and status.

Shared test infrastructure (`apps/web/test/`, excluded from coverage as test support, the
same policy as `testsupport/`):

| Module | Purpose |
|---|---|
| `fixtures.ts` | One factory per wire type (`session()`, `tag()`, `event()`, `clip()`, `video()`, …) taking partial overrides. |
| `fakeApi.ts` | In-memory `ApiClient` seeded from fixtures, with `failWith(status)` and deferred-resolution controls. |
| `render.tsx` | `renderWithShell()` wraps a component in theme, toast and role providers. |
| `states.ts` | `expectHonestStates()` drives loading, empty, error and degraded through any data component. |
| `axe.ts` | `expectNoA11yViolations()` over a rendered container (WCAG AA rules). |

Above component level:

- **Contract parity:** `scripts/dump_openapi.py` writes `apps/web/test/openapi.json` from
  `create_app()`; a vitest asserts every enum vocabulary and response interface in
  `lib/api.ts` matches the schema field by field, so backend drift fails the web gate.
- **Proxy tests:** the route handler reads the bearer only from the cookie, strips any
  client-sent `Authorization`, clears the cookie on an upstream 401, and forwards only
  allow-listed headers.
- **End-to-end:** Playwright journeys mirroring `docs/uat_scripts.md` (player, parent,
  coach) against the in-memory test API seeded with the demo session. Separate CI job,
  non-gating until the rig exists.

New Makefile targets: `dev`, `contract`, `web-e2e`; `check` gains `contract`.

## Stages

| # | Stage | Delivers |
|---|---|---|
| 0 | Baseline | Install deps, run every existing gate, record results before any change. |
| 1 | Test and loop infrastructure | The `test/` modules above, contract dump + parity test. |
| 2 | UI foundation | Tailwind, tokens, themes, print CSS, app shell, primitives, error boundary, toasts, login page, BFF proxy. |
| 3 | Today (home) | Published daily report card (one correction, one drill, one goal) and workload status. |
| 4 | Sessions | List and detail restyled; video-first multi-camera player on tablet; keyboard stepping kept. |
| 5 | Pitch map and progress | Zone colours from tokens, restyled trends, milestone feed. |
| 6 | Reports | Net-wall print layout, PDF/PNG export polish. |
| 7 | Missing surfaces | Pipeline run status, session lifecycle, wellness check-in, alerts, camera health, settings. |
| 8 | Coach tools | Review queue and notes on the new primitives. |
| 9 | Ship | Standalone Next build in `Dockerfile.web`, PWA manifest, Playwright journeys, docs sweep. |

## Build machine (Windows)

The workspace lives in `C:\CricAi` (code in `cricAI\`, portable Node 22 + pnpm 10 in
`tools\node\`, all caches in `cache\`, gate logs in `logs\`). `source
/c/CricAi/tools/env.sh` in Git Bash before any command. `make` is not installed there, so
gates run as their underlying `uv run …` and `pnpm …` commands; the Makefile remains the
source of truth for CI.
