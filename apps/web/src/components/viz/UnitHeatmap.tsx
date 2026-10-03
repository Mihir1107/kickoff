import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useRef, useState } from "react";
import type { UnitOut } from "@/api/types";
import { conversationName } from "@/lib/names";
import { recon, toneVar } from "@/lib/status";
import { num } from "@/lib/format";

/**
 * How one unit is drawn (WCAG 1.4.11: every cell reaches 3:1 against the panel).
 * - pending: a hollow slot (control-border outline); nothing collected yet;
 * - matched with no messages that day: a hollow mint slot (collected, empty);
 * - otherwise a solid fill: live while running, the reconciliation colour once done.
 */
function cellStyle(u: UnitOut): { fill: string; hollow: boolean } {
  if (u.status === "running") return { fill: "var(--live)", hollow: false };
  if (u.status === "pending") return { fill: "var(--control-border)", hollow: true };
  if (u.recon_status === "matched" && (u.expected_count ?? 0) === 0) return { fill: "var(--ok)", hollow: true };
  return { fill: toneVar[recon(u.recon_status).tone], hollow: false };
}

/**
 * Conversation × UTC day grid: one cell per work unit (ADR 0005). Gaps, surplus and unverifiable days
 * stand out, and live units pulse while they collect.
 *
 * Keyboard (one tab stop, like any composite widget): focus the map, then the arrow keys move between
 * units, Home/End jump to the row's ends. The active unit is outlined, shows the same details as the
 * mouse tooltip, and is announced to screen readers through a polite live region.
 */
export function UnitHeatmap({ units }: { units: UnitOut[] }) {
  const [hover, setHover] = useState<{ u: UnitOut; x: number; y: number } | null>(null);
  const [active, setActive] = useState<{ r: number; c: number } | null>(null);
  const [focused, setFocused] = useState(false);
  const grid = useRef<HTMLDivElement>(null);
  const { rows, days } = useMemo(() => {
    const byConv = new Map<string, Map<string, UnitOut>>();
    const daySet = new Set<string>();
    for (const u of units) {
      if (u.kind !== "conversation_day") continue;
      const [conv, d] = u.unit_key.split("/") as [string, string];
      daySet.add(d);
      if (!byConv.has(conv)) byConv.set(conv, new Map());
      byConv.get(conv)!.set(d, u);
    }
    return { rows: [...byConv.entries()], days: [...daySet].sort() };
  }, [units]);

  const max = useMemo(() => Math.max(1, ...units.map((u) => u.expected_count ?? u.collected_count)), [units]);
  const activeUnit = active ? rows[active.r]?.[1].get(days[active.c] ?? "") : undefined;

  // Place the tooltip on the active cell and keep that cell scrolled into view.
  useEffect(() => {
    if (!focused || !activeUnit) return;
    const el = grid.current?.querySelector<HTMLElement>(`[data-unit="${CSS.escape(activeUnit.unit_key)}"]`);
    if (!el) return;
    el.scrollIntoView({ block: "nearest", inline: "nearest" });
    const b = el.getBoundingClientRect();
    setHover({ u: activeUnit, x: b.left + b.width / 2, y: b.top });
  }, [focused, activeUnit]);

  const onKey = (e: React.KeyboardEvent) => {
    if (!rows.length || !days.length) return;
    const cur = active ?? { r: 0, c: 0 };
    const moves: Record<string, () => { r: number; c: number }> = {
      ArrowRight: () => ({ r: cur.r, c: Math.min(days.length - 1, cur.c + 1) }),
      ArrowLeft: () => ({ r: cur.r, c: Math.max(0, cur.c - 1) }),
      ArrowDown: () => ({ r: Math.min(rows.length - 1, cur.r + 1), c: cur.c }),
      ArrowUp: () => ({ r: Math.max(0, cur.r - 1), c: cur.c }),
      Home: () => ({ r: cur.r, c: 0 }),
      End: () => ({ r: cur.r, c: days.length - 1 }),
    };
    if (moves[e.key]) {
      e.preventDefault();
      setActive(moves[e.key]!());
    } else if (e.key === "Escape") {
      setHover(null);
    }
  };

  const describe = (u: UnitOut) =>
    `${conversationName(u.unit_key.split("/")[0]!)}, ${u.unit_key.split("/")[1]}: ${u.status}, ${recon(u.recon_status).label}, expected ${num(u.expected_count)}, collected ${num(u.collected_count)}` +
    (u.file_gaps ? `, ${u.file_gaps} file gaps` : "");

  return (
    <div className="relative">
      <div ref={grid} tabIndex={0} role="group"
        aria-label={`Reconciliation map, ${rows.length} conversations by ${days.length} days. Use the arrow keys to read each unit.`}
        onKeyDown={onKey}
        onFocus={() => { setFocused(true); setActive((a) => a ?? { r: 0, c: 0 }); }}
        onBlur={() => { setFocused(false); setHover(null); }}
        className="overflow-x-auto rounded-lg pb-2">
        <div className="inline-grid min-w-full gap-y-[5px]" style={{ gridTemplateColumns: `150px repeat(${days.length}, minmax(11px, 1fr))`, columnGap: 3 }}>
          <div />
          {days.map((d, i) => (
            <div key={d} aria-hidden className="h-4 overflow-visible whitespace-nowrap font-mono text-[9px] text-white/55">{i % 7 === 0 ? d.slice(5) : ""}</div>
          ))}
          {rows.map(([conv, cells], r) => (
            <div key={conv} className="contents">
              <div aria-hidden className="truncate pr-3 text-[12px] leading-[14px] text-white/60" title={conv}>{conversationName(conv)}</div>
              {days.map((d, c) => {
                const u = cells.get(d);
                if (!u) return <div key={d} />;
                const { fill: color, hollow } = cellStyle(u);
                // Volume shades matched cells, never below the 3:1 floor (opacity 0.5 measured at >= 3.7:1).
                const intensity = !hollow && u.status === "done" && (u.recon_status === "matched" || u.recon_status === "matched_against_archive") ? 0.5 + 0.5 * Math.sqrt((u.collected_count || 0) / max) : 1;
                const isActive = focused && active?.r === r && active.c === c;
                const glow = u.status === "done" && u.recon_status !== "matched" && u.recon_status !== "matched_against_archive" ? `0 0 10px ${color}` : "";
                return (
                  <div key={d} data-unit={u.unit_key} data-unit-state={u.status === "done" ? u.recon_status : u.status} data-active={isActive || undefined}
                    onMouseEnter={(e) => { const b = e.currentTarget.getBoundingClientRect(); setHover({ u, x: b.left + b.width / 2, y: b.top }); }}
                    onMouseLeave={() => !focused && setHover(null)}
                    onClick={() => { setActive({ r, c }); grid.current?.focus(); }}
                    className="relative h-[14px] cursor-crosshair rounded-[3px] transition-[transform,background] duration-500 hover:z-10 hover:scale-[1.6]"
                    style={{
                      background: hollow ? "transparent" : color, opacity: intensity,
                      outline: hollow ? `1.5px solid ${color}` : undefined, outlineOffset: hollow ? "-1.5px" : undefined,
                      // The active unit: a white ring with a dark gap, >= 3:1 against cell and panel alike.
                      boxShadow: isActive ? "0 0 0 2px var(--color-ink-950), 0 0 0 4px #ffffff" : glow || undefined,
                      zIndex: isActive ? 20 : undefined,
                      animation: `cell-in 0.6s cubic-bezier(.16,1,.3,1) both ${(c * 8 + r * 25) / 1000}s${u.status === "running" ? ", pulse 1.2s ease-in-out infinite" : ""}`,
                    }} />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="sr-only" aria-live="polite" aria-atomic="true">{focused && activeUnit ? describe(activeUnit) : ""}</div>
      <style>{`@keyframes cell-in{from{transform:scale(0);opacity:0}}`}</style>
      <AnimatePresence>
        {hover && (
          <motion.div aria-hidden initial={{ opacity: 0, y: 6, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}
            data-testid="unit-tooltip"
            className="glass pointer-events-none fixed z-[90] w-60 -translate-x-1/2 -translate-y-full p-3 text-[12px]"
            style={{ left: hover.x, top: hover.y - 10 }}>
            <div className="font-mono text-[11px] text-white/65">{hover.u.unit_key}</div>
            <div className="mt-2 grid grid-cols-2 gap-y-1">
              <span className="text-white/60">Status</span><span className="text-right">{hover.u.status}</span>
              <span className="text-white/60">Reconciliation</span><span className="text-right" style={{ color: toneVar[recon(hover.u.recon_status).tone] }}>{recon(hover.u.recon_status).label}</span>
              <span className="text-white/60">Expected</span><span className="text-right font-mono">{num(hover.u.expected_count)}</span>
              <span className="text-white/60">Collected</span><span className="text-right font-mono">{num(hover.u.collected_count)}</span>
              {hover.u.file_gaps > 0 && (<><span className="text-white/60">File gaps</span><span className="text-right font-mono text-amber">{hover.u.file_gaps}</span></>)}
            </div>
            {hover.u.last_error && <div className="mt-2 rounded-md bg-rose/10 p-2 font-mono text-[10.5px] text-rose">{hover.u.last_error}</div>}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export function HeatLegend() {
  const items: [string, string][] = [
    ["Matched", "var(--ok)"], ["No messages", "hollow:var(--ok)"], ["Matched (export)", "var(--info)"], ["Collecting", "var(--live)"], ["Gap", "var(--warn)"], ["Surplus", "var(--orange)"],
    ["Unverifiable", "var(--violet)"], ["Failed / access lost", "var(--bad)"], ["Pending", "hollow:var(--control-border)"],
  ];
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-2">
      {items.map(([l, c]) => (
        <span key={l} className="flex items-center gap-1.5 text-[11.5px] text-white/65"><span aria-hidden className="size-2.5 rounded-[3px]" style={c.startsWith("hollow:") ? { outline: `1.5px solid ${c.slice(7)}`, outlineOffset: "-1.5px" } : { background: c }} />{l}</span>
      ))}
    </div>
  );
}
