/**
 * Wire types, GENERATED from the API's OpenAPI document (`npm run gen:api` writes schema.gen.ts).
 * This file only gives the generated schemas readable names: never add or change a field here.
 * CI (`npm run check:api`) fails when schema.gen.ts is stale against the backend.
 *
 * Status fields (`JobOut.status`, `UnitOut.recon_status`, `ExportOut.status`, ...) are plain strings in
 * the spec. The UI's known values and their meaning live in `src/lib/status.ts`, which renders unknown
 * values instead of failing.
 */
import type { components } from "./schema.gen";

type S = components["schemas"];

export type Me = S["Me"];
export type ClientIn = S["ClientIn"];
export type ClientOut = S["ClientOut"];
export type MatterIn = S["MatterIn"];
export type MatterOut = S["MatterOut"];
export type WorkspaceIn = S["WorkspaceIn"];
export type WorkspaceOut = S["WorkspaceOut"];
export type Credentials = S["Credentials"];
export type ConnectionIn = S["ConnectionIn"];
export type ConnectionOut = S["ConnectionOut"];
export type ReauthIn = S["ReauthIn"];
export type JobIn = S["JobIn"];
export type JobOut = S["JobOut"];
export type ScopeIn = S["ScopeIn"];
export type ScopeOut = S["ScopeOut"];
export type ThreadParentPolicy = S["ThreadParentPolicy"];
export type UnitOut = S["UnitOut"];
export type ReconciliationOut = S["ReconciliationOut"];
export type VerifyOut = S["VerifyOut"];
export type ExportIn = S["ExportIn"];
export type ExportOut = S["ExportOut"];
export type LimitOverrides = S["LimitOverrides"];
export type UploadInfo = S["UploadInfo"];
export type PartOut = S["PartOut"];
export type PrincipalIn = S["PrincipalIn"];
export type PrincipalOut = S["PrincipalOut"];
export type GroupIn = S["GroupIn"];
export type GroupOut = S["GroupOut"];
export type AssignmentIn = S["AssignmentIn"];
export type AssignmentOut = S["AssignmentOut"];

/** edisc_api.pagination.Page[T]; the spec has one concrete Page_X_ schema per T, all this shape. */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

/** Query values of GET /evidence/{id}/content (an enum on a query parameter, not a schema). */
export type EvidencePurpose = "preview" | "download" | "export" | "rsmf";

/** Values of ExportIn.plan (a regex pattern in the spec, so not an enum). */
export const SLACK_PLANS = ["free", "pro", "business_plus", "enterprise_grid"] as const;
export type SlackPlan = (typeof SLACK_PLANS)[number];

export type ScopeType = AssignmentIn["scope_type"];

export type * from "./pending";
