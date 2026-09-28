# cricai-web

Next.js dashboard for the cricAI lab LAN (US-K1..K5): session timeline and
multi-cam player, pitch map, coach notes, reports and the progress dashboard.

## API configuration (single convention)

Every feature reads exactly one pair of build-time variables through
`lib/api.ts` — there are no per-feature environment names and no runtime
(localStorage) token source. One `next build` serves the whole deployment.

| Variable | Meaning | Default |
| --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | Absolute origin of the `cricai_api` server | `http://localhost:8000` |
| `NEXT_PUBLIC_API_TOKEN` | Bearer token for the viewing role (US-L3) | empty (no header sent) |
| `NEXT_PUBLIC_CRICAI_MEDIA_BASE` | LAN object-store base URL for clip media (US-B5) | `http://localhost:9000/cricai` |

These are **build-time** values (Next.js inlines `NEXT_PUBLIC_*` at build):
changing them requires rebuilding. For a LAN lab deployment, build once per
role/device class, e.g.

```sh
NEXT_PUBLIC_API_BASE_URL=http://lab.local:8000 \
NEXT_PUBLIC_API_TOKEN=<role-token> \
NEXT_PUBLIC_CRICAI_MEDIA_BASE=http://lab.local:9000/cricai \
pnpm build
```

The API has no auth-off mode: a missing or empty bearer token is answered with
401, and an empty configured role token can never authenticate. So
`NEXT_PUBLIC_API_TOKEN` must be set to one of the API's configured
parent/coach/player tokens for any deployment to work — local development
included.

## Scripts

- `pnpm dev` — dev server
- `pnpm lint` / `pnpm typecheck` — gates
- `pnpm test` — vitest with 100% line/branch coverage thresholds
- `pnpm build` — production build
