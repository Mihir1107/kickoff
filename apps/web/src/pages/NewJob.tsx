import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, ArrowRight, Check, Hash as HashIcon, Rocket, User, X } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "@/api";
import { useAction, useClientConnections, useDirectory, useMatter, useRollup, useWorkspaces } from "@/api/hooks";
import type { ScopeIn, ThreadParentPolicy } from "@/api/types";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Glass } from "@/components/ui/Glass";
import { Field } from "@/components/ui/Modal";
import { StatusPill } from "@/components/ui/StatusPill";
import { useToast } from "@/components/ui/Toast";
import { newKey } from "@/lib/format";
import { conversationName, custodianName } from "@/lib/names";
import { connectionStatus } from "@/lib/status";
import { SourceGlyph } from "./ClientDetail";

const STEPS = ["Matter & source", "Scopes", "Review"] as const;
const POLICIES: { v: ThreadParentPolicy; label: string; hint: string }[] = [
  { v: "include_parent_and_thread", label: "Parent + whole thread", hint: "Default (ADR 0011). In-range replies pull in their parent and the full thread." },
  { v: "include_parent_only", label: "Parent only", hint: "In-range replies pull in only the thread parent." },
  { v: "replies_only", label: "Replies only", hint: "Only messages inside the date range." },
];

export function NewJob() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const toast = useToast();
  const rollup = useRollup();
  const [step, setStep] = useState(0);
  const [dir, setDir] = useState(1);
  const [matterId, setMatterId] = useState(params.get("matter") ?? "");
  const matter = useMatter(matterId);
  const conns = useClientConnections(matter.data?.client_id);
  const ws = useWorkspaces(matterId);
  const [connectionId, setConnectionId] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [kind, setKind] = useState<"channel" | "custodian">("channel");
  const [picked, setPicked] = useState<string[]>([]);
  const [manual, setManual] = useState("");
  const directory = useDirectory(connectionId || undefined);
  const options = [...new Set([...(kind === "channel" ? directory.data?.conversations : directory.data?.custodians)?.map((x) => x.id) ?? [], ...picked])];
  const addManual = () => { const id = manual.trim(); if (id && !picked.includes(id)) setPicked((p) => [...p, id]); setManual(""); };
  const [from, setFrom] = useState("2026-06-01");
  const [to, setTo] = useState("2026-08-31");
  const [policy, setPolicy] = useState<ThreadParentPolicy>("include_parent_and_thread");
  const key = useRef(newKey()); // one Idempotency-Key per intended job; retries reuse it

  const scopes: ScopeIn[] = picked.map((external_id) => ({ type: kind, external_id, date_from: `${from}T00:00:00Z`, date_to: `${to}T00:00:00Z`, thread_parent_policy: policy }));
  const days = Math.max(0, Math.round((Date.parse(to) - Date.parse(from)) / 86_400_000));
  const start = useAction(() => api.startJob(matterId, { connection_id: connectionId, scopes, workspace_id: workspaceId || null }, key.current), () => [["rollup"]]);

  const openMatters = useMemo(() => rollup.data?.matters.filter((m) => !m.closed_at) ?? [], [rollup.data]);
  const clientName = new Map(rollup.data?.clients.map((c) => [c.id, c.name]));
  const can = [!!matterId && !!connectionId, picked.length > 0 && days > 0 && days <= 3660, true][step];
  const go = (d: number) => { setDir(d); setStep((s) => s + d); };

  return (
    <>
      <PageHeader crumbs={[{ label: "Collections", to: "/collections" }, { label: "New" }]} eyebrow="Start a collection"
        title={<>New <em className="text-gradient">collection</em></>}
        subtitle="The job splits into one unit per conversation per UTC day. Every unit is checkpointed in the database, so the job survives a crash anywhere." />

      {/* Stepper */}
      <div className="mb-6 flex items-center gap-2">
        {STEPS.map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <motion.div animate={{ backgroundColor: i <= step ? "rgba(124,245,210,1)" : "rgba(255,255,255,0.06)", color: i <= step ? "#05060a" : "rgba(255,255,255,0.5)", scale: i === step ? 1.08 : 1 }}
              className="grid size-7 place-items-center rounded-full text-[12px] font-semibold">{i < step ? <Check className="size-3.5" strokeWidth={3} /> : i + 1}</motion.div>
            <span className={clsx("text-[13px]", i === step ? "text-white" : "text-white/60")}>{s}</span>
            {i < STEPS.length - 1 && <div className="relative mx-2 h-px w-16 bg-white/10"><motion.div className="absolute inset-y-0 left-0 bg-mint" animate={{ width: i < step ? "100%" : "0%" }} /></div>}
          </div>
        ))}
      </div>

      <Glass className="min-h-[440px] p-7" spotlight={false}>
        <AnimatePresence mode="wait" custom={dir}>
          <motion.div key={step} custom={dir} initial={{ opacity: 0, x: 40 * dir, filter: "blur(8px)" }} animate={{ opacity: 1, x: 0, filter: "blur(0px)" }} exit={{ opacity: 0, x: -40 * dir, filter: "blur(8px)" }} transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}>
            {step === 0 && (
              <div className="grid gap-8 lg:grid-cols-2">
                <div>
                  <div className="eyebrow mb-3">Matter</div>
                  <div className="max-h-[340px] space-y-1.5 overflow-y-auto pr-1">
                    {openMatters.map((m) => (
                      <button key={m.id} onClick={() => { setMatterId(m.id); setConnectionId(""); }}
                        className={clsx("flex w-full items-center gap-3 rounded-xl border px-4 py-3 text-left transition", matterId === m.id ? "border-mint/40 bg-mint/[0.06]" : "border-white/[0.06] bg-white/[0.02] hover:border-white/15")}>
                        <div className="min-w-0 flex-1"><div className="truncate text-[13.5px]">{m.name}</div><div className="text-[11.5px] text-white/60">{clientName.get(m.client_id)}</div></div>
                        {matterId === m.id && <motion.div layoutId="m-check"><Check className="size-4 text-mint" /></motion.div>}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <div className="eyebrow mb-3">Connection (owned by the client)</div>
                  {!matterId && <div className="rounded-xl border border-dashed border-white/10 p-8 text-center text-[12.5px] text-white/60">Pick a matter first.</div>}
                  <div className="space-y-2">
                    {conns.data?.map((c) => {
                      const s = connectionStatus(c.status);
                      const usable = c.status === "active";
                      return (
                        <button key={c.id} disabled={!usable} onClick={() => setConnectionId(c.id)}
                          className={clsx("flex w-full items-center gap-3 rounded-xl border px-4 py-3 text-left transition disabled:cursor-not-allowed disabled:opacity-45", connectionId === c.id ? "border-mint/40 bg-mint/[0.06]" : "border-white/[0.06] bg-white/[0.02] hover:border-white/15")}>
                          <SourceGlyph source={c.source} />
                          <div className="min-w-0 flex-1"><div className="text-[13.5px]">{c.source}</div><div className="truncate font-mono text-[11px] text-white/60">{c.external_org_id}</div></div>
                          {s && <StatusPill tone={s.tone} label={s.label} />}
                        </button>
                      );
                    })}
                  </div>
                  {ws.data && ws.data.length > 0 && (
                    <div className="mt-6"><Field label="Workspace (optional)">
                      <select className="input" value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)}>
                        <option value="">No workspace</option>
                        {ws.data.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                      </select>
                    </Field></div>
                  )}
                </div>
              </div>
            )}

            {step === 1 && (
              <div className="grid gap-8 lg:grid-cols-[1.3fr_1fr]">
                <div>
                  <div className="mb-4 flex items-center gap-2">
                    {(["channel", "custodian"] as const).map((k) => (
                      <button key={k} onClick={() => { setKind(k); setPicked([]); }} className={clsx("relative flex items-center gap-2 rounded-xl px-4 py-2 text-[13px]", kind === k ? "text-white" : "text-white/60")}>
                        {kind === k && <motion.span layoutId="kind" className="absolute inset-0 rounded-xl bg-white/[0.08]" />}
                        <span className="relative flex items-center gap-2">{k === "channel" ? <HashIcon className="size-3.5" /> : <User className="size-3.5" />}{k === "channel" ? "Conversations" : "Custodians"}</span>
                      </button>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {options.map((x) => {
                      const on = picked.includes(x);
                      return (
                        <motion.button key={x} layout whileTap={{ scale: 0.94 }} onClick={() => setPicked((p) => (on ? p.filter((y) => y !== x) : [...p, x]))}
                          className={clsx("flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] transition-colors", on ? "border-mint/40 bg-mint/10 text-mint" : "border-white/10 bg-white/[0.02] text-white/60 hover:border-white/25")}>
                          {on ? <Check className="size-3" /> : null}{kind === "channel" ? conversationName(x) : custodianName(x)}
                        </motion.button>
                      );
                    })}
                  </div>
                  <div className="mt-5 flex gap-2">
                    <input className="input font-mono" value={manual} onChange={(e) => setManual(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addManual(); } }}
                      placeholder={kind === "channel" ? "Add a conversation id, e.g. C04LEGALHOLD" : "Add a custodian id, e.g. U01ABCD"} aria-label={kind === "channel" ? "Conversation id" : "Custodian id"} />
                    <Button type="button" onClick={addManual} disabled={!manual.trim()}>Add</Button>
                  </div>
                  {!directory.available && <p className="mt-3 text-[11.5px] text-white/60">No directory route yet: enter source ids directly.</p>}
                </div>
                <div className="space-y-4">
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="From (UTC, inclusive)"><input type="date" className="input" value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
                    <Field label="To (UTC, exclusive)"><input type="date" className="input" value={to} onChange={(e) => setTo(e.target.value)} /></Field>
                  </div>
                  <div>
                    <div className="mb-1.5 text-[12px] font-medium text-white/60">Thread parent policy</div>
                    <div className="space-y-1.5">
                      {POLICIES.map((p) => (
                        <button key={p.v} onClick={() => setPolicy(p.v)} className={clsx("w-full rounded-xl border px-3.5 py-2.5 text-left transition", policy === p.v ? "border-iris/40 bg-iris/[0.07]" : "border-white/[0.06] hover:border-white/15")}>
                          <div className="text-[13px]">{p.label}</div><div className="text-[11.5px] text-white/60">{p.hint}</div>
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {step === 2 && (
              <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
                <div className="space-y-3">
                  <Summary k="Matter" v={matter.data?.name} />
                  <Summary k="Connection" v={conns.data?.find((c) => c.id === connectionId)?.external_org_id} mono />
                  <Summary k="Window" v={`${from} → ${to} (${days} days, UTC)`} mono />
                  <Summary k="Thread policy" v={policy} mono />
                  <div className="glass-sub p-4">
                    <div className="mb-2 text-[12px] text-white/60">{scopes.length} {kind} scope{scopes.length === 1 ? "" : "s"}</div>
                    <div className="flex flex-wrap gap-1.5">{picked.map((x) => <span key={x} className="flex items-center gap-1 rounded-md bg-white/[0.05] px-2 py-1 text-[12px]">{kind === "channel" ? conversationName(x) : custodianName(x)}<X className="size-3 cursor-pointer text-white/55 hover:text-white" onClick={() => setPicked((p) => p.filter((y) => y !== x))} /></span>)}</div>
                  </div>
                </div>
                <div className="glass-sub relative overflow-hidden p-5">
                  <div className="pointer-events-none absolute -right-10 -top-10 size-40 rounded-full bg-mint/10 blur-3xl" />
                  <div className="eyebrow">Estimate</div>
                  <div className="mt-2 text-[44px] font-light leading-none">{kind === "channel" ? (picked.length * days).toLocaleString() : "≈"}</div>
                  <div className="text-[12px] text-white/60">{kind === "channel" ? "conversation-day units" : "units are resolved from custodians' conversations at enumeration"}</div>
                  <div className="mt-5 text-[11.5px] text-white/60">Idempotency-Key</div>
                  <div className="font-mono text-[11px] text-white/60">{key.current}</div>
                  <p className="mt-2 text-[11px] leading-relaxed text-white/55">Retrying with the same key returns the same job, never a duplicate.</p>
                </div>
              </div>
            )}
          </motion.div>
        </AnimatePresence>
      </Glass>

      <div className="mt-5 flex justify-between">
        <Button variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={() => (step === 0 ? nav(-1) : go(-1))}>{step === 0 ? "Cancel" : "Back"}</Button>
        {step < 2 ? (
          <Button variant="primary" disabled={!can} onClick={() => go(1)}>Continue <ArrowRight className="size-4" /></Button>
        ) : (
          <Button variant="primary" icon={<Rocket className="size-4" />} loading={start.isPending}
            onClick={() => start.mutate(undefined, { onSuccess: (j) => { toast("ok", "Collection started", "Watch units fill in live."); nav(`/jobs/${j.id}`); }, onError: (e) => toast("error", "Could not start", (e as Error).message) })}>
            Start collection
          </Button>
        )}
      </div>
    </>
  );
}

function Summary({ k, v, mono }: { k: string; v?: string; mono?: boolean }) {
  return <div className="glass-sub flex items-center justify-between gap-4 px-4 py-3"><span className="text-[12px] text-white/60">{k}</span><span className={clsx("truncate text-right text-[13px]", mono && "font-mono text-[12px]")}>{v ?? "—"}</span></div>;
}
