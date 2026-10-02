import clsx from "clsx";
import { toneVar, type Tone } from "@/lib/status";

/** Status chip. Live tones get a radar pulse; the clean tone is reserved for clean states. */
export function StatusPill({ tone, label, title, className, pulse }: { tone: Tone; label: string; title?: string; className?: string; pulse?: boolean }) {
  const c = toneVar[tone];
  const live = pulse ?? tone === "live";
  return (
    <span
      title={title}
      className={clsx("inline-flex h-6 items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 text-[11.5px] font-medium", className)}
      style={{ color: c, background: `color-mix(in oklab, ${c} 11%, transparent)`, boxShadow: `inset 0 0 0 1px color-mix(in oklab, ${c} 26%, transparent)` }}
    >
      <span className="relative flex size-1.5">
        {live && <span className="absolute inset-0 animate-pulse-ring rounded-full" style={{ background: c }} />}
        <span className="relative size-1.5 rounded-full" style={{ background: c, boxShadow: `0 0 8px ${c}` }} />
      </span>
      {label}
    </span>
  );
}
