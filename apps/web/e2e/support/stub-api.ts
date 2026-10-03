import type { Page, Request } from "@playwright/test";

export interface Captured {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: string | null;
}

export const CLIENT_ID = "01990000-0000-7000-8000-000000000001";
const now = new Date().toISOString();

/**
 * Answers /v1 for the contract project, shaped like the API (and the M17 plan for routes not built yet).
 * Every request is recorded so tests can assert exactly what the browser sent.
 */
export async function stubApi(page: Page, overrides: Record<string, (req: Request) => { status: number; body: unknown }> = {}) {
  const seen: Captured[] = [];
  const connections = [
    { id: "01990000-0000-7000-8000-0000000000c1", client_id: CLIENT_ID, source: "slack", external_org_id: "T0ERRORED", status: "error", plan_tier: "pro", granted_scopes: [] as string[], created_at: now, updated_at: now },
  ];
  const client = { id: CLIENT_ID, name: "Contract Test Client", is_default: false, created_at: now, closed_at: null };

  await page.route("**/v1/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    seen.push({ method: req.method(), url: req.url(), headers: req.headers(), body: req.postData() });
    const json = (status: number, body: unknown) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

    const key = `${req.method()} ${path}`;
    if (overrides[key]) {
      const r = overrides[key](req);
      return json(r.status, r.body);
    }
    if (req.isNavigationRequest()) return route.fulfill({ status: 200, contentType: "text/html", body: "<p>server-side flow</p>" });
    if (path === "/v1/auth/csrf") return json(200, { csrf_token: "csrf-bound-to-session" });
    if (path === "/v1/me") return json(200, { principal_id: "01990000-0000-7000-8000-0000000000aa", kind: "user", subject: "priya@halcyon", issuer: "https://idp.example" });
    if (path === "/v1/me/permissions") return json(200, { scopes: [{ scope_type: "tenant", scope_id: null, permissions: ["tenant.admin"] }] });
    if (path === "/v1/clients") return json(200, { items: [client], next_cursor: null });
    if (path === `/v1/clients/${CLIENT_ID}`) return json(200, client);
    if (key === `POST /v1/clients/${CLIENT_ID}/connections/slack/install`) return json(200, { authorize_url: "https://slack.com/oauth/v2/authorize?client_id=x&state=s-slack" });
    if (key === `POST /v1/clients/${CLIENT_ID}/connections/teams/consent`) return json(200, { authorize_url: "https://login.microsoftonline.com/common/adminconsent?client_id=x&state=s-teams" });
    if (req.method() === "PUT" && /^\/v1\/connections\/[^/]+\/token$/.test(path)) return json(200, { ...connections[0]!, status: "active" });
    if (key === `POST /v1/clients/${CLIENT_ID}/connections/slack/token`) {
      const c = { ...connections[0]!, id: "01990000-0000-7000-8000-0000000000c2", external_org_id: "T0INTERNAL", status: "active", granted_scopes: ["channels:history"] };
      connections.push(c);
      return json(201, c);
    }
    if (path === `/v1/clients/${CLIENT_ID}/connections`) return json(200, { items: connections, next_cursor: null });
    if (req.method() === "GET") return json(200, { items: [], next_cursor: null });
    return json(404, { error: "not_found", detail: "" });
  });
  // Provider pages: the test only needs to see the browser arrive there.
  await page.route(/^https:\/\/(slack\.com|login\.microsoftonline\.com)\//, (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<p>provider</p>" }));
  return seen;
}
