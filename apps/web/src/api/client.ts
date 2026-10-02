import type {
  AssignmentIn,
  AssignmentOut,
  ClientIn,
  ClientOut,
  ConnectionIn,
  ConnectionOut,
  CustodyEventView,
  DirectoryOut,
  ExportIn,
  ExportOut,
  GroupIn,
  GroupOut,
  JobIn,
  JobOut,
  MatterIn,
  MatterOut,
  Me,
  MyPermissionsOut,
  Page,
  PartOut,
  PrincipalIn,
  PrincipalOut,
  ReauthIn,
  ReconciliationOut,
  RoleMatrixOut,
  SessionOut,
  UnitOut,
  VerifyOut,
  WorkspaceIn,
  WorkspaceOut,
} from "./types";

export interface PageQuery {
  cursor?: string | null;
  limit?: number; // 1..200, API default 50
}

/**
 * One method per API route (all under /v1). Pages call this through the hooks in `hooks.ts` only.
 * Implementations: `http.ts` (production) and `demo/` (dev-only demo data, never in a production build).
 * Optional methods have no backend route yet (pending.ts): the UI degrades when they are undefined.
 */
export interface ApiClient {
  /** Proposed GET /v1/session. */
  session(): Promise<SessionOut>;
  /** Where to send the browser to sign in (server-side session; the backend redirects to the IdP). */
  loginUrl(returnTo: string): string;
  logout(): Promise<void>;
  me(): Promise<Me>;
  /** Proposed GET /v1/me/permissions. */
  myPermissions(): Promise<MyPermissionsOut>;
  /** Proposed GET /v1/roles. */
  roleMatrix(): Promise<RoleMatrixOut>;

  listClients(q?: PageQuery): Promise<Page<ClientOut>>;
  getClient(id: string): Promise<ClientOut>;
  createClient(body: ClientIn): Promise<ClientOut>;
  closeClient(id: string): Promise<ClientOut>;

  listMatters(clientId: string, q?: PageQuery): Promise<Page<MatterOut>>;
  getMatter(id: string): Promise<MatterOut>;
  createMatter(clientId: string, body: MatterIn): Promise<MatterOut>;
  closeMatter(id: string): Promise<MatterOut>;

  listWorkspaces(matterId: string, q?: PageQuery): Promise<Page<WorkspaceOut>>;
  createWorkspace(matterId: string, body: WorkspaceIn): Promise<WorkspaceOut>;

  listClientConnections(clientId: string, q?: PageQuery): Promise<Page<ConnectionOut>>;
  listMatterConnections(matterId: string, q?: PageQuery): Promise<Page<ConnectionOut>>;
  getConnection(id: string): Promise<ConnectionOut>;
  createConnection(clientId: string, body: ConnectionIn): Promise<ConnectionOut>;
  reauthConnection(id: string, body: ReauthIn): Promise<ConnectionOut>;
  disableConnection(id: string): Promise<ConnectionOut>;

  listJobs(matterId: string, q?: PageQuery): Promise<Page<JobOut>>;
  getJob(id: string): Promise<JobOut>;
  /** `idempotencyKey` goes in the Idempotency-Key header; reuse it when retrying the same request. */
  startJob(matterId: string, body: JobIn, idempotencyKey: string): Promise<JobOut>;
  cancelJob(id: string): Promise<JobOut>;
  resumeJob(id: string): Promise<JobOut>;
  rerunJob(id: string, idempotencyKey: string): Promise<JobOut>;
  listUnits(jobId: string, q?: PageQuery): Promise<Page<UnitOut>>;
  reconciliation(jobId: string): Promise<ReconciliationOut>;
  verifyCustody(jobId: string): Promise<VerifyOut>;
  /** Audited BEFORE bytes are returned. The x-evidence-sha256 header carries the evidence hash. */
  evidenceUrl(evidenceId: string, purpose: "preview" | "download" | "export" | "rsmf"): string;

  listExports(clientId: string, q?: PageQuery): Promise<Page<ExportOut>>;
  getExport(id: string): Promise<ExportOut>;
  createExport(clientId: string, body: ExportIn): Promise<ExportOut>;
  /** Sends Content-Digest: sha-256=:<base64>: computed over the part. */
  uploadPart(exportId: string, partNumber: number, bytes: Blob): Promise<PartOut>;
  completeExport(id: string): Promise<ExportOut>;

  listPrincipals(q?: PageQuery): Promise<Page<PrincipalOut>>;
  createPrincipal(body: PrincipalIn): Promise<PrincipalOut>;
  deactivatePrincipal(id: string): Promise<PrincipalOut>;
  createGroup(body: GroupIn): Promise<GroupOut>;
  listAssignments(q?: PageQuery): Promise<Page<AssignmentOut>>;
  createAssignment(body: AssignmentIn): Promise<AssignmentOut>;
  revokeAssignment(id: string): Promise<AssignmentOut>;

  /** No route yet: a job stream's custody events. */
  custodyEvents?(jobId: string): Promise<CustodyEventView[]>;
  /** No route yet: conversations and custodians visible through a connection. */
  directory?(connectionId: string): Promise<DirectoryOut>;
  /** No route yet: GET /v1/groups (only POST exists). */
  listGroups?(): Promise<GroupOut[]>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail: string,
    readonly requestId: string | null,
  ) {
    super(`${status} ${code}${detail ? `: ${detail}` : ""}`);
  }
}
