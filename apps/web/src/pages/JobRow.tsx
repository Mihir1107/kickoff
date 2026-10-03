import { motion } from "framer-motion";
import { ArrowUpRight, Lock, RotateCcw } from "lucide-react";
import { Link } from "react-router-dom";
import type { JobOut } from "@/api/types";
import { StatusPill } from "@/components/ui/StatusPill";
import { ago, duration, shortId } from "@/lib/format";
import { scopeName } from "@/lib/names";
import { jobStatus } from "@/lib/status";

export const unitTotal = (j: JobOut) => Object.values(j.units).reduce((a, b) => a + (b ?? 0), 0);
export const unitProgress = (j: JobOut) => (unitTotal(j) ? ((j.units.done ?? 0) + (j.units.failed ?? 0)) / unitTotal(j) : 0);

/** One job as a row: status, scopes, a live progress rail, and seal state. */
export function JobRow({ j, matterName, i = 0 }: { j: JobOut; matterName?: string; i?: number }) {
  const m = jobStatus(j.status);
  const p = unitProgress(j);
  const color = j.clean ? "var(--ok)" : `var(--${m.tone === "live" ? "live" : m.tone === "bad" ? "bad" : m.tone === "violet" ? "violet" : m.tone === "muted" ? "muted" : "warn"})`;
  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(i * 0.04, 0.5) }}>
      <Link to={`/jobs/${j.id}`} className="group relative grid grid-cols-[1fr_auto] items-center gap-4 overflow-hidden rounded-2xl border border-white/[0.06] bg-white/[0.02] px-5 py-4 transition hover:border-white/15 hover:bg-white/[0.04] md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_170px_auto]">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[11px] text-white/55">{shortId(j.id)}</span>
            {j.rerun_of && <span className="flex items-center gap-1 rounded-md bg-iris/10 px-1.5 text-[10.5px] text-iris"><RotateCcw className="size-2.5" />rerun</span>}
            {j.sealed && <span title="Chain sealed to WORM" className="flex items-center gap-1 rounded-md bg-white/[0.05] px-1.5 text-[10.5px] text-white/55"><Lock className="size-2.5" />sealed</span>}
          </div>
          <div className="mt-1 truncate text-[14px] font-medium">{matterName ?? `${j.scopes.length} scope${j.scopes.length === 1 ? "" : "s"}`}</div>
          <div className="truncate text-[12px] text-white/60">{j.scopes.slice(0, 4).map((s) => scopeName(s.type, s.external_id)).join(" · ")}{j.scopes.length > 4 ? ` +${j.scopes.length - 4}` : ""}</div>
        </div>
        <div className="hidden md:block">
          <div className="mb-1.5 flex justify-between font-mono text-[11px] text-white/60"><span>{j.units.done ?? 0}/{unitTotal(j)} units</span><span>{Math.round(p * 100)}%</span></div>
          <div data-progress-track className="relative h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
            <motion.div data-progress-fill className="absolute inset-y-0 left-0 rounded-full" style={{ background: color, boxShadow: `0 0 10px ${color}` }} initial={{ width: 0 }} animate={{ width: `${p * 100}%` }} transition={{ duration: 1.1, ease: [0.16, 1, 0.3, 1] }} />
            {j.status === "running" && <div className="absolute inset-0 animate-shimmer bg-[linear-gradient(90deg,transparent,rgba(255,255,255,0.35),transparent)] bg-[length:200%_100%]" />}
          </div>
        </div>
        <div className="hidden text-[12px] text-white/60 md:block">
          {ago(j.created_at)}
          {j.finished_at && <div className="font-mono text-[11px] text-white/55">took {duration(Date.parse(j.finished_at) - Date.parse(j.created_at))}</div>}
          {j.paused_ms != null && <div className="font-mono text-[11px] text-amber/80">paused {duration(j.paused_ms)}</div>}
        </div>
        <div className="flex items-center gap-3">
          <StatusPill tone={m.tone} label={m.label} title={m.hint} />
          <ArrowUpRight aria-hidden className="size-4 text-white/20 transition group-hover:text-white/70" />
        </div>
      </Link>
    </motion.div>
  );
}
