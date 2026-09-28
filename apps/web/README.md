# cricai-web

Next.js dashboard for the cricAI lab LAN (US-K1..K5): session timeline and
multi-cam player, pitch map, coach notes, reports and the progress dashboard.
Tablet first: 18px text, 44px tap targets, a bottom tab bar under 1024px and a
sidebar above, light and dark themes, and a print stylesheet for the net-wall
report.

## How the dashboard reaches the API (US-L3)

The browser never holds an API token and only ever talks to its own origin:

```
browser ──/api/cricai/*──▶ Next.js route handler ──Bearer <token>──▶ cricai_api
          (same origin)     app/api/cricai/[...path]  CRICAI_API_BASE_URL
```

1. `/login` asks who is using the device (player, parent or coach) and for that
   role's token. The server checks the token against the API, including that it
   really is a token of the chosen role, then stores role and token in the
   `cricai_session` cookie: httpOnly (no script can read it), SameSite=Strict,
   30 days, Secure when served over https.
2. Every page without a valid cookie redirects to `/login?next=<page>`
   (`middleware.ts`).
3. Feature code calls `/api/cricai/<api path>` through `lib/api.ts`
   (`defaultConfig()`, `apiRequest()`, `createApiClient()`). The route handler
   forwards to the API with the bearer from the cookie. A browser-sent
   `Authorization` header is dropped, and only allow-listed headers cross in
   either direction. An API 401 clears the cookie, so the next page load goes
   back to `/login`.
4. `GET /api/cricai/_session` returns `{ "role": ... }`; `DELETE` signs out
   (the **Sign out** button in the shell).

One build serves every role, a role change is a sign-out and sign-in (no
rebuild), and the API needs no CORS configuration.

## Configuration

| Variable | When read | Meaning | Default |
| --- | --- | --- | --- |
| `CRICAI_API_BASE_URL` | **runtime**, server only | Where the dashboard server reaches `cricai_api` | `http://localhost:8000` |
| `NEXT_PUBLIC_CRICAI_MEDIA_BASE` | build time (inlined) | LAN object-store base URL for clip media (US-B5) | `http://localhost:9000/cricai` |

`CRICAI_API_BASE_URL` is read by the server on every request, so it can change
with a restart and no rebuild. It is the address *the dashboard server* uses,
so `http://localhost:8000` is right when both run on the lab box, and
`http://api:8000` inside docker compose. Browsers on other devices never call
it.

`NEXT_PUBLIC_API_BASE_URL` and `NEXT_PUBLIC_API_TOKEN` are retired. They are
no longer read, so remove them from any old `.env`.

The API has no auth-off mode: a missing or wrong token is answered with 401,
so sign in with one of the API's configured parent, coach or player tokens,
local development included.

```sh
# local development against an API on this machine
CRICAI_API_BASE_URL=http://localhost:8000 pnpm dev
# then open http://localhost:3000 and sign in
```

## Install on the lab tablet (PWA)

The dashboard ships a web app manifest (`public/manifest.webmanifest`) and
icons (`public/icons`), so "Add to Home screen" / "Install app" opens it
full-screen like a native app. There is no offline mode: data always comes
live from the API.

## Production image

`deploy/Dockerfile.web --target runtime` builds a self-contained Next.js
standalone server (no pnpm or sources inside, runs as `node`). Standalone
output is opt-in through `CRICAI_WEB_STANDALONE=1`, which that stage sets;
local builds stay plain `next build` + `next start` (standalone tracing
needs symlinks, which Windows refuses without Developer Mode). The build and
run commands are in the header of `deploy/Dockerfile.web`.

## Design system

- Tokens live in `app/globals.css` (Tailwind CSS v4 `@theme`): `bg`, `surface`,
  `surface-raised`, `border`, `ink`, `ink-muted`, `accent`, `accent-ink`,
  `success`, `warning`, `danger`, `info`, pitch-map zones (`zone-yorker`,
  `zone-full`, `zone-good`, `zone-short`) and lines (`line-outside-off`,
  `line-off`, `line-middle`, `line-leg`). Use them as utilities (`bg-surface`,
  `text-ink-muted`, `border-border`, `fill-zone-good`); never a raw colour.
- Primitives come from `@/components/ui`: `Button`, `LinkButton`,
  `buttonClassName`, `Card` (+ `CardHeader`, `CardTitle`, `CardBody`), `Badge`,
  `Skeleton`, `EmptyState`, `ErrorState`, `DegradedBanner`, `PageHeader`,
  `StatTile`, `Tabs` (+ `TabList`, `Tab`, `TabPanel`), `Dialog`, `useToast`.
- The shell (`components/shell/AppShell`) wraps every page and renders no
  `<main>`; each page renders exactly one. Routes and the roles that see them
  are registered in `components/shell/nav.ts`.
- Print: mark controls `data-print="hide"`; `data-print="keep"` keeps a block
  on one page.

## Scripts

- `pnpm dev` — dev server
- `pnpm lint` / `pnpm typecheck` — gates
- `pnpm test` — vitest with 100% line/branch coverage thresholds
- `pnpm build` — production build
