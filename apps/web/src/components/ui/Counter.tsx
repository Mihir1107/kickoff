import { animate, motion, useMotionValue, useTransform } from "framer-motion";
import { useEffect } from "react";
import { useReducedMotion } from "@/lib/motion";

/** Number that rolls to its new value. */
export function Counter({ value, decimals = 0, className, suffix = "" }: { value: number; decimals?: number; className?: string; suffix?: string }) {
  const reduced = useReducedMotion();
  const mv = useMotionValue(reduced ? value : 0);
  const text = useTransform(mv, (v) => v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + suffix);
  useEffect(() => {
    if (reduced) {
      mv.set(value);
      return;
    }
    const c = animate(mv, value, { duration: 1.1, ease: [0.16, 1, 0.3, 1] });
    return c.stop;
  }, [value, mv, reduced]);
  return <motion.span className={className}>{text}</motion.span>;
}
