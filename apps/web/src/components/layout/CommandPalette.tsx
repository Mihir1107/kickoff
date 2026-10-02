import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { Briefcase, CornerDownLeft, Plus, Radar, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useRollup } from "@/api/hooks";
import { jobStatus } from "@/lib/status";
import { shortId } from "@/lib/format";
import { NAV } from "./nav";

interface Item { id: string; label: string; sub?: string; icon: React.ElementType; to: string; group: string }

export function CommandPalette({ open, setOpen }: { open: boolean; setOpen: (v: boolean) => void }) {
  const nav = useNavigate();
  const { data } = useRollup();
  const [q, setQ] = useState("");
  const [i, setI] = useState(0);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setOpen(!open); }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);
  useEffect(() => { if (open) { setQ(""); setI(0); } }, [open]);

  const items = useMemo<Item[]>(() => {
    const base: Item[] = [
      ...NAV.map((n) => ({ id: n.to, label: n.label, icon: n.icon, to: n.to, group: "Navigate" })),
      { id: "new-job", label: "Start a collection", sub: "Pick a matter, connection and scopes", icon: Plus, to: "/collections/new", group: "Actions" },
    ];
    const clientName = new Map(data?.clients.map((c) => [c.id, c.name]));
    for (const m of data?.matters ?? []) base.push({ id: m.id, label: m.name, sub: clientName.get(m.client_id), icon: Briefcase, to: `/matters/${m.id}`, group: "Matters" });
    for (const j of data?.jobs.slice(0, 30) ?? []) base.push({ id: j.id, label: `Job ${shortId(j.id)}`, sub: jobStatus(j.status).label, icon: Radar, to: `/jobs/${j.id}`, group: "Jobs" });
    const s = q.trim().toLowerCase();
    return s ? base.filter((x) => `${x.label} ${x.sub ?? ""}`.toLowerCase().includes(s)) : base.slice(0, 14);
  }, [data, q]);

  const go = (it: Item | undefined) => { if (!it) return; setOpen(false); nav(it.to); };
  let lastGroup = "";

  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-[80] flex justify-center px-4 pt-[14vh]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <div className="absolute inset-0 bg-ink-950/60 backdrop-blur-sm" onClick={() => setOpen(false)} />
          <motion.div className="glass relative h-fit w-full max-w-[620px]" initial={{ y: -20, scale: 0.97, opacity: 0 }} animate={{ y: 0, scale: 1, opacity: 1 }} exit={{ y: -10, scale: 0.98, opacity: 0 }}
            transition={{ type: "spring", stiffness: 450, damping: 34 }}>
            <div className="flex items-center gap-3 border-b border-white/[0.07] px-4">
              <Search className="size-4 text-white/60" />
              <input autoFocus value={q} onChange={(e) => { setQ(e.target.value); setI(0); }} placeholder="Search matters, jobs, pages…"
                onKeyDown={(e) => {
                  if (e.key === "ArrowDown") { e.preventDefault(); setI((x) => Math.min(x + 1, items.length - 1)); }
                  if (e.key === "ArrowUp") { e.preventDefault(); setI((x) => Math.max(x - 1, 0)); }
                  if (e.key === "Enter") go(items[i]);
                }}
                className="h-14 flex-1 bg-transparent text-[15px] outline-none placeholder:text-white/55" />
              <span className="rounded-md border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-white/60">ESC</span>
            </div>
            <div className="max-h-[52vh] overflow-y-auto p-2">
              {items.length === 0 && <div className="py-10 text-center text-[13px] text-white/60">Nothing matches “{q}”.</div>}
              {items.map((it, idx) => {
                const header = it.group !== lastGroup ? (lastGroup = it.group) : null;
                return (
                  <div key={it.group + it.id}>
                    {header && <div className="eyebrow px-3 pb-1 pt-3">{header}</div>}
                    <button onMouseEnter={() => setI(idx)} onClick={() => go(it)}
                      className={clsx("relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left", idx === i ? "text-white" : "text-white/65")}>
                      {idx === i && <motion.span layoutId="cmd-hl" className="absolute inset-0 rounded-xl bg-white/[0.06]" transition={{ type: "spring", stiffness: 600, damping: 40 }} />}
                      <it.icon className="relative size-4 text-white/60" />
                      <span className="relative text-[13.5px]">{it.label}</span>
                      {it.sub && <span className="relative truncate text-[12px] text-white/55">{it.sub}</span>}
                      {idx === i && <CornerDownLeft className="relative ml-auto size-3.5 text-white/60" />}
                    </button>
                  </div>
                );
              })}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
