import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { short } from "@/lib/format";

/** A hash or id: monospace, shortened, copyable, full value on hover. */
export function Hash({ value, n = 6, className, label }: { value: string; n?: number; className?: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      title={value}
      onClick={(e) => {
        e.stopPropagation();
        e.preventDefault();
        void navigator.clipboard?.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
      className={clsx("focus-ring group inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 font-mono text-[11.5px] text-white/60 transition hover:bg-white/[0.06] hover:text-white", className)}
    >
      {label && <span className="text-white/55">{label}</span>}
      <span>{short(value, n)}</span>
      <span className="relative size-3 opacity-0 transition group-hover:opacity-100">
        <AnimatePresence mode="wait" initial={false}>
          {copied ? (
            <motion.span key="c" initial={{ scale: 0 }} animate={{ scale: 1 }} exit={{ scale: 0 }} className="absolute inset-0"><Check className="size-3 text-mint" /></motion.span>
          ) : (
            <motion.span key="p" initial={{ scale: 0 }} animate={{ scale: 1 }} exit={{ scale: 0 }} className="absolute inset-0"><Copy className="size-3" /></motion.span>
          )}
        </AnimatePresence>
      </span>
    </button>
  );
}
