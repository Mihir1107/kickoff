import { motion, type Variants } from "framer-motion";
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export function PageHeader({ crumbs, title, subtitle, actions, eyebrow }: { crumbs?: { label: string; to?: string }[]; title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode }) {
  return (
    <header className="mb-8">
      {crumbs && (
        <nav className="mb-4 flex flex-wrap items-center gap-1 text-[12px] text-white/60">
          {crumbs.map((c, i) => (
            <span key={i} className="flex items-center gap-1">
              {i > 0 && <ChevronRight aria-hidden className="size-3 text-white/20" />}
              {c.to ? <Link to={c.to} className="transition hover:text-white">{c.label}</Link> : <span className="text-white/65">{c.label}</span>}
            </span>
          ))}
        </nav>
      )}
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="min-w-0">
          {eyebrow && <div className="eyebrow mb-3">{eyebrow}</div>}
          <motion.h1 initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
            className="font-display text-[44px] leading-[0.95] tracking-[-0.015em] md:text-[56px]">{title}</motion.h1>
          {subtitle && <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.15 }} className="mt-3 max-w-2xl text-[14px] leading-relaxed text-white/65">{subtitle}</motion.p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </header>
  );
}

/** Stagger container + child for list entrances. */
export const stagger: Variants = { hidden: {}, show: { transition: { staggerChildren: 0.05, delayChildren: 0.05 } } };
export const rise: Variants = { hidden: { opacity: 0, y: 16, filter: "blur(6px)" }, show: { opacity: 1, y: 0, filter: "blur(0px)", transition: { duration: 0.55, ease: [0.16, 1, 0.3, 1] } } };
