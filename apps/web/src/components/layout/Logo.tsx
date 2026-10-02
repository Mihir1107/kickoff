import { motion } from "framer-motion";

export function Logo({ size = 34 }: { size?: number }) {
  return (
    <div className="relative grid place-items-center" style={{ width: size, height: size }}>
      <div className="absolute inset-0 rounded-[30%] bg-[conic-gradient(from_0deg,#7cf5d2,#5ad8ff,#8b7cff,#7cf5d2)] opacity-70 blur-md animate-spin-slow" />
      <div className="relative grid size-full place-items-center rounded-[30%] border border-white/15 bg-ink-900">
        <svg viewBox="0 0 32 32" className="size-[62%]">
          <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#7cf5d2" /><stop offset="1" stopColor="#8b7cff" /></linearGradient></defs>
          <motion.path d="M16 3l11 5v8c0 6.6-4.6 11.4-11 13-6.4-1.6-11-6.4-11-13V8z" fill="none" stroke="url(#lg)" strokeWidth="2.4" strokeLinejoin="round"
            initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 1.6, ease: "easeInOut" }} />
          <motion.circle cx="16" cy="15" r="3.6" fill="url(#lg)" initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ delay: 1, type: "spring" }} />
        </svg>
      </div>
    </div>
  );
}
