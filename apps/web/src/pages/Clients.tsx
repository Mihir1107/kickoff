import { motion } from "framer-motion";
import { ArrowUpRight, Building2, Plus, Star } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api";
import { qk, useAction, useRollup } from "@/api/hooks";
import { PageHeader, rise, stagger } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Empty";
import { Glass } from "@/components/ui/Glass";
import { Field, Modal } from "@/components/ui/Modal";
import { StatusPill } from "@/components/ui/StatusPill";
import { useToast } from "@/components/ui/Toast";
import { ago } from "@/lib/format";

export function Clients() {
  const { data, isLoading } = useRollup();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const toast = useToast();
  const create = useAction((n: string) => api.createClient({ name: n }), () => [qk.clients, ["rollup"]]);

  return (
    <>
      <PageHeader eyebrow="Hierarchy · client › matter › workspace" title={<>Clients <em className="text-gradient">&</em> matters</>}
        subtitle="Clients own their connections and Slack exports. Matters hold retention and jobs. Nothing is deleted: matters and clients are closed."
        actions={<Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setOpen(true)}>New client</Button>} />

      <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {isLoading && [0, 1, 2].map((i) => <Skeleton key={i} className="h-56" />)}
        {data?.clients.map((c) => {
          const ms = data.matters.filter((m) => m.client_id === c.id);
          const js = data.jobs.filter((j) => ms.some((m) => m.id === j.matter_id));
          const live = js.filter((j) => j.status === "running").length;
          const conns = data.connections.filter((x) => x.client_id === c.id);
          return (
            <motion.div key={c.id} variants={rise}>
              <Link to={`/clients/${c.id}`} className="group block h-full">
                <Glass className="h-full p-6 transition-transform duration-500 group-hover:-translate-y-1">
                  <div className="flex items-start justify-between">
                    <div className="grid size-11 place-items-center rounded-2xl border border-white/10 bg-[linear-gradient(135deg,rgba(124,245,210,0.15),rgba(139,124,255,0.12))]">
                      <Building2 className="size-5 text-white/80" />
                    </div>
                    <div className="flex items-center gap-2">
                      {c.is_default && <span className="flex items-center gap-1 rounded-full bg-amber/10 px-2 py-0.5 text-[10.5px] text-amber"><Star className="size-3" />default</span>}
                      {c.closed_at ? <StatusPill tone="muted" label="Closed" /> : live > 0 ? <StatusPill tone="live" label={`${live} live`} /> : <StatusPill tone="ok" label="Open" pulse={false} />}
                    </div>
                  </div>
                  <h3 className="mt-5 text-[19px] font-medium tracking-tight">{c.name}</h3>
                  <div className="mt-1 text-[12px] text-white/60">since {ago(c.created_at)}{c.closed_at ? ` · closed ${ago(c.closed_at)}` : ""}</div>
                  <div className="mt-6 grid grid-cols-3 gap-2">
                    {[["Matters", ms.length], ["Jobs", js.length], ["Sources", conns.length]].map(([l, v]) => (
                      <div key={l} className="glass-sub px-3 py-2.5"><div className="text-[20px] font-light">{v}</div><div className="text-[10.5px] uppercase tracking-wider text-white/55">{l}</div></div>
                    ))}
                  </div>
                  <div className="mt-4 flex flex-wrap gap-1.5">
                    {ms.slice(0, 3).map((m) => <span key={m.id} className="max-w-full truncate rounded-lg bg-white/[0.04] px-2 py-1 text-[11px] text-white/55">{m.name}</span>)}
                  </div>
                  <ArrowUpRight aria-hidden className="absolute right-5 bottom-5 size-5 text-white/15 transition group-hover:text-mint" />
                </Glass>
              </Link>
            </motion.div>
          );
        })}
      </motion.div>

      <Modal open={open} onClose={() => setOpen(false)} title="New client" subtitle="A client groups matters, connections and exports.">
        <form onSubmit={(e) => { e.preventDefault(); create.mutate(name, { onSuccess: (c) => { toast("ok", "Client created", c.name); setOpen(false); setName(""); }, onError: (err) => toast("error", "Could not create client", String(err)) }); }} className="space-y-4">
          <Field label="Name"><input autoFocus className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Contoso Holdings" maxLength={200} /></Field>
          <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!name.trim()}>Create</Button></div>
        </form>
      </Modal>
    </>
  );
}
