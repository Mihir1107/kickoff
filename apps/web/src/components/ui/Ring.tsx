import { motion } from "framer-motion";
import type { ReactNode } from "react";

/** Progress ring with a gradient stroke and a soft glow. */
export function Ring({ value, size = 120, stroke = 9, children, from = "#7cf5d2", to = "#5ad8ff" }: { value: number; size?: number; stroke?: number; children?: ReactNode; from?: string; to?: string }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const id = `ring-${from.slice(1)}-${to.slice(1)}`;
  return (
    <div className="relative grid place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor={from} /><stop offset="1" stopColor={to} /></linearGradient>
          <filter id={`${id}-glow`}><feGaussianBlur stdDeviation="3" /></filter>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={stroke} />
        <motion.circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={`url(#${id})`} strokeWidth={stroke} strokeLinecap="round" strokeDasharray={c}
          initial={{ strokeDashoffset: c }} animate={{ strokeDashoffset: c * (1 - Math.min(1, Math.max(0, value))) }} transition={{ duration: 1.4, ease: [0.16, 1, 0.3, 1] }}
          filter={`url(#${id}-glow)`} opacity={0.6} />
        <motion.circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={`url(#${id})`} strokeWidth={stroke} strokeLinecap="round" strokeDasharray={c}
          initial={{ strokeDashoffset: c }} animate={{ strokeDashoffset: c * (1 - Math.min(1, Math.max(0, value))) }} transition={{ duration: 1.4, ease: [0.16, 1, 0.3, 1] }} />
      </svg>
      <div className="absolute inset-0 grid place-items-center">{children}</div>
    </div>
  );
}
