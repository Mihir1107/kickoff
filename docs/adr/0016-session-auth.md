# ADR 0016: Browser sessions, CSRF, re-authentication and source install flows

Status: **Accepted** (2026-10-03), not implemented yet (M17). Builds on ADR 0013 (tenant from the
host, OIDC, scoped roles) and decision 7 of `docs/plans/phase-2.md` (server-side sessions, httpOnly
cookies, CSRF, revocation, no tokens in the browser). It fixes the details the frontend
(`feat/web-ui`, `apps/web/src/api/pending.ts`) had to assume, adopting its assumptions.

## Context
- The UI is a Vite single-page app served on the tenant host, not a Next.js server. No separate
  backend-for-frontend exists, so the API itself holds the browser session.
- API clients and scripts keep using bearer tokens (ADR 0013). Browsers never hold a token of any kind.
- The frontend needs fixed contracts for:
  - how sign-in learns where to return, and that a sign-in is a re-authentication;
  - the CSRF token endpoint;
  - the source install flows and the internal-app token submission.

## Decision

### 1. Sign-in
- **`GET /v1/auth/login?return_to=<path>&reauth=1`.** Both parameters are optional.
  - **`return_to`** must be a same-origin **relative path**. It is validated server-side; anything else
    is rejected with 400 `invalid_return_to` (never replaced silently, never followed). This rules out
    open redirects. Rules:
    - it starts with a single `/`, and not with `//` or `/\`;
    - no scheme, host, userinfo, backslash, control character or NUL, after percent-decoding once;
    - at most 2,048 bytes;
    - it is stored server-side with the login state and used only after a successful callback.

    Default: `/`.
  - **`reauth=1`** marks a re-authentication for a sensitive action (§4).
    - It requires a live session; otherwise it is a plain sign-in.
    - The IdP request carries `prompt=login` and `max_age=0`.
    - The callback requires an `auth_time` newer than the start of this login, and the same principal
      as the existing session; otherwise 403 `reauth_mismatch` and the old session is left as it was.
- **The login itself** is OIDC authorization code + PKCE with the tenant's IdP (tenant from the Host
  subdomain).
  - A `login_attempts` row holds `state`, `nonce`, the PKCE verifier, `return_to`, `reauth` and an
    expiry (10 min). It is single use.
  - The browser carries only an opaque handle to it in `__Host-edisc_login` (httpOnly, Secure,
    SameSite=Lax, because the IdP's redirect back is a cross-site navigation; deleted at the callback).
- **`GET /v1/auth/callback`** checks state (single use, unexpired, the same browser via the login
  cookie), nonce, issuer, audience and signature. It exchanges the code server-side, resolves the active
  principal by the ADR 0013 rules, and creates the session (§2). It then redirects (303) to the stored
  `return_to`.
  - The IdP's tokens are used once, inside the request, and never stored or sent to the browser.
  - Failures redirect to `/?auth_error=<code>`, where the code comes from a fixed list (`denied`,
    `expired`, `invalid`, `unknown_user`, `inactive`). Provider error text is never reflected.
- **`POST /v1/auth/logout`** (CSRF-protected) revokes the session, clears the cookie, and answers 204.

### 2. Sessions
- **Cookie:** `__Host-edisc_session` holds a random 256-bit id (base64url). It is httpOnly, Secure,
  SameSite=Strict, `Path=/` with no `Domain`: host-only, so scoped to exactly the tenant subdomain.
- **Storage:** a `sessions` table (tenant-scoped, FORCE RLS) holds only the SHA-256 of the id. Columns:
  principal, `created_at`, `authenticated_at`, `last_seen_at`, absolute expiry (12 h), idle expiry
  (30 min), `revoked_at`/`revoked_by`/`revoke_reason`, user agent. Every request checks the row, so
  expiry and revocation apply immediately. `last_seen_at` is written at most once a minute.
- **Fixation:** a NEW id is issued at every login and every re-authentication; the previous one is
  revoked. An id the browser held before signing in is never promoted.
- **Revocation:**
  - `GET /v1/me/sessions` lists the caller's own sessions; `POST /v1/me/sessions/{id}/revoke` ends one.
  - Tenant admins have `POST /v1/principals/{id}/sessions/revoke`.
  - Deactivating a principal revokes all of its sessions.
  - All of these are audited and need a recent sign-in (§4).
- **One credential per request:** a request with BOTH a session cookie and an `Authorization` header is
  rejected with 400 `ambiguous_credentials` before either is evaluated.

### 3. CSRF
- **`GET /v1/auth/csrf`** returns `{"csrf_token": "<token>"}` (`Cache-Control: no-store`). The token is
  an HMAC of the session id under a server key: bound to the session, and invalid after rotation.
- **Every state-changing request** authenticated by the cookie must carry it in `X-CSRF-Token`, and its
  `Origin` (or, when absent, `Referer`) must be the tenant host. Otherwise 403 `csrf`.
- Bearer-authenticated requests are exempt: no ambient credential is involved.
- The OAuth callbacks (§5) are top-level GETs from the provider. They are exempt from the token and
  protected by their single-use, session-bound `state`.

### 4. Recent sign-in for sensitive actions
- **Covered actions:**
  - closing and reopening matters and clients;
  - export limit overrides;
  - role assignment and group membership changes;
  - session revocation;
  - submitting or replacing an internal-app token.
  - creating an RSMF render (ADR 0015 §14; the check is `edisc_api.auth.require_recent_sign_in`, a
    no-op until these sessions exist).
- They require `authenticated_at` within `EDISC_API_REAUTH_MAX_AGE_SECONDS` (default 600). Otherwise the
  API answers 401 `reauth_required` and the UI navigates to `/v1/auth/login?reauth=1&return_to=<here>`.
- Bearer callers are judged on their token's `auth_time` where the IdP provides it, else on `iat`.
- The audit event of a sensitive action records `authenticated_at`.

### 5. Source install flows (no token in the browser)
- **Slack OAuth v2 (our distributed app):**
  - Start: `POST /v1/clients/{c}/connections/slack/install` (`connection.manage`). The body is optional:
    `{"connection_id": "<id>"}` to re-authorize an existing Slack connection of that client.
  - It answers `{"authorize_url": "https://slack.com/oauth/v2/authorize?...", "connection_id": "<id>"}`.
    For a new install the id is that of a `pending` connection created now, so the UI can follow it.
  - The page follows only an `https:` URL on the provider's host.
  - The `state` is single use, expires in 10 min, and is bound to the tenant, the client, the connection
    and the acting session.
  - `GET /v1/oauth/slack/callback` exchanges the code server-side (`oauth.v2.access`), stores the tokens
    through `connection_tokens` (envelope-encrypted), records granted scopes, team id and blind spots,
    and redirects to the connection's page in the UI.
  - Failures: the connection stays `pending` (or keeps its previous tokens on a re-authorization) and the
    redirect carries `?install_error=<code>` from a fixed list.
- **Microsoft Teams admin consent:**
  - Start: `POST /v1/clients/{c}/connections/teams/consent`, with the same body and the same
    `{authorize_url, connection_id}` answer.
  - `GET /v1/oauth/microsoft/callback` takes `admin_consent` and the customer's Entra tenant id, verifies
    `state`, and records the connection with the granted permissions.
  - Graph tokens are obtained later, server-side, by client credentials with a certificate.
- **Common to both:**
  - Redirect URIs are exact and registered per environment.
  - Tokens never appear in a response, a redirect URL, a log, an audit payload or Temporal.
  - Start, success, denial, expiry and replay are audited without values.

### 6. Slack internal-app token (the one exception: a credential typed into the browser)
- **Endpoints:** `POST /v1/clients/{c}/connections/slack/token` with body `{"token": "<xoxb-...>"}`
  creates the connection; `PUT /v1/connections/{id}/token` with the same body replaces its token. Both
  need `connection.manage` and a recent sign-in.
- **Handling:**
  - The value is registered with the log redactor before anything else touches it.
  - It is validated against Slack (`auth.test`; the team id and token type come from there, never from
    the browser), then encrypted on receipt (`connection_tokens`).
  - The plaintext lives only in that request.
- **Write-only:** no endpoint ever returns it or any part of it. The connection shows only
  `token_set_at` and the token type.
- **Audit:** `connection_token_submitted` / `connection_token_replaced` record actor, time, team id,
  token type and scopes, never the value.
- **Never anywhere else:** request logs, error details, custody and audit payloads, and Temporal
  inputs exclude it.

## Consequences
- Plus: no token reaches the browser. Fixation, CSRF, ambiguous credentials and open redirects are
  closed by construction. Revocation is immediate.
- Plus: the frontend's assumed contracts become the API's, so `pending.ts` entries map one to one onto
  generated types when the routes land.
- Minus: every cookie request costs a session row read. It is indexed by the id hash; `last_seen_at`
  writes are throttled.
- Minus: the OIDC and OAuth flows add redirect URIs to register per environment and IdP.

## Tests (M17)
- `return_to`:
  - absolute URLs, `//host`, `/\host`, encoded variants, control characters and over-long values are
    all rejected with 400;
  - a valid path round-trips;
  - nothing is followed before the callback succeeds.
- Re-authentication:
  - `reauth=1` without a session is a plain sign-in;
  - with a session, a different principal or a stale `auth_time` is refused and leaves the session
    untouched;
  - success rotates the id.
- Session lifecycle: idle and absolute expiry, logout, self and admin revocation, deactivation, and
  rotation at every login.
- CSRF: refused without a token, with another session's token or with a foreign Origin; bearer requests
  unaffected. Cookie plus bearer gives 400.
- Install flows: `state` replay, expiry and another session's `state` are refused; denial leaves no
  token; the answer shape is `{authorize_url, connection_id}`.
- Token submission: the existing credential scan finds the submitted canary in no response, log line,
  audit or custody payload.
