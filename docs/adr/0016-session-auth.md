# ADR 0016: Browser sessions, CSRF, re-authentication and source install flows

Status: **Accepted** (2026-10-03), **amended 2026-10-12** (Amendment 1 below: the M17 plan review,
decisions D1, D2, D3, D5, D10, D11). Not implemented yet (M17), except A8 (no error path echoes a
request value: M17 step 0). Builds on ADR 0013 (tenant from the
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
  `return_to`. *(Amended, A1: a same-site bounce page, not a 303.)*
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
    and the acting session. *(Amended, A2 and A3: signed, bound through a Lax install cookie and the
    session ROW, received by one relay host.)*
  - `GET /v1/oauth/slack/callback` exchanges the code server-side (`oauth.v2.access`), stores the tokens
    through `connection_tokens` (envelope-encrypted), records granted scopes, team id and blind spots,
    and redirects to the connection's page in the UI.
  - Failures: the connection stays `pending` (or keeps its previous tokens on a re-authorization) and the
    redirect carries `?install_error=<code>` from a fixed list.
- **Microsoft Teams admin consent:**
  - Start: `POST /v1/clients/{c}/connections/teams/consent`, with the same body and the same
    `{authorize_url, connection_id}` answer.
  - `GET /v1/oauth/microsoft/callback` takes `admin_consent` and the customer's Entra tenant id, verifies
    `state`, and records the connection with the granted permissions. *(Superseded, A4: the
    callback's `tenant` is never trusted; consent goes through OIDC and the signed ID token's `tid`
    is bound.)*
  - Graph tokens are obtained later, server-side, by client credentials with a certificate.
- **Common to both:**
  - Redirect URIs are exact and registered per environment. *(Amended, A3: one relay URI per
    provider and environment.)*
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

## Amendment 1 (2026-10-12): the M17 plan review

Source: `docs/plans/m17.md` (§11 lists the design changes, §12 the decisions). Approved by the
product owner on 2026-10-12: D1, D2 (extended to Slack), D3, D5 (with alerts and a tenant
setting), D10 (enforced in production builds), D11 (as a class). Where this amendment and the
sections above disagree, this amendment wins. Everything here is built in M17 except A8, which is
M17 step 0 (built).

### A1. Cookie delivery after a cross-site redirect: a same-site bounce page
- The IdP starts the navigation that reaches `GET /v1/auth/callback`, so the browser treats the whole
  redirect chain as cross-site: the `SameSite=Strict` session cookie set by the callback is stored
  but NOT sent on a 303's next hop, and the UI would load signed out.
- The callback therefore answers **200 with a static page**: CSP `default-src 'none'`, no script,
  `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, and a
  `<meta http-equiv="refresh" content="0;url=<return_to>">` that starts a new, same-site navigation.
  `return_to` is the value stored with the login attempt (validated by §1), never one from the
  callback's query.
- Failures go through the same page to `/?auth_error=<code>` (fixed list of §1).
- Test: the full login in Chromium, Firefox and WebKit (Playwright): the first page after the
  bounce is signed in. The rule is browser behaviour, so it is tested in browsers, not asserted.

### A2. Install flows bind the browser with a Lax install cookie and the session ROW
- The provider's redirect back is also a cross-site navigation: the Strict session cookie never
  reaches an install callback. The start route sets `__Host-edisc_install` (httpOnly, Secure,
  SameSite=Lax, `Path=/`, Max-Age 600) holding a random handle; the `oauth_installs` row stores its
  SHA-256.
- The callback requires handle + state + row to match, then re-reads the bound SESSION ROW (not a
  cookie): still live, the same principal, and that principal still holds `connection.manage` on the
  client (a role removed during the 10 minutes ends the install). Any failure: the outcome is
  recorded and audited, nothing is stored, the connection is unchanged.

### A3. One OAuth relay host; the tenant comes only from signed, single-use state (D1)
- Providers redirect to ONE registered URI per provider and environment:
  `https://oauth.<base>/v1/oauth/<provider>/callback` (one per tenant subdomain does not scale, and
  Microsoft limits their number).
- **State** = `v1.<kid>.<install id>.<expiry>.<mac>`: `mac` = HMAC-SHA-256 under a server key
  (keyring with `kid`, `SecretStr`, rotation accepts the previous key for 10 minutes) over the
  install id, the expiry, **our tenant id, the principal id and the install's nonce**. The tenant,
  user and nonce are bound by the MAC but not carried in clear. Lifetime 10 minutes; single use
  (consumed atomically at the tenant host: `UPDATE ... WHERE consumed_at IS NULL AND expires_at >
  now() RETURNING`). PKCE S256 on every authorization-code leg (the verifier stored encrypted with
  the install; Slack: confirm at build that our app accepts PKCE, else the client secret plus the
  bound state are the protection, recorded in this ADR).
- **The relay** exchanges nothing and sets no cookie. It parses the state, rejects a bad format or a
  past expiry without a database read, looks the install up through a SECURITY DEFINER function
  (returns the tenant id, principal id, nonce and tenant subdomain of an UNCONSUMED, UNEXPIRED
  install, nothing else), recomputes the MAC from what the row says, and only then answers 303 to
  `https://<that subdomain>.<base>/v1/oauth/<provider>/callback` with the provider's query unchanged.
  The destination host is built from the row whose binding the MAC proves: **never from a query
  parameter** (`redirect_uri`, `return_to`, `tenant`, `team` and anything else in the query are not
  read by the relay). Every refusal is one generic page, byte-identical whatever the reason
  (no tenant enumeration). `Referrer-Policy: no-referrer` on both hops.
- The tenant host repeats every check (MAC, row, handle cookie, session row, A2) and consumes the
  state; the UI destination after the install is the connection page built server-side.
- Tests: **open redirect** (extra `redirect_uri` / `return_to` / encoded host / `//` values in the
  callback query are never followed; the relay's only destination is the row's tenant host);
  **state replay** (a consumed state: refused at the relay and at the tenant host, audited
  `oauth_install_replayed`); **state from another tenant** (A's valid state presented on B's host:
  refused, nothing stored in either tenant; a state whose MAC was computed for another tenant or
  principal: refused); **expired state** (refused at the relay before any database read, audited at
  the tenant host as `oauth_install_expired` when it gets there); a forged MAC; an unknown install id.

### A4. External tenant and workspace binding (D2, extended to Slack)
- **Microsoft:** consent goes through the authorization-code endpoint with OIDC (`/authorize`,
  `prompt=consent`, our app's `.default` scope, `openid`, PKCE, nonce). The Entra tenant bound is the
  `tid` of the **signed ID token** (signature from Microsoft's JWKS, issuer
  `https://login.microsoftonline.com/<tid>/v2.0`, audience, nonce), and the signed-in user must be an
  administrator of it (`wids` holding the Global or Privileged Role Administrator template id; the
  claim set is confirmed at build). The callback's `tenant` and `admin_consent` parameters are never
  read. The granted application permissions are read from a client-credentials token for that `tid`
  (certificate, `private_key_jwt`); missing ones are a partial grant with blind spots.
- **Slack:** the team id (and the enterprise id for Grid) come from the authenticated
  `oauth.v2.access` response (server-side, our client secret, TLS) for installs, and from `auth.test`
  for an internal-app token (§6); never from the redirect's query or the browser. A
  re-authorization must return the team/enterprise the connection recorded, else `mismatch`, the old
  token kept.
- **One external tenant or workspace per tenant of ours** (supersedes the plan's D13 "allow"): an
  owner-only cross-tenant registry keyed by (provider, external id: Entra `tid`, Slack team id, Slack
  enterprise id), written only through a SECURITY DEFINER function. Binding an id already bound to
  another of our tenants is refused (`external_already_bound`), audited in the requesting tenant, and
  alerts operations. **Moving** an id between our tenants is an audited platform-operator action (the
  `edisc_api.admin` tool, a required reason, an audit event in BOTH tenants' streams; the source
  tenant's connections on it must be revoked first). Slack export uploads carry no authenticated
  workspace id and are not bound by this rule (stated in the export's report section).
- Tests, the cross-customer attack for both providers: customer B starts a flow and presents a
  callback naming A's Entra tenant (query `tenant`) or A's Slack team (query `team`, `enterprise`):
  nothing of A is bound; a provider answer (signed ID token / exchange response) naming an id bound
  to A: refused in B, alert raised, A untouched; a re-authorization returning another team: refused,
  previous token kept.

### A5. Revocation cuts in-flight downloads and streams (D3)
- Every streaming response authenticated by a session re-checks the session row at most every 15 s
  of streaming, at chunk boundaries, through one per-process `SessionWatch`. On revocation, expiry
  (idle included) or principal deactivation the transfer is ABORTED: the connection is reset without
  a terminating chunk (HTTP/1.1) or with `RST_STREAM` (HTTP/2), so the client sees an error, never a
  file that looks complete; a zip never gets its central directory.
- An audit event `download_aborted` (route, object, bytes sent, reason `session_revoked` /
  `session_expired` / `principal_deactivated`) is committed and anchored. Bytes already sent cannot
  be recalled; the audit says how many.
- Bearer-authenticated streams: the principal's active flag is re-checked on the same cadence (an
  IdP's token cannot be revoked by us).
- The SSE job stream follows the same rule (a final `revoked` event, then close).

### A6. Revoked or expired credentials end as `access_lost` units after a deadline (D5; amends ADR 0012)
- Detection is unchanged (`AuthenticationError` pauses every running job on the connection, ADR 0012
  §3), now with a reason class: `credentials_revoked`, `credentials_expired`, `consent_withdrawn`,
  `platform_credential_expired` (OUR certificate: an operations alert, never the customer's fault).
- **Deadline:** a TENANT setting `reauth_deadline_hours`, default **168 (7 days)**, changed by a
  tenant admin (audited, bounded 1 h..90 days). A paused job past it, or on the explicit audited
  action `POST /v1/jobs/{id}/finish-without-access` (`job.resume`, a reason, recent sign-in), closes
  every unit not yet reconciled as **`access_lost`** with the reason class, each unit's fact in the
  chain (`unit_reconciled`, ADR 0018 §28 format), and finishes `completed_with_gaps`. Nothing is
  left unsettled by a revoked token. A re-authorization before the deadline resumes as today.
- **Alerts:** one when a job pauses for re-authorization (per job: which job, which connection,
  the deadline), and one more before the deadline (24 h before it, or at half the deadline when the
  deadline is under 48 h), each raised once per pause; closing the pause (re-authorization,
  finish-without-access, deadline) closes them.
- Each unit closes in its own transaction with its chain event; a crash mid-way resumes and closes
  the rest; the final event comes only after all.

### A7. The generic credential endpoint exists only where the dummy does (D10)
- `POST /v1/clients/{c}/connections` with `credentials` and `POST /v1/connections/{id}/reauth` are
  registered ONLY when the dummy connector is permitted (local/test/ci, `dummy_permitted`), like the
  demo mode: in every other environment the routes do not exist (404, absent from the OpenAPI
  document). Real sources connect only through A3/A4 installs or §6 token submission.
- Checked in the `production-install` CI job: the API started from the production install with a
  production environment has neither route in its OpenAPI and answers 404 to both.

### A8. No error path echoes a request value (D11; built in M17 step 0)
- Found while planning M17: FastAPI's default 422 body carries each error's `input` (and `ctx`), so
  a credential in a misplaced field or of the wrong type came back in the response.
- **Rule:** an error answer and a log line never contain a value the request carried (body, query,
  path, headers, cookies). Error details are fixed texts or built from server-side state (statuses,
  limits, counts). The one exception is `X-Request-Id` (a client's correlation id, 128 characters of
  `[A-Za-z0-9_-]`), whose purpose is to be echoed.
- **Validation** (`edisc_api.safe_errors`): `{"error": "validation_error", "detail": [{type, loc,
  msg}]}`. Never `input`, `ctx` or `url`. A `loc` element the client chose (an extra key, a dict key)
  becomes `*`: only names our request models and parameters declare, list indexes and the location
  kind remain. Pydantic's message is kept only for types whose message comes from the schema
  (constraints, expected literals); anything else gets a fixed text (`uuid_parsing` and
  `value_error` quote input). Our validators raise `PydanticCustomError` with a type prefixed
  `edisc_` and a static message.
- **500s:** the outermost middleware turns an unhandled exception into `{"error":
  "internal_error", "detail": "internal error", "request_id"}` and logs the exception TYPES and
  frames (file, line, function), never its text. After the first byte it logs the same way and
  re-raises, so the transfer breaks (those errors are integrity failures of stored objects).
- **Log lines:** the request's path with every non-static segment replaced by `*`, never the query
  string; uvicorn's access log (the raw request line) is switched off at import of
  `edisc_api.main`, its error log goes through our redacting formatter, and the API writes its own
  access line (method, route template, status, duration). Database engines use
  `hide_parameters=True`: a statement error's text never carries its parameters (they reach logs,
  unit errors recorded in the chain and API answers); the SQLSTATE stays on the error.
- **Handler refusals** that quoted the request were changed: unknown source, unknown role, an
  unknown render or preview time zone, preview `not_in_scope` / `page_not_found` / `job_not_sealed`
  (the audit keeps the full detail; the answer is fixed per code), an export's declared size.
- **Workflow failure texts surfaced by the API** (unit `last_error`, render and report
  `reason`/`detail`, episode details, export rejections) pass the log redactor on the way out
  (`RecordedText`, `RecordedDetail`): registered secrets and credential-shaped values never leave in
  an answer. They are written by workers from source and system errors; request values reach
  workers only after validation (ids, dates, options), and statement errors no longer carry
  parameters.
- **Tests:** `tests/integration/api/test_no_echo.py` generates, from the app's own OpenAPI document,
  a canary placement for every route: each path, query and header parameter, every body field at
  every depth in wrong types, lists, objects and over-long values, extra keys and keys that ARE the
  canary, whole/broken/non-JSON/invalid-UTF-8 bodies, undeclared headers the routes read themselves;
  plus the canary as the bearer token and as the tenant host, and the handler refusals above with
  otherwise valid requests. It asserts the canary is in no response body or header and no line of
  the production logging setup (`tests/logcapture.py`), that every wrong shape is refused (no
  2xx, no 500), and that validation was reached (not stopped at authentication).
  `tests/unit/api/test_safe_errors.py` covers the sanitiser, the 500 path, a mid-stream error and
  the uvicorn loggers; `tests/integration/db/test_statement_errors.py` the hidden parameters; the
  production-install job sends a canary in the path, query and token to the real uvicorn process and
  asserts it is absent from the process's output while the API's own access line is present.
  Mutation round `m17-d11` (17 breaks, all caught).

### Consequences of the amendment
- Plus: no tenant, workspace or Entra tenant is ever taken from a browser-controlled value; one
  registered redirect URI per provider and environment.
- Plus: revocation reaches transfers already under way; a revoked token ends in a settled job with
  reasons, never an open-ended pause.
- Minus: the relay host is one more public endpoint (stateless, no cookies, generic answers); a
  second server key (state MAC) to rotate.
- Minus: no customer can bind a workspace or Entra tenant that another customer of ours already
  holds without an operator; legitimate moves need that operator step.
