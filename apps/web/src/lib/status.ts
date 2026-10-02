/**
 * Known status values. The API types these fields as plain strings (they are `str` on the Pydantic
 * models), so these unions describe what the UI knows how to present, not what the wire allows.
 * Always look up through jobStatus()/recon()/...: an unknown value renders as itself, never crashes.
 */
export type JobStatus =
  | "pending" | "running" | "paused_awaiting_reauth" | "completed" | "completed_with_gaps"
  | "completed_unverified" | "completed_against_archive" | "failed" | "cancelled";
export type UnitStatus = "pending" | "running" | "done" | "failed";
export type ReconStatus =
  | "pending" | "matched" | "matched_against_archive" | "gap" | "surplus" | "unverifiable" | "failed"
  | "access_lost" | "not_applicable";

export type Tone = "ok" | "live" | "warn" | "bad" | "muted" | "info" | "violet" | "orange";

interface Meta { label: string; tone: Tone; hint: string }

/**
 * Status language. Only `completed` is clean (ADR 0005). `completed_unverified` and `completed_with_gaps`
 * are never shown with the clean tone, the check icon, or the word "complete" on its own.
 */
export const JOB_STATUS: Record<JobStatus, Meta> = {
  pending: { label: "Queued", tone: "muted", hint: "Waiting for a worker on the source's task queue." },
  running: { label: "Collecting", tone: "live", hint: "Units are being collected; each batch commits atomically with its checkpoint." },
  paused_awaiting_reauth: { label: "Paused · re-auth", tone: "warn", hint: "The source rejected the token. Re-authorize the connection to resume from the last checkpoint." },
  completed: { label: "Complete · reconciled", tone: "ok", hint: "Every unit's collected count matches the expected count. Chain sealed." },
  completed_with_gaps: { label: "Completed with gaps", tone: "warn", hint: "Some units collected fewer (or more) items than expected. Not a clean completion." },
  completed_unverified: { label: "Unverified", tone: "violet", hint: "Some units could not be reconciled against an expected count. Never a clean completion." },
  completed_against_archive: { label: "Matched to export", tone: "info", hint: "Every unit matched the uploaded Slack export. The export's own completeness against the workspace was NOT verified, so this is never a clean completion." },
  failed: { label: "Failed", tone: "bad", hint: "Collection stopped with unrecoverable errors. Committed batches are preserved." },
  cancelled: { label: "Cancelled", tone: "muted", hint: "Stopped by a user. Committed batches are preserved and recorded in custody." },
};

export const RECON: Record<ReconStatus, Meta> = {
  pending: { label: "Pending", tone: "muted", hint: "Not yet reconciled." },
  matched: { label: "Matched", tone: "ok", hint: "Collected = expected." },
  matched_against_archive: { label: "Matched (export)", tone: "info", hint: "Matched every entry of the export; the export itself is not verified complete." },
  gap: { label: "Gap", tone: "warn", hint: "Collected fewer than expected." },
  surplus: { label: "Surplus", tone: "orange", hint: "Collected more than expected." },
  unverifiable: { label: "Unverifiable", tone: "violet", hint: "The source gives no expected count for this unit." },
  failed: { label: "Failed", tone: "bad", hint: "The unit failed." },
  access_lost: { label: "Access lost", tone: "bad", hint: "Access to the conversation was lost mid-collection." },
  not_applicable: { label: "N/A", tone: "muted", hint: "Not reconciled for this unit kind." },
};

export const EXPORT_STATUS: Record<string, Meta> = {
  uploading: { label: "Uploading", tone: "live", hint: "Parts are being received (Content-Digest verified per part)." },
  locking: { label: "Hashing & locking", tone: "info", hint: "Hashing the staged object and placing it under WORM Object Lock." },
  validating: { label: "Validating", tone: "info", hint: "Streaming central-directory validation of the LOCKED version." },
  ready: { label: "Ready", tone: "ok", hint: "Locked, validated, and available as a credential-less connection." },
  rejected: { label: "Rejected", tone: "bad", hint: "Rejected before ingestion. The locked bytes are kept as evidence." },
};

export const CONNECTION_STATUS: Record<string, Meta> = {
  active: { label: "Active", tone: "ok", hint: "Token valid." },
  pending: { label: "Pending", tone: "muted", hint: "Awaiting authorization." },
  error: { label: "Needs re-auth", tone: "warn", hint: "The source rejected the token." },
  revoked: { label: "Disabled", tone: "bad", hint: "Disabled. Tokens are kept encrypted, never deleted." },
};

/** Tone → CSS custom property (defined in index.css). */
const unknown = (v: string): Meta => ({ label: v.replace(/_/g, " "), tone: "muted", hint: `Status "${v}" is not known to this version of the UI.` });
const lookup = (table: Record<string, Meta>) => (v: string): Meta => (Object.hasOwn(table, v) ? table[v]! : unknown(v));
export const jobStatus = lookup(JOB_STATUS);
export const recon = lookup(RECON);
export const exportStatus = lookup(EXPORT_STATUS);
export const connectionStatus = lookup(CONNECTION_STATUS);

export const toneVar: Record<Tone, string> = {
  ok: "var(--ok)", live: "var(--live)", warn: "var(--warn)", bad: "var(--bad)",
  muted: "var(--muted)", info: "var(--info)", violet: "var(--violet)", orange: "var(--orange)",
};

export const isActive = (s: string) => s === "pending" || s === "running" || s === "paused_awaiting_reauth";
