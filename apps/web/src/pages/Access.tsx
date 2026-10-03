import clsx from "clsx";
import { motion } from "framer-motion";
import { Bot, Check, Plus, ShieldCheck, UserRound, Users } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/api";
import { qk, useAction, useAssignments, useGroups, usePrincipals, useRoleMatrix, useRollup } from "@/api/hooks";
import type { ScopeType } from "@/api/types";
import { Skeleton } from "@/components/ui/Empty";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Glass, SectionTitle } from "@/components/ui/Glass";
import { Field, Modal } from "@/components/ui/Modal";
import { StatusPill } from "@/components/ui/StatusPill";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { ago } from "@/lib/format";

/** Presentation only: the roles themselves and what they grant come from GET /v1/roles. */
const roleColor = (r: string): string => ROLE_COLOR[r] ?? "#b3bad0";
const ROLE_COLOR: Record<string, string> = { tenant_admin: "#ff6b8b", client_admin: "#ffc86b", matter_manager: "#8b7cff", collector: "#5ad8ff", reviewer: "#7cf5d2", auditor: "#9fb4ff" };

export function Access() {
  const principals = usePrincipals();
  const assignments = useAssignments();
  const { data } = useRollup();
  const groups = useGroups();
  const matrix = useRoleMatrix();
  const toast = useToast();
  const [tab, setTab] = useState<"assignments" | "principals" | "matrix">("assignments");
  const [open, setOpen] = useState(false);
  const revoke = useAction((id: string) => api.revokeAssignment(id), () => [qk.assignments]);
  const deactivate = useAction((id: string) => api.deactivatePrincipal(id), () => [qk.principals]);

  const who = useMemo(() => {
    const m = new Map<string, { name: string; kind: "user" | "service" | "group" }>();
    principals.data?.forEach((p) => m.set(p.id, { name: p.display_name, kind: p.kind === "service" ? "service" : "user" }));
    groups.data?.forEach((g) => m.set(g.id, { name: g.name, kind: "group" })); // no GET /groups yet: names only in demo
    return m;
  }, [principals.data, groups.data]);
  const scopeLabel = (id: string | null) => {
    if (!id) return "Entire tenant";
    return data?.clients.find((c) => c.id === id)?.name ?? data?.matters.find((m) => m.id === id)?.name ?? id.slice(-8);
  };

  return (
    <>
      <PageHeader eyebrow="ADR 0013 · scoped roles" title={<>Access <em className="text-gradient">control</em></>}
        subtitle="Fixed roles assigned at tenant, client, matter or workspace scope, inherited downward. Nothing is deleted: memberships and assignments are ended, and every change is audited."
        actions={<Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setOpen(true)}>Assign role</Button>} />
      <div className="mb-5"><Tabs id="access" value={tab} onChange={setTab} options={[
        { value: "assignments", label: "Role assignments", count: assignments.data?.filter((a) => !a.revoked_at).length },
        { value: "principals", label: "Principals", count: principals.data?.length },
        { value: "matrix", label: "Permission matrix" },
      ]} /></div>

      {tab === "assignments" && (
        <Glass className="p-3" spotlight={false}>
          {assignments.data?.map((a, i) => {
            const w = who.get(a.principal_id ?? a.group_id ?? "");
            const Icon = w?.kind === "group" ? Users : w?.kind === "service" ? Bot : UserRound;
            return (
              <motion.div key={a.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: a.revoked_at ? 0.45 : 1, y: 0 }} transition={{ delay: i * 0.03 }}
                className="grid grid-cols-[auto_1fr_auto] items-center gap-4 rounded-xl px-4 py-3 hover:bg-white/[0.025] md:grid-cols-[auto_1.2fr_1fr_1fr_auto]">
                <div className="grid size-9 place-items-center rounded-xl border border-white/10 bg-white/[0.03]"><Icon className="size-4 text-white/60" /></div>
                <div className="min-w-0"><div className="truncate text-[13.5px]">{w?.name ?? "Unknown"}</div><div className="text-[11px] text-white/55">{w?.kind}</div></div>
                <div className="hidden md:block"><span className="rounded-lg px-2 py-1 font-mono text-[11.5px]" style={{ color: roleColor(a.role), background: `${roleColor(a.role)}18` }}>{a.role}</span></div>
                <div className="hidden min-w-0 md:block"><div className="truncate text-[12.5px] text-white/70">{scopeLabel(a.scope_id)}</div><div className="text-[11px] text-white/55">{a.scope_type} scope · {ago(a.created_at)}</div></div>
                <div>{a.revoked_at ? <StatusPill tone="muted" label={`Revoked ${ago(a.revoked_at)}`} /> : <Button size="sm" variant="ghost" onClick={() => revoke.mutate(a.id, { onSuccess: () => toast("info", "Assignment revoked", "Ended, not deleted. Audited.") })}>Revoke</Button>}</div>
              </motion.div>
            );
          })}
        </Glass>
      )}

      {tab === "principals" && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {principals.data?.map((p, i) => (
            <Glass key={p.id} className="p-5" initial={{ opacity: 0, scale: 0.97 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: i * 0.04 }}>
              <div className="flex items-start justify-between">
                <div className={clsx("grid size-11 place-items-center rounded-full text-[14px] font-semibold", p.kind === "service" ? "border border-white/10 bg-white/[0.04] text-white/70" : "bg-[linear-gradient(135deg,#7cf5d2,#8b7cff)] text-ink-950")}>
                  {p.kind === "service" ? <Bot className="size-5" /> : p.display_name.split(" ").map((s) => s[0]).join("")}
                </div>
                {p.active ? <StatusPill tone="ok" label="Active" pulse={false} /> : <StatusPill tone="muted" label="Deactivated" />}
              </div>
              <div className="mt-4 text-[15px] font-medium">{p.display_name}</div>
              <div className="text-[12px] text-white/60">{p.email ?? "service principal"}</div>
              <div className="mt-3 truncate font-mono text-[10.5px] text-white/55" title={`${p.issuer} · ${p.subject}`}>{p.issuer.replace("https://", "")} · {p.subject}</div>
              {p.active && <Button size="sm" variant="ghost" className="mt-3 -ml-2" onClick={() => deactivate.mutate(p.id, { onSuccess: () => toast("info", "Principal deactivated") })}>Deactivate</Button>}
            </Glass>
          ))}
        </div>
      )}

      {tab === "matrix" && (
        <Glass className="overflow-x-auto p-6" spotlight={false}>
          <SectionTitle eyebrow="edisc_api.authz" title={<span className="flex items-center gap-2"><ShieldCheck className="size-4 text-mint" />What each role can do</span>} />
          {!matrix.data && <Skeleton className="h-72" />}
          {matrix.data && (<table className="w-full text-[12px]">
            <thead><tr><th className="py-2 text-left font-normal text-white/60">Permission</th>{matrix.data.roles.map(({ name: r }) => <th key={r} className="px-2 py-2 font-mono text-[11px] font-normal" style={{ color: roleColor(r) }}>{r}</th>)}</tr></thead>
            <tbody>
              {[...new Set(matrix.data.roles.flatMap((r) => r.permissions))].sort().map((p, i) => (
                <motion.tr key={p} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: i * 0.025 }} className="border-t border-white/[0.05]">
                  <td className="py-2 font-mono text-white/65">{p}</td>
                  {matrix.data.roles.map(({ name: r, permissions }) => (
                    <td key={r} className="px-2 py-2 text-center">
                      {permissions.includes(p) ? <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ delay: 0.2 + i * 0.02, type: "spring" }} className="inline-grid size-5 place-items-center rounded-md" style={{ background: `${roleColor(r)}22`, color: roleColor(r) }}><Check className="size-3" strokeWidth={3} aria-hidden /><span className="sr-only">granted</span></motion.span> : <span className="text-white/55"><span aria-hidden>·</span><span className="sr-only">not granted</span></span>}
                    </td>
                  ))}
                </motion.tr>
              ))}
            </tbody>
          </table>)}
        </Glass>
      )}

      <AssignModal open={open} onClose={() => setOpen(false)} />
    </>
  );
}

function AssignModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const principals = usePrincipals();
  const { data } = useRollup();
  const toast = useToast();
  const [pid, setPid] = useState("");
  const matrix = useRoleMatrix();
  const [role, setRole] = useState("reviewer");
  const [scopeType, setScopeType] = useState<ScopeType>("matter");
  const [scopeId, setScopeId] = useState("");
  const create = useAction(() => api.createAssignment({ principal_id: pid, role, scope_type: scopeType, scope_id: scopeType === "tenant" ? null : scopeId }), () => [qk.assignments]);
  const targets = scopeType === "client" ? data?.clients : scopeType === "matter" ? data?.matters : [];
  return (
    <Modal open={open} onClose={onClose} title="Assign a role" subtitle="Roles inherit downward: client › matter › workspace.">
      <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); create.mutate(undefined, { onSuccess: () => { toast("ok", "Role assigned"); onClose(); } }); }}>
        <Field label="Principal"><select className="input" value={pid} onChange={(e) => setPid(e.target.value)}><option value="">Choose…</option>{principals.data?.filter((p) => p.active).map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}</select></Field>
        <div className="grid grid-cols-3 gap-1.5">
          {matrix.data?.roles.map(({ name: r }) => (
            <button type="button" key={r} onClick={() => setRole(r)} className={clsx("rounded-xl border px-2 py-2 font-mono text-[11px] transition", role === r ? "border-white/30 bg-white/[0.07]" : "border-white/[0.06] text-white/65")} style={role === r ? { color: roleColor(r) } : undefined}>{r}</button>
          ))}
        </div>
        <div className="grid grid-cols-[140px_1fr] gap-3">
          <Field label="Scope"><select className="input" value={scopeType} onChange={(e) => { setScopeType(e.target.value as ScopeType); setScopeId(""); }}>{(["tenant", "client", "matter"] as const).map((s) => <option key={s}>{s}</option>)}</select></Field>
          {scopeType !== "tenant" && <Field label="Target"><select className="input" value={scopeId} onChange={(e) => setScopeId(e.target.value)}><option value="">Choose…</option>{targets?.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></Field>}
        </div>
        <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!pid || (scopeType !== "tenant" && !scopeId)}>Assign</Button></div>
      </form>
    </Modal>
  );
}
