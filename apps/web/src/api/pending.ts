/**
 * Contracts the UI needs that the backend has NOT shipped yet. These are proposals: the shapes here
 * are what the UI is built against. When an endpoint lands, delete its type from this file and alias
 * the generated schema in types.ts instead; the compiler then shows any mismatch.
 */

/**
 * Proposed: GET /v1/session. The server-side session (cookie, HttpOnly). No token ever reaches the
 * browser; `csrf_token` is echoed in the X-CSRF-Token header on every state-changing request.
 */
export interface SessionOut {
  authenticated: boolean;
  csrf_token: string | null;
  principal_id: string | null;
  display_name: string | null;
  email: string | null;
}

/** Proposed: GET /v1/me/permissions. The caller's live assignments and what they grant. */
export interface MyPermissionsOut {
  principal_id: string;
  display_name: string;
  kind: "user" | "service";
  assignments: { role: string; scope_type: "tenant" | "client" | "matter" | "workspace"; scope_id: string | null; via_group: string | null }[];
  /** Effective permissions at tenant scope (what the shell can offer globally). */
  tenant_permissions: string[];
}

/** Proposed: GET /v1/roles. The fixed role → permission matrix, from edisc_api.authz. */
export interface RoleMatrixOut {
  permissions: { name: string; audited: boolean }[];
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
