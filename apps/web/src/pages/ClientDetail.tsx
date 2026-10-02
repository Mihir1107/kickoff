import { motion } from "framer-motion";
import { ArrowUpRight, CalendarClock, FileArchive, Lock, Plug, Plus, RefreshCcw } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "@/api";
import { qk, useAction, useClient, useClientConnections, useExports, useMatters, useRollup } from "@/api/hooks";
import { SOURCES } from "@/lib/sources";
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
  const [tab, setTab] = useState<"matters" | "connections" | "exports">("matters");
  const [newMatter, setNewMatter] = useState(false);
  const [newConn, setNewConn] = useState(false);
  const toast = useToast();
  const close = useAction(() => api.closeClient(id), () => [qk.client(id), ["rollup"]]);
  const reauth = useAction((cid: string) => api.reauthConnection(cid, { credentials: { access_token: "xoxp-demo" } }), () => [qk.clientConnections(id), ["rollup"]]);
  const disable = useAction((cid: string) => api.disableConnection(cid), () => [qk.clientConnections(id)]);

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
            const src = SOURCES.find((y) => y.id === x.source);
            return (
              <motion.div key={x.id} variants={rise}>
                <Glass className="p-5">
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-3">
                      <SourceGlyph source={x.source} />
                      <div>
                        <div className="text-[15px] font-medium">{src?.name ?? x.source}</div>
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
                      {(x.status === "error" || x.status === "pending") && <Button size="sm" variant="primary" icon={<RefreshCcw className="size-3.5" />} loading={reauth.isPending && reauth.variables === x.id}
                        onClick={() => reauth.mutate(x.id, { onSuccess: () => toast("ok", "Re-authorized", "Paused jobs on this connection resume from their checkpoints.") })}>Re-authorize</Button>}
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
      <NewConnectionModal open={newConn} onClose={() => setNewConn(false)} clientId={id} />
    </>
  );
}

export function SourceGlyph({ source }: { source: string }) {
  const map: Record<string, [string, string]> = {
    slack: ["#", "linear-gradient(135deg,#e01e5a,#ecb22e 50%,#2eb67d)"],
    slack_export: ["Z", "linear-gradient(135deg,#8b7cff,#5ad8ff)"],
    teams: ["T", "linear-gradient(135deg,#5b5fc7,#7b83eb)"],
    dummy: ["◆", "linear-gradient(135deg,#7cf5d2,#5ad8ff)"],
  };
  const [g, bg] = map[source] ?? ["?", "#333"];
  return <div className="grid size-10 place-items-center rounded-xl text-[17px] font-bold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.3)]" style={{ background: bg }}>{g}</div>;
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

function NewConnectionModal({ open, onClose, clientId }: { open: boolean; onClose: () => void; clientId: string }) {
  const [source, setSource] = useState("slack");
  const [org, setOrg] = useState("");
  const [token, setToken] = useState("");
  const toast = useToast();
  const create = useAction(() => api.createConnection(clientId, { source, external_org_id: org, credentials: { access_token: token } }), () => [qk.clientConnections(clientId), ["rollup"]]);
  return (
    <Modal open={open} onClose={onClose} title="Connect a source" subtitle="Tokens are envelope-encrypted per tenant and never returned, logged, or sent to the workflow engine." width={560}>
      <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); create.mutate(undefined, { onSuccess: () => { toast("ok", "Connection created"); setToken(""); onClose(); } }); }}>
        <div className="grid grid-cols-3 gap-2">
          {SOURCES.filter((s) => s.id !== "slack_export").map((s) => (
            <button type="button" key={s.id} onClick={() => setSource(s.id)}
              className={`relative rounded-2xl border p-3 text-left transition ${source === s.id ? "border-mint/40 bg-mint/[0.06]" : "border-white/[0.07] bg-white/[0.02] hover:border-white/15"}`}>
              <SourceGlyph source={s.id} />
              <div className="mt-2 text-[12.5px] font-medium">{s.name}</div>
              <div className="text-[10.5px] leading-tight text-white/60">{s.hint}</div>
            </button>
          ))}
        </div>
        <Field label="External org / workspace id"><input className="input font-mono" value={org} onChange={(e) => setOrg(e.target.value)} placeholder="T0123ABCD" /></Field>
        <Field label="Access token" hint="Write-only. In production this comes from the OAuth install flow, not a text box."><input type="password" autoComplete="off" className="input font-mono" value={token} onChange={(e) => setToken(e.target.value)} placeholder="xoxp-…" /></Field>
        <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!org || !token}>Connect</Button></div>
      </form>
    </Modal>
  );
}
