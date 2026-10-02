import { ApiError, type ApiClient, type PageQuery } from "../client";
import type { CustodyEventView, ExportOut, JobOut, Page, UnitOut } from "../types";
import { registerNames } from "@/lib/names";
import {
  assignments,
  clients,
  connections,
  directory,
  exportsData,
  groups,
  jobs,
  matters,
  ME_ACTOR,
  principals,
  rng,
  roleMatrix,
  sha,
  uuid7,
  workspaces,
  type MockJob,
} from "./data";

/**
 * Demo data for the dev server only (never in a production build; see api/index.ts and vite.config.ts).
 * In-memory stand-in for the API. It follows the API's rules where the UI depends on them: cursor
 * pages, 404 for unknown ids, 409 for invalid transitions, idempotent job starts, retention that may
 * only be extended, nothing deleted (ended instead). Running jobs advance in real time.
 */
const latency = () => new Promise((res) => setTimeout(res, 180 + Math.random() * 320));

function page<T extends { id?: string; unit_key?: string }>(rows: T[], q?: PageQuery): Page<T> {
  const limit = Math.min(Math.max(q?.limit ?? 50, 1), 200);
  const start = q?.cursor ? Number(atob(q.cursor)) : 0;
  const items = rows.slice(start, start + limit);
  const next = start + limit < rows.length ? btoa(String(start + limit)) : null;
  return { items, next_cursor: next };
}

function find<T extends { id: string }>(rows: T[], id: string, what: string): T {
  const row = rows.find((x) => x.id === id);
  if (!row) throw new ApiError(404, "not_found", `${what} not found`, null);
  return row;
}

const conflict = (detail: string) => new ApiError(409, "conflict", detail, null);
const now = () => new Date().toISOString();

// ------------------------------------------------------------------ live simulation of running jobs
function tick(mj: MockJob) {
  if (!mj.live || mj.job.status !== "running") return;
  const elapsed = (Date.now() - mj.live.startedAt) / 1000;
  const target = Math.floor(elapsed * mj.live.ratePerSec);
  const rand = rng(target + 7);
  let advanced = 0;
  for (const u of mj.units) {
    if (u.status === "done" || u.status === "failed") continue;
    if (advanced >= target) break;
    const expected = rand() < 0.15 ? 0 : Math.floor(4 + rand() ** 2 * 380);
    Object.assign(u, { status: "done", recon_status: "matched", expected_count: expected, collected_count: expected });
    advanced++;
  }
  mj.live.startedAt = Date.now() - ((elapsed - advanced / mj.live.ratePerSec) * 1000);
  // keep a couple of units visibly in flight
  const inFlight = mj.units.filter((u) => u.status === "running").length;
  for (const u of mj.units.filter((x) => x.status === "pending").slice(0, Math.max(0, 3 - inFlight))) u.status = "running";
  if (!mj.units.some((u) => u.status !== "done")) finalize(mj);
  mj.job.units = counts(mj.units);
}

function counts(units: UnitOut[]): JobOut["units"] {
  const c: JobOut["units"] = {};
  for (const u of units) c[u.status] = (c[u.status] ?? 0) + 1;
  return c;
}

function finalize(mj: MockJob) {
  const gaps = mj.units.some((u) => u.recon_status !== "matched");
  mj.job.status = gaps ? "completed_with_gaps" : "completed";
  mj.job.clean = mj.job.status === "completed";
  mj.job.finished_at = now();
  mj.job.sealed = true;
  mj.live = undefined;
}

const jobRow = (id: string) => {
  const mj = jobs.find((j) => j.job.id === id);
  if (!mj) throw new ApiError(404, "not_found", "job not found", null);
  tick(mj);
  return mj;
};

// ------------------------------------------------------------------ custody chain (no route yet; demo only)
async function sha256hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
const canonical = (v: unknown): string =>
  Array.isArray(v)
    ? `[${v.map(canonical).join(",")}]`
    : v && typeof v === "object"
      ? `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical((v as Record<string, unknown>)[k])}`).join(",")}}`
      : JSON.stringify(v);

const chainCache = new Map<string, CustodyEventView[]>();
async function buildChain(mj: MockJob): Promise<CustodyEventView[]> {
  const cached = chainCache.get(mj.job.id);
  if (cached && mj.job.status !== "running") return cached;
  const rand = rng(parseInt(mj.job.id.slice(0, 8), 16));
  const t0 = Date.parse(mj.job.created_at);
  const done = mj.units.filter((u) => u.status === "done");
  const batches = Math.max(1, Math.min(42, Math.ceil(done.length / 6)));
  const types: { t: string; items?: number }[] = [
    { t: "connection_verified" },
    { t: "job_started" },
    { t: "units_enumerated" },
  ];
  for (let i = 0; i < batches; i++) types.push({ t: "items_collected", items: 40 + Math.floor(rand() * 900) });
  if (mj.job.status === "paused_awaiting_reauth") types.push({ t: "job_paused" });
  if (mj.job.finished_at) types.push({ t: "units_reconciled" }, { t: "job_finalized" });
  if (mj.job.sealed) types.push({ t: "chain_sealed" });
  const out: CustodyEventView[] = [];
  let prev = "0".repeat(64);
  for (const [seq, e] of types.entries()) {
    const event = {
      seq: seq + 1,
      event_type: e.t,
      actor: e.t === "job_started" ? mj.job.requested_by : "system:collection-worker",
      created_at: new Date(t0 + seq * 41_000 + Math.floor(rand() * 9000)).toISOString(),
      ...(e.items ? { merkle_root: sha(rand), items: e.items } : {}),
    };
    const hash = await sha256hex(prev + canonical(event));
    out.push({
      ...event,
      prev_hash: prev,
      event_hash: hash,
      anchored: ["job_started", "job_finalized", "chain_sealed", "job_paused"].includes(e.t) || (seq + 1) % 8 === 0,
    });
    prev = hash;
  }
  chainCache.set(mj.job.id, out);
  return out;
}

// ------------------------------------------------------------------ the client
const idempotency = new Map<string, string>();

export function createDemoClient(): ApiClient {
  registerNames(directory);
  const api: ApiClient = {
    async session() {
      await latency();
      return { authenticated: true, csrf_token: "demo", principal_id: ME_ACTOR.split(":")[1]!, display_name: "Priya Raman", email: "priya.raman@halcyon-legal.com" };
    },
    loginUrl: (returnTo) => returnTo, // demo: no IdP, signing in just enters the app
    async logout() {},
    async myPermissions() {
      await latency();
      return {
        principal_id: ME_ACTOR.split(":")[1]!, display_name: "Priya Raman", kind: "user",
        assignments: [{ role: "tenant_admin", scope_type: "tenant", scope_id: null, via_group: null }],
        tenant_permissions: roleMatrix.roles[0]!.permissions,
      };
    },
    async roleMatrix() { await latency(); return roleMatrix; },
    async directory() { await latency(); return directory; },
    async listGroups() { await latency(); return groups; },
    async me() {
      await latency();
      return { principal_id: ME_ACTOR.split(":")[1]!, kind: "user", subject: "00u8priya2r4m4n", issuer: "https://login.halcyon-legal.com" };
    },

    async listClients(q) { await latency(); return page(clients, q); },
    async getClient(id) { await latency(); return find(clients, id, "client"); },
    async createClient(b) {
      await latency();
      const c = { id: uuid7(Date.now()), name: b.name, is_default: false, created_at: now(), closed_at: null };
      clients.push(c);
      return c;
    },
    async closeClient(id) {
      await latency();
      const c = find(clients, id, "client");
      if (c.closed_at) throw conflict("client already closed");
      if (matters.some((m) => m.client_id === id && !m.closed_at)) throw conflict("client has open matters");
      c.closed_at = now();
      return c;
    },

    async listMatters(c, q) { await latency(); return page(matters.filter((m) => m.client_id === c), q); },
    async getMatter(id) { await latency(); return find(matters, id, "matter"); },
    async createMatter(c, b) {
      await latency();
      const client = find(clients, c, "client");
      if (client.closed_at) throw conflict("client is closed");
      const mt = { id: uuid7(Date.now()), client_id: c, name: b.name, retention_until: b.retention_until, created_at: now(), closed_at: null };
      matters.push(mt);
      return mt;
    },
    async closeMatter(id) {
      await latency();
      const mt = find(matters, id, "matter");
      if (mt.closed_at) throw conflict("matter already closed");
      if (jobs.some((j) => j.job.matter_id === id && ["pending", "running", "paused_awaiting_reauth"].includes(j.job.status)))
        throw conflict("matter has active jobs");
      mt.closed_at = now();
      return mt;
    },

    async listWorkspaces(m, q) { await latency(); return page(workspaces.filter((w) => w.matter_id === m), q); },
    async createWorkspace(m, b) {
      await latency();
      find(matters, m, "matter");
      const w = { id: uuid7(Date.now()), matter_id: m, name: b.name, created_at: now() };
      workspaces.push(w);
      return w;
    },

    async listClientConnections(c, q) { await latency(); return page(connections.filter((x) => x.client_id === c), q); },
    async listMatterConnections(m, q) {
      await latency();
      const mt = find(matters, m, "matter");
      return page(connections.filter((x) => x.client_id === mt.client_id), q);
    },
    async getConnection(id) { await latency(); return find(connections, id, "connection"); },
    async createConnection(c, b) {
      await latency();
      find(clients, c, "client");
      const x = {
        id: uuid7(Date.now()), client_id: c, source: b.source, external_org_id: b.external_org_id, status: "active",
        plan_tier: null, granted_scopes: ["channels:history", "users:read"], created_at: now(), updated_at: now(),
      };
      connections.push(x);
      return x; // credentials are never echoed back
    },
    async reauthConnection(id) {
      await latency();
      const x = find(connections, id, "connection");
      if (x.status === "revoked") throw conflict("connection is disabled");
      x.status = "active";
      x.updated_at = now();
      for (const mj of jobs) {
        if (mj.job.connection_id === id && mj.job.status === "paused_awaiting_reauth") {
          mj.job.status = "running";
          mj.job.paused_ms = null;
          mj.live = { startedAt: Date.now(), ratePerSec: 2.5 };
        }
      }
      return x;
    },
    async disableConnection(id) {
      await latency();
      const x = find(connections, id, "connection");
      x.status = "revoked";
      x.updated_at = now();
      return x;
    },

    async listJobs(m, q) {
      await latency();
      const rows = jobs.filter((j) => j.job.matter_id === m);
      rows.forEach(tick);
      return page(rows.map((j) => j.job).sort((x, y) => y.created_at.localeCompare(x.created_at)), q);
    },
    async getJob(id) { await latency(); return jobRow(id).job; },
    async startJob(m, b, key) {
      await latency();
      const seen = idempotency.get(key);
      if (seen) return jobRow(seen).job;
      const mt = find(matters, m, "matter");
      if (mt.closed_at) throw conflict("matter is closed");
      const cx = find(connections, b.connection_id, "connection");
      if (cx.status !== "active") throw conflict(`connection is ${cx.status}`);
      const units: UnitOut[] = [];
      for (const s of b.scopes) {
        const days = Math.max(1, Math.round((Date.parse(s.date_to) - Date.parse(s.date_from)) / 86_400_000));
        for (let d = 0; d < Math.min(days, 120); d++) {
          const day = new Date(Date.parse(s.date_from) + d * 86_400_000).toISOString().slice(0, 10);
          units.push({ unit_key: `${s.external_id}/${day}`, kind: "conversation_day", status: "pending", recon_status: "pending", expected_count: null, collected_count: 0, file_gaps: 0, last_error: null, day_anomalies: 0, caveat: null });
        }
      }
      const job: JobOut = {
        id: uuid7(Date.now()), matter_id: m, connection_id: b.connection_id, workspace_id: b.workspace_id ?? null,
        status: "running", clean: false, clean_basis: null, caveat: null, requested_by: ME_ACTOR, rerun_of: null, created_at: now(), finished_at: null,
        sealed: false, paused_ms: null, units: { pending: units.length },
        scopes: b.scopes.map((s) => ({ ...s, thread_parent_policy: s.thread_parent_policy ?? "include_parent_and_thread" })),
      };
      jobs.unshift({ job, units, live: { startedAt: Date.now(), ratePerSec: 3 } });
      idempotency.set(key, job.id);
      return job;
    },
    async cancelJob(id) {
      await latency();
      const mj = jobRow(id);
      if (!["pending", "running", "paused_awaiting_reauth"].includes(mj.job.status)) throw conflict(`job is ${mj.job.status}`);
      mj.job.status = "cancelled";
      mj.job.finished_at = now();
      mj.live = undefined;
      return mj.job;
    },
    async resumeJob(id) {
      await latency();
      const mj = jobRow(id);
      if (mj.job.status === "paused_awaiting_reauth") throw conflict("job resumes through the connection's re-authorization");
      throw conflict(`job is ${mj.job.status}`);
    },
    async rerunJob(id, key) {
      await latency();
      const src = jobRow(id);
      const j = await api.startJob(src.job.matter_id, { connection_id: src.job.connection_id, scopes: src.job.scopes.map((s) => ({ ...s, type: s.type as "channel" | "custodian", thread_parent_policy: "include_parent_and_thread" })) }, key);
      j.rerun_of = id;
      return j;
    },
    async listUnits(jobId, q) { await latency(); return page(jobRow(jobId).units, q); },
    async reconciliation(jobId) {
      await latency();
      const mj = jobRow(jobId);
      const by: Record<string, number> = {};
      for (const u of mj.units) by[u.recon_status] = (by[u.recon_status] ?? 0) + 1;
      return {
        job_id: jobId, status: mj.job.status, clean: mj.job.clean, by_recon_status: by,
        clean_basis: mj.job.clean_basis ?? null, caveat: mj.job.caveat ?? null,
        not_matched: mj.units.filter((u) => u.recon_status !== "matched" && u.recon_status !== "matched_against_archive").slice(0, 500),
      };
    },
    async verifyCustody(jobId) {
      await new Promise((res) => setTimeout(res, 1600));
      const mj = jobRow(jobId);
      const chain = await buildChain(mj);
      let prev = "0".repeat(64);
      const errors: string[] = [];
      for (const e of chain) {
        const { prev_hash, event_hash, anchored: _a, ...event } = e;
        if (prev_hash !== prev) errors.push(`seq ${e.seq}: prev_hash does not link`);
        if ((await sha256hex(prev + canonical(event))) !== event_hash) errors.push(`seq ${e.seq}: event_hash mismatch`);
        prev = event_hash;
      }
      return {
        ok: errors.length === 0, events: chain.length,
        batches_checked: chain.filter((e) => e.event_type === "items_collected").length,
        items_checked: chain.reduce((n, e) => n + (e.items ?? 0), 0),
        anchors_checked: chain.filter((e) => e.anchored).length, errors,
      };
    },
    evidenceUrl: (e, purpose) => `/v1/evidence/${e}/content?purpose=${purpose}`,
    async custodyEvents(jobId) { await latency(); return buildChain(jobRow(jobId)); },

    async listExports(c, q) { await latency(); return page(exportsData.filter((x) => x.client_id === c), q); },
    async getExport(id) { await latency(); return find(exportsData, id, "export"); },
    async createExport(c, b) {
      await latency();
      find(clients, c, "client");
      const x: ExportOut = {
        id: uuid7(Date.now()), client_id: c, status: "uploading", reject_reason: null, reject_detail: null,
        declared_size: b.size_bytes, declared_sha256: b.sha256 ?? null, declared_plan: b.plan ?? null,
        limits: { max_archive_bytes: 64 * 2 ** 30, max_entries: 2_000_000 }, sha256: null, size_bytes: null,
        evidence_object_id: null, version_id: null, entry_count: null, detected_tier: null, tier_confirmed: null,
        findings: {}, connection_id: null, root_prefix: null, workspace_id: null, created_by: ME_ACTOR, created_at: now(),
        locked_at: null, validated_at: null,
        upload: { part_min_bytes: 5 * 2 ** 20, part_max_bytes: 512 * 2 ** 20, parts_received: 0, bytes_received: 0, expires_at: new Date(Date.now() + 2 * 86_400_000).toISOString() },
      };
      exportsData.unshift(x);
      return x;
    },
    async uploadPart(id, n, blob) {
      await latency();
      const x = find(exportsData, id, "export");
      if (x.status !== "uploading" || !x.upload) throw conflict(`export is ${x.status}`);
      x.upload.parts_received = Math.max(x.upload.parts_received, n);
      x.upload.bytes_received += blob.size;
      return { part_number: n, size_bytes: blob.size, sha256: [...new Uint8Array(await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()))].map((b) => b.toString(16).padStart(2, "0")).join("") };
    },
    async completeExport(id) {
      await latency();
      const x = find(exportsData, id, "export");
      if (x.status !== "uploading") throw conflict(`export is ${x.status}`);
      x.status = "locking";
      x.upload = null;
      const steps: [number, Partial<ExportOut>][] = [
        [1800, { status: "validating", sha256: x.declared_sha256 ?? sha(), size_bytes: x.declared_size, locked_at: now(), evidence_object_id: uuid7(Date.now()), version_id: "3f9a1c2e-77b1-4c0e" }],
        [4200, { status: "ready", validated_at: now(), entry_count: 9_204, detected_tier: x.declared_plan ?? "pro", tier_confirmed: true, findings: { channels: 18, dms: 47, day_files: 9_011, files_referenced: 802 } }],
      ];
      for (const [ms, patch] of steps) setTimeout(() => Object.assign(x, patch), ms);
      return x;
    },

    async listPrincipals(q) { await latency(); return page(principals, q); },
    async createPrincipal(b) {
      await latency();
      if (principals.some((p) => p.issuer === b.issuer && p.subject === b.subject)) throw conflict("principal exists");
      const p = { id: uuid7(Date.now()), kind: b.kind ?? "user", issuer: b.issuer, subject: b.subject, display_name: b.display_name, email: b.email ?? null, active: true, created_at: now() };
      principals.push(p);
      return p;
    },
    async deactivatePrincipal(id) {
      await latency();
      const p = find(principals, id, "principal");
      p.active = false;
      return p;
    },
    async createGroup(b) {
      await latency();
      const g = { id: uuid7(Date.now()), name: b.name, external_id: b.external_id ?? null, created_at: now() };
      groups.push(g);
      return g;
    },
    async listAssignments(q) { await latency(); return page(assignments, q); },
    async createAssignment(b) {
      await latency();
      const x = {
        id: uuid7(Date.now()), principal_id: b.principal_id ?? null, group_id: b.group_id ?? null, role: b.role,
        scope_type: b.scope_type, scope_id: b.scope_id ?? null, created_at: now(), created_by: ME_ACTOR, revoked_at: null, revoked_by: null,
      };
      assignments.push(x);
      return x;
    },
    async revokeAssignment(id) {
      await latency();
      const x = find(assignments, id, "assignment");
      if (x.revoked_at) throw conflict("already revoked");
      x.revoked_at = now();
      x.revoked_by = ME_ACTOR;
      return x;
    },
  };
  return api;
}

