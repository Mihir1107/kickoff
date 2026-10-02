import { motion } from "framer-motion";

/** Smooth area sparkline (decorative trend). */
export function Sparkline({ data, color = "#7cf5d2", height = 44, width = 160 }: { data: number[]; color?: string; height?: number; width?: number }) {
  if (data.length < 2) return null;
  const max = Math.max(...data, 1);
  const pts = data.map((v, i) => [(i / (data.length - 1)) * width, height - (v / max) * (height - 4) - 2] as const);
  const d = pts.reduce((acc, [x, y], i) => {
    if (i === 0) return `M${x},${y}`;
    const [px, py] = pts[i - 1]!;
    const cx = (px + x) / 2;
    return `${acc} C${cx},${py} ${cx},${y} ${x},${y}`;
  }, "");
  const id = `sp-${color.slice(1)}`;
  return (
    <svg width={width} height={height} className="overflow-visible">
      <defs><linearGradient id={id} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={color} stopOpacity="0.35" /><stop offset="1" stopColor={color} stopOpacity="0" /></linearGradient></defs>
      <motion.path d={`${d} L${width},${height} L0,${height} Z`} fill={`url(#${id})`} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.5, duration: 0.8 }} />
      <motion.path d={d} fill="none" stroke={color} strokeWidth="1.8" strokeLinecap="round" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 1.4, ease: "easeInOut" }} style={{ filter: `drop-shadow(0 0 6px ${color})` }} />
    </svg>
  );
}
