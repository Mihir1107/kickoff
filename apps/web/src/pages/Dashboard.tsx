import { motion } from "framer-motion";
import { Activity, AlertTriangle, ArrowUpRight, Database, FileArchive, Fingerprint, Plus, Radar, ShieldCheck } from "lucide-react";
import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useRollup } from "@/api/hooks";
import type { JobOut } from "@/api/types";
import { PageHeader, rise, stagger } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Counter } from "@/components/ui/Counter";
import { Skeleton } from "@/components/ui/Empty";
import { Glass, SectionTitle } from "@/components/ui/Glass";
import { Ring } from "@/components/ui/Ring";
import { StatusPill } from "@/components/ui/StatusPill";
import { ago, shortId } from "@/lib/format";
import { scopeName } from "@/lib/names";
import { jobStatus } from "@/lib/status";

const progress = (j: JobOut) => {
  const total = Object.values(j.units).reduce((a, b) => a + (b ?? 0), 0);
  return total ? ((j.units.done ?? 0) + (j.units.failed ?? 0)) / total : 0;
};

export function Dashboard() {
  const { data, isLoading } = useRollup();
  const nav = useNavigate();
  const stats = useMemo(() => {
    if (!data) return null;
    const { jobs, matters, exports, connections } = data;
    const finished = jobs.filter((j) => j.finished_at && j.status !== "cancelled");
    const unitsDone = jobs.reduce((n, j) => n + (j.units.done ?? 0), 0);
    return {
      openMatters: matters.filter((m) => !m.closed_at).length,
      live: jobs.filter((j) => j.status === "running" || j.status === "pending"),
      attention: jobs.filter((j) => ["paused_awaiting_reauth", "failed", "completed_with_gaps", "completed_unverified", "completed_against_archive"].includes(j.status)),
      cleanRate: finished.length ? finished.filter((j) => j.clean).length / finished.length : 0,
      sealed: jobs.filter((j) => j.sealed).length,
      unitsDone,
      exportsReady: exports.filter((x) => x.status === "ready").length,
      connErrors: connections.filter((c) => c.status === "error").length,
      finished: finished.length,
    };
  }, [data]);
  const matterName = new Map(data?.matters.map((m) => [m.id, m.name]));

  return (
    <>
      <PageHeader
        eyebrow={<span className="flex items-center gap-2"><span className="size-1.5 animate-pulse rounded-full bg-mint" />Halcyon Legal · {new Date().toUTCString().slice(0, 16)}</span>}
        title={<>Command <em className="text-gradient">Center</em></>}
        subtitle="Every collection, reconciled per conversation-day and sealed into a tamper-evident chain. Here's where things stand."
        actions={<Button variant="primary" icon={<Plus className="size-4" />} onClick={() => nav("/collections/new")}>New collection</Button>}
      />

      <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {[
          { label: "In flight (running + queued)", value: stats?.live.length, icon: Radar, color: "#5ad8ff" },
          { label: "Open matters", value: stats?.openMatters, icon: Database, color: "#8b7cff" },
          { label: "Conversation-days collected", value: stats?.unitsDone, icon: Activity, color: "#7cf5d2" },
          { label: "Chains sealed to WORM", value: stats?.sealed, icon: Fingerprint, color: "#ffc86b" },
        ].map((k) => (
          <motion.div key={k.label} variants={rise}>
            <Glass className="p-5" glow={`${k.color}22`}>
              <div className="flex items-start justify-between">
                <div className="grid size-9 place-items-center rounded-xl border border-white/10 bg-white/[0.04]" style={{ color: k.color }}><k.icon className="size-[18px]" /></div>
              </div>
              <div className="mt-5 text-[40px] font-light leading-none tracking-tight">{k.value === undefined ? <Skeleton className="h-10 w-20" /> : <Counter value={k.value} />}</div>
              <div className="mt-2 text-[12.5px] text-white/60">{k.label}</div>
            </Glass>
          </motion.div>
        ))}
      </motion.div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1.6fr_1fr]">
        <Glass className="p-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25 }}>
          <SectionTitle eyebrow="In flight" title="Live collections" right={<Link to="/collections" className="flex items-center gap-1 text-[12px] text-white/60 hover:text-white">All <ArrowUpRight className="size-3.5" /></Link>} />
          {isLoading && <div className="space-y-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-16" />)}</div>}
          <div className="space-y-2.5">
            {stats?.live.map((j, i) => (
              <motion.div key={j.id} initial={{ opacity: 0, x: -12 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.3 + i * 0.06 }}>
                <Link to={`/jobs/${j.id}`} className="group relative block overflow-hidden rounded-2xl border border-white/[0.06] bg-white/[0.02] p-4 transition hover:border-white/15 hover:bg-white/[0.04]">
                  <div className="absolute inset-y-0 left-0 bg-[linear-gradient(90deg,rgba(90,216,255,0.10),transparent)] transition-[width] duration-1000" style={{ width: `${progress(j) * 100}%` }} />
                  <div className="relative flex items-center gap-4">
                    <Ring value={progress(j)} size={46} stroke={4.5}><span className="font-mono text-[10px]">{Math.round(progress(j) * 100)}</span></Ring>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[14px] font-medium">{matterName.get(j.matter_id)}</div>
                      <div className="mt-0.5 truncate text-[12px] text-white/60">
                        {j.scopes.slice(0, 3).map((s) => scopeName(s.type, s.external_id)).join(", ")}{j.scopes.length > 3 ? ` +${j.scopes.length - 3}` : ""}
                      </div>
                    </div>
                    <div className="hidden text-right sm:block">
                      <div className="font-mono text-[13px]"><Counter value={j.units.done ?? 0} /> <span className="text-white/55">/ {Object.values(j.units).reduce((a, b) => a + (b ?? 0), 0)}</span></div>
                      <div className="text-[11px] text-white/55">units · started {ago(j.created_at)}</div>
                    </div>
                    <StatusPill tone={jobStatus(j.status).tone} label={jobStatus(j.status).label} />
                    <ArrowUpRight aria-hidden className="size-4 text-white/20 transition group-hover:-translate-y-0.5 group-hover:translate-x-0.5 group-hover:text-white/70" />
                  </div>
                </Link>
              </motion.div>
            ))}
            {stats && stats.live.length === 0 && <div className="py-10 text-center text-[13px] text-white/60">No collections in flight.</div>}
          </div>
        </Glass>

        <Glass className="flex flex-col p-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.32 }}>
          <SectionTitle eyebrow="Integrity" title="Clean completion rate" />
          <div className="flex flex-1 items-center gap-6">
            <Ring value={stats?.cleanRate ?? 0} size={150} stroke={11}>
              <div className="text-center">
                <div className="text-[34px] font-light leading-none"><Counter value={(stats?.cleanRate ?? 0) * 100} decimals={0} suffix="%" /></div>
                <div className="mt-1 text-[10.5px] uppercase tracking-widest text-white/55">reconciled</div>
              </div>
            </Ring>
            <div className="space-y-3 text-[12.5px]">
              <p className="text-white/65">Only <span className="text-mint">completed</span> counts. Gaps and unverified units never pass as clean.</p>
              <div className="flex items-center gap-2 text-white/60"><ShieldCheck className="size-4 text-mint" />{stats?.finished ?? "…"} finished · {stats?.sealed ?? "…"} sealed</div>
              <div className="flex items-center gap-2 text-white/60"><FileArchive className="size-4 text-iris" />{stats?.exportsReady ?? "…"} Slack exports ready</div>
            </div>
          </div>
        </Glass>
      </div>

      <Glass className="mt-4 p-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.4 }} glow="rgba(255,200,107,0.08)">
        <SectionTitle eyebrow="Needs attention" title={<span className="flex items-center gap-2"><AlertTriangle className="size-4 text-amber" />Not clean, and loud about it</span>} />
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {stats?.attention.map((j, i) => {
            const m = jobStatus(j.status);
            return (
              <motion.div key={j.id} initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: 0.45 + i * 0.05 }}>
                <Link to={`/jobs/${j.id}`} className="block rounded-2xl border border-white/[0.06] bg-white/[0.02] p-4 transition hover:-translate-y-0.5 hover:border-white/15">
                  <div className="flex items-center justify-between">
                    <StatusPill tone={m.tone} label={m.label} />
                    <span className="font-mono text-[11px] text-white/55">{shortId(j.id)}</span>
                  </div>
                  <div className="mt-3 truncate text-[13.5px] font-medium">{matterName.get(j.matter_id)}</div>
                  <div className="mt-1 text-[12px] leading-snug text-white/60">{m.hint}</div>
                </Link>
              </motion.div>
            );
          })}
        </div>
      </Glass>
    </>
  );
}
