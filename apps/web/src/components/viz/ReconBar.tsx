import { motion } from "framer-motion";
import { recon, toneVar, type ReconStatus } from "@/lib/status";
import { num } from "@/lib/format";

const ORDER: ReconStatus[] = ["matched", "matched_against_archive", "surplus", "gap", "unverifiable", "access_lost", "failed", "pending", "not_applicable"];

/** Stacked bar of units by reconciliation status, with a legend that doubles as counts. */
export function ReconBar({ counts }: { counts: Record<string, number> }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  // Known statuses in a fixed order, then anything the UI does not know yet (rendered, never dropped).
  const parts = [...ORDER, ...Object.keys(counts).filter((k) => !(ORDER as string[]).includes(k))].filter((k) => (counts[k] ?? 0) > 0);
  return (
    <div>
      <div className="flex h-3 overflow-hidden rounded-full bg-white/[0.05]">
        {parts.map((k, i) => (
          <motion.div key={k} initial={{ width: 0 }} animate={{ width: `${((counts[k] ?? 0) / total) * 100}%` }} transition={{ duration: 1, delay: i * 0.08, ease: [0.16, 1, 0.3, 1] }}
            style={{ background: k === "pending" ? "rgba(255,255,255,0.12)" : toneVar[recon(k).tone], boxShadow: `0 0 14px ${toneVar[recon(k).tone]}` }} className="h-full first:rounded-l-full last:rounded-r-full" title={recon(k).label} />
        ))}
      </div>
      <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2.5 sm:grid-cols-4">
        {parts.map((k) => (
          <div key={k} title={recon(k).hint}>
            <div className="flex items-center gap-1.5 text-[11.5px] text-white/60"><span className="size-2 rounded-full" style={{ background: toneVar[recon(k).tone] }} />{recon(k).label}</div>
            <div className="mt-0.5 font-mono text-[15px]">{num(counts[k])}<span className="ml-1.5 text-[11px] text-white/55">{(((counts[k] ?? 0) / total) * 100).toFixed(1)}%</span></div>
          </div>
        ))}
      </div>
    </div>
  );
}
