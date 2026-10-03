/**
 * Contracts the UI needs that the backend has NOT shipped yet. Paths follow the M17 plan
 * (docs/plans/phase-2.md); body shapes the plan leaves open are assumptions, marked as such. When an endpoint lands, delete its type from this file and alias
 * the generated schema in types.ts instead; the compiler then shows any mismatch.
 */

/** GET /v1/auth/csrf (M17 plan, section 1). The plan does not give the body; `csrf_token` is assumed. */
export interface CsrfOut {
  csrf_token: string;
}

/**
 * GET /v1/me/permissions (M17 plan, section 2): the caller's effective permissions per scope (tenant, and
 * every client, matter and workspace where they hold a role), computed by authz.py. Field names assumed.
 */
export interface MyPermissionsOut {
  scopes: { scope_type: "tenant" | "client" | "matter" | "workspace"; scope_id: string | null; permissions: string[] }[];
}

/** GET /v1/roles (M17 plan, section 2): role → permissions, generated from ROLE_PERMISSIONS. Field names assumed. */
export interface RoleMatrixOut {
  roles: { name: string; permissions: string[] }[];
}

/** Proposed: GET /v1/jobs/{id}/custody/events (seq order). */
export interface CustodyEventView {
  seq: number;
  event_type: string;
  actor: string;
  created_at: string;
  prev_hash: string;
  event_hash: string;
  merkle_root?: string;
  items?: number;
  anchored?: boolean;
}

/** Proposed: a per-connection directory of conversations and custodians (for pickers and names). */
export interface DirectoryOut {
  conversations: { id: string; name: string }[];
  custodians: { id: string; name: string }[];
}

/**
 * Answer of POST .../slack/install and .../teams/consent (M17 plan section 6): "the authorize URL".
 * The field name is not fixed by the plan; `authorize_url` is assumed.
 */
export interface InstallStartOut {
  authorize_url: string;
}

/**
 * Body of POST .../slack/install and .../teams/consent. The plan says re-authorization "uses the same
 * flow" but not how the connection is named; `connection_id` is assumed.
 */
export interface InstallStartIn {
  connection_id?: string;
}

/** Body of POST .../slack/token and PUT /connections/{id}/token (M17 plan section 7). Field name assumed. */
export interface SlackTokenIn {
  token: string;
}
