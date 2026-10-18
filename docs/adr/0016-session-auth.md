# ADR 0016: Browser sessions, CSRF, re-authentication and source install flows

Status: **Accepted** (2026-10-03), **amended 2026-10-12** (Amendment 1 below: the M17 plan review,
decisions D1, D2, D3, D5, D10, D11) and **2026-10-14** (Amendment 2: D4, D6-D9, D12-D14, the
access-lost bounds, operator-only moves, Slack's terms). Implemented so far: A8 (no error path
echoes a request value: M17 step 0) and §1-§4 with A1 (browser sessions: M17 step 2, notes at the
end), the UI's reads (step 3) and `private_key_jwt` client authentication (step 3b, notes at the end;
real-tenant confirmation pending), live job streams and the download cut-off (steps 5, 5b), install
flows (step 6), access lost (step 8, A6) and, closing M17 (2026-10-18), the directory snapshots,
the SSE `connection` event and the report's pause endings (notes at the end). Step 7 (A2.10) is on
hold. Builds on ADR 0013 (tenant from the
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

## Implementation notes: M17 step 5, a job's live stream (2026-10-15)

Built (`docs/plans/m17.md` §4; A2.4, A5 for streams): `GET /v1/jobs/{id}/stream`
(`edisc_api.stream`, `edisc_api.stream_events`), `SessionWatch`, settings `EDISC_API_STREAM_*`.
**Not built here: the cut-off of in-flight DOWNLOADS on revocation (§1.5)**, step 5b. Choices:
1. **Events and ids.** `job` (a snapshot: status, clean, clean basis, sealed, paused, units by
   status and recon status), `units` (units the chain recorded as settled, `unit_reconciled` /
   `unit_failed`, each with its chain `seq`, at most 500 per event), `end` (the last event, with a
   reason: `sealed`, `session_revoked`, `session_expired`, `principal_deactivated`,
   `permission_lost`, `slow_consumer`, `lifetime`). Ids follow the job's custody chain, which is
   ordered by commit for one job: a `units` event's id is its last entry's `seq`, a snapshot's is
   `<head>.<digest>`. The plan's "a fresh snapshot, nothing replayed" could MISS settled units
   across a reconnect; the user required none missed or repeated, hence chain ids.
2. **Reconnection:** `Last-Event-ID` = `<seq>` or `<head>.<digest>` (anything else: a fresh start).
   The server sends the units settled after that `seq` and the snapshot only if it differs from the
   client's last. Each subscriber subscribes and catches up under its feed's lock (polls hold it
   too), and trims `units` entries at or below its last `seq`: **found by the reconnect test**, a
   surviving feed's event covered chain events since the FEED's head and partly overlapped a newer
   subscriber's catch-up (units were sent twice); per-entry `seq` fixed it; a deterministic test
   keeps a feed behind on purpose.
3. **One feed per job per process**, polling one statement (status, sealed flag, chain head) per
   `api_stream_poll_seconds` (1 s) while it has subscribers; only a change reads the units and
   counts. **Found by an intermittent hang:** sealing moves neither the status nor the chain head;
   a poll between `job_finished` and the seal never saw it and the stream never ended. The sealed
   flag is part of the compared state (deterministic test + mutation). A unit's progress that writes
   no chain event shows in the next snapshot.
4. **Continuous authorization (D3):** at most `api_stream_recheck_seconds` (<= 15) apart, the
   session row by the database clock (revoked, idle or absolute expiry) or, for bearer callers, the
   principal's active flag (`SessionWatch`, cached per session or principal across its streams),
   then `job.read` on the job (`authorize`, per stream). The 15 s bound is tested at its production
   value. **D6:** the stream authenticates passively (`request.state.passive`), so it never moves
   the session's activity; idle and absolute limits end it (`session_expired`). Maximum lifetime
   `api_stream_max_seconds` (30 min, `lifetime`); 10 streams per principal per process (429
   `too_many_streams`).
5. **Backpressure:** each subscriber's queue holds `api_stream_queue_events` (64); the producer never
   waits for a client: a full queue marks the subscriber overflowed and drops it from the feed (no
   more buffering), and its writer ends with `slow_consumer` as soon as it can write.
6. **No evidence content:** the module runs only the statements in `stream.STATEMENTS` (job status,
   chain seq/event type/unit key, unit status columns, open pauses, session and principal flags) and
   imports nothing that reads evidence (unit tests parse both). End to end, a real dummy collection
   is streamed to its seal and no string of its normalized items (message text, file, conversation
   and user names) or custody payloads (hashes, roots) appears in the stream's bytes.
7. **The SSE compat rule has real input:** the operation lists `EndEvent`, `EndReason`, `JobEvent`,
   `UnitState`, `UnitsEvent` in `x-sse-events`; any change to them fails `api_compat` (stricter than
   D9 for responses: a client switches on these). A unit test changes each in the published
   document and gets `edisc-sse-event-changed` for `stream_job`.
8. **Found while building:** the local KMS loaded the RSA 3072 private key on the event loop at the
   first sign-in of a process (382 ms under CPU contention; the loop guard failed the revocation
   test): `LocalKmsClient._signing_key` now runs in a thread (`OFF_LOOP`). The 500-stream
   measurement found the API's Redis pool unbounded-then-failing (`MaxConnectionsError` as a 500):
   now a fixed, blocking pool and 503 `service_busy` (docs/runs/2026-10-15-sse-500-streams.md).
9. **A disconnect never leaks** (found by a statement-counting test elsewhere picking up a feed
    that polled for ever): a client gone while its catch-up ran left its subscriber registered, and
    the cancellation could interrupt a transaction (pooled connection unreturned). The stream's
    database work runs shielded from the disconnect (`stream.shielded`); a join that completes after
    the client left unsubscribes itself, and a feed that never started is forgotten
    (`JobFeeds.release`). Tested with the catch-up held inside its transaction while the client
    disconnects: no feed, no checked-out connection afterwards.
10. **Proxies:** `Cache-Control: no-store, no-transform` and `X-Accel-Buffering: no` (no-store, not
   the plan's no-cache: cookie responses are no-store by rule). Not done: the plan's real-uvicorn
   first-event test (the ASGI test client here streams chunk by chunk).

Tests: `tests/integration/api/test_job_stream.py` (17), `tests/unit/api/test_stream_module.py` (4),
the Redis pool test. Measurement: docs/runs/2026-10-15-sse-500-streams.md. Mutation round
`m17-stream`: 16 breaks, all caught (the first run left the per-entry trim uncaught: it depended on
timing; the deterministic overlap test was added).

## Implementation notes: the step-5 fixes (2026-10-16, before 5b)

1. **The replay is bounded.** A subscriber's catch-up sends at most `EDISC_SSE_REPLAY_MAX` (10,000)
   settled-unit events; for a reconnect, never one older than `EDISC_SSE_REPLAY_MAX_AGE_SECONDS`
   (1 h, measured on the OLDEST unit event the replay would send, by the database clock: a quiet job
   whose last event is old still replays nothing old); a `Last-Event-ID` past the chain head is
   refused the same way. Beyond, ONE `reset` event (`id` = the head, `reason` `replay_limit` /
   `replay_age` / `unknown_position`), the snapshot, then live events: the client reloads the job and
   its units from the list endpoints and keeps the stream. One row past the cap is read
   (`LIMIT cap + 1`), so "exactly at" and "one over" differ without counting the rest. A fresh start
   (no `Last-Event-ID`) has the count cap but not the age. `ResetEvent` / `ResetReason` are in
   `x-sse-events` (adding them is not breaking; changing them is). Tested at the cap, one over, the
   fresh start, the age (old unit vs a newer one), past the head.
2. **The 15 s bound had a hole (found while designing 5b).** `SessionWatch` cached an answer for the
   whole bound and each stream asked once per bound, so a second stream of one session could use an
   answer read just before a revocation and then wait a full period: up to 30 s. Now the bound is
   the sum of two halves: every stream (and download) asks every `interval` = bound / 2, and a
   cached answer is at most `interval` old. `job.read` is still asked once per bound (never cached).
   Tested on both halves (the watch's answer age, the stream's cadence via its keepalives).
3. **422 bodies are published as sent** (`edisc_api.error_codes.complete`): `ValidationErrorOut`
   (`error` = `validation_error`, `detail`: `ValidationIssue` {type, loc, msg}, nothing else) where a
   422 can only be a validation error, `UnprocessableOut` (detail: the issues or a text) where it can
   also be `cursor_mismatch` / `unprocessable` (open to extra server-state fields, like `ErrorOut`: the export routes' `digest_mismatch` carries the declared and received hashes; found by the new check). The test client validates every error body of every
   API test against its documented schema (and validation errors against `ValidationErrorOut`
   itself); `test_error_bodies.py` validates real bodies against the COMMITTED document. The change
   on 34 list operations (`detail` widened by `string`) is one allow-list entry, "pre-client": the
   frontend (`aeee68e`) has no code reading the 422 type. Allow-list entries may now name
   `operations` (a list; each must match, or the entry fails as stale).
4. **Proxies:** `Cache-Control: no-cache, no-store, no-transform` (no-cache as the plan asked;
   no-store stays: every cookie response is no-store) and `X-Accel-Buffering: no`. The plan's
   real-server test is built (`test_stream_uvicorn.py`): `create_app` (so whatever middleware
   production adds) served by uvicorn over TCP, read chunk by chunk, plus a case behind Starlette's
   `GZipMiddleware`: the first event within 5 s. Why 5 s: with production settings and an unchanged
   job the next write after the catch-up is the first keepalive at 7.5 s, so a layer that buffers
   until the next write or the end exceeds the bound; the honest path costs tens of milliseconds.
   Production has no compression middleware; Starlette's excludes `text/event-stream` (a compressor
   without that exclusion is the mutation the test catches).

## Implementation notes: M17 step 5b, in-flight downloads end with the session (2026-10-16)

Built (plan §1.5, A5, D3): `edisc_api.downloads.guarded` wraps the body of every content route:
evidence content, render file, native, render package, job package, report file, report package,
preview (a unit test finds content routes as the routes that anchor a read, and fails one that is
not guarded or has no cut test).
1. **Re-checked at piece boundaries**, never inside a write, at most `SessionWatch.interval` apart
   (the same shared watch and two halves as the streams: revocation to cut <= 15 s of streaming):
   the session row for cookie callers (revoked, idle or absolute expiry), the principal's active
   flag for bearer callers. Bodies are re-cut into pieces of at most `EDISC_API_DOWNLOAD_CHUNK_BYTES`
   (64 KiB), so a large chunk or a whole preview page still has boundaries.
2. **The cut is an error, never a shorter file.** Every content route now declares an exact
   `Content-Length` (evidence content, the render file and the preview did not); the guard raises
   `DownloadCut` (a `safe_errors.ResponseCut`: no 500 line) after the response started, so the
   server closes the connection short of it. Proven through a real uvicorn and HTTP client
   (`httpx.RemoteProtocolError`, fewer bytes than declared). A cut package has no central
   directory: not a zip (tested for the render, job and report packages).
3. **Audited:** `download_aborted` (operationId, what was served, `bytes_sent` = exactly the bytes
   handed to the server, `size`, reason `session_revoked` / `session_expired` /
   `principal_deactivated`, `read_seq` of the read) committed and anchored before the cut. Bytes
   already sent cannot be recalled; the audit says how many.
4. **What is not covered:** a client that stops reading holds the server in its write; nothing more
   leaves until it reads again, and then the next boundary cuts. A download that completes within
   the bound after a revocation completes (the bound, not an instant cut, is the promise).
   `job.read`-style permission loss is not re-checked on downloads (A5 names the session and the
   principal only); the read was authorized and audited at its start.
Tests: a cut per route (`cut.py`: the server's second write held, the session revoked, one
interval, released), a bearer principal deactivated mid-native, a steady reader at the production
15 s bound (no piece later than 15 s after the revocation), the real-server client view, full
downloads equal to their Content-Length. Round `m17-cutoff`: 10 breaks, all caught (one first
pointed at a test where it was equivalent: a package's zip chunks are smaller than a piece).

## Implementation notes: the fixes before step 6 (2026-10-16)

1. **Stalled readers are cut.** Every write of a content download AND of the job stream is timed
   (`downloads.GuardedResponse`): one not completed after `EDISC_DOWNLOAD_STALL_SECONDS` (60 s) cuts
   the connection, audited `download_aborted` reason `stalled` (the stream: route `stream_job`,
   no size, no read). Why 60 s: a write blocks only once the client's receive window and our send
   buffer are full, i.e. it read nothing for that long; TCP's own retransmission backoff gives up
   on an unreachable peer after about a minute; browser and network hiccups last seconds.
2. **Found: Starlette's `BaseHTTPMiddleware` (our request-context middleware) buffered every
   streamed body** through its own task: a route's write "completed" into that buffer, so
   `bytes_sent` counted one write the client had not received, and a cut raised in the route never
   reached the server (the middleware's forwarding task stayed blocked on the stalled client: in
   production a stalled download would never have been closed). The middleware is now plain ASGI
   (`app.RequestContext`, same behaviour); a unit test refuses `BaseHTTPMiddleware`.
3. **Found: a stalled download held a pooled S3 connection.** `EvidenceWriter.open` and the
   packages' object members kept a GetObject body open across every write; 50 stalled clients
   (the S3 client's pool) would have starved every other download and the S3 anchor every content
   read writes. Both now read pinned versions range by range (`worm.read_ranges`: each range read
   whole and its connection returned before it is yielded; the end is the object's real end, so
   "more bytes than recorded" is still seen). Database work while streaming is one short
   transaction per page, never across a write; Redis is not used. Tested with 40 stalled natives,
   10 render packages stalled inside their object members and 10 stalled streams: no database
   connection checked out, no Redis connection in use, no S3 connection lent out, and another
   client downloads the native whole and reads its permissions (200, well within the bound).
4. **Permission loss cuts downloads** (`permission_lost`): at the same 7.5 s boundary, after the
   session, each content route's own read authorization on its object's matter is asked again
   (`still_permitted`, never cached). Tested on all eight routes (a bearer principal whose role is
   revoked), with the revocation and the stall cases (`cut.cut_every_way`).
5. **CI:** branches and pull requests still cancel older runs; on main every commit has its own
   concurrency group and nothing is cancelled (GitHub cancels a PENDING run of a group even
   without `cancel-in-progress`). `tests/unit/test_ci_workflow.py` evaluates both expressions.
Round `m17-pre6`: 8 breaks, all caught (the S3 one for packages only after the test stalled the
packages inside their object members, not at their first write).
6. **CI run 38894640908 (shard 2) failed `test_stalled_clients...`: diagnosed as a TEST bug.** It
   sampled the database pool's checked-out count at one instant while the 10 stalled job streams'
   feed polled the database once a second (and their session checks ran): the count caught a poll
   in flight. Every checkout during the stall window was traced to its task: only `JobFeed.run`,
   `SessionWatch.check` and the stream's own re-check, all short transactions, none across a write.
   The check now asserts what is claimed: every connection out when the stall is established is
   back after three poll cycles (pool checkout/checkin events, per checkout); 3/3 under CPU
   contention. The same diagnosis found the package half of the S3 check VACUOUS: the earlier
   "caught" came from the racy database sample. A member read in one S3 read (<= 1 MiB) is read
   whole, its connection returned, before it is written, so stalling a package anywhere but
   INSIDE a member larger than one read proves nothing; the test now stalls packages there (the
   offset from the package's own zip directory) and the S3 assertion itself catches the break.
7. **CI run 38897021941 (shard 2) failed the replay test: a PRODUCT bug in step 5.** The feed's
   poll read the chain head, then (READ COMMITTED: each statement sees later commits) the unit
   counts after another unit had committed: a snapshot named "head 5" showing unit 6, so the
   client got a `job` before unit 6's `units` (and a snapshot id that did not name its content).
   The artifact's chain showed it: the job's settles came within 33 ms of its start, the next one
   during a poll. The catch-up and the poll now read in ONE snapshot (`tenant_tx(...,
   repeatable_read=True)`); a deterministic test commits a unit between the poll's head read and
   its counts (`test_a_snapshot_is_the_state_at_its_head...`), broken twice in the catalog.

## Implementation notes: M17 step 6, source install flows (2026-10-16)

Built (plan §2.1-§2.5; A2, A3, A4, A7, A2.3): migration 0040 (`oauth_installs`, the cross-tenant
`external_bindings` registry, `bind_external`, `oauth_install_route`), `edisc_api.installs`,
`edisc_api.routes.installs`, `edisc_connector_slack.scopes`, `edisc_connector_teams.permissions`,
`connection_tokens.install_tokens`, `edisc_api.admin move-binding`. Not built here: §2.6 (pause
deadline, `check-connections`, finish-without-access: step 8). Choices:
1. **State** exactly as A3: `v1.<kid>.<install id>.<expiry>.<mac>`, MAC under a key derived from
   the API keyring (`Keyring.install_mac`, domain-separated from CSRF and sign-in), over the install
   id, expiry, our tenant, the principal and the nonce. The PKCE verifier is DERIVED from the install
   cookie's handle (as for sign-in, step-2 note), so `oauth_installs` holds no secret. 10 minutes,
   consumed atomically at the tenant host (`UPDATE ... WHERE consumed_at IS NULL`) before anything
   else; the guard trigger makes identity, consume and outcome final.
2. **The relay** (`oauth.<base>`, reserved: onboarding refuses that subdomain) checks format, key
   and expiry without a database read (tested: zero statements for an expired state), looks the
   install up through `oauth_install_route` (unconsumed, unexpired), recomputes the MAC from the
   ROW's tenant, principal and nonce, and answers 303 to the row's tenant host with the query
   unchanged. It reads nothing else from the query. Every refusal: one byte-identical 400 page.
   `Referrer-Policy: no-referrer` on both hops.
3. **The tenant host** repeats every check: the MAC against THIS host's tenant (another tenant's
   state finds no row under RLS), the consume, the Lax `__Host-edisc_install` cookie's handle, the
   bound SESSION ROW (live, same principal; `auth.caller_of_session_row`) and `connection.manage`
   on the client, then the provider's leg. Outcomes go back as the A1 bounce page to
   `/clients/<c>/connections/<id>?install=ok` or `?install_error=` one of `denied`, `expired`,
   `replayed`, `invalid`, `mismatch`, `already_bound`, `failed`, `refused`. Starting needs a
   browser session (bearer: 400 `not_a_session`): the install is bound to the session row.
4. **External ids only from authenticated answers.** Slack: team and enterprise from
   `oauth.v2.access` (our secret, TLS, through `call_with_limits`, bucket `slack.oauth.v2.access`).
   Microsoft: the `tid` of the ID token verified against Microsoft's keys, with the issuer
   `https://login.microsoftonline.com/<tid>/v2.0` checked against that `tid` (multi-tenant), the
   audience, nonce, and `wids` holding Global or Privileged Role Administrator; then a
   client-credentials token for that `tid` (our `private_key_jwt` assertion), whose `roles` are the
   granted permissions (missing ones: blind spots). That token's claims are read without verifying
   its signature: it is Graph's token, received over TLS from Microsoft's token endpoint. No Graph
   token is stored (client credentials on demand, ADR 0009).
5. **One external id per tenant of ours:** `external_bindings` (owner-only; the app role has no
   privilege on it) written only by `bind_external`, which binds to the CALLER's tenant
   (`app.tenant_id`, never a parameter) and returns the holder. Team and enterprise bind in ONE
   transaction (all or none). Held by another tenant: `already_bound`, audited in the requesting
   tenant, an alert to its admins (`external_already_bound`) and an operations error line; the
   other tenant is never named. A re-authorization returning another team/enterprise/tid:
   `mismatch`, the old token kept. **Moves** (A2.3): `python -m edisc_api.admin move-binding`
   (owner credentials, a reason; the source's connections on it must be revoked first; audited in
   both tenants naming each other; alerts in both). A test proves no route of any environment
   writes the registry and that `bind_external` is the only function `edisc_app` can execute on it.
6. **Tokens:** registered with the redactor the moment the provider's body is parsed; journaled
   encrypted in their own transaction (`token_refresh_journal`, as refreshes), then applied with
   the connection's identity, status and the install's outcome in one transaction. A crash in
   between is repaired by `reconcile_token_refreshes` at worker start (tested with the crash hook):
   the token is applied; the connection stays `pending` until an install completes it.
7. **D10 (A7):** `create_connection` and `reauthorize` are on `connections.credential_router`,
   registered only where `dummy_permitted` (local/test/ci). Gone from the published contract: two
   allow-list entries. **The frontend at `aeee68e` still calls both from its live client** (it must
   move to the install routes before production); recorded in HANDOFF. Checked in a unit test and in
   the production-install job (the production app's document, 404 on reauth, 405 on the list path).
8. **To confirm against the real apps** (with the real-tenant work of step 3b): whether our Slack
   app accepts PKCE (`EDISC_SLACK_PKCE`, off until confirmed: the client secret and the bound state
   protect the code meanwhile); that Entra puts `wids` in the ID token of this flow (it may need
   the optional claim configured on the app registration); the exact Graph app roles we request
   (`edisc_connector_teams.permissions`).
Tests: `tests/integration/api/test_installs.py` (19: both happy paths with tokens absent from logs,
audit payloads and pages; open redirect; replayed, other-tenant, expired, forged and unknown
states; cookie, session and permission re-checks; denial; the cross-customer attacks for Microsoft
and Slack; re-authorization mismatch; the registry's privileges; no move route; the operator move;
the reserved subdomain; the journal crash), `providers.py` (fake Slack and Microsoft on the
production URLs), the RLS seed, `tests/unit/api/test_credential_routes.py`. Round `m17-installs`:
16 breaks, all caught.

## Implementation notes: the fixes to M17 step 6 (2026-10-17)

1. **A break the frontend has not absorbed is `pending_frontend`, never a commit id.** The two D10
   allow-list entries named `aeee68e`, a frontend commit that does NOT absorb the change (it still
   calls both routes): the rule was satisfied while its purpose was defeated. An entry now names
   EITHER `frontend_commit` (a commit that absorbed it) OR `status: pending_frontend` + `tracking`
   (`<file>.md#<anchor>`; the file must hold `<!-- pending-frontend: <anchor> -->` at the
   requirement: `docs/HANDOFF.md#install-routes`). Both forms, mixing them, another status or a
   tracking that names nothing are refused. A pending entry is a standing record: it outlives the
   change it allowed (the next diff no longer shows the break; an absorbed entry matching nothing
   still fails as stale), and the compat summary lists it. **Production readiness**
   (`scripts/production_readiness.py`, `make production-readiness`) fails while any pending entry
   exists: the CI job `production-readiness` (red while the frontend has not caught up; it blocks
   the run on release tags `v*`, on main and branches it is `continue-on-error` so the other results
   stay readable) and the report image's production target, whose `readiness` stage runs the
   check (`report` copies its marker; `runtime` is the same image without the gate, for the demo;
   `report-test` derives from `runtime`). Found on the way: the step-6 entries had already gone
   stale against their own merge (CI run 38907553613's api-compat failure on `8172a9a`); pending
   entries no longer do.
2. **Microsoft consent never depends on `wids`.** Entra emits `wids` only when the app
   registration's optional claims ask for it. The gate is now Microsoft's admin consent itself: a
   client-credentials token for the ID token's `tid` (our assertion), whose claims must name that
   `tid`, OUR app (`appid`/`azp`) and at least one application role (only an administrator's
   consent grants application permissions). No token (Entra answers `unauthorized_client` for an
   app with no service principal there), another tenant or app, or no roles: `refused`
   (`consent_token_refused`, `consent_token_tenant`, `consent_token_app`, `admin_consent_missing`),
   nothing bound, the connection pending. `wids`, when present, must be a list of strings naming an
   administrator role (`wids_malformed`, `not_tenant_admin`: a contradiction refuses even with
   consent); when absent, nothing is skipped (the consent token is required either way) and
   `oauth_install_completed.admin_evidence` records `consent_token` (else `consent_token+wids`).
   Microsoft not answering stays `failed` (`exchange_unreachable`), not a refusal. To confirm on
   the real tenant: the `appid` claim of a Graph app-only token (v1 tokens carry it).
3. **The token journal holds a token only until it is applied** (migration 0041). An entry is
   deleted in the transaction that applies or supersedes it (`_close_journal`: resolve, then
   delete), for refreshes and installs; the app role may delete, a trigger refuses deleting an
   entry still `received`, TRUNCATE stays refused (`test_app_role.DELETES_RESOLVED` is the one
   documented exception to "the app never deletes"). Both token columns must be a sealed
   `SecretBox` envelope (`is_sealed_secret`, a CHECK: plaintext is refused by the database); the
   blobs are sealed with the connection's own context (tenant + connection + purpose), so the apply
   copies them unopened. **An install's entry carries its completion facts** (`install`: install,
   connection, principal, request, the connection's identity and grant, the audit payload; no
   secret, `edisc_db.install_completion.InstallFacts`), and the reconciler
   (`edisc_worker.install_recovery.reconcile_tokens`, worker start and the 10-minute schedule)
   finishes the install in the same transaction: connection active, outcome `completed`,
   `audit.oauth_install_completed` with `recovered: true` (actor `system:token-reconciler`, the
   principal in the payload), a re-authorization's paused jobs resumed (`resume_paused_jobs`,
   shared with `Pipeline.resume_connection`); a superseded entry ends the install `failed`
   (`recovery_superseded`). Exactly once: the entry is deleted with the apply under the connection
   row lock, so a second reconciler racing it, or a rerun, finds nothing (tested: two at once plus
   a rerun, for a new install and a re-authorization). An install entry met without a finisher
   raises (`UnfinishedInstallError`): never applied half. Before: the tokens were applied but the
   connection stayed `pending` and the install had no outcome; applied entries stayed for ever.

Tests: `tests/unit/api/test_api_compat.py` (pending entries), `tests/unit/test_production_readiness.py`
(the gate, a file run, the image stages, the CI job), `tests/integration/api/test_installs.py`
(consent with and without `wids`, without consent with and without `wids`, four consent tokens
that prove nothing, `wids` contradicting, no journal entry after an install, the crash repaired
exactly once for a new install and a re-authorization),
`tests/integration/secrets/test_connection_tokens.py` (no entry survives a refresh; sealed for the
connection, not another; plaintext and deleting a received entry refused by the database). Round
`m17-step6-fixes`: 16 breaks, all caught; `microsoft_admin_unchecked` (round `m17-installs`) is
superseded by `wids_non_admin_accepted` (its old test's non-admin case has no consent, which the gate
now refuses on its own).

## Implementation notes: M17 step 8, access lost (2026-10-17)

Built (plan §2.6, §8 last row; A6, A2.1, A2.2; migration 0042): the pause deadline, its alerts,
`access_lost` closing in the chain, the audited finish-without-access action and
`check-connections`. **Not built in this step:** the directory snapshots of plan §3.3 (left for a
later step by the user's scope for step 8), the SSE `connection` event (a new event schema changes
`x-sse-events`; the job snapshot already says `paused`), and the report stating HOW a pause ended
(below, item 7). Choices:
1. **Reason classes** (`edisc_connectors_base.types`): `AuthenticationError(reason, reason_class)`
   carries one of `credentials_revoked`, `credentials_expired`, `consent_withdrawn`,
   `platform_credential_expired`, derived from the provider's code when not given
   (`auth_reason_class`: Slack `token_expired`, Entra `AADSTS...` / OAuth `error` codes; anything
   else is a revocation). The class crosses Temporal inside the error text (`(class)`), so the
   workflow code is unchanged (no patch): `pause_connection` reads it back (`reason_class_in`).
   `platform_credential_expired` also logs an error line for operations (our key, not the
   customer's).
2. **The pause** (`job_pauses`): reason class, `deadline_at` = pause + the tenant's
   `reauth_deadline_hours` (fixed when the pause starts; a later change of the setting applies to
   later pauses), `warn_at` = 24 h before the deadline, or half-way when the deadline is under 48 h
   (A2.1); `job_paused` in the chain carries the class and the deadline. One alert per job
   (`job_paused`, naming the job and the deadline) besides the connection's `reauth_required`
   (now also raised when the connection turns `reauth_required` with no job running, i.e. found by
   `check-connections`). A trigger keeps a pause's identity, deadline and end final.
3. **The tenant setting** `tenants.reauth_deadline_hours` (24..2,160, default 168; a CHECK and the
   request model, 422 outside, never clamped): `GET /v1/tenant/settings`, `PUT
   /v1/tenant/settings/reauth-deadline` (`tenant.admin`, a reason, audited
   `tenant_setting_changed` with old and new). Tests on test stacks use
   `EDISC_REAUTH_DEADLINE_OVERRIDE_SECONDS` (test/ci only, refused elsewhere, like the retention
   override).
4. **Ending a pause without access** (`edisc_worker.access_lost`): DECIDE in one transaction (the
   pause's `ended_how` `deadline` / `finished_without_access`, `ended_by`, `end_reason`, and
   `job_pause_ended` in the job's chain), then CARRY OUT: every unit not yet reconciled closes
   `access_lost` with the class, each in its own transaction with its `unit_reconciled` event (the
   §28 format, `collected` = what its links hold), and only after the last one the pause ends
   (`resumed_at`), its alerts resolve (`alerts.resolved_at`) and the job's `stop_reason` becomes
   `access_lost`; the workflow (woken by a signal, else its own poll) finalizes it
   `completed_with_gaps` with `stop_reason` in `job_finished`. A crash anywhere leaves a decided
   pause that the sweep (`check-reauth-deadlines`, every 5 minutes, `open_reauth_pauses`) finishes;
   closed units are skipped. A re-authorization never resumes a decided pause's job.
5. **Found while building: a job revoked before enumeration would have finished clean.**
   Enumeration writes every unit at once, so a job whose credentials were refused during
   enumeration has no unit at all; "close every remaining unit" closes nothing, and
   `finalize_job` (every counted unit settled, none with a gap) would have said `completed`.
   A job without any work unit now finalizes `failed` (`status_detail.units_enumerated: false`),
   whatever stopped it. Tested.
6. **Finish without access** (`POST /v1/jobs/{id}/finish-without-access`, `job.resume`, a reason,
   `require_recent_sign_in` and in `test_sessions._sensitive_requests`): 409 unless the job is
   paused with an undecided pause (decided once); the decision and `audit.job_finished_without_access`
   (with `authenticated_at`) in one transaction, then the maintenance workflow is started at once
   (`access-lost-<job>`, the same sweep). The answer is the job as it is then (still paused).
7. **The report** states the closed units (each `access_lost` with its class, from the chain;
   `units.jsonl` recomputes from the job package byte for byte, tested) and the pause as "never
   resumed" (true). It does NOT yet state how the pause ended (deadline or the action, who, the
   reason): that is a report model change (`job_pause_ended`), a renderer bump with new HTML/JSON
   goldens and PDF goldens recorded in the report image: next, recorded in HANDOFF.
8. **check-connections** (a maintenance sweep every 6 hours (the plan said daily; the sweeps are bounded at 6 h), `connections_to_check`: active connections with
   stored tokens, least recently checked first): `Connector.check_credentials` (new in the
   protocol: the source's cheapest authenticated call through the limiter; the dummy's `auth.test`,
   none for exports). A refusal pauses the connection as a job would have (status, alerts);
   success stamps `connections.credentials_checked_at`. A source no connector of the worker serves
   is counted `unchecked` (never reported fine); any other failure is collected and fails the run
   after the rest. **Slack and Teams have no live connector yet (Phase 3)**: their connections are
   `unchecked` until the live connectors implement `check_credentials` (Slack `auth.test`,
   Microsoft a client-credentials token with the platform signer).
9. **Found by an intermittent hang of the re-authorization test: a stale refusal re-paused a
   resumed job.** A unit activity that loaded the revoked credentials could fail AFTER the
   connection was re-authorized and the job resumed; its `pause_for_reauth` then paused the job
   again (and turned the connection `reauth_required` again), so the job never finished (1 hang in
   9 runs; reconstructed from the flow, then made deterministic). Every activity now records a
   stamp of the credentials it loaded (`credentials_stamp`: token version + configuration digest,
   `Activities._context`); an `AuthenticationError` carries it across Temporal in its text
   (`[credentials <stamp>]`, added by `_classified`), and `pause_for_reauth` pauses only when the
   refused credentials are the ones in force; otherwise the unit simply runs again with the
   current ones (a refusal of THOSE pauses: no loop). Test:
   `test_a_refusal_of_replaced_credentials_never_pauses_the_job_again` (deterministic, through the
   activity); the step-8 worker tests then passed 5/5 under 4-core contention. Workflow code is
   unchanged.

Tests: `tests/integration/worker/test_access_lost.py` (the deadline end to end with the report's
`units.jsonl` recomputed from the job package, the warning once even from two racing sweeps, a crash
while closing, re-authorization before the deadline, a job revoked before enumeration, a stale
refusal, check-connections), `tests/integration/api/test_access_lost.py` (the setting's bounds and
audit, deadlines and warnings from the setting, finish-without-access audited, recorded, closing the
units, refused for a job not paused or a caller without `job.resume`, a decided pause never resumed),
`tests/unit/connectors/test_auth_reason.py`, the action in `test_sessions._sensitive_requests`, and
`test_workflows` (one alert per paused job besides the connection's). Round `m17-access-lost`: 20
breaks, all caught (the first run left `warning_repeated` uncaught: the sweep's own selection already
excludes a warned pause, so the re-check matters only for two sweeps at once; the test now races two).

## Implementation notes: closing M17 (2026-10-18)

Built: the directory snapshots (plan §3.3, the rest of step 8; migration 0043), the SSE
`connection` event (plan §4), the report stating how each pause ended (ADR 0018 §33), and the
production-readiness gate's two modes. Round `m17-close`.
1. **Production readiness warns on main.** `scripts/production_readiness.py --mode enforce|warn`:
   `enforce` (the default, `make production-readiness`, the report image's `readiness` stage, CI on
   `v*` tags) exits 1 while a `pending_frontend` entry exists; `warn` (CI on main, branches and pull
   requests) prints one `::warning` annotation per item and writes a job summary, exit 0. The CI
   job has no `continue-on-error` any more: its verdict is its own, so red always means something.
   A malformed allow-list fails in both modes. Tested by evaluating the job's expression per ref
   and running the script through the Makefile with GitHub's environment.
2. **The `connection` event.** `ConnectionEvent` (connection id, status verbatim,
   `needs_reauthorization`) is sent when the job's connection is not `active` at a stream's start
   and on every change of its status, immediately before the snapshot that shows it, WITHOUT an
   `id:` (the client's `Last-Event-ID` stays the previous event's until that snapshot arrives).
   The snapshot's digest covers the connection's status, so a reconnect whose last id is that
   snapshot gets neither again, and one from before the change gets both (tested live, across a
   reconnect, and for a change of the connection ALONE: disabled while the job runs, where neither
   the job's status nor its chain moves). The plan's "sent with the first snapshot" became "when not
   active": a healthy job's stream is unchanged and a client that saw no `connection` event knows
   the connection is active. The stream reads only `connections.status` (the statement test checks
   the columns). In `x-sse-events`: adding it is not breaking, changing it is (tested).
3. **Directory snapshots, as built** (where they differ from §3.3):
   - **One generation covers the whole directory** (conversations and users): the connector's
     `fetch_directory` already pages users then conversations behind ONE resumable cursor (the
     job's directory unit uses it), so one checkpoint serves both. Entries carry their `kind`;
     the route takes `kind=conversation|user`.
   - **The checkpoint** is `source_cursor` + `pages` on the generation's row, moved in the page's
     own transaction with its entries (insert-only, unique per kind and external id); a second
     executor whose checkpoint moved under it stores nothing and reloads. The last page completes
     the generation in that same transaction (no window where all pages are stored but the
     generation is not complete) and supersedes every complete generation but the current and the
     previous one. Tested with REAL SIGKILLs of a capture process inside a page's transaction and
     right after a commit (`directory_kill_target`), plus a crash hook, two executors, a budget
     stop, refused credentials (`failed`, the previous generation stays served).
   - **Due** (`directories_due`, SECURITY DEFINER for the sweeper): an active connection with a
     generation still capturing, none complete, the current older than
     `EDISC_DIRECTORY_REFRESH_MAX_AGE_HOURS` (24), or credentials replaced since it started
     (`token_updated_at`: a re-authorization). `refresh-directories` runs every 15 minutes, each
     run bounded by `EDISC_DIRECTORY_REFRESH_BUDGET_SECONDS` (1,200, under the maintenance
     activity's 30 minutes): a Grid directory spans several runs. An install starts a run at once
     (`edisc_api.directory_refresh.start`, best effort; `EDISC_DIRECTORY_REFRESH_ON_CONNECT`, off in
     the API test settings so tests never leave maintenance runs on the shared queue); the dummy's
     credential routes (local/test/ci only) rely on the sweep.
   - **Who** (D12's default): `connection.read` on the client, or `job.start` at the client, the
     tenant or on a matter of the client. Anyone else gets what `connection.read` answers: 404 when
     the client is invisible to them (reviewers and auditors hold their roles at matters, so the
     client is invisible: 404, where the plan's table said 403; `authorize`'s rule decides), 403
     when it is visible. Tested per role.
   - **Serving**: `status: not_captured` with `items: null` until a generation completes;
     `approximate_total` is the generation's count of that kind (exact for it); `q` is a prefix of
     the NFKC case-folded name, served as a `[q, successor)` range on the C-collated search index,
     never `LIKE` (a bind-parameter pattern defeats the index). Grid scale tested: 30,000 users in
     1,000-entry source pages, 60 served pages of 500, each user once, and the work of a page bounded
     by its size at every depth (EXPLAIN ANALYZE at the first, a middle and the last full page: no
     node over two pages of rows, no sequential scan). **CI run 39126275352 failed the first form of
     this test** ("no Sort node" at the last full page): diagnosed as a TEST bug. With about 1,000
     rows left, the planner on the CI runner (fresh ANALYZE) chose a bitmap read of the index range
     plus a sort of the ~500 matching rows over walking the index in order, on cost; both read only
     the range. The assertion stated more than the requirement (bounded work per page); it now
     measures that requirement at three depths, and a break that sorts the whole kind is caught.
   - **Exports**: no source call. Conversations from the validated `export_conversations` rows
     (stand-in ids and exact names kept), users from the locked archive's `users.json` through
     the export connector.
   - Tenant isolation: the RLS seed and copy tests cover both tables; two tenants with colliding
     external ids each see only their own generation; A's connection on B's host is 404; A's cursor
     on B's connection is `cursor_mismatch` (bound to A's path).
4. **Not built:** a directory refresh route (none was planned; re-authorizing triggers one);
   archiving superseded generations (BACKLOG: no-delete rule).
