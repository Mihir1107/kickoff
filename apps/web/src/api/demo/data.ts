import type {
  AssignmentOut,
  ClientOut,
  ConnectionOut,
  DirectoryOut,
  ExportOut,
  GroupOut,
  JobOut,
  MatterOut,
  PrincipalOut,
  RoleMatrixOut,
  UnitOut,
  WorkspaceOut,
} from "../types";
import type { JobStatus, ReconStatus, UnitStatus } from "@/lib/status";

/** Deterministic PRNG so the demo dataset is identical on every load. */
export function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const r = rng(1107);
const hex = (n: number, rand = r) =>
  Array.from({ length: n }, () => Math.floor(rand() * 16).toString(16)).join("");

/** UUIDv7-shaped ids, time-ordered like edisc_core.ids. */
let clock = Date.parse("2026-03-02T09:00:00Z");
export function uuid7(at?: number): string {
  const ms = at ?? (clock += 3_600_000 + Math.floor(r() * 7_200_000));
  const t = ms.toString(16).padStart(12, "0");
  return `${t.slice(0, 8)}-${t.slice(8, 12)}-7${hex(3)}-${"89ab"[Math.floor(r() * 4)]}${hex(3)}-${hex(12)}`;
}
export const sha = (rand = r) => hex(64, rand);

const iso = (ms: number) => new Date(ms).toISOString();
const NOW = Date.parse("2026-10-02T14:30:00Z");
const daysAgo = (d: number, h = 0) => iso(NOW - d * 86_400_000 - h * 3_600_000);

/** Verbatim from edisc_core.schemas.ARCHIVE_CAVEAT (ADR 0014 section 4). */
export const ARCHIVE_CAVEAT =
  "Completeness was verified against the provided Slack export only. Every entry of the export was " +
  "accounted for, but the export's own completeness relative to the Slack workspace was NOT verified: " +
  "content excluded by the plan, the export's date range, Slack retention settings or the export " +
  "settings cannot be detected from the export.";

export const ME_ACTOR = "principal:0192f0aa-6c1e-7d10-9a51-3c2e9b1d7f00";

// ------------------------------------------------------------------ hierarchy
export const clients: ClientOut[] = [
  { id: uuid7(), name: "Northwind Pharmaceuticals", is_default: false, created_at: daysAgo(212), closed_at: null },
  { id: uuid7(), name: "Atlas Freight & Logistics", is_default: false, created_at: daysAgo(180), closed_at: null },
  { id: uuid7(), name: "Meridian Capital Partners", is_default: false, created_at: daysAgo(131), closed_at: null },
  { id: uuid7(), name: "Halcyon Internal", is_default: true, created_at: daysAgo(240), closed_at: null },
  { id: uuid7(), name: "Juniper Biotech", is_default: false, created_at: daysAgo(301), closed_at: daysAgo(44) },
];
const [northwind, atlas, meridian, halcyon, juniper] = clients as [ClientOut, ClientOut, ClientOut, ClientOut, ClientOut];

const m = (client: ClientOut, name: string, created: number, retentionDays: number, closed?: number): MatterOut => ({
  id: uuid7(),
  client_id: client.id,
  name,
  retention_until: daysAgo(-retentionDays),
  created_at: daysAgo(created),
  closed_at: closed === undefined ? null : daysAgo(closed),
});

export const matters: MatterOut[] = [
  m(northwind, "Northwind v. Castellan · Trade Secrets", 160, 1460),
  m(northwind, "FDA Inquiry 26-0417", 74, 1095),
  m(atlas, "DOJ Antitrust · Rate Coordination", 150, 2190),
  m(atlas, "Internal Investigation · Driver Safety", 38, 730),
  m(meridian, "SEC Subpoena 2026-114", 120, 2555),
  m(meridian, "Employment · Ruiz Arbitration", 21, 540),
  m(halcyon, "Records Hold · Q3 Audit", 90, 365),
  m(juniper, "Juniper Patent Interference", 290, 900, 44),
];

export const workspaces: WorkspaceOut[] = matters.flatMap((mt, i) =>
  ["Primary Review", "Privilege", "Hot Docs"].slice(0, 1 + (i % 3)).map((name, j) => ({
    id: uuid7(),
    matter_id: mt.id,
    name,
    created_at: iso(Date.parse(mt.created_at) + (j + 1) * 86_400_000),
  })),
);

// ------------------------------------------------------------------ connections
const conn = (
  client: ClientOut,
  source: string,
  org: string,
  status: ConnectionOut["status"],
  plan: string | null,
  scopes: string[],
  age: number,
): ConnectionOut => ({
  id: uuid7(),
  client_id: client.id,
  source,
  external_org_id: org,
  status,
  plan_tier: plan,
  granted_scopes: scopes,
  created_at: daysAgo(age),
  updated_at: daysAgo(Math.max(0, age - 30)),
});
const SLACK_SCOPES = ["channels:history", "groups:history", "im:history", "mpim:history", "users:read", "files:read"];
export const connections: ConnectionOut[] = [
  conn(northwind, "slack", "E04NWPHARMA", "active", "enterprise_grid", SLACK_SCOPES, 158),
  conn(northwind, "teams", "nwpharma.onmicrosoft.com", "pending", null, [], 12),
  conn(atlas, "slack", "T02ATLASFRT", "error", "business_plus", SLACK_SCOPES.slice(0, 5), 149),
  conn(atlas, "slack_export", "T02ATLASFRT", "active", "business_plus", [], 41),
  conn(meridian, "slack", "T07MERIDIAN", "active", "pro", SLACK_SCOPES, 119),
  conn(halcyon, "dummy", "golden-small", "active", null, ["*"], 230),
  conn(juniper, "slack", "T01JUNIPER", "revoked", "pro", SLACK_SCOPES, 290),
];

// ------------------------------------------------------------------ jobs + units
const CHANNELS = [
  "C04LEGALHOLD", "C02ENGRD", "C05EXECSTAFF", "C01GENERAL", "C03SALESOPS", "D07DM-RCHEN",
  "G02BOARD-PRIV", "C06PRICING", "C08LOGISTICS", "D01DM-MRUIZ", "C09QA-SUBMIT", "G05M-AND-A",
];
const CUSTODIANS = ["U01R.CHEN", "U02M.RUIZ", "U03A.OKAFOR", "U04J.PARK", "U05L.MORENO", "U06S.ISHII"];

export interface MockJob {
  job: JobOut;
  units: UnitOut[];
  /** Units still to "finish" while the demo runs (running jobs only). */
  live?: { startedAt: number; ratePerSec: number };
}

function makeUnits(seed: number, convs: string[], fromDay: number, days: number, status: JobStatus): UnitOut[] {
  const rand = rng(seed);
  const out: UnitOut[] = [];
  for (const c of convs) {
    for (let d = 0; d < days; d++) {
      const day = new Date(Date.parse("2026-01-01T00:00:00Z") + (fromDay + d) * 86_400_000).toISOString().slice(0, 10);
      const quiet = rand() < 0.16;
      const expected = quiet ? 0 : Math.floor(4 + rand() ** 2 * 380);
      let unitStatus: UnitStatus = "done";
      let recon: ReconStatus = "matched";
      let collected = expected;
      let fileGaps = 0;
      let lastError: string | null = null;
      const roll = rand();
      if (status === "completed_with_gaps" && roll < 0.05) {
        recon = "gap";
        collected = Math.max(0, expected - 1 - Math.floor(rand() * 9));
        fileGaps = Math.floor(rand() * 3);
      } else if (status === "completed_against_archive") {
        recon = "matched_against_archive";
      } else if (status === "completed_unverified" && roll < 0.09) {
        recon = "unverifiable";
      } else if (status === "failed" && roll < 0.12) {
        unitStatus = "failed";
        recon = "failed";
        collected = Math.floor(expected * rand());
        lastError = "SourceAuthError: token_revoked (connection requires re-authorization)";
      } else if (status === "completed_with_gaps" && roll < 0.065) {
        recon = "surplus";
        collected = expected + 1 + Math.floor(rand() * 3);
      } else if (status === "paused_awaiting_reauth" && roll < 0.04) {
        recon = "access_lost";
      }
      out.push({
        unit_key: `${c}/${day}`,
        kind: "conversation_day",
        status: unitStatus,
        recon_status: recon,
        expected_count: recon === "unverifiable" ? null : expected,
        collected_count: collected,
        file_gaps: fileGaps,
        last_error: lastError,
        day_anomalies: 0,
        caveat: recon === "matched_against_archive" ? ARCHIVE_CAVEAT : null,
      });
    }
  }
  return out;
}

function unitCounts(units: UnitOut[]): JobOut["units"] {
  const c: JobOut["units"] = {};
  for (const u of units) c[u.status] = (c[u.status] ?? 0) + 1;
  return c;
}

const CLEAN: JobStatus[] = ["completed"];

function job(
  matter: MatterOut,
  connection: ConnectionOut,
  status: JobStatus,
  opts: { channels: number; fromDay: number; days: number; age: number; seed: number; custodian?: boolean; rerunOf?: string },
): MockJob {
  const convs = CHANNELS.slice(opts.seed % 4, (opts.seed % 4) + opts.channels);
  let units = makeUnits(opts.seed, convs, opts.fromDay, opts.days, status);
  const dateFrom = iso(Date.parse("2026-01-01T00:00:00Z") + opts.fromDay * 86_400_000);
  const dateTo = iso(Date.parse("2026-01-01T00:00:00Z") + (opts.fromDay + opts.days) * 86_400_000);
  const terminal = !["pending", "running", "paused_awaiting_reauth"].includes(status);
  let live: MockJob["live"];
  if (status === "running" || status === "pending" || status === "paused_awaiting_reauth") {
    const doneShare = status === "running" ? 0.38 : status === "paused_awaiting_reauth" ? 0.61 : 0;
    const doneCount = Math.floor(units.length * doneShare);
    units = units.map((u, i) => {
      if (i < doneCount) return u;
      if (status === "running" && i < doneCount + 3) return { ...u, status: "running", recon_status: "pending", collected_count: Math.floor((u.expected_count ?? 0) / 2) };
      return { ...u, status: "pending", recon_status: "pending", collected_count: 0, expected_count: null };
    });
    if (status === "running") live = { startedAt: Date.now(), ratePerSec: 2.2 };
  }
  const id = uuid7(Date.parse(daysAgo(opts.age)));
  return {
    job: {
      id,
      matter_id: matter.id,
      connection_id: connection.id,
      workspace_id: workspaces.find((w) => w.matter_id === matter.id)?.id ?? null,
      status,
      clean: CLEAN.includes(status),
      clean_basis: status === "completed" ? "source" : status === "completed_against_archive" ? "archive" : null,
      caveat: status === "completed_against_archive" ? ARCHIVE_CAVEAT : null,
      requested_by: ME_ACTOR,
      rerun_of: opts.rerunOf ?? null,
      created_at: daysAgo(opts.age),
      finished_at: terminal ? daysAgo(opts.age, -3 - (opts.seed % 9)) : null,
      sealed: terminal && status !== "cancelled",
      paused_ms: status === "paused_awaiting_reauth" ? 7_420_000 : null,
      units: unitCounts(units),
      scopes: opts.custodian
        ? CUSTODIANS.slice(0, 2).map((c) => ({ type: "custodian", external_id: c, date_from: dateFrom, date_to: dateTo, thread_parent_policy: "include_parent_and_thread" }))
        : convs.map((c) => ({ type: "channel", external_id: c, date_from: dateFrom, date_to: dateTo, thread_parent_policy: "include_parent_and_thread" })),
    },
    units,
    live,
  };
}

const [mNw1, mNw2, mAt1, mAt2, mMe1, mMe2, mHa1, mJu1] = matters as [MatterOut, MatterOut, MatterOut, MatterOut, MatterOut, MatterOut, MatterOut, MatterOut];
const [cNwSlack, , cAtSlack, cAtExport, cMeSlack, cHaDummy, cJuSlack] = connections as ConnectionOut[] as [ConnectionOut, ConnectionOut, ConnectionOut, ConnectionOut, ConnectionOut, ConnectionOut, ConnectionOut];

const jNwFirst = job(mNw1, cNwSlack, "completed_with_gaps", { channels: 6, fromDay: 0, days: 45, age: 140, seed: 11 });
export const jobs: MockJob[] = [
  jNwFirst,
  job(mNw1, cNwSlack, "completed", { channels: 6, fromDay: 0, days: 45, age: 96, seed: 12, rerunOf: jNwFirst.job.id }),
  job(mNw1, cNwSlack, "running", { channels: 8, fromDay: 45, days: 60, age: 0, seed: 13 }),
  job(mNw2, cNwSlack, "completed", { channels: 4, fromDay: 60, days: 30, age: 55, seed: 21, custodian: true }),
  job(mNw2, cNwSlack, "pending", { channels: 3, fromDay: 150, days: 21, age: 0, seed: 22 }),
  job(mAt1, cAtSlack, "paused_awaiting_reauth", { channels: 7, fromDay: 20, days: 50, age: 2, seed: 31 }),
  job(mAt1, cAtExport, "completed_against_archive", { channels: 5, fromDay: 0, days: 40, age: 30, seed: 32 }),
  job(mAt2, cAtSlack, "failed", { channels: 3, fromDay: 200, days: 28, age: 9, seed: 41 }),
  job(mMe1, cMeSlack, "completed", { channels: 9, fromDay: 10, days: 80, age: 70, seed: 51 }),
  job(mMe1, cMeSlack, "cancelled", { channels: 2, fromDay: 120, days: 14, age: 18, seed: 52 }),
  job(mMe2, cMeSlack, "running", { channels: 4, fromDay: 230, days: 35, age: 0, seed: 61, custodian: true }),
  job(mHa1, cHaDummy, "completed", { channels: 5, fromDay: 90, days: 30, age: 60, seed: 71 }),
  job(mJu1, cJuSlack, "completed", { channels: 6, fromDay: 0, days: 60, age: 260, seed: 81 }),
];

// ------------------------------------------------------------------ Slack exports
const exp = (client: ClientOut, status: ExportOut["status"], age: number, size: number, extra: Partial<ExportOut> = {}): ExportOut => {
  const hash = sha();
  const done = status === "ready";
  return {
    id: uuid7(Date.parse(daysAgo(age))),
    client_id: client.id,
    status,
    reject_reason: null,
    reject_detail: null,
    declared_size: size,
    declared_sha256: hash,
    declared_plan: "business_plus",
    limits: {
      max_archive_bytes: 64 * 2 ** 30,
      max_entries: 2_000_000,
      max_entry_bytes: 2 * 2 ** 30,
      max_total_bytes: 256 * 2 ** 30,
      max_total_ratio: 200,
      max_entry_ratio: 1000,
      ratio_floor_bytes: 1_048_576,
      max_name_bytes: 1024,
    },
    sha256: status === "uploading" ? null : hash,
    size_bytes: status === "uploading" ? null : size,
    evidence_object_id: status === "uploading" ? null : uuid7(),
    version_id: status === "uploading" ? null : `${hex(8)}-${hex(4)}-${hex(4)}`,
    entry_count: done ? 18_442 : null,
    detected_tier: done ? "business_plus" : null,
    tier_confirmed: done ? true : null,
    findings: done
      ? { channels: 41, private_channels: 6, dms: 112, mpims: 9, day_files: 17_903, files_referenced: 2_318, unknown_entries: 3, macos_resource_forks: 12, wrapper_folder: "atlas-export-2026-08/" }
      : {},
    connection_id: done ? cAtExport.id : null,
    root_prefix: done ? "atlas-export-2026-08/" : null,
    workspace_id: null,
    created_by: ME_ACTOR,
    created_at: daysAgo(age),
    locked_at: status === "uploading" ? null : daysAgo(age, -1),
    validated_at: done ? daysAgo(age, -2) : null,
    upload:
      status === "uploading"
        ? { part_min_bytes: 5 * 2 ** 20, part_max_bytes: 512 * 2 ** 20, parts_received: 9, bytes_received: 9 * 256 * 2 ** 20, expires_at: daysAgo(-2) }
        : null,
    ...extra,
  };
};
export const exportsData: ExportOut[] = [
  exp(atlas, "ready", 42, 7_812_331_904),
  exp(atlas, "rejected", 44, 7_812_331_904, {
    reject_reason: "sha256_mismatch",
    reject_detail: { declared: "9f1c…e2a0", computed: "41b7…0c9d", stage: "hash" },
  }),
  exp(meridian, "validating", 0, 2_114_908_160),
  exp(northwind, "uploading", 0, 11_480_000_000),
];

// ------------------------------------------------------------------ admin
const p = (name: string, email: string | null, kind: "user" | "service", active: boolean, age: number): PrincipalOut => ({
  id: uuid7(),
  kind,
  issuer: kind === "user" ? "https://login.halcyon-legal.com" : "https://sts.halcyon-legal.com",
  subject: kind === "user" ? `00u${hex(14)}` : `svc-${name.toLowerCase().replace(/\W+/g, "-")}`,
  display_name: name,
  email,
  active,
  created_at: daysAgo(age),
});
export const principals: PrincipalOut[] = [
  p("Priya Raman", "priya.raman@halcyon-legal.com", "user", true, 239),
  p("Daniel Okafor", "d.okafor@halcyon-legal.com", "user", true, 200),
  p("Sofia Lindqvist", "s.lindqvist@halcyon-legal.com", "user", true, 160),
  p("Marcus Bell", "m.bell@halcyon-legal.com", "user", true, 120),
  p("Hana Ishii", "h.ishii@halcyon-legal.com", "user", false, 300),
  p("Collection Runner", null, "service", true, 230),
  p("Audit Exporter", null, "service", true, 80),
];
export const groups: GroupOut[] = [
  { id: uuid7(), name: "Litigation Support", external_id: "okta:grp-lit-support", created_at: daysAgo(220) },
  { id: uuid7(), name: "Forensic Collectors", external_id: "okta:grp-collectors", created_at: daysAgo(210) },
  { id: uuid7(), name: "Outside Counsel · Reviewers", external_id: null, created_at: daysAgo(95) },
];
const [pPriya, pDaniel, pSofia, pMarcus, pHana, pRunner, pAudit] = principals as [PrincipalOut, PrincipalOut, PrincipalOut, PrincipalOut, PrincipalOut, PrincipalOut, PrincipalOut];
const [gLit, gCol, gRev] = groups as [GroupOut, GroupOut, GroupOut];
const a = (
  who: { principal_id?: string; group_id?: string },
  role: string,
  scope_type: AssignmentOut["scope_type"],
  scope_id: string | null,
  age: number,
  revoked?: number,
): AssignmentOut => ({
  id: uuid7(),
  principal_id: who.principal_id ?? null,
  group_id: who.group_id ?? null,
  role,
  scope_type,
  scope_id,
  created_at: daysAgo(age),
  created_by: ME_ACTOR,
  revoked_at: revoked === undefined ? null : daysAgo(revoked),
  revoked_by: revoked === undefined ? null : ME_ACTOR,
});
export const assignments: AssignmentOut[] = [
  a({ principal_id: pPriya.id }, "tenant_admin", "tenant", null, 239),
  a({ principal_id: pDaniel.id }, "client_admin", "client", northwind.id, 190),
  a({ principal_id: pSofia.id }, "matter_manager", "matter", mMe1.id, 118),
  a({ principal_id: pMarcus.id }, "collector", "client", atlas.id, 110),
  a({ principal_id: pHana.id }, "reviewer", "matter", mJu1.id, 280, 44),
  a({ principal_id: pRunner.id }, "collector", "tenant", null, 230),
  a({ principal_id: pAudit.id }, "auditor", "tenant", null, 80),
  a({ group_id: gLit.id }, "matter_manager", "client", meridian.id, 200),
  a({ group_id: gCol.id }, "collector", "tenant", null, 205),
  a({ group_id: gRev.id }, "reviewer", "matter", mNw1.id, 90),
];


export const CONVERSATION_NAMES: Record<string, string> = {
  C04LEGALHOLD: "#legal-hold",
  C02ENGRD: "#eng-r-and-d",
  C05EXECSTAFF: "#exec-staff",
  C01GENERAL: "#general",
  C03SALESOPS: "#sales-ops",
  "D07DM-RCHEN": "DM · R. Chen",
  "G02BOARD-PRIV": "🔒 board-private",
  C06PRICING: "#pricing",
  C08LOGISTICS: "#logistics",
  "D01DM-MRUIZ": "DM · M. Ruiz",
  "C09QA-SUBMIT": "#qa-submissions",
  "G05M-AND-A": "🔒 m-and-a",
};
export const CUSTODIAN_NAMES: Record<string, string> = {
  "U01R.CHEN": "Rebecca Chen",
  "U02M.RUIZ": "Mateo Ruiz",
  "U03A.OKAFOR": "Ada Okafor",
  "U04J.PARK": "Jin Park",
  "U05L.MORENO": "Lucia Moreno",
  "U06S.ISHII": "Sora Ishii",
};
export { CHANNELS, CUSTODIANS, NOW };

// ------------------------------------------------------------------ demo answers for proposed routes
/** Demo answer for GET /v1/roles. The real matrix comes from edisc_api.authz through the API. */
const PERMS = [
  "tenant.admin", "client.create", "client.read", "connection.manage", "connection.read", "matter.create", "matter.read",
  "workspace.create", "workspace.read", "job.start", "job.cancel", "job.resume", "job.rerun", "job.read", "custody.read", "evidence.read",
];
const READ_MATTER = ["matter.read", "workspace.read", "job.read", "client.read"];
export const roleMatrix: RoleMatrixOut = {
  roles: [
    { name: "tenant_admin", permissions: PERMS },
    { name: "client_admin", permissions: ["client.read", "connection.manage", "connection.read", "matter.create", "workspace.create", "job.start", "job.cancel", "job.resume", "job.rerun", "custody.read", "evidence.read", ...READ_MATTER] },
    { name: "matter_manager", permissions: ["workspace.create", "job.start", "job.cancel", "job.resume", "job.rerun", "custody.read", "evidence.read", ...READ_MATTER] },
    { name: "collector", permissions: ["job.start", "job.cancel", ...READ_MATTER] },
    { name: "reviewer", permissions: ["evidence.read", ...READ_MATTER] },
    { name: "auditor", permissions: ["custody.read", ...READ_MATTER] },
  ],
};

export const directory: DirectoryOut = {
  conversations: Object.entries(CONVERSATION_NAMES).map(([id, name]) => ({ id, name })),
  custodians: Object.entries(CUSTODIAN_NAMES).map(([id, name]) => ({ id, name })),
};
