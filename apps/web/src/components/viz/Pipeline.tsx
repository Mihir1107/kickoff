import clsx from "clsx";
import { motion } from "framer-motion";
import { Check, FileSearch, Fingerprint, Lock, UploadCloud, X } from "lucide-react";

const STAGES = [
  { key: "uploading", label: "Upload", sub: "parts · Content-Digest", icon: UploadCloud },
  { key: "locking", label: "Hash & lock", sub: "SHA-256 · Object Lock", icon: Fingerprint },
  { key: "validating", label: "Validate", sub: "central directory (locked)", icon: FileSearch },
  { key: "ready", label: "Ready", sub: "connection created", icon: Lock },
] as const;

/** Export ingest stages (ADR 0014). A rejection marks the stage it happened in. */
export function Pipeline({ status, rejectStage }: { status: string; rejectStage?: string }) {
  const rejected = status === "rejected";
  const idx = rejected ? Math.max(0, STAGES.findIndex((s) => s.key === (rejectStage === "hash" ? "locking" : "validating"))) : STAGES.findIndex((s) => s.key === status);
  return (
    <div className="flex items-start">
      {STAGES.map((s, i) => {
        const done = !rejected ? i < idx || status === "ready" : i < idx;
        const active = i === idx && status !== "ready";
        const bad = rejected && i === idx;
        return (
          <div key={s.key} className="flex flex-1 items-start last:flex-none">
            <div className="flex w-24 flex-col items-center text-center">
              <div className={clsx("relative grid size-11 place-items-center rounded-2xl border transition-all duration-700",
                bad ? "border-rose/50 bg-rose/15 text-rose" : done ? "border-mint/40 bg-mint/15 text-mint" : active ? "border-cyan/50 bg-cyan/10 text-cyan" : "border-white/10 bg-white/[0.03] text-white/55")}>
                {active && !bad && <span className="absolute inset-0 animate-pulse-ring rounded-2xl border border-cyan/60" />}
                {bad ? <X className="size-5" /> : done ? <Check className="size-5" /> : <s.icon className="size-5" />}
              </div>
              <div className={clsx("mt-2 text-[12px] font-medium", done || active ? "text-white/90" : "text-white/60")}>{s.label}</div>
              <div className="text-[10.5px] text-white/55">{s.sub}</div>
            </div>
            {i < STAGES.length - 1 && (
              <div className="relative mt-[22px] h-[2px] flex-1 overflow-hidden rounded-full bg-white/[0.07]">
                <motion.div className="absolute inset-y-0 left-0 bg-gradient-to-r from-mint to-cyan" initial={{ width: 0 }} animate={{ width: done ? "100%" : active ? "40%" : "0%" }} transition={{ duration: 0.9 }} />
                {active && !rejected && <div className="absolute inset-y-0 w-1/3 animate-shimmer bg-[linear-gradient(90deg,transparent,rgba(90,216,255,0.8),transparent)] bg-[length:200%_100%]" />}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
