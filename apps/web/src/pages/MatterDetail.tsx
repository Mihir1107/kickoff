import { motion } from "framer-motion";
import { CalendarClock, FolderKanban, Layers, Lock, Plus, Radar } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "@/api";
import { qk, useAction, useClient, useJobs, useMatter, useMatterConnections, useWorkspaces } from "@/api/hooks";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Empty, Skeleton } from "@/components/ui/Empty";
import { Glass, SectionTitle } from "@/components/ui/Glass";
import { Hash } from "@/components/ui/Hash";
import { Field, Modal } from "@/components/ui/Modal";
import { StatusPill } from "@/components/ui/StatusPill";
import { useToast } from "@/components/ui/Toast";
import { ago, utc } from "@/lib/format";
import { connectionStatus } from "@/lib/status";
import { SourceGlyph } from "@/components/connect/SourceGlyph";
import { JobRow } from "./JobRow";

export function MatterDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const matter = useMatter(id);
  const client = useClient(matter.data?.client_id ?? "");
  const jobs = useJobs(id);
  const ws = useWorkspaces(id);
  const conns = useMatterConnections(id);
  const [wsOpen, setWsOpen] = useState(false);
  const [wsName, setWsName] = useState("");
  const toast = useToast();
  const addWs = useAction(() => api.createWorkspace(id, { name: wsName }), () => [qk.workspaces(id)]);
  const close = useAction(() => api.closeMatter(id), () => [qk.matter(id), ["rollup"]]);
  const m = matter.data;
  const days = m ? Math.round((Date.parse(m.retention_until) - Date.now()) / 86_400_000) : 0;
  const span = m ? Math.max(1, (Date.parse(m.retention_until) - Date.parse(m.created_at)) / 86_400_000) : 1;

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Clients", to: "/clients" }, { label: client.data?.name ?? "…", to: m ? `/clients/${m.client_id}` : undefined }, { label: m?.name ?? "…" }]}
        eyebrow={m ? <span className="flex items-center gap-2">Matter <Hash value={m.id} /></span> : undefined}
        title={m?.name ?? <Skeleton className="h-14 w-[28rem]" />}
        actions={m && !m.closed_at && (
          <>
            <Button icon={<Lock className="size-4" />} loading={close.isPending} onClick={() => close.mutate(undefined, { onSuccess: () => toast("ok", "Matter closed"), onError: (e) => toast("error", "Cannot close matter", (e as Error).message) })}>Close matter</Button>
            <Button variant="primary" icon={<Radar className="size-4" />} onClick={() => nav(`/collections/new?matter=${id}`)}>Start collection</Button>
          </>
        )} />

      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        <Glass className="p-6">
          <SectionTitle eyebrow="Jobs" title="Collections in this matter" right={<span className="font-mono text-[12px] text-white/60">{jobs.data?.length ?? "…"}</span>} />
          <div className="space-y-2.5">
            {jobs.isLoading && [0, 1].map((i) => <Skeleton key={i} className="h-20" />)}
            {jobs.data?.map((j, i) => <JobRow key={j.id} j={j} i={i} />)}
            {jobs.data?.length === 0 && <Empty icon={<Radar />} title="No collections yet" body="Pick a connection and scopes to start the first one." action={<Button size="sm" variant="primary" onClick={() => nav(`/collections/new?matter=${id}`)}>Start collection</Button>} />}
          </div>
        </Glass>

        <div className="space-y-4">
          <Glass className="p-6" glow="rgba(255,200,107,0.08)">
            <SectionTitle eyebrow="Retention · WORM" title={<span className="flex items-center gap-2"><CalendarClock className="size-4 text-amber" />{m ? utc(m.retention_until, false) : "…"}</span>} />
            <div className="relative h-2 overflow-hidden rounded-full bg-white/[0.06]">
              <motion.div className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-amber/80 to-mint" initial={{ width: 0 }} animate={{ width: `${Math.max(2, 100 - (days / span) * 100)}%` }} transition={{ duration: 1.2 }} />
            </div>
            <div className="mt-2 flex justify-between font-mono text-[11px] text-white/60"><span>opened {m ? ago(m.created_at) : ""}</span><span>{days}d left</span></div>
            <p className="mt-3 text-[12px] leading-relaxed text-white/60">Locks roll forward in windows while the matter is open; retention may only be extended. COMPLIANCE mode: nobody can delete early, including us.</p>
            {m?.closed_at && <div className="mt-3"><StatusPill tone="muted" label={`Closed ${ago(m.closed_at)}`} /></div>}
          </Glass>

          <Glass className="p-6">
            <SectionTitle eyebrow="Review" title={<span className="flex items-center gap-2"><Layers className="size-4 text-iris" />Workspaces</span>}
              right={<Button size="sm" variant="ghost" icon={<Plus className="size-3.5" />} onClick={() => setWsOpen(true)}>Add</Button>} />
            <div className="space-y-1.5">
              {ws.data?.map((w, i) => (
                <motion.div key={w.id} initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i * 0.05 }} className="glass-sub flex items-center gap-3 px-3 py-2.5">
                  <FolderKanban className="size-4 text-white/60" /><span className="text-[13px]">{w.name}</span><span className="ml-auto text-[11px] text-white/55">{ago(w.created_at)}</span>
                </motion.div>
              ))}
            </div>
          </Glass>

          <Glass className="p-6">
            <SectionTitle eyebrow="Owned by the client" title="Usable connections" />
            <div className="space-y-2">
              {conns.data?.map((c) => {
                const s = connectionStatus(c.status);
                return (
                  <div key={c.id} className="flex items-center gap-3">
                    <SourceGlyph source={c.source} />
                    <div className="min-w-0 flex-1"><div className="text-[13px]">{c.source}</div><div className="truncate font-mono text-[11px] text-white/55">{c.external_org_id}</div></div>
                    {s && <StatusPill tone={s.tone} label={s.label} />}
                  </div>
                );
              })}
            </div>
          </Glass>
        </div>
      </div>

      <Modal open={wsOpen} onClose={() => setWsOpen(false)} title="New workspace" subtitle="Workspaces partition a matter for review (Relativity model).">
        <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); addWs.mutate(undefined, { onSuccess: () => { toast("ok", "Workspace created"); setWsOpen(false); setWsName(""); } }); }}>
          <Field label="Name"><input autoFocus className="input" value={wsName} onChange={(e) => setWsName(e.target.value)} /></Field>
          <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={() => setWsOpen(false)}>Cancel</Button><Button variant="primary" loading={addWs.isPending} disabled={!wsName.trim()}>Create</Button></div>
        </form>
      </Modal>
    </>
  );
}
