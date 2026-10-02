import { ApiError, type ApiClient, type PageQuery } from "./client";
import type { SessionOut } from "./types";

/**
 * The real client. Authentication is a server-side session: an HttpOnly cookie set by the backend's
 * login flow. The browser never holds a token, so nothing here reads, stores or sends one.
 *
 * - Every request sends cookies (`credentials: "include"`).
 * - Every state-changing request (anything but GET/HEAD/OPTIONS) sends the session's CSRF token in
 *   `X-CSRF-Token`. The token comes from GET /v1/session and is cached for the page's lifetime; a 403
 *   `csrf_invalid` refreshes it once and retries.
 * - Same origin: the tenant comes from the page's Host subdomain (ADR 0013), never from the client.
 *
 * The session routes are a proposal until the backend ships them (src/api/pending.ts).
 */
export const SESSION = {
  path: "/session",
  loginPath: "/auth/login", // GET, redirects to the tenant's IdP; returns to `return_to` (same origin)
  logoutPath: "/auth/logout", // POST, CSRF-protected
  csrfHeader: "X-CSRF-Token",
  csrfErrorCode: "csrf_invalid",
} as const;

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

interface ErrorBody {
  error?: string;
  detail?: string;
}

export function createHttpClient(opts: { base?: string; onUnauthenticated?: () => void } = {}): ApiClient {
  const base = opts.base ?? "/v1";
  let csrf: Promise<string | null> | null = null;

  async function fetchSession(): Promise<SessionOut> {
    const res = await fetch(base + SESSION.path, { credentials: "include", headers: { accept: "application/json" } });
    if (!res.ok) throw await toError(res);
    return (await res.json()) as SessionOut;
  }
  const csrfToken = () => (csrf ??= fetchSession().then((s) => s.csrf_token, (e: unknown) => { csrf = null; throw e; }));

  async function toError(res: Response): Promise<ApiError> {
    let body: ErrorBody = {};
    try {
      body = (await res.json()) as ErrorBody;
    } catch {
      // non-JSON error body (proxy, gateway): keep the status
    }
    return new ApiError(res.status, body.error ?? "http_error", body.detail ?? res.statusText, res.headers.get("x-request-id"));
  }

  async function call<T>(method: string, path: string, init: { body?: unknown; headers?: Record<string, string>; raw?: BodyInit } = {}, retried = false): Promise<T> {
    const headers: Record<string, string> = { accept: "application/json", ...init.headers };
    if (!SAFE.has(method)) {
      const token = await csrfToken();
      if (token) headers[SESSION.csrfHeader] = token;
    }
    let body: BodyInit | undefined = init.raw;
    if (init.body !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(init.body);
    }
    const res = await fetch(base + path, { method, headers, body, credentials: "include" });
    if (res.ok) return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
    const err = await toError(res);
    if (res.status === 403 && err.code === SESSION.csrfErrorCode && !retried) {
      csrf = null; // rotated or expired: fetch a fresh one and try once more
      return call<T>(method, path, init, true);
    }
    if (res.status === 401) {
      csrf = null;
      opts.onUnauthenticated?.();
    }
    throw err;
  }

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
    session: () => fetchSession(),
    loginUrl: (returnTo) => `${base}${SESSION.loginPath}?return_to=${encodeURIComponent(returnTo)}`,
    logout: async () => {
      await post<void>(SESSION.logoutPath);
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
