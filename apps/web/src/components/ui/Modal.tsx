import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "@/lib/focus";

/** A modal dialog: portalled outside #root, focus trapped inside, focus restored on close, Escape closes. */
export function Modal({ open, onClose, title, subtitle, children, width = 520 }: { open: boolean; onClose: () => void; title: ReactNode; subtitle?: ReactNode; children: ReactNode; width?: number }) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const subtitleId = useId();
  useFocusTrap(open, panel);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50 grid place-items-center p-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <motion.div aria-hidden className="absolute inset-0 bg-ink-950/70 backdrop-blur-md" onClick={onClose} />
          <motion.div ref={panel} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={subtitle ? subtitleId : undefined} tabIndex={-1}
            className="glass relative max-h-[90vh] w-full overflow-y-auto p-6" style={{ maxWidth: width }}
            initial={{ opacity: 0, y: 24, scale: 0.96, filter: "blur(8px)" }} animate={{ opacity: 1, y: 0, scale: 1, filter: "blur(0px)" }}
            exit={{ opacity: 0, y: 12, scale: 0.98, filter: "blur(6px)" }} transition={{ type: "spring", stiffness: 380, damping: 32 }}>
            <div className="mb-5 flex items-start justify-between gap-4">
              <div>
                <h2 id={titleId} className="font-display text-[28px] leading-none tracking-tight">{title}</h2>
                {subtitle && <p id={subtitleId} className="mt-2 text-[13px] text-white/65">{subtitle}</p>}
              </div>
              <button type="button" onClick={onClose} aria-label="Close dialog" className="focus-ring rounded-lg p-1.5 text-white/60 hover:bg-white/5 hover:text-white"><X className="size-4" /></button>
            </div>
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[12px] font-medium text-white/60">{label}</span>
      {children}
      {hint && <span className="mt-1.5 block text-[11.5px] text-white/55">{hint}</span>}
    </label>
  );
}
