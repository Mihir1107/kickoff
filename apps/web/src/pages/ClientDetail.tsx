import { motion } from "framer-motion";
import { ArrowUpRight, CalendarClock, FileArchive, Lock, Plug, Plus, RefreshCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "@/api";
import { qk, useAction, useClient, useClientConnections, useExports, useMatters, useRollup } from "@/api/hooks";
import { ConnectSourceModal } from "@/components/connect/ConnectSourceModal";
import { SourceGlyph } from "@/components/connect/SourceGlyph";
import { SOURCE_NAMES } from "@/lib/sources";
import type { ConnectionOut } from "@/api/types";
import { PageHeader, rise, stagger } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Empty, Skeleton } from "@/components/ui/Empty";
import { Glass } from "@/components/ui/Glass";
import { Hash } from "@/components/ui/Hash";
import { Field, Modal } from "@/components/ui/Modal";
import { StatusPill } from "@/components/ui/StatusPill";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { ExportCard } from "./Exports";
import { ago, utc } from "@/lib/format";
import { connectionStatus } from "@/lib/status";

export function ClientDetail() {
  const { id = "" } = useParams();
  const client = useClient(id);
  const matters = useMatters(id);
  const conns = useClientConnections(id);
  const exports = useExports(id);
  const rollup = useRollup();
  const [params, setParams] = useSearchParams();
  const initialTab = params.get("tab");
  const [tab, setTab] = useState<"matters" | "connections" | "exports">(initialTab === "connections" || initialTab === "exports" ? initialTab : "matters");
  const [reauthing, setReauthing] = useState<ConnectionOut | undefined>();
  const [newMatter, setNewMatter] = useState(false);
  const [newConn, setNewConn] = useState(false);
  const toast = useToast();
  const close = useAction(() => api.closeClient(id), () => [qk.client(id), ["rollup"]]);
  const disable = useAction((cid: string) => api.disableConnection(cid), () => [qk.clientConnections(id)]);

  const installed = params.get("installed");
  const reauthId = params.get("reauth");
  useEffect(() => {
    if (installed) {
      toast("ok", "Source connected", "The grant went straight from the provider to our API.");
      void conns.refetch();
      setParams((p) => { p.delete("installed"); return p; }, { replace: true });
    }
  }, [installed, conns, setParams, toast]);
  useEffect(() => {
    const target = reauthId ? conns.data?.find((x) => x.id === reauthId) : undefined;
    if (target) {
      setTab("connections");
      setReauthing(target);
      setParams((p) => { p.delete("reauth"); return p; }, { replace: true });
    }
  }, [reauthId, conns.data, setParams]);
  const c = client.data;
  return (
    <>
      <PageHeader crumbs={[{ label: "Clients", to: "/clients" }, { label: c?.name ?? "…" }]}
        eyebrow={c ? <span className="flex items-center gap-2">Client <Hash value={c.id} /></span> : undefined}
        title={c?.name ?? <Skeleton className="h-14 w-96" />}
        subtitle={c ? <>Created {utc(c.created_at, false)}{c.closed_at && <> · <span className="text-amber">closed {ago(c.closed_at)}</span></>}</> : undefined}
        actions={c && !c.closed_at && (
          <>
            <Button variant="outline" icon={<Lock className="size-4" />} loading={close.isPending}
              onClick={() => close.mutate(undefined, { onSuccess: () => toast("ok", "Client closed"), onError: (e) => toast("error", "Cannot close client", (e as Error).message) })}>Close client</Button>
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setNewMatter(true)}>New matter</Button>
          </>
        )} />

      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <Tabs id="client" value={tab} onChange={setTab} options={[
          { value: "matters", label: "Matters", count: matters.data?.length },
          { value: "connections", label: "Connections", count: conns.data?.length },
          { value: "exports", label: "Slack exports", count: exports.data?.length },
        ]} />
        {tab === "connections" && <Button size="sm" icon={<Plug className="size-3.5" />} onClick={() => setNewConn(true)}>Connect source</Button>}
        {tab === "exports" && <Link to={`/exports?client=${id}`}><Button size="sm" icon={<FileArchive className="size-3.5" />}>Upload export</Button></Link>}
      </div>

      {tab === "matters" && (
        <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 lg:grid-cols-2">
          {matters.data?.map((m) => {
            const js = rollup.data?.jobs.filter((j) => j.matter_id === m.id) ?? [];
            const daysLeft = Math.round((Date.parse(m.retention_until) - Date.now()) / 86_400_000);
            return (
              <motion.div key={m.id} variants={rise}>
                <Link to={`/matters/${m.id}`} className="group block">
                  <Glass className="p-5 transition group-hover:-translate-y-0.5">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <h3 className="truncate text-[16px] font-medium">{m.name}</h3>
                        <div className="mt-1 flex items-center gap-2 text-[12px] text-white/60"><CalendarClock className="size-3.5" />retain until {utc(m.retention_until, false)} · {daysLeft}d</div>
                      </div>
                      {m.closed_at ? <StatusPill tone="muted" label="Closed" /> : <ArrowUpRight aria-hidden className="size-5 text-white/20 transition group-hover:text-mint" />}
                    </div>
                    <div className="mt-4 flex gap-1">
                      {js.slice(0, 24).map((j) => (
                        <div key={j.id} title={j.status} className="h-6 flex-1 rounded-[4px]" style={{ maxWidth: 18, background: j.clean ? "var(--ok)" : j.status === "running" ? "var(--live)" : j.status === "cancelled" ? "rgba(255,255,255,0.15)" : j.status === "failed" ? "var(--bad)" : "var(--warn)", opacity: 0.75 }} />
                      ))}
                      {js.length === 0 && <span className="text-[12px] text-white/55">No jobs yet</span>}
                    </div>
                  </Glass>
                </Link>
              </motion.div>
            );
          })}
          {matters.data?.length === 0 && <Glass className="lg:col-span-2"><Empty icon={<Plus />} title="No matters yet" body="Create a matter to set retention and start collecting." /></Glass>}
        </motion.div>
      )}

      {tab === "connections" && (
        <motion.div variants={stagger} initial="hidden" animate="show" className="grid gap-4 lg:grid-cols-2">
          {conns.data?.map((x) => {
            const s = connectionStatus(x.status) ?? { label: x.status, tone: "muted" as const, hint: "" };
            return (
              <motion.div key={x.id} variants={rise}>
                <Glass className="p-5">
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-3">
                      <SourceGlyph source={x.source} />
                      <div>
                        <div className="text-[15px] font-medium">{SOURCE_NAMES[x.source] ?? x.source}</div>
                        <div className="font-mono text-[11.5px] text-white/60">{x.external_org_id}{x.plan_tier ? ` · ${x.plan_tier}` : ""}</div>
                      </div>
                    </div>
                    <StatusPill tone={s.tone} label={s.label} title={s.hint} />
                  </div>
                  <div className="mt-4 flex flex-wrap gap-1.5">
                    {x.granted_scopes.map((g) => <span key={g} className="rounded-md bg-white/[0.04] px-1.5 py-0.5 font-mono text-[10.5px] text-white/65">{g}</span>)}
                    {x.granted_scopes.length === 0 && <span className="text-[11.5px] text-white/55">{x.source === "slack_export" ? "credential-less (archive)" : "no scopes granted yet"}</span>}
                  </div>
                  <div className="mt-4 flex items-center justify-between border-t border-white/[0.06] pt-3">
                    <span className="text-[11.5px] text-white/55">updated {ago(x.updated_at)} · tokens envelope-encrypted</span>
                    <div className="flex gap-1.5">
                      {(x.status === "error" || x.status === "pending") && <Button size="sm" variant="primary" icon={<RefreshCcw className="size-3.5" />} onClick={() => setReauthing(x)}>Re-authorize</Button>}
                      {x.status !== "revoked" && x.source !== "slack_export" && <Button size="sm" variant="ghost" onClick={() => disable.mutate(x.id, { onSuccess: () => toast("info", "Connection disabled", "Tokens are kept encrypted, never deleted.") })}>Disable</Button>}
                    </div>
                  </div>
                </Glass>
              </motion.div>
            );
          })}
        </motion.div>
      )}

      {tab === "exports" && (
        <div className="grid gap-4 lg:grid-cols-2">
          {exports.data?.map((x) => <ExportCard key={x.id} x={x} />)}
          {exports.data?.length === 0 && <Glass className="lg:col-span-2"><Empty icon={<FileArchive />} title="No Slack exports" body="Upload a workspace export ZIP; it is hashed and locked before anything parses it." /></Glass>}
        </div>
      )}

      <NewMatterModal open={newMatter} onClose={() => setNewMatter(false)} clientId={id} />
      <ConnectSourceModal open={newConn} onClose={() => setNewConn(false)} clientId={id} />
      <ConnectSourceModal open={!!reauthing} onClose={() => setReauthing(undefined)} clientId={id} reauth={reauthing} />
    </>
  );
}


function NewMatterModal({ open, onClose, clientId }: { open: boolean; onClose: () => void; clientId: string }) {
  const [name, setName] = useState("");
  const [date, setDate] = useState(() => new Date(Date.now() + 3 * 365 * 86_400_000).toISOString().slice(0, 10));
  const toast = useToast();
  const create = useAction(() => api.createMatter(clientId, { name, retention_until: `${date}T00:00:00Z` }), () => [qk.matters(clientId), ["rollup"]]);
  return (
    <Modal open={open} onClose={onClose} title="New matter" subtitle="Retention can only ever be extended, never shortened.">
      <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); create.mutate(undefined, { onSuccess: () => { toast("ok", "Matter created", name); onClose(); setName(""); }, onError: (err) => toast("error", "Could not create matter", (err as Error).message) }); }}>
        <Field label="Name"><input className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Contoso v. Fabrikam" /></Field>
        <Field label="Retain evidence until (UTC)" hint="WORM locks roll forward in windows up to this date while the matter is open."><input type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} /></Field>
        <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!name.trim()}>Create matter</Button></div>
      </form>
    </Modal>
  );
}
