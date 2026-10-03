/**
 * Every server-side flow the browser takes part in, in one place.
 *
 * AUTH follows the M17 backend plan (docs/plans/phase-2.md, "M17 backend", section 1; ADR 0016), which
 * is the source of truth. The session is the `__Host-edisc_session` cookie: httpOnly, so this code
 * never reads or names it, and no token ever reaches the browser.
 */
export const AUTH = {
  /** GET: starts OIDC (code + PKCE) with the tenant's IdP; the tenant comes from the Host subdomain. */
  loginPath: "/auth/login",
  /** GET: the IdP redirects here; the API exchanges the code server-side. The UI never calls it. */
  callbackPath: "/auth/callback",
  /** GET: the CSRF token bound to the session (HMAC of the session id). */
  csrfPath: "/auth/csrf",
  /** POST: revokes the current session and clears the cookie (CSRF-protected like any mutation). */
  logoutPath: "/auth/logout",
  /** Sent on every state-changing request (POST/PUT/PATCH/DELETE) authenticated by the cookie. */
  csrfHeader: "X-CSRF-Token",
  /** 401 error code for sensitive actions after EDISC_API_REAUTH_MAX_AGE_SECONDS: sign in again. */
  reauthRequired: "reauth_required",
} as const;

/**
 * Source connection flows, M17 plan sections 6 and 7 (the source of truth). The browser never handles a
 * provider grant; the only credential it ever carries is the Slack internal-app token (section 7).
 */
export const SOURCE_FLOWS = {
  /** POST (connection.manage): starts Slack OAuth v2 and answers with the authorize URL to follow. */
  slackInstall: (clientId: string) => `/clients/${clientId}/connections/slack/install`,
  /** POST (connection.manage): starts Microsoft tenant-admin consent and answers with the consent URL. */
  teamsConsent: (clientId: string) => `/clients/${clientId}/connections/teams/consent`,
  /** POST: the internal-app tier's one-time token (validated with auth.test, encrypted on receipt). */
  slackToken: (clientId: string) => `/clients/${clientId}/connections/slack/token`,
  /** PUT: replaces an internal-app token. */
  replaceToken: (connectionId: string) => `/connections/${connectionId}/token`,
  /** Provider callbacks, handled by the API on the tenant host (exempt from CSRF, bound by `state`). */
  slackCallbackPath: "/oauth/slack/callback",
  microsoftCallbackPath: "/oauth/microsoft/callback",
} as const;
