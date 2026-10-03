import { AUTH, SOURCE_FLOWS } from "./auth";
import { ApiError, type ApiClient, type PageQuery } from "./client";
import type { CsrfOut } from "./types";

/**
 * The real client. Authentication is the API's server-side session (ADR 0016; M17 plan, section 1):
 * an httpOnly `__Host-` cookie set by the API's login callback. The browser never holds a token, so
 * nothing here reads, stores or sends one. Route names and codes live in `auth.ts`.
 *
 * - Every request sends cookies (`credentials: "include"`); same origin, so the tenant comes from the
 *   page's Host subdomain (ADR 0013) and the browser's Origin header is the tenant host, as the API checks.
 * - Every state-changing request sends `X-CSRF-Token`, fetched from GET /v1/auth/csrf and cached for the
 *   page's lifetime (a new login is a full navigation, so a new session always gets a new token).
 * - 401 `reauth_required` (sensitive actions with an old sign-in) → `onReauthRequired`: the user goes
 *   through the IdP again and comes back. Any other 401 → `onUnauthenticated` (sign in).
 */
const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

interface ErrorBody {
  error?: string;
  detail?: string;
}

export function createHttpClient(
  opts: { base?: string; onUnauthenticated?: () => void; onReauthRequired?: () => void } = {},
): ApiClient {
  const base = opts.base ?? "/v1";
  let csrf: Promise<string> | null = null;

  async function toError(res: Response): Promise<ApiError> {
    let body: ErrorBody = {};
    try {
      body = (await res.json()) as ErrorBody;
    } catch {
      // non-JSON error body (proxy, gateway): keep the status
    }
    return new ApiError(res.status, body.error ?? "http_error", body.detail ?? res.statusText, res.headers.get("x-request-id"));
  }

  async function fetchCsrf(): Promise<string> {
    const res = await fetch(base + AUTH.csrfPath, { credentials: "include", headers: { accept: "application/json" } });
    if (!res.ok) throw await toError(res);
    return ((await res.json()) as CsrfOut).csrf_token;
  }
  const csrfToken = () => (csrf ??= fetchCsrf().catch((e: unknown) => { csrf = null; throw e; }));

  async function call<T>(method: string, path: string, init: { body?: unknown; headers?: Record<string, string>; raw?: BodyInit } = {}): Promise<T> {
    const headers: Record<string, string> = { accept: "application/json", ...init.headers };
    if (!SAFE.has(method)) headers[AUTH.csrfHeader] = await csrfToken();
    let body: BodyInit | undefined = init.raw;
    if (init.body !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(init.body);
    }
    const res = await fetch(base + path, { method, headers, body, credentials: "include" });
    if (res.ok) return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
    const err = await toError(res);
    if (res.status === 401) {
      csrf = null;
      if (err.code === AUTH.reauthRequired) opts.onReauthRequired?.();
      else opts.onUnauthenticated?.();
    }
    throw err;
  }

  // The plan does not yet say how /auth/login learns where to return or that a sign-in is a
  // re-authentication; `return_to` and `reauth=1` are proposals until ADR 0016 fixes them.
  const loginUrl = (returnTo: string, reauth = false) =>
    `${base}${AUTH.loginPath}?return_to=${encodeURIComponent(returnTo)}${reauth ? "&reauth=1" : ""}`;

  const qs = (q?: PageQuery) => {
    const p = new URLSearchParams();
    if (q?.cursor) p.set("cursor", q.cursor);
    if (q?.limit) p.set("limit", String(q.limit));
    const s = p.toString();
    return s ? `?${s}` : "";
  };
  const get = <T>(path: string) => call<T>("GET", path);
  const post = <T>(path: string, body?: unknown, headers?: Record<string, string>) => call<T>("POST", path, { body, headers });

  return {
    loginUrl,
    startInstall: (clientId, source, connectionId) =>
      post((source === "slack" ? SOURCE_FLOWS.slackInstall : SOURCE_FLOWS.teamsConsent)(clientId), connectionId ? { connection_id: connectionId } : {}),
    // The token is only ever in this request body: not logged, not cached, not retried by this client.
    submitSlackToken: (clientId, token, connectionId) =>
      connectionId
        ? call("PUT", SOURCE_FLOWS.replaceToken(connectionId), { body: { token } })
        : post(SOURCE_FLOWS.slackToken(clientId), { token }),
    logout: async () => {
      await post<void>(AUTH.logoutPath);
      csrf = null;
    },
    me: () => get("/me"),
    myPermissions: () => get("/me/permissions"),
    roleMatrix: () => get("/roles"),

    listClients: (q) => get(`/clients${qs(q)}`),
    getClient: (id) => get(`/clients/${id}`),
    createClient: (b) => post(`/clients`, b),
    closeClient: (id) => post(`/clients/${id}/close`),

    listMatters: (c, q) => get(`/clients/${c}/matters${qs(q)}`),
    getMatter: (id) => get(`/matters/${id}`),
    createMatter: (c, b) => post(`/clients/${c}/matters`, b),
    closeMatter: (id) => post(`/matters/${id}/close`),

    listWorkspaces: (m, q) => get(`/matters/${m}/workspaces${qs(q)}`),
    createWorkspace: (m, b) => post(`/matters/${m}/workspaces`, b),

    listClientConnections: (c, q) => get(`/clients/${c}/connections${qs(q)}`),
    listMatterConnections: (m, q) => get(`/matters/${m}/connections${qs(q)}`),
    getConnection: (id) => get(`/connections/${id}`),
    createConnection: (c, b) => post(`/clients/${c}/connections`, b),
    reauthConnection: (id, b) => post(`/connections/${id}/reauth`, b),
    disableConnection: (id) => post(`/connections/${id}/disable`),

    listJobs: (m, q) => get(`/matters/${m}/jobs${qs(q)}`),
    getJob: (id) => get(`/jobs/${id}`),
    startJob: (m, b, key) => post(`/matters/${m}/jobs`, b, { "idempotency-key": key }),
    cancelJob: (id) => post(`/jobs/${id}/cancel`),
    resumeJob: (id) => post(`/jobs/${id}/resume`),
    rerunJob: (id, key) => post(`/jobs/${id}/rerun`, undefined, { "idempotency-key": key }),
    listUnits: (j, q) => get(`/jobs/${j}/units${qs(q)}`),
    reconciliation: (j) => get(`/jobs/${j}/reconciliation`),
    verifyCustody: (j) => get(`/jobs/${j}/custody/verify`),
    // A plain navigation: the session cookie authenticates it and the read is audited server-side.
    evidenceUrl: (e, purpose) => `${base}/evidence/${e}/content?purpose=${purpose}`,

    listExports: (c, q) => get(`/clients/${c}/exports${qs(q)}`),
    getExport: (id) => get(`/exports/${id}`),
    createExport: (c, b) => post(`/clients/${c}/exports`, b),
    uploadPart: async (id, n, blob) => {
      const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
      const b64 = btoa(String.fromCharCode(...new Uint8Array(digest)));
      return call("PUT", `/exports/${id}/parts/${n}`, {
        raw: blob,
        headers: { "content-type": "application/octet-stream", "content-digest": `sha-256=:${b64}:` },
      });
    },
    completeExport: (id) => post(`/exports/${id}/complete`),

    listPrincipals: (q) => get(`/principals${qs(q)}`),
    createPrincipal: (b) => post(`/principals`, b),
    deactivatePrincipal: (id) => post(`/principals/${id}/deactivate`),
    createGroup: (b) => post(`/groups`, b),
    listAssignments: (q) => get(`/role-assignments${qs(q)}`),
    createAssignment: (b) => post(`/role-assignments`, b),
    revokeAssignment: (id) => post(`/role-assignments/${id}/revoke`),
    // custodyEvents, directory, listGroups: no routes yet (pending.ts). Left undefined so the UI degrades.
  };
}
