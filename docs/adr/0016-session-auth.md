# ADR 0016: Browser sessions, CSRF, re-authentication and source install flows

Status: **Accepted** (2026-10-03), **amended 2026-10-12** (Amendment 1 below: the M17 plan review,
decisions D1, D2, D3, D5, D10, D11) and **2026-10-14** (Amendment 2: D4, D6-D9, D12-D14, the
access-lost bounds, operator-only moves, Slack's terms). Implemented so far: A8 (no error path
echoes a request value: M17 step 0) and §1-§4 with A1 (browser sessions: M17 step 2, notes at the
end), the UI's reads (step 3) and `private_key_jwt` client authentication (step 3b, notes at the end;
real-tenant confirmation pending); the rest is M17 steps 5-8. Builds on ADR 0013 (tenant from the
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
  tenant admin (audited, bounded 24 h..90 days: A2.2). A paused job past it, or on the explicit audited
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

## Amendment 2 (2026-10-14): the remaining M17 decisions, and Slack's terms

Decided by the product owner on 2026-10-14. Where this amendment and the text above disagree, this
amendment wins.

### A2.1 Approved as written in Amendment 1
- The signed state of A3 (`v1.<kid>.<install id>.<expiry>.<mac>`, MAC over install id, expiry,
  tenant, principal and nonce).
- The alert timing of A6 (one alert at the pause, one 24 h before the deadline or at half of it
  when the deadline is under 48 h, each once per pause).
- Slack export uploads stay outside the one-workspace rule of A4 (they carry no authenticated
  workspace id; the report's export section says so).

### A2.2 The access-lost deadline (amends A6)
- The tenant setting `reauth_deadline_hours` is bounded **24 hours .. 90 days** (was 1 h .. 90 days),
  default **7 days** (168 h). A value outside the bounds is refused (422, a fixed text), never
  clamped.

### A2.3 Moving an external id between tenants (amends A4)
- **Operator CLI only; no API route** in any environment. The CLI requires a reason, records the move
  in BOTH tenants' audit streams (the source tenant's event names the destination tenant and the
  reverse, plus the operator and the reason), and raises an alert to the tenant admins of BOTH
  tenants. The source tenant's connections on that id must be revoked first.
- Test: no route exposes it: the OpenAPI document and the app's route table hold no path or
  operation that writes the cross-tenant registry other than the bind of a new install, and the
  registry's definer functions are not reachable from the `edisc_app` role except that bind.

### A2.4 Live updates (D4, D6)
- D4 approved: the SSE source polls (1 s per job per API process while it has subscribers);
  revocation bound 15 s; LISTEN/NOTIFY only if measurements require it. **M17 step 5 measures 500
  concurrent streams** (database queries per second and p99 event latency, on the test stack) and
  records it in `docs/runs/`.
- D6 approved: an open SSE stream never keeps a session alive.

### A2.5 Session numbers (D7)
- Approved: absolute 12 h, idle 30 min, re-auth window 10 min (§2, §4).

### A2.6 Back-channel logout and SCIM (D8)
- BACKLOG, **required before production**: SCIM deprovisioning and OIDC back-channel logout. Until
  then the bound on an IdP-side disablement is the 12 h absolute lifetime.

### A2.7 API compatibility (D9), directory page size (D14)
- D9 approved: a new enum value in a RESPONSE is not breaking (surfaced in review). **The frontend
  must render unknown enum values safely** (a neutral "unknown" presentation, never a crash, never
  a value treated as a known one); recorded in the frontend requirements (HANDOFF).
- D14 approved: directory pages hold at most 500 entries; other lists stay at 200.

### A2.8 Directory names (D12), one Slack workspace in two tenants (D13)
- D12: the plan's default stands (`connection.read`, or `job.start` on a matter of the client),
  pending the mentor (same question as ADR 0018 §17 (b)).
- D13: confirmed **no** (already decided by A4).

### A2.9 Slack's limit on conversation history for non-Marketplace apps
- Since May 2025 Slack limits `conversations.history` for apps distributed outside the Slack
  Marketplace to 1 request per minute; internal customer-built apps and Marketplace apps keep
  Tier 3. The live connector (Phase 3) detects the tier it is on and reports it as a job condition;
  it never crawls silently. Details in `docs/plans/phase-3.md`.

### A2.10 The internal-app token exception (§6) is pending legal review
- Slack's terms treat templated custom apps as commercial distribution, so §6 is **pending legal
  review**. Until it is cleared, M17 step 7 (token submission) is not built, and nothing is built
  that ships customers a ready-made Slack app manifest.

### A2.11 Note: the log-assertion sweep (before M17 step 2)
- Every test asserting on log output was checked to read the production logging pipeline. Four were
  blind: the connection-credential check used `caplog` (structlog lines never reach it); the
  "canary as host" check compared a mixed-case canary with a host the client lowercases; the unit
  422 checks ran an app without the access-line middleware, so the capture held nothing; the KMS
  "redacted from logs" test never logged. `tests/logcapture.py` now fails on an empty capture and
  offers `require()` (a positive control on the very lines under test); the request middleware is
  `edisc_api.app.add_request_context`, shared by the unit app. Mutation round `m17-logs` (7 breaks,
  all caught; 4 of them passed the tests as they were).

## Implementation notes: M17 step 2, browser sessions (2026-10-14)

Built: migration 0036 (`sessions`, `login_attempts`, `tenant_idps.login_client_id`), `edisc_api.sessions`
(primitives), `edisc_api.oidc` (relying party), `edisc_api.routes.auth` (login, callback, logout, CSRF),
the session routes (`GET /v1/me/sessions`, `POST /v1/me/sessions/{id}/revoke`,
`POST /v1/principals/{id}/sessions/revoke`), the cookie path of the caller dependency, and
`require_recent_sign_in` made real on every action of §4. Not built here (later steps): SSE and the
download cut-off (A5, step 5), install flows (step 6), token submission (step 7, on hold: A2.10).

Choices made while building (each one a deviation from, or a detail the plan left open):
1. **The PKCE verifier is not stored.** It is `HMAC(server key, login cookie handle)`; the attempt
   row records the key id. The plan proposed an encrypted column; this way the row holds no secret
   at all and only the browser's handle plus our key rebuild the verifier.
2. **Sign-in is a public OIDC client** (authorization code + PKCE S256, no client secret). *(Built
   in step 3b: `private_key_jwt`, the default; `none` only in local/test/ci.)* Many IdPs
   require client authentication for a web application. Production needs `private_key_jwt` with a
   platform key (no per-tenant secret to store). *(2026-10-14: moved from the BACKLOG into M17 as
   step 3b, before frontend wiring: `docs/plans/m17.md` §1.6.)*
3. **Fetch metadata:** a cookie-authenticated request whose `Sec-Fetch-Site` is present and not
   `same-origin` or `none` is refused (403 `cross_site`), `same-site` included: a sibling tenant
   subdomain is same-site and would otherwise carry the Strict cookie. Absent header (old clients):
   allowed, CSRF still applies to state changes.
4. **One sign-in IdP per tenant** (`login_client_id`, unique per tenant): the login route takes no
   parameter naming an IdP. A tenant without one answers the generic 401.
5. **ID token keys come from the registered `jwks_url`**, never from the discovery document's
   `jwks_uri` or the token. The discovery document's `issuer` must equal the registered issuer; its
   endpoints must be https; redirects are not followed.
6. **Callback failures:** the bounce page to `/?auth_error=<code>` with status 400 (`invalid`,
   `expired`, `denied`), 401 (`unknown_user`, `inactive`) or 403 (`reauth_mismatch`). Every failure
   records the attempt's outcome, is audited `login_failed` (reason + a fixed detail code; actor
   `anonymous` when no principal was resolved) and counts in the per-(host, address)
   authentication-failure throttle. The IdP's `error` is recorded as one of a fixed set of codes
   (else `other`); `error_description` is never read.
7. **Sign-in rotates whatever session the browser held at the START** (the Strict cookie does not
   reach the callback), whoever it belonged to, revoked `rotated` in the session's transaction.
8. **Deactivating a principal needs a recent sign-in** (§2: "all of these ... need a recent
   sign-in") and its audit event lists the sessions it ended.
9. **Time is the database's:** expiry, idle and the once-a-minute activity write are evaluated with
   `now()` in SQL. Expired sessions are not marked; `idle`/`absolute` stay in the reason list for an
   archiving job (BACKLOG) and are never written today. The idle expiry never passes the absolute
   one (a CHECK, and the activity write's `LEAST`), so the absolute term of the lookup is defence in
   depth that no test can observe alone: its mutation entry breaks the CHECK instead.
10. **Passive requests** (`request.state.passive`) authenticate without moving the idle expiry: the
    hook the SSE stream (step 5) uses (A2.4, D6).
11. **Revoking one's own session:** another principal's session id is 404, an already revoked one 409
    `already_revoked`; ending the CURRENT session never needs a recent sign-in.
12. **Host-only, proven in browsers (2026-10-14):** the session cookie is `__Host-edisc_session`
    (`Secure`, `Path=/`, no `Domain`). `test_the_session_cookie_never_reaches_another_subdomain`
    (Chromium, Firefox, WebKit) signs in on a tenant host and shows that a neighbouring tenant's
    subdomain (typed, and by a same-site link) and the parent domain never receive it, while a control
    cookie with `Domain` does; and that a credentialed request from the neighbour's page back to the
    tenant carries the Strict cookie (same-site) and is refused 403 `cross_site` (note 3, the second
    layer). Mutation round `m17-host-cookie` (a domain-wide cookie; `same-site` allowed): both caught.

Tests: `tests/integration/api/test_sessions.py` (74: sign-in, cookie flags, the bounce page, every
callback refusal, `return_to` table, fixation, rotation, re-authentication, cookie + bearer, another
tenant's cookie and state, expiries, the activity write, deactivation, CSRF token / Origin / Referer /
bearer exemption, fetch metadata, logout, own and admin revocation, recent sign-in on every covered
route for bearer `auth_time`, bearer `iat` and sessions, both throttles, three crash points),
`tests/integration/api/test_sessions_browser.py` (A1 in Chromium, Firefox and WebKit over real
HTTPS through a CONNECT proxy, with a control proving the harness withholds the Strict cookie
cross-site), `tests/unit/api/test_session_primitives.py`, `tests/integration/db/test_session_guards.py`
(on a database migrated from scratch in the test, so the migration's triggers and CHECKs are what is
tested). Mutation round `m17-sessions`: 35 breaks, all caught (the first run left three uncaught:
the fixation test planted a Strict cookie, which a browser withholds on the cross-site callback
anyway, now planted as an attacker would (SameSite=None); the absolute-expiry term, see note 9; the
guard tests ran on the already migrated database, so a migration edit changed nothing).

## Implementation notes: M17 step 3, the UI's reads (2026-10-14)

Built (`docs/plans/m17.md` §3.1, §3.2, §3.4, §3.5; migration 0037): `GET /v1/me/permissions`,
`GET /v1/roles`, `GET /v1/jobs`, `GET /v1/jobs/{id}/custody/events`, `GET /v1/groups`,
`GET /v1/groups/{id}/members`, and cursors bound to their request. Choices made while building:
1. **Cursor binding is app-wide.** `edisc_api.pagination.bind_cursors` (a dependency of the whole
   app) digests the route template, the path parameters and every query parameter but `cursor` and
   `limit`; `encode`/`decode` read it from a context variable, so every list route, old and new, is
   bound without passing anything, and a new route cannot forget it. A mismatch is 422
   `cursor_mismatch`; an unreadable cursor stays 422 `unprocessable`. Cursors stay unsigned (§3.2).
   The cursor format changed (`{"p", "f"}`): cursors issued before this release are refused, which
   costs a client one restart of its listing.
2. **The jobs list's visibility is `authorize`'s rule, as a join:** a live role assignment (direct or
   through a group) granting `job.read` at the job's matter or its client, or at the tenant. A role
   at a WORKSPACE makes the matter visible (`visible_ids`) but not its jobs, because `GET
   /v1/jobs/{id}` authorizes at the matter; the list follows the GET, and a test checks for every
   role that the listed jobs are exactly those the GET answers.
3. **Filters:** `status`, `clean_basis` (`source` = `completed`, `archive` =
   `completed_against_archive`; contradicting a `status` filter gives an empty page), `client_id`,
   `matter_id`, `connection_id`, `source`, and `[from, to)` ranges on `created_at` and `finished_at`
   (timezone-aware only: a naive time is 422). A filter naming an invisible client or matter gives an
   empty page, never a 404 (no existence leak through the filter).
4. **Custody events:** keyset on `seq`, `limit` at most 500, a 1 MiB payload budget measured in SQL
   (`octet_length(payload::text)` over the window) before any payload is fetched; a single event over
   the budget is a page on its own. Each event carries `prev_hash` and `event_hash` so a client can
   follow the chain. `custody.read`, authorized at the job's matter.
5. **Groups:** members are listed by membership (a principal removed and re-added has two rows);
   `include_removed=true` adds ended memberships. Both routes need `tenant.admin`, like the rest of
   tenant administration (404/403 per `authorize`).
6. **`/me/permissions`** groups the caller's live assignments by scope (the tenant as scope id null,
   ordered first in its type by the nil UUID), with the roles and `authz.permissions` of them; a
   hint for the UI, never an authorization. **`/roles`** is generated from `ROLE_PERMISSIONS`.
7. **Lists build their rows in three statements per page** (`_jobs_out`), not three per job; the
   per-matter jobs list uses the same code.

Tests: `tests/integration/api/test_ui_reads.py` (17), the access matrix in `test_authz.py` gained
the custody events, job and group routes (the world now has connections, jobs and a group), the
no-echo fuzzer covers the new routes and filters from the OpenAPI document. Mutation round
`m17-reads`: 17 breaks, all caught (the first run left `cursor_route_unbound` uncaught: every
route case also changed a parameter; a case where only the route differs was added).

## Implementation notes: M17 step 3b, `private_key_jwt` (2026-10-15)

Built (`docs/plans/m17.md` §1.6; migration 0038): `edisc_api.client_auth` (`ClientSigner`), the
`KmsSigner` calls on the KMS (`get_public_key`, `sign` over a SHA-256 digest; the local KMS holds RSA
keys, AWS semantics), `tenant_idps.login_client_auth`, the assertion in the code exchange, the
published keys (`edisc_api.routes.wellknown`), the test IdP checking assertions, and a test path for
real tenants. Choices made while building:
1. **`kid` is the RFC 7638 thumbprint** of the public key (never the KMS key handle, which is
   configuration and stays out of every document, response and log). The header also carries
   `x5t` (SHA-1) and `x5t#S256` of our certificate: Entra matches the uploaded certificate by
   thumbprint; RFC 7515 lets an IdP ignore the members it does not use.
2. **The certificate is deterministic** per key and configured validity
   (`EDISC_API_CLIENT_KEYS`: `kms_key_id`, `not_before`, `not_after`, current first): serial from
   SHA-256 of the key, fixed name `CN=edisc client authentication`, fixed extensions
   (BasicConstraints CA false, KeyUsage digitalSignature, both critical; SubjectKeyIdentifier), and a
   PKCS#1 v1.5 signature (deterministic) made by KMS over the TBSCertificate. Every replica and
   restart publishes the same bytes, so the thumbprints in the headers never drift between replicas.
   `cryptography` has no external-signer certificate builder: the TBS is taken from a builder run
   with a throwaway key (same algorithm identifier) and re-signed by KMS; the result is parsed and
   verified as self-signed before use.
3. **Only the first key signs**; the others are published (JWKS, PEM) for a rollover. The runbook:
   add the new key second, wait until every tenant IdP has it (Entra: uploaded certificate; Okta: the
   JWKS URL's cache or an upload), move it first, later remove the old one.
4. **Fail closed in four places:** no client key outside local/test/ci = the API does not start
   (`ClientSigner.from_settings`, eager like the session keys); a key under RSA 3072 is refused;
   `login_client_auth = 'none'` outside local/test/ci = the generic 401 at the start, `login_failed`
   `client_auth_not_permitted`, an alert (and refused again at the callback if it changed during the
   sign-in); a discovery document whose published lists lack `private_key_jwt` / `RS256` = the start
   bounces to `auth_error=invalid` (details `idp_no_private_key_jwt`, `idp_no_rs256`) before the user
   reaches the IdP. A document that publishes no list is used (Entra does not publish the algorithm
   list as far as we know: to confirm).
5. **`invalid_client` is recorded as such:** the callback's `login_failed` detail is
   `exchange_invalid_client` when the IdP refuses our authentication, `exchange_refused` for any
   other token-endpoint refusal; a KMS failure is `client_auth_unavailable` (an error line with the
   exception type only).
6. **`client_id` stays in the token request** with the assertion (RFC 7523 §3.1 allows it; to
   confirm that both IdPs accept it).
7. **Published on the base host only** (`GET /.well-known/edisc-client-jwks.json`, `GET
   /.well-known/edisc-client-cert.pem`, `Cache-Control: public, max-age=300`; a tenant host answers
   404): nothing there varies by tenant.
8. **Key work off the loop:** creating the local dev key (RSA 3072 generation) and building the TBS
   run in threads (`OFF_LOOP`); the API loads its keys at startup (`build_resources`), tests on the
   first sign-in.

Tests: `tests/unit/api/test_client_auth.py` (a key derived from a seed, so no private key is
committed: exact claims and header, deterministic assertion and certificate, the RFC 7638 vector,
rollover order, key size, the start refusal, the KMS's algorithm), `tests/integration/api/
test_client_auth.py` (16: sign-in with assertions checked against the PUBLISHED JWKS, two sign-ins
with fresh `jti`s, PKCE kept, the public client in test, each refused variant (another key, wrong
`aud`, expired, replayed `jti`, no assertion, a client secret) failing the sign-in with no session,
the public client refused in staging with its audit and alert, the callback re-check, the discovery
check, the published documents (public parts only, match the signer, identical from a fresh signer,
base host only), no assertion / key handle / private key material in any response, log line or
audit payload). The browser test of A1 now signs in with `private_key_jwt`. Mutation round
`m17-client-auth`: 27 breaks, all caught (21 of the product, 6 of the test IdP's checks, so the
refused variants are known to be live).

### To confirm on real tenants (Entra ID and Okta developer tenants; not done yet)
Run `make test-real-idp` (`tests/real_idp/test_real_idp.py`: skipped with its reason when
`EDISC_REAL_IDP_ISSUER` is unset; FAILS when it is half-configured; setup in its docstring and
`scripts/real_idp.py`). Record each answer here.

**Entra ID** (app registration: Web platform, redirect URI, certificate uploaded, no secret):
1. A confidential Web app refuses a PKCE-only exchange (expected AADSTS7000218) and accepts ours.
2. Header: RS256 accepted with our `x5t#S256`; is `x5t` (SHA-1) still required or ignored when
   both are sent; is `kid` ignored. (Microsoft documents `x5t#S256` for newer flows and `x5t`
   historically: the reason we send both.)
3. `aud` = the discovery document's `token_endpoint` (`https://login.microsoftonline.com/<tid>/
   oauth2/v2.0/token`) accepted byte for byte.
4. Lifetime: our 5 minutes accepted; is `nbf` required; does Entra refuse a replayed `jti`
   (`test_a_replayed_assertion_is_refused` answers it).
5. Is client authentication checked before the code (the probe test depends on it; otherwise the
   client-credentials leg with `EDISC_REAL_IDP_SCOPE=<client id>/.default` is the proof).
6. Discovery: `token_endpoint_auth_methods_supported` lists `private_key_jwt`; whether
   `token_endpoint_auth_signing_alg_values_supported` is published at all.
7. ID token: `iss` = `https://login.microsoftonline.com/<tid>/v2.0` (equal to the registered
   issuer), `aud` = client id, `nonce` echoed; **`auth_time` is an OPTIONAL claim in v2.0 tokens**
   (re-authentication requires it: confirm it must be added under Token configuration);
   `prompt=login` and `max_age=0` honoured.
8. Groups: the `groups` claim needs configuration; over 200 groups Entra sends an overage
   (`_claim_names`) instead (our sign-in would then see no groups: decide before production).
9. `sub` is pairwise per application (stable for us; `oid` is the directory id): principals are
   keyed by `(issuer, sub)`.
10. The self-signed certificate is accepted at upload (validity window limits, PEM/CER form).

**Okta** (Web app (OIDC), client authentication "Public key / Private key", Require PKCE):
1. A web app with public-key authentication refuses an exchange without an assertion and accepts
   ours; `client_id` alongside the assertion accepted.
2. `aud` = the token endpoint of the authorization server in use (org `https://<org>/oauth2/v1/token`
   vs custom `https://<org>/oauth2/default/v1/token`), byte for byte as discovery gives it.
3. `kid` must match a registered key (ours: the thumbprint); RS256 with a 3072-bit key accepted;
   the `x5t` members ignored.
4. `jti` replay refused (documented); `exp` at most 1 hour (ours 5 minutes).
5. Discovery lists `private_key_jwt` and `RS256` in both lists.
6. ID token: `iss` form for the org vs a custom authorization server (must equal the registered
   issuer); `auth_time` present; `nonce`; `prompt=login` / `max_age=0` honoured.
7. Groups claim: configured per authorization server (filter), name `groups`.
8. The JWKS URL option: how long Okta caches our `/.well-known/edisc-client-jwks.json` (it sets the
   rollover overlap in note 3).

**Settings and inputs per tenant** (onboarding): the issuer exactly as discovery states it, the
client id, `jwks_url` (registered, never discovered at sign-in), `groups_claim`,
`login_client_auth = private_key_jwt`; on the platform side `EDISC_API_CLIENT_KEYS` (the KMS key
and the certificate validity) and, for Okta's URL option, the base host public over https.

## Implementation note: the groups claim is never read as "no groups" (2026-10-15, before step 4)
Found reviewing the real-tenant list (Entra item 8): Entra ID replaces the `groups` claim with an
overage marker (`_claim_names` / `_claim_sources`; `hasgroups` in implicit-flow tokens) above 200
groups, and the sign-in and bearer paths read anything but a list as an EMPTY list, so a user would
silently lose every role granted through IdP groups. Decided by the user: refuse.
- `edisc_api.idp_groups.read` reports `overage`, `absent` or `malformed`. An overage refuses the
  sign-in and a bearer token whatever the tenant's mapping (`groups_overage`). An absent or
  malformed claim refuses them when the tenant's roles DEPEND on groups, i.e. a live role
  assignment held by a group with an `external_id` (`groups_missing`); a present empty list is an
  answer and is accepted.
- Sign-in: the bounce page to `/?auth_error=groups_overage|groups_missing` (403), `login_failed`
  with that reason (migration 0039 adds the two outcomes). Bearer: the generic 401 (ADR 0013: every
  authentication failure looks the same). Both: an `authentication_refused` audit event (reason,
  detail, `via`, issuer, principal) and an alert to the tenant admins, once per principal and reason
  while unacknowledged (`alerts` kind = the reason).
- **Okta to confirm:** a groups claim with a filter is OMITTED when no group matches; a tenant with
  group-based roles must configure the claim so it is always emitted, or its users in no group
  cannot sign in.
- Fetching the groups from Microsoft Graph on an overage is a BACKLOG item.
- Tests: `tests/integration/api/test_idp_groups.py` (overage by `_claim_names` and by `hasgroups`,
  sign-in and bearer, the alert once; missing and malformed with group-based roles, sign-in and
  bearer; present and empty lists accepted; a mapped group without a role does not make groups
  required). The test helper's bearer tokens now carry `groups: []` by default, as a configured
  IdP's would. Mutation round `m17-groups`: 6 breaks, all caught.

## Implementation notes: M17 step 4, the published contract and the compatibility check (2026-10-15)

Built (`docs/plans/m17.md` §5, threat T26): `docs/api/openapi.json` (`make openapi`), the drift
check (`make openapi-check`, and a unit test comparing byte for byte), `scripts/api_compat.py`
(`make api-compat`), `docs/api/breaking-allowlist.yaml`, and the CI job `api-compat`. Choices made
while building:
1. **The document is the production build's, from fixed settings** (`Settings.model_construct(env=
   production)`: no `.env`, no `EDISC_*` variable reaches it; a test changes them and gets the same
   bytes). Sorted keys, two-space indent, final newline, no `servers`. It holds 77 operations today.
   When step 6 removes the generic credential routes from production builds (A7), they leave this
   document: that is a break the allow-list must name.
2. **`operationId` = the route function's name** (`generate_unique_id_function`; FastAPI's default
   embedded the path and method). Generation fails on a duplicate or on an operation without
   `x-permission`. This renamed every operationId once, before any client was generated from them.
3. **oasdiff 1.33.0, pinned by the SHA-256 of each release archive** (darwin, linux amd64),
   downloaded once into `~/.cache/edisc` (`EDISC_TOOL_CACHE`), never taken from PATH; a download
   with another checksum is refused before extraction.
4. **What oasdiff 1.33.0 does not rate ERR, found by one fixture pair per rule of §5**
   (`tests/unit/api/test_api_compat.py`, base `tests/unit/api/data/compat_base.json`): a removed
   non-success response status, a removed OPTIONAL response field, a response enum value removed, a
   changed default (parameter or body), a changed media type, and an operationId change were not
   reported at all. Each is our rule (`edisc-response-status-removed`, `edisc-property-removed`
   (any schema: a client sending a removed request field gets a 422, our models forbid extras),
   `edisc-enum-value-removed`, `edisc-default-changed`, `edisc-media-type-removed`,
   `edisc-operation-id-changed`). The other classes of §5 are oasdiff's own ERR findings, each
   proven by its pair. oasdiff rates a NEW response enum value ERR
   (`response-property-enum-value-added`): D9 makes it a warning (listed in the job summary, not
   failing).
5. **Our §5 rules:** `edisc-x-permission-changed`, `edisc-error-code-removed` (an operation's
   `x-error-codes` list) and `edisc-sse-event-changed` (schemas an operation lists in
   `x-sse-events`). **No route declares `x-error-codes` yet, and no SSE route exists before step 5:**
   those two rules are tested on fixtures but inert on the real document until routes declare the
   lists (step 5 declares its event schemas; documenting each route's error codes is not done).
6. **The allow-list:** each entry needs `rule`, `operation` (operationId or `METHOD /path`; schema
   findings use the schema location), `reason` and `frontend_commit` (a commit id). An entry missing
   any, or matching no finding (stale), fails the check.
7. **The base in CI:** a pull request compares with its merge base; a push with the previous head
   (`github.event.before`). A base without the document (this commit) passes with a printed note.

Mutation round `m17-openapi`: 20 breaks, all caught.

## Implementation note: every route declares its error codes (2026-10-15, before step 5)
- **One registry:** `edisc_api.error_codes.E` (44 codes, one status each, a meaning). Each route
  declares the codes ITS handler can answer in `openapi_extra` (`perm(P.X, E.NOT_FOUND, ...)`,
  `public(...)`, `authenticated(...)`); `complete()` adds the codes implied by the operation's
  shape (every operation: `internal_error`, `db_pool_exhausted`; parameters or a body:
  `validation_error`; a body: `malformed_body`; authenticated: `unauthorized`,
  `too_many_requests`, `ambiguous_credentials`, `cross_site`, and `csrf` on a state change; a
  cursor: `cursor_mismatch`, `unprocessable`) and writes the full `x-error-codes` list and one
  response per error status (`ErrorOut`; 422 keeps FastAPI's validation schema, of which our body
  is a compatible superset) into the published document. The first declarations came from a
  call-graph pass over the handlers (direct raises and helpers, transitively), reviewed by hand.
- **Test-time check** (`tests/integration/api/error_check.py`, on every response of the API test
  client): an error answer of a documented operation must carry a code (JSON `error`, or the
  bounce page's `auth_error`), declared for that operation, with the registry's status. With it on,
  the whole API suite passed after one product fix it found: **FastAPI's own body-parse refusal
  answered 400 `{"detail": ...}` without a code** (the no-echo fuzzer's form bodies); Starlette's
  and FastAPI's refusals now answer `malformed_body` (400), `not_found` (404) and
  `method_not_allowed` (405) through `safe_errors`.
- The compat rule `edisc-error-code-removed` now has real input; a test removes `not_found` from
  `get_job` in the published document and the check reports exactly that.
- Mutation round `m17-error-codes`: 8 breaks, all caught.
