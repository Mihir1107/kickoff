import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { Anchor, Check, Link2, Lock, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { CustodyEventView } from "@/api/types";
import { Hash } from "@/components/ui/Hash";
import { utc } from "@/lib/format";
import { useReducedMotion } from "@/lib/motion";

const TYPE_COLOR: Record<string, string> = {
  connection_verified: "#9fb4ff", job_started: "#5ad8ff", units_enumerated: "#5ad8ff", items_collected: "#7cf5d2",
  job_paused: "#ffc86b", units_reconciled: "#b59cff", job_finalized: "#b59cff", chain_sealed: "#ffffff",
};

/**
 * The custody hash chain as linked blocks: event_hash = sha256(prev_hash + canonical_json(event)).
 * `verifying` sweeps a scan across the blocks; `verified` maps seq → ok after the result arrives.
 */
export function ChainViz({ events, verifyingTo, failedAt }: { events: CustodyEventView[]; verifyingTo: number; failedAt?: number }) {
  const [sel, setSel] = useState<CustodyEventView | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  useEffect(() => {
    const el = scroller.current?.querySelector<HTMLElement>(`[data-seq="${verifyingTo}"]`);
    if (el && verifyingTo > 0) el.scrollIntoView({ behavior: reduced ? "auto" : "smooth", inline: "center", block: "nearest" });
  }, [verifyingTo, reduced]);

  return (
    <div>
      <div ref={scroller} className="relative overflow-x-auto pb-4 pt-2">
        <div className="flex w-max items-center px-6">{/* w-max: the end padding counts toward scroll width */}
          {events.map((e, i) => {
            const verified = verifyingTo >= e.seq && failedAt !== e.seq;
            const color = TYPE_COLOR[e.event_type] ?? "#8a90a6";
            return (
              <div key={e.seq} className="flex items-center" data-seq={e.seq}>
                {i > 0 && (
                  <svg width="44" height="24" className="shrink-0 overflow-visible">
                    <line x1="0" y1="12" x2="44" y2="12" stroke={verified ? "#7cf5d2" : "rgba(255,255,255,0.18)"} strokeWidth="1.5" strokeDasharray="4 4" className={verified && !reduced ? "animate-dash" : ""} />
                    {verified && !reduced && <circle r="2.5" cy="12" fill="#7cf5d2"><animate attributeName="cx" from="0" to="44" dur="1.1s" repeatCount="indefinite" /></circle>}
                  </svg>
                )}
                <motion.button
                  initial={{ opacity: 0, y: 20, rotateX: -40 }} animate={{ opacity: 1, y: 0, rotateX: 0 }} transition={{ delay: Math.min(i * 0.03, 1), type: "spring", stiffness: 260, damping: 22 }}
                  whileHover={{ y: -4 }} onClick={() => setSel(e)}
                  className={clsx("focus-ring group relative w-[150px] shrink-0 rounded-2xl border p-3 text-left transition-colors duration-500",
                    failedAt === e.seq ? "border-rose/60 bg-rose/10" : verified ? "border-mint/40 bg-mint/[0.06]" : "border-white/[0.08] bg-white/[0.025]",
                    sel?.seq === e.seq && "ring-1 ring-white/40")}
                  style={{ boxShadow: verified ? "0 0 30px -8px rgba(124,245,210,0.5)" : undefined }}>
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] text-white/60">#{String(e.seq).padStart(3, "0")}</span>
                    <span className="flex items-center gap-1">
                      {e.anchored && <Anchor className="size-3 text-amber" />}
                      <AnimatePresence>
                        {verified && <motion.span initial={{ scale: 0, rotate: -90 }} animate={{ scale: 1, rotate: 0 }} className="grid size-4 place-items-center rounded-full bg-mint text-ink-950"><Check className="size-2.5" strokeWidth={3.5} /></motion.span>}
                        {failedAt === e.seq && <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} className="grid size-4 place-items-center rounded-full bg-rose text-ink-950"><X className="size-2.5" strokeWidth={3.5} /></motion.span>}
                      </AnimatePresence>
                    </span>
                  </div>
                  <div className="mt-2 flex items-center gap-1.5">
                    <span className="size-1.5 rounded-full" style={{ background: color, boxShadow: `0 0 8px ${color}` }} />
                    <span className="truncate text-[12px] font-medium">{e.event_type.replace(/_/g, " ")}</span>
                  </div>
                  <div className="mt-2 font-mono text-[10px] leading-tight text-white/55">{e.event_hash.slice(0, 16)}</div>
                  {e.items && <div className="mt-1 font-mono text-[10px] text-mint/70">{e.items} items · merkle</div>}
                  {e.event_type === "chain_sealed" && <Lock className="absolute bottom-3 right-3 size-3.5 text-white/60" />}
                </motion.button>
              </div>
            );
          })}
        </div>
      </div>

      <AnimatePresence mode="wait">
        {sel && (
          <motion.div key={sel.seq} initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden">
            <div className="glass-sub mt-2 grid gap-4 p-4 md:grid-cols-[1fr_auto_1fr]">
              <div>
                <div className="eyebrow mb-2">Event #{sel.seq} · {sel.event_type}</div>
                <div className="space-y-1.5 text-[12.5px]">
                  <Row k="actor" v={<span className="font-mono text-[11.5px]">{sel.actor}</span>} />
                  <Row k="created_at" v={utc(sel.created_at)} />
                  {sel.merkle_root && <Row k="merkle_root" v={<Hash value={sel.merkle_root} />} />}
                  {sel.items && <Row k="items (RFC 6962 leaves)" v={sel.items} />}
                  <Row k="anchored to WORM" v={sel.anchored ? <span className="text-amber">yes</span> : "no"} />
                </div>
              </div>
              <div className="hidden w-px bg-white/[0.07] md:block" />
              <div className="font-mono text-[11.5px]">
                <div className="eyebrow mb-2">Link</div>
                <div className="text-white/60">prev_hash</div>
                <div className="break-all text-white/70">{sel.prev_hash}</div>
                <div className="my-2 flex items-center gap-2 text-white/55"><Link2 className="size-3" />sha256(prev_hash ‖ jcs(event))</div>
                <div className="text-white/60">event_hash</div>
                <div className="break-all text-mint">{sel.event_hash}</div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex justify-between gap-4"><span className="text-white/60">{k}</span><span className="text-right">{v}</span></div>;
}
