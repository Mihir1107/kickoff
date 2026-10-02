import { AnimatePresence, motion } from "framer-motion";
import { useMemo, useState } from "react";
import type { UnitOut } from "@/api/types";
import { conversationName } from "@/lib/names";
import { recon, toneVar } from "@/lib/status";
import { num } from "@/lib/format";

/** Colour of one unit: in-flight/pending by status, finished units by reconciliation result. */
function cellColor(u: UnitOut): string {
  if (u.status === "running") return "var(--live)";
  if (u.status === "pending") return "rgba(255,255,255,0.06)";
  if (u.recon_status === "matched" && (u.expected_count ?? 0) === 0) return "rgba(124,245,210,0.22)";
  return toneVar[recon(u.recon_status).tone];
}

/**
 * Conversation × UTC day grid: one cell per work unit (ADR 0005). Gaps, surplus and unverifiable days
 * stand out, and live units pulse while they collect.
 */
export function UnitHeatmap({ units }: { units: UnitOut[] }) {
  const [hover, setHover] = useState<{ u: UnitOut; x: number; y: number } | null>(null);
  const { rows, days } = useMemo(() => {
    const byConv = new Map<string, Map<string, UnitOut>>();
    const daySet = new Set<string>();
    for (const u of units) {
      if (u.kind !== "conversation_day") continue;
      const [conv, d] = u.unit_key.split("/") as [string, string];
      daySet.add(d);
      if (!byConv.has(conv)) byConv.set(conv, new Map());
      byConv.get(conv)!.set(d, u);
    }
    return { rows: [...byConv.entries()], days: [...daySet].sort() };
  }, [units]);

  const max = useMemo(() => Math.max(1, ...units.map((u) => u.expected_count ?? u.collected_count)), [units]);

  return (
    <div className="relative">
      <div className="overflow-x-auto pb-2">
        <div className="inline-grid min-w-full gap-y-[5px]" style={{ gridTemplateColumns: `150px repeat(${days.length}, minmax(11px, 1fr))`, columnGap: 3 }}>
          <div />
          {days.map((d, i) => (
            <div key={d} className="h-4 overflow-visible whitespace-nowrap font-mono text-[9px] text-white/55">{i % 7 === 0 ? d.slice(5) : ""}</div>
          ))}
          {rows.map(([conv, cells], r) => (
            <div key={conv} className="contents">
              <div className="truncate pr-3 text-[12px] leading-[14px] text-white/60" title={conv}>{conversationName(conv)}</div>
              {days.map((d, c) => {
                const u = cells.get(d);
                if (!u) return <div key={d} />;
                const color = cellColor(u);
                const intensity = u.status === "done" && (u.recon_status === "matched" || u.recon_status === "matched_against_archive") ? 0.35 + 0.65 * Math.sqrt((u.collected_count || 0) / max) : 1;
                return (
                  <div key={d}
                    onMouseEnter={(e) => { const b = e.currentTarget.getBoundingClientRect(); setHover({ u, x: b.left + b.width / 2, y: b.top }); }}
                    onMouseLeave={() => setHover(null)}
                    className="relative h-[14px] cursor-crosshair rounded-[3px] transition-[transform,background] duration-500 hover:z-10 hover:scale-[1.6]"
                    style={{
                      background: color, opacity: intensity,
                      boxShadow: u.status === "done" && u.recon_status !== "matched" && u.recon_status !== "matched_against_archive" ? `0 0 10px ${color}` : undefined,
                      animation: `cell-in 0.6s cubic-bezier(.16,1,.3,1) both ${(c * 8 + r * 25) / 1000}s${u.status === "running" ? ", pulse 1.2s ease-in-out infinite" : ""}`,
                    }} />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <style>{`@keyframes cell-in{from{transform:scale(0);opacity:0}}`}</style>
      <AnimatePresence>
        {hover && (
          <motion.div initial={{ opacity: 0, y: 6, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}
            className="glass pointer-events-none fixed z-[90] w-60 -translate-x-1/2 -translate-y-full p-3 text-[12px]"
            style={{ left: hover.x, top: hover.y - 10 }}>
            <div className="font-mono text-[11px] text-white/65">{hover.u.unit_key}</div>
            <div className="mt-2 grid grid-cols-2 gap-y-1">
              <span className="text-white/60">Status</span><span className="text-right">{hover.u.status}</span>
              <span className="text-white/60">Reconciliation</span><span className="text-right" style={{ color: toneVar[recon(hover.u.recon_status).tone] }}>{recon(hover.u.recon_status).label}</span>
              <span className="text-white/60">Expected</span><span className="text-right font-mono">{num(hover.u.expected_count)}</span>
              <span className="text-white/60">Collected</span><span className="text-right font-mono">{num(hover.u.collected_count)}</span>
              {hover.u.file_gaps > 0 && (<><span className="text-white/60">File gaps</span><span className="text-right font-mono text-amber">{hover.u.file_gaps}</span></>)}
            </div>
            {hover.u.last_error && <div className="mt-2 rounded-md bg-rose/10 p-2 font-mono text-[10.5px] text-rose">{hover.u.last_error}</div>}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export function HeatLegend() {
  const items: [string, string][] = [
    ["Matched", "var(--ok)"], ["Matched (export)", "var(--info)"], ["Collecting", "var(--live)"], ["Gap", "var(--warn)"], ["Surplus", "var(--orange)"],
    ["Unverifiable", "var(--violet)"], ["Failed / access lost", "var(--bad)"], ["Pending", "rgba(255,255,255,0.12)"],
  ];
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-2">
      {items.map(([l, c]) => (
        <span key={l} className="flex items-center gap-1.5 text-[11.5px] text-white/65"><span className="size-2.5 rounded-[3px]" style={{ background: c }} />{l}</span>
      ))}
    </div>
  );
}
