# apps/web handoff (2026-10-04)

Read this first, then `README.md` (more detail on each topic) and `CLAUDE.md` at the repo root.

## Where things are
- **Branch:** `feat/web-ui`, in its own worktree (`git worktree add ../kickoff-web feat/web-ui`), because
  other sessions work in the main checkout. Rebased on `main` as of `37bd5b8`.
- **PR:** draft #1. CI is green: `web`, `lint`, `typecheck`, `unit`, `integration`. The draft is only there
  so CI runs (the workflow triggers on pull requests). **Do not merge** until the user says so.
- **Status:** paused by the user's decision until the M17 backend lands. Nothing below is in progress.
- **Decisions in force:**
  - server-side sessions, never a token in the browser (ADR 0016);
  - API types are generated, never hand-copied;
  - demo mode is dev-only;
  - dark theme only for M17;
  - no fabricated data shown as real (the sample trend charts were removed; real trends are in
    `docs/BACKLOG.md`).

## What is built
Vite + React 19 + TypeScript (strict) + Tailwind 4 + Framer Motion + TanStack Query.

**Pages:**
- **Command Center:** in-flight jobs, clean-completion rate, jobs needing attention.
- **Clients and matters:** connections, Slack exports, workspaces, retention.
- **Collections:** the job list and a three-step job wizard.
- **Job detail:**
  - live progress;
  - the conversation × day reconciliation heatmap, which works with the keyboard and announces units to
    screen readers;
  - units that did not match;
  - the custody chain with animated verification.
- **Slack exports:** SHA-256 in the browser, upload parts with Content-Digest, then the ingest pipeline.
- **Custody explorer.**
- **Access:** role assignments, principals, and the role matrix as returned by the API.

**Status language** (`src/lib/status.ts`):
- Only `completed` looks clean.
- Gaps, unverified and `completed_against_archive` never look clean. The archive caveat is shown word for word.
- Unknown status strings render as themselves, without crashing.

**API layer** (`src/api/`):
- `types.ts` aliases `schema.gen.ts`, which is generated from the FastAPI OpenAPI spec.
- `client.ts` is one interface with one method per route.
- `http.ts` is the real client: cookies, `X-CSRF-Token`, and `reauth_required` handling.
- `demo/` is the in-memory demo client.
- `hooks.ts` holds the React Query hooks.
- `auth.ts` holds every auth and source-flow route plus the provider allowlist.

**Source connection** (`src/components/connect/`):
- **Slack and Teams:** start the flow on the server. The page follows only an `https:` `authorize_url` on
  exactly `slack.com` or `login.microsoftonline.com`.
- **Slack internal app:** the one-time `xoxb-` token goes into an unnamed, uncontrolled password input. It
  never enters React state, the query cache, storage, a URL or logs, and the field is cleared on success.

**Redirect errors:** `?auth_error=` and `?install_error=` are shown as dismissable notices
(`src/components/layout/ReturnNotices.tsx`). Only code-shaped values are ever echoed.

**Accessibility:**
- WCAG AA text and non-text contrast, measured on rendered pixels.
- One global focus ring.
- Modals and the command palette are portalled, trap focus, make `#root` inert and restore focus on close
  (`src/lib/focus.ts`).
- `prefers-reduced-motion` is honoured everywhere (`src/lib/motion.ts`).

## Demo mode vs live mode
| | Demo (default for `npm run dev`) | Live (`VITE_API_MODE=http`) |
|---|---|---|
| Data | in memory (`src/api/demo/`), deterministic, running jobs advance in real time | the API via the Vite proxy (`/v1` → `EDISC_API_ORIGIN`, default `127.0.0.1:8000`) |
| Sign-in | the button enters the app | `GET /v1/auth/login` (does not exist yet, so live mode cannot sign in today) |
| Production build | **impossible**: the build fails on `VITE_API_MODE=demo` or if any demo module reaches a chunk; ESLint blocks demo imports outside `src/api/index.ts` | always |

In live mode, open `http://<tenant>.edisc.localhost:5173`. The API resolves the tenant from the Host
subdomain, and the proxy keeps the Host header.

## Waiting on the M17 backend
These come from `docs/plans/phase-2.md` ("M17 backend") and ADR 0016. Until each one lands, the UI uses
demo data for it, or leaves the feature out in live mode.

| Backend piece | UI side today | When it lands |
|---|---|---|
| `GET /v1/auth/login`, `/auth/callback`, `/auth/csrf`, `POST /auth/logout` | wired in `http.ts` per ADR 0016 | point the E2E suite at the live stack (below) |
| `GET /v1/me/permissions`, `GET /v1/roles` | wired; types in `pending.ts` | regenerate types, delete the pending types |
| `POST …/connections/slack/install`, `…/teams/consent`, `…/slack/token`, `PUT /connections/{id}/token` | wired per ADR 0016 | regenerate types, delete the pending types |
| `GET /v1/jobs` (tenant-wide) | `useRollup` walks clients, then matters, then jobs (N+1 requests) | replace `useRollup` with the one route |
| `GET /v1/jobs/{id}/custody/events` | demo only (`custodyEvents?` is undefined in `http.ts`) | implement in `http.ts`; check the shape |
| `GET /v1/connections/{id}/directory?kind=…` | demo only; the wizard accepts typed ids without it | implement; **the shape differs** (see below) |
| `GET /v1/groups` | demo only (group names in Access) | implement in `http.ts` |
| `GET /v1/jobs/{id}/stream` (SSE) | polling every 2–4 s while a job is active | add SSE with the polling fallback the plan describes |
| `make openapi` → `docs/api/openapi.json` | we dump our own `apps/web/openapi.json` (`scripts/dump_openapi.py`) | generate from `docs/api/openapi.json` and drop our copy and script |

## Open items in `src/api/pending.ts`
Contracts the UI uses that the generated spec does not have yet. When a route lands, delete its type here,
alias the generated one in `types.ts`, and let the compiler show any mismatch. Do not hand-patch.

| Type | Status |
|---|---|
| `CsrfOut` `{csrf_token}` | **confirmed** by ADR 0016 §3 |
| `InstallStartIn` `{connection_id?}` | **confirmed** by ADR 0016 §5 (the comment in the file still says "assumed": stale) |
| `InstallStartOut` `{authorize_url}` | **confirmed**; the ADR also returns `connection_id`, deliberately not hand-added: it comes with the generated types |
| `SlackTokenIn` `{token}` | **confirmed** by ADR 0016 §6 |
| `MyPermissionsOut` `{scopes: [{scope_type, scope_id, permissions}]}` | **assumed** field names (the plan says "effective permissions per scope") |
| `RoleMatrixOut` `{roles: [{name, permissions}]}` | **assumed** field names (the plan says "role → permissions") |
| `CustodyEventView` | **assumed** shape (the plan lists type, actor, time, hashes, payload; cursor-paginated) |
| `DirectoryOut` `{conversations, custodians}` | **does not match the plan**: the plan has one paginated list per `kind`, plus `captured_at` and a refresh flag, and an empty snapshot means "not captured yet". Rework when built |

The header comment of `pending.ts` still says "M17 plan". ADR 0016 now settles the auth and install items.

Other open points:
- ADR 0016 names no `install_error` codes, so every install error gets the generic message plus the code.
  Add messages in `ReturnNotices.tsx` (`INSTALL_ERRORS`) when the codes are listed.
- §8 of the plan runs E2E against the REAL stack. The `contract` project stubs `/v1` until the endpoints
  exist.

## How to run things
```
cd apps/web
npm ci                    # the lockfile carries Linux and macOS native binaries; regenerate it from scratch, never --no-save
npm run dev               # demo mode, http://localhost:5173
npm run typecheck         # app + e2e code
npm run lint              # ESLint + static WCAG contrast check (scripts/check-contrast.mjs)
npm run build             # production build (refuses demo mode)
npm run check:api         # regenerate the OpenAPI spec and types; fails if the committed ones are stale (needs uv)
npx playwright install chromium   # once per machine
npm run e2e               # 38 Playwright tests, about 5–9 min; starts its own dev servers on 5173 and 5174
npx playwright test e2e/a11y/keyboard.spec.ts -g "focus ring"   # one spec or test
```

**E2E projects** (`playwright.config.ts`):
- `ui`, demo data on :5173: `a11y/axe`, `contrast`, `non-text-contrast`, `keyboard`, `reduced-motion`, on
  every page and with the overlays open.
- `contract`, the real HTTP client on :5174 with `/v1` stubbed (`e2e/support/stub-api.ts`): session/CSRF,
  where a credential may appear (canary token), the install flow and provider allowlist, redirect error
  notices, re-authorization.

**When the backend lands:** set `E2E_BASE_URL=http://<tenant>.edisc.localhost:5173` (live mode) and add
the plan's flows: sign-in, dummy connection, collection, live status, report, audited download, and the
export path.

**Conventions for tests:**
- Mutation-test every new check: break the code, see the test fail, restore **from a copy** (never
  `git checkout`, which loses uncommitted work).
- Audits call `settle()` (`e2e/support/pages.ts`), which waits for real stillness rather than a fixed sleep.
  Infinite decorative motion must carry `data-decorative-motion`.
- Pixel audits retake frames torn by the live demo job.

## Gotchas
- **npm registry flakiness on this machine:** downloads stall mid-body. Retry with
  `npm install --prefer-offline --fetch-timeout=45000` in a loop; each attempt fills the cache.
- **Framer Motion adds `tabindex="0"` to tappable buttons,** even disabled ones. Anything that lists tab
  stops must filter `:disabled` and `tabIndex < 0` (see `focusables` in `src/lib/focus.ts`).
- **Scroll containers clip focus rings.** The global `scroll-padding` and the padded lists exist for this. A
  flex row inside a horizontal scroller needs `w-max` for its end padding to count.
- **Never nest `<Button>` inside `<Link>`** (two tab stops for one action). Use `ButtonLink`.
- **Sign-in redirects use the router** (`App.tsx` registers `onUnauthenticated`), so app state such as an
  error notice survives. A full page navigation would drop it.
- **Demo data is deterministic** (seeded PRNG), but running jobs advance with wall-clock time. Tests must
  not assume fixed unit counts on running jobs.
