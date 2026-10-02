import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Ban, CheckCircle2, Fingerprint, KeyRound, Loader2, RotateCcw, ShieldAlert, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "@/api";
import { qk, useAction, useClient, useCustodyEvents, useJob, useMatter, useReconciliation, useUnits } from "@/api/hooks";
import type { VerifyOut } from "@/api/types";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Counter } from "@/components/ui/Counter";
import { Skeleton } from "@/components/ui/Empty";
import { Glass, SectionTitle } from "@/components/ui/Glass";
import { Hash } from "@/components/ui/Hash";
import { Ring } from "@/components/ui/Ring";
import { StatusPill } from "@/components/ui/StatusPill";
import { useToast } from "@/components/ui/Toast";
import { ChainViz } from "@/components/viz/ChainViz";
import { ReconBar } from "@/components/viz/ReconBar";
import { HeatLegend, UnitHeatmap } from "@/components/viz/UnitHeatmap";
import { ago, duration, newKey, num, utc } from "@/lib/format";
import { useReducedMotion } from "@/lib/motion";
import { scopeName } from "@/lib/names";
import { isActive, jobStatus, recon, toneVar } from "@/lib/status";

export function JobDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const job = useJob(id);
  const j = job.data;
  const live = !!j && isActive(j.status);
  const units = useUnits(id, live);
  const reconQ = useReconciliation(id, live);
  const matter = useMatter(j?.matter_id ?? "");
  const client = useClient(matter.data?.client_id ?? "");
  const chain = useCustodyEvents(id);

  const cancel = useAction(() => api.cancelJob(id), () => [qk.job(id), ["rollup"]]);
  const rerunKey = useRef(newKey()); // reused on retry so a double click starts one job
  const rerun = useAction(() => api.rerunJob(id, rerunKey.current), () => [["rollup"]]);
  const reauth = useAction(() => api.reauthConnection(j!.connection_id, { credentials: { access_token: "xoxp-demo" } }), () => [qk.job(id), ["rollup"]]);

  const totals = useMemo(() => {
    const us = units.data ?? [];
    return {
      expected: us.reduce((n, u) => n + (u.expected_count ?? 0), 0),
      collected: us.reduce((n, u) => n + u.collected_count, 0),
      fileGaps: us.reduce((n, u) => n + u.file_gaps, 0),
      total: us.length,
      done: us.filter((u) => u.status === "done" || u.status === "failed").length,
    };
  }, [units.data]);

  const m = j ? jobStatus(j.status) : null;
  const p = totals.total ? totals.done / totals.total : 0;

  return (
    <>
      <PageHeader
        crumbs={[
          { label: "Clients", to: "/clients" },
          { label: client.data?.name ?? "…", to: matter.data ? `/clients/${matter.data.client_id}` : undefined },
          { label: matter.data?.name ?? "…", to: j ? `/matters/${j.matter_id}` : undefined },
          { label: "Job" },
        ]}
        eyebrow={j ? <span className="flex items-center gap-2">Collection job <Hash value={j.id} /></span> : undefined}
        title={matter.data?.name ?? <Skeleton className="h-14 w-96" />}
        subtitle={j && <>Requested {ago(j.created_at)} by <span className="font-mono text-[12px]">{j.requested_by}</span>{j.rerun_of && <> · rerun of <Link className="text-iris hover:underline" to={`/jobs/${j.rerun_of}`}>{j.rerun_of.slice(-8)}</Link></>}</>}
        actions={j && (
          <>
            {j.status === "paused_awaiting_reauth" && <Button variant="primary" icon={<KeyRound className="size-4" />} loading={reauth.isPending} onClick={() => reauth.mutate(undefined, { onSuccess: () => toast("ok", "Connection re-authorized", "The job resumes from its last checkpoint: no gaps, no duplicates.") })}>Re-authorize & resume</Button>}
            {isActive(j.status) && <Button variant="danger" icon={<Ban className="size-4" />} loading={cancel.isPending} onClick={() => cancel.mutate(undefined, { onSuccess: () => toast("info", "Job cancelled", "Committed batches stay in evidence and custody.") })}>Cancel</Button>}
            {!isActive(j.status) && <Button icon={<RotateCcw className="size-4" />} loading={rerun.isPending} onClick={() => rerun.mutate(undefined, { onSuccess: (r) => { rerunKey.current = newKey(); toast("ok", "Rerun started"); nav(`/jobs/${r.id}`); } })}>Rerun</Button>}
          </>
        )}
      />

      {/* Status hero */}
      <div className="grid gap-4 xl:grid-cols-[400px_1fr]">
        <Glass className="flex items-center gap-6 p-6" glow={j ? `color-mix(in oklab, ${toneVar[m!.tone]} 14%, transparent)` : undefined}>
          <Ring value={p} size={136} stroke={10} from={j?.clean ? "#7cf5d2" : j?.status === "running" ? "#5ad8ff" : "#ffc86b"} to={j?.clean ? "#5ad8ff" : j?.status === "running" ? "#8b7cff" : "#ff6b8b"}>
            <div className="text-center">
              <div className="text-[30px] font-light leading-none"><Counter value={p * 100} suffix="%" /></div>
              <div className="mt-1 text-[10px] uppercase tracking-widest text-white/55">units</div>
            </div>
          </Ring>
          <div className="min-w-0 space-y-3">
            {m && <StatusPill tone={m.tone} label={m.label} className="!h-7 !text-[12.5px]" />}
            <p className="text-[12.5px] leading-relaxed text-white/55">{m?.hint}</p>
            {j && (
              <div className="flex flex-wrap gap-2 text-[11.5px]">
                {j.sealed ? <span className="flex items-center gap-1 text-mint"><ShieldCheck className="size-3.5" />chain sealed</span> : <span className="flex items-center gap-1 text-white/60"><Loader2 className="size-3.5 animate-spin" />chain open</span>}
                {j.finished_at && <span className="text-white/60">· took {duration(Date.parse(j.finished_at) - Date.parse(j.created_at))}</span>}
                {j.paused_ms != null && <span className="text-amber">· paused {duration(j.paused_ms)}</span>}
              </div>
            )}
          </div>
        </Glass>

        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {[
            { l: "Conversation-days", v: totals.total, sub: `${totals.done} finished` },
            { l: "Items expected", v: totals.expected, sub: j?.clean_basis === "archive" ? "from the uploaded export" : "from the source's counts" },
            { l: "Items collected", v: totals.collected, sub: totals.collected !== totals.expected ? `${num(totals.collected - totals.expected)} delta` : j?.clean_basis === "archive" ? "matched to the export only" : "exactly matched", tone: totals.collected !== totals.expected ? "text-amber" : j?.clean_basis === "archive" ? "text-info" : "text-mint" },
            { l: "File gaps", v: totals.fileGaps, sub: "attachments not retrieved", tone: totals.fileGaps ? "text-amber" : "text-white/60" },
          ].map((k, i) => (
            <Glass key={k.l} className="p-5" initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 + i * 0.06 }}>
              <div className="eyebrow">{k.l}</div>
              <div className="mt-3 text-[32px] font-light leading-none tracking-tight">{units.data ? <Counter value={k.v} /> : <Skeleton className="h-8 w-20" />}</div>
              <div className={clsx("mt-2 text-[11.5px]", k.tone ?? "text-white/60")}>{k.sub}</div>
            </Glass>
          ))}
        </div>
      </div>

      {j && !j.clean && !isActive(j.status) && j.status !== "cancelled" && (
        <motion.div initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} className="mt-4 flex items-start gap-3 rounded-2xl border border-amber/25 bg-amber/[0.06] px-5 py-4">
          <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber" />
          <div className="text-[13px] leading-relaxed">
            <span className="font-medium text-amber">This collection is not clean.</span>{" "}
            {j.caveat && <span className="mb-2 block text-white/75">{j.caveat}</span>}
            {j.clean_basis !== "archive" && <span className="text-white/60">Every unit that did not reconcile is listed below with its counts. Rerun the job to re-collect; items deduplicate by idempotency key, and edits become new versions, never overwrites.</span>}
          </div>
        </motion.div>
      )}

      {/* Heatmap */}
      <Glass className="mt-4 p-6" spotlight={false}>
        <SectionTitle eyebrow="Work units · conversation × UTC day" title="Reconciliation map" right={<HeatLegend />} />
        {units.data ? <UnitHeatmap units={units.data} /> : <Skeleton className="h-48" />}
      </Glass>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1fr_1fr]">
        <Glass className="p-6">
          <SectionTitle eyebrow="ADR 0005" title="Expected vs collected" />
          {reconQ.data ? <ReconBar counts={reconQ.data.by_recon_status} /> : <Skeleton className="h-20" />}
          <div className="mt-6">
            <div className="eyebrow mb-2">Finished units not matched {reconQ.data && `(${reconQ.data.not_matched.filter((u) => u.recon_status !== "pending").length})`}</div>
            <div className="max-h-[300px] overflow-y-auto rounded-xl border border-white/[0.06]">
              <table className="w-full text-[12px]">
                <thead className="sticky top-0 bg-ink-900/95 text-left text-white/60 backdrop-blur">
                  <tr><th className="px-3 py-2 font-normal">Unit</th><th className="px-3 py-2 font-normal">Result</th><th className="px-3 py-2 text-right font-normal">Expected</th><th className="px-3 py-2 text-right font-normal">Collected</th></tr>
                </thead>
                <tbody>
                  {reconQ.data?.not_matched.filter((u) => u.recon_status !== "pending").slice(0, 120).map((u) => (
                    <tr key={u.unit_key} className="border-t border-white/[0.04] hover:bg-white/[0.03]">
                      <td className="px-3 py-2 font-mono text-[11px] text-white/70">{u.unit_key}</td>
                      <td className="px-3 py-2"><span style={{ color: toneVar[recon(u.recon_status).tone] }}>{recon(u.recon_status).label}</span>{u.file_gaps > 0 && <span className="ml-1.5 text-amber/80">+{u.file_gaps} files</span>}</td>
                      <td className="px-3 py-2 text-right font-mono">{num(u.expected_count)}</td>
                      <td className="px-3 py-2 text-right font-mono">{num(u.collected_count)}</td>
                    </tr>
                  ))}
                  {reconQ.data && reconQ.data.not_matched.filter((u) => u.recon_status !== "pending").length === 0 && (
                    <tr><td colSpan={4} className="px-3 py-8 text-center text-white/60"><CheckCircle2 className="mx-auto mb-2 size-5 text-mint" />Every finished unit reconciled.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </Glass>

        <Glass className="p-6">
          <SectionTitle eyebrow="Scope" title="What this job collects" />
          <div className="space-y-2">
            {j?.scopes.map((s, i) => (
              <motion.div key={i} initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i * 0.04 }} className="glass-sub flex items-center gap-3 px-3.5 py-2.5">
                <span className="rounded-md bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10px] uppercase text-white/65">{s.type}</span>
                <span className="text-[13px]">{scopeName(s.type, s.external_id)}</span>
                <span className="ml-auto font-mono text-[11px] text-white/60">{s.date_from.slice(0, 10)} → {s.date_to.slice(0, 10)}</span>
              </motion.div>
            ))}
          </div>
          <div className="mt-4 text-[11.5px] text-white/60">Thread policy: <span className="font-mono">{j?.scopes[0]?.thread_parent_policy}</span> · a conversation is in scope if any scope's range covers it.</div>
          {j && <div className="mt-4 grid grid-cols-2 gap-2 text-[12px]">
            <div className="glass-sub p-3"><div className="text-white/60">Created</div><div className="mt-0.5">{utc(j.created_at)}</div></div>
            <div className="glass-sub p-3"><div className="text-white/60">Finished</div><div className="mt-0.5">{j.finished_at ? utc(j.finished_at) : "—"}</div></div>
          </div>}
        </Glass>
      </div>

      <CustodyPanel jobId={id} events={chain.data} loading={chain.isLoading} sealed={!!j?.sealed} />
    </>
  );
}

/** The job's hash chain with an animated verification sweep driven by GET /jobs/{id}/custody/verify. */
export function CustodyPanel({ jobId, events, loading, sealed }: { jobId: string; events?: import("@/api/types").CustodyEventView[]; loading: boolean; sealed: boolean }) {
  const reduced = useReducedMotion();
  const [sweep, setSweep] = useState(0);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<VerifyOut | null>(null);
  const timer = useRef<number>(undefined);
  useEffect(() => () => window.clearInterval(timer.current), []);
  useEffect(() => { setSweep(0); setResult(null); }, [jobId]);

  async function verify() {
    if (!events) return;
    setRunning(true);
    setResult(null);
    setSweep(0);
    const n = events.length;
    // The sweep is decoration while the request runs; with reduced motion the result simply appears.
    if (!reduced) timer.current = window.setInterval(() => setSweep((s) => Math.min(s + 1, n - 1)), Math.max(30, 1400 / n));
    try {
      const r = await api.verifyCustody(jobId);
      window.clearInterval(timer.current);
      setSweep(r.ok ? n : sweep);
      setResult(r);
    } finally {
      window.clearInterval(timer.current);
      setRunning(false);
    }
  }

  return (
    <Glass className="mt-4 p-6" spotlight={false} glow="rgba(124,245,210,0.06)">
      <SectionTitle eyebrow="Chain of custody · ADR 0003" title={<span className="flex items-center gap-2"><Fingerprint className="size-4 text-mint" />Hash chain {events && <span className="font-mono text-[12px] text-white/60">{events.length} events</span>}</span>}
        right={<Button variant={result?.ok ? "outline" : "primary"} size="sm" loading={running} icon={<ShieldCheck className="size-3.5" />} onClick={verify} disabled={!events?.length}>{result ? "Verify again" : "Verify chain"}</Button>} />
      {loading ? <Skeleton className="h-32" /> : events && events.length > 0 ? <ChainViz events={events} verifyingTo={sweep} /> : (
        <div className="rounded-xl border border-dashed border-white/10 p-6 text-center text-[12.5px] text-white/60">The event list needs an API route (see README, API gaps). Verification still works through <span className="font-mono">/custody/verify</span>.</div>
      )}
      <AnimatePresence>
        {result && (
          <motion.div initial={{ opacity: 0, y: 10, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0 }}
            className={clsx("mt-4 flex flex-wrap items-center gap-x-8 gap-y-3 rounded-2xl border px-5 py-4", result.ok ? "border-mint/30 bg-mint/[0.06]" : "border-rose/30 bg-rose/[0.06]")}>
            <div className="flex items-center gap-3">
              {result.ok ? <ShieldCheck className="size-7 text-mint" /> : <ShieldAlert className="size-7 text-rose" />}
              <div>
                <div className={clsx("text-[15px] font-medium", result.ok ? "text-mint" : "text-rose")}>{result.ok ? "Chain intact" : "Chain verification failed"}</div>
                <div className="text-[11.5px] text-white/60">Recomputed every link, Merkle root and WORM anchor{sealed ? " · sealed" : ""}</div>
              </div>
            </div>
            {[["events", result.events], ["batches", result.batches_checked], ["items", result.items_checked], ["anchors", result.anchors_checked]].map(([l, v]) => (
              <div key={l as string}><div className="font-mono text-[18px]"><Counter value={v as number} /></div><div className="text-[10.5px] uppercase tracking-wider text-white/55">{l}</div></div>
            ))}
            {result.errors.length > 0 && <div className="w-full space-y-1">{result.errors.map((e) => <div key={e} className="flex items-center gap-2 font-mono text-[11.5px] text-rose"><XCircle className="size-3.5" />{e}</div>)}</div>}
          </motion.div>
        )}
      </AnimatePresence>
    </Glass>
  );
}
