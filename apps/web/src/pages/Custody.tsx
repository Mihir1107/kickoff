import clsx from "clsx";
import { motion } from "framer-motion";
import { Anchor, FileCheck2, Lock, Terminal } from "lucide-react";
import { useMemo, useState } from "react";
import { useCustodyEvents, useRollup } from "@/api/hooks";
import { PageHeader } from "@/components/layout/PageHeader";
import { Glass, SectionTitle } from "@/components/ui/Glass";
import { StatusPill } from "@/components/ui/StatusPill";
import { ago, shortId } from "@/lib/format";
import { jobStatus } from "@/lib/status";
import { CustodyPanel } from "./JobDetail";

/** A tiny animated RFC 6962 Merkle tree: leaves are items ordered by idempotency key. */
function MerkleTree() {
  const levels = [8, 4, 2, 1];
  const W = 340, H = 170;
  const pos = (lvl: number, i: number) => ({ x: ((i + 0.5) / levels[lvl]!) * W, y: H - 18 - lvl * 46 });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      {levels.slice(1).map((n, l) => Array.from({ length: n }, (_, i) => {
        const p = pos(l + 1, i);
        return [0, 1].map((k) => {
          const c = pos(l, i * 2 + k);
          return <motion.line key={`${l}-${i}-${k}`} x1={c.x} y1={c.y} x2={p.x} y2={p.y} stroke="rgba(124,245,210,0.35)" strokeWidth="1.2"
            initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ delay: 0.3 + l * 0.35, duration: 0.5 }} />;
        });
      }))}
      {levels.map((n, l) => Array.from({ length: n }, (_, i) => {
        const p = pos(l, i);
        const root = l === levels.length - 1;
        return (
          <motion.g key={`${l}-${i}`} initial={{ scale: 0, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ delay: l * 0.35 + i * 0.04, type: "spring" }} style={{ transformOrigin: `${p.x}px ${p.y}px` }}>
            <rect x={p.x - (root ? 30 : 14)} y={p.y - 10} width={root ? 60 : 28} height={20} rx={6} fill={root ? "rgba(124,245,210,0.18)" : l === 0 ? "rgba(139,124,255,0.14)" : "rgba(255,255,255,0.05)"} stroke={root ? "#7cf5d2" : "rgba(255,255,255,0.15)"} />
            <text x={p.x} y={p.y + 3.5} textAnchor="middle" className="fill-white/70 font-mono" fontSize="8.5">{root ? "root" : l === 0 ? `i${i}` : "h"}</text>
          </motion.g>
        );
      }))}
    </svg>
  );
}

export function Custody() {
  const { data } = useRollup();
  const jobs = useMemo(() => data?.jobs.filter((j) => j.status !== "pending") ?? [], [data]);
  const [sel, setSel] = useState<string>("");
  const id = sel || jobs[0]?.id || "";
  const events = useCustodyEvents(id);
  const job = jobs.find((j) => j.id === id);
  const name = new Map(data?.matters.map((m) => [m.id, m.name]));

  return (
    <>
      <PageHeader eyebrow="Tamper evidence" title={<>Chain of <em className="text-gradient">custody</em></>}
        subtitle="Every action appends an event whose hash covers the previous one. Batches carry a Merkle root over their items; anchors and seals go to WORM storage, so the history can be proven offline." />

      <div className="grid gap-4 lg:grid-cols-3">
        {[
          { icon: FileCheck2, t: "Hash chain", d: "event_hash = sha256(prev_hash ‖ jcs(event)). The table is append-only; triggers reject UPDATE and DELETE.", c: "#7cf5d2" },
          { icon: Anchor, t: "WORM anchors", d: "Lifecycle events and every N events anchor the head to S3 Object Lock (COMPLIANCE). Anchors are listed from S3 versions, never the DB.", c: "#ffc86b" },
          { icon: Lock, t: "Seal + offline verify", d: "Finalize seals the job's chain. edisc-verify checks an exported package with no database and no cloud access.", c: "#8b7cff" },
        ].map((x, i) => (
          <Glass key={x.t} className="p-6" glow={`${x.c}1a`} initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.08 }}>
            <x.icon className="size-5" style={{ color: x.c }} />
            <div className="mt-4 text-[15px] font-medium">{x.t}</div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-white/65">{x.d}</p>
          </Glass>
        ))}
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[320px_1fr]">
        <Glass className="p-4" spotlight={false}>
          <div className="eyebrow mb-3 px-2">Streams (one per job)</div>
          <div className="max-h-[560px] space-y-1 overflow-y-auto">
            {jobs.map((j) => (
              <button key={j.id} onClick={() => setSel(j.id)} className={clsx("relative w-full rounded-xl px-3 py-2.5 text-left transition", id === j.id ? "text-white" : "text-white/55 hover:bg-white/[0.03]")}>
                {id === j.id && <motion.span layoutId="stream" className="absolute inset-0 rounded-xl border border-mint/25 bg-mint/[0.06]" />}
                <div className="relative flex items-center justify-between gap-2">
                  <span className="truncate text-[13px]">{name.get(j.matter_id)}</span>
                  {j.sealed && <Lock className="size-3 shrink-0 text-white/60" />}
                </div>
                <div className="relative mt-0.5 flex items-center gap-2 font-mono text-[10.5px] text-white/55">{shortId(j.id)} · {ago(j.created_at)}</div>
              </button>
            ))}
          </div>
        </Glass>

        <div className="min-w-0 space-y-4">
          {job && (
            <div className="flex flex-wrap items-center gap-3">
              <StatusPill tone={jobStatus(job.status).tone} label={jobStatus(job.status).label} />
              <span className="text-[13px] text-white/60">{name.get(job.matter_id)}</span>
            </div>
          )}
          {id && <div className="-mt-4"><CustodyPanel jobId={id} events={events.data} loading={events.isLoading} sealed={!!job?.sealed} /></div>}
          <div className="grid gap-4 md:grid-cols-2">
            <Glass className="p-6">
              <SectionTitle eyebrow="RFC 6962" title="Batch Merkle root" />
              <MerkleTree />
              <p className="mt-2 text-[12px] text-white/60">Each items_collected event commits to exactly the links inserted in its transaction, so a missing or altered item changes the root.</p>
            </Glass>
            <Glass className="p-6">
              <SectionTitle eyebrow="Offline" title={<span className="flex items-center gap-2"><Terminal className="size-4 text-iris" />edisc-verify</span>} />
              {job?.sealed ? (
                <pre className="overflow-x-auto rounded-xl border border-white/[0.06] bg-ink-950/70 p-4 font-mono text-[11.5px] leading-relaxed text-white/70">
{`$ edisc-verify package.zip
`}<span className="text-white/60">{`  chain     `}</span>{`${events.data?.length ?? 0} events linked
`}<span className="text-white/60">{`  batches   `}</span>{`${events.data?.filter((e) => e.items).length ?? 0} merkle roots recomputed
`}<span className="text-white/60">{`  anchors   `}</span>{`${events.data?.filter((e) => e.anchored).length ?? 0} matched to WORM versions
`}<span className="text-mint">{`  OK `}</span>{`no database, no cloud credentials`}
                </pre>
              ) : (
                <div className="rounded-xl border border-dashed border-white/10 p-5 text-[12.5px] leading-relaxed text-white/60">
                  No package yet: this chain is still open. A custody package is exported once the job is finalized and its chain sealed.
                </div>
              )}
              <p className="mt-2 text-[11px] text-white/55">Illustrative output (demo data). Package export has no API route yet.</p>
            </Glass>
          </div>
        </div>
      </div>
    </>
  );
}
