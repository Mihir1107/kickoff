import clsx from "clsx";
import { motion, useMotionTemplate, useMotionValue, type HTMLMotionProps } from "framer-motion";
import type { ReactNode } from "react";
import { useReducedMotion } from "@/lib/motion";

/** Glass panel with a cursor-following spotlight. */
export function Glass({
  className,
  children,
  spotlight = true,
  glow = "rgba(124,245,210,0.10)",
  ...rest
}: HTMLMotionProps<"div"> & { spotlight?: boolean; glow?: string; children?: ReactNode }) {
  const reduced = useReducedMotion();
  if (reduced) spotlight = false;
  const x = useMotionValue(-400);
  const y = useMotionValue(-400);
  const bg = useMotionTemplate`radial-gradient(420px circle at ${x}px ${y}px, ${glow}, transparent 70%)`;
  return (
    <motion.div
      {...rest}
      onMouseMove={(e) => {
        if (!spotlight) return;
        const r = e.currentTarget.getBoundingClientRect();
        x.set(e.clientX - r.left);
        y.set(e.clientY - r.top);
      }}
      onMouseLeave={() => { x.set(-400); y.set(-400); }}
      className={clsx("glass overflow-hidden", className)}
    >
      {spotlight && <motion.div aria-hidden className="pointer-events-none absolute inset-0 rounded-[inherit]" style={{ background: bg }} />}
      <div className="relative">{children}</div>
    </motion.div>
  );
}

export function SectionTitle({ eyebrow, title, right }: { eyebrow?: string; title: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div>
        {eyebrow && <div className="eyebrow mb-1">{eyebrow}</div>}
        <h3 className="text-[15px] font-medium tracking-tight text-white/90">{title}</h3>
      </div>
      {right}
    </div>
  );
}
