import clsx from "clsx";
import { motion } from "framer-motion";
import type { ReactNode } from "react";

/** Segmented tabs whose active pill morphs between options (shared layoutId). */
export function Tabs<T extends string>({ value, onChange, options, id }: { value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode; count?: number }[]; id: string }) {
  return (
    <div className="inline-flex rounded-2xl border border-white/[0.07] bg-white/[0.025] p-1 backdrop-blur">
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={value === o.value} onClick={() => onChange(o.value)}
          className={clsx("focus-ring relative rounded-xl px-3.5 py-1.5 text-[13px] font-medium transition-colors", value === o.value ? "text-white" : "text-white/65 hover:text-white/90")}>
          {value === o.value && (
            <motion.span layoutId={`tab-${id}`} className="absolute inset-0 rounded-xl border border-mint/80 bg-white/[0.08] shadow-[0_6px_20px_-8px_rgba(124,245,210,0.35)]"
              transition={{ type: "spring", stiffness: 420, damping: 34 }} />
          )}
          <span className="relative flex items-center gap-2">
            {o.label}
            {o.count !== undefined && <span className="rounded-md bg-white/[0.07] px-1.5 font-mono text-[10.5px] text-white/80">{o.count}</span>}
          </span>
        </button>
      ))}
    </div>
  );
}
