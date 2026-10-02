import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { FileArchive, Fingerprint, UploadCloud } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "@/api";
import { qk, useRollup } from "@/api/hooks";
import type { ExportOut, SlackPlan } from "@/api/types";
import { SLACK_PLANS } from "@/api/types";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Empty } from "@/components/ui/Empty";
import { Glass, SectionTitle } from "@/components/ui/Glass";
import { Hash } from "@/components/ui/Hash";
import { StatusPill } from "@/components/ui/StatusPill";
import { useToast } from "@/components/ui/Toast";
import { Pipeline } from "@/components/viz/Pipeline";
import { ago, bytes, num } from "@/lib/format";
import { exportStatus } from "@/lib/status";
import { useQueryClient } from "@tanstack/react-query";

const PART = 8 * 2 ** 20; // within [part_min_bytes, part_max_bytes]; the server states the bounds

export function Exports() {
  const [params] = useSearchParams();
  const { data } = useRollup();
  const qc = useQueryClient();
  const toast = useToast();
  const [clientId, setClientId] = useState(params.get("client") ?? "");
  const [plan, setPlan] = useState<SlackPlan | "">("");
  const [file, setFile] = useState<File | null>(null);
  const [drag, setDrag] = useState(false);
  const [phase, setPhase] = useState<"idle" | "hashing" | "uploading" | "done">("idle");
  const [pct, setPct] = useState(0);
  const [digest, setDigest] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const client = clientId || data?.clients.find((c) => !c.closed_at)?.id || "";
  const exports = useMemo(() => (data?.exports ?? []).filter((x) => !clientId || x.client_id === clientId), [data, clientId]);

  async function upload() {
    if (!file || !client) return;
    try {
      // Declared hash: a mismatch with what the server computes rejects before any parsing (R7).
      setPhase("hashing");
      setPct(0);
      let sha: string | null = null;
      if (file.size <= 512 * 2 ** 20) {
        const d = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
        sha = [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
        setDigest(sha);
      }
      const x = await api.createExport(client, { size_bytes: file.size, sha256: sha, plan: plan || null });
      setPhase("uploading");
      const parts = Math.max(1, Math.ceil(file.size / PART));
      for (let n = 1; n <= parts; n++) {
        await api.uploadPart(x.id, n, file.slice((n - 1) * PART, n * PART));
        setPct(n / parts);
      }
      await api.completeExport(x.id);
      setPhase("done");
      toast("ok", "Upload complete", "Hashing, locking under WORM, then validating the locked version.");
      await qc.invalidateQueries({ queryKey: ["rollup"] });
      await qc.invalidateQueries({ queryKey: qk.exports(client) });
      setTimeout(() => { setFile(null); setPhase("idle"); setDigest(null); }, 1400);
    } catch (e) {
      setPhase("idle");
      toast("error", "Upload failed", (e as Error).message);
    }
  }

  return (
    <>
      <PageHeader eyebrow="ADR 0014 · archive ingestion" title={<>Slack <em className="text-gradient">exports</em></>}
        subtitle="The ZIP is evidence the moment it lands: hashed, locked under WORM, and only then parsed, from the locked version, with hard limits on entries and ratios." />

      <div className="grid gap-4 xl:grid-cols-[420px_1fr]">
        <Glass className="h-fit p-6">
          <SectionTitle eyebrow="Upload" title="New export" />
          <div className="space-y-3">
            <select className="input" value={client} onChange={(e) => setClientId(e.target.value)}>
              {data?.clients.filter((c) => !c.closed_at).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            <select className="input" value={plan} onChange={(e) => setPlan(e.target.value as SlackPlan | "")}>
              <option value="">Plan: detect from the archive</option>
              {SLACK_PLANS.map((p) => <option key={p} value={p}>Declared plan: {p}</option>)}
            </select>
          </div>

          <motion.div onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) setFile(f); }}
            onClick={() => input.current?.click()} animate={{ scale: drag ? 1.02 : 1 }}
            className={clsx("relative mt-4 grid cursor-pointer place-items-center overflow-hidden rounded-2xl border border-dashed px-6 py-10 text-center transition-colors", drag ? "border-mint/60 bg-mint/[0.06]" : "border-white/15 bg-white/[0.015] hover:border-white/30")}>
            <input ref={input} type="file" accept=".zip,application/zip" className="hidden" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            {drag && <div className="pointer-events-none absolute inset-x-0 h-1/2 animate-scan bg-gradient-to-b from-transparent via-mint/15 to-transparent" />}
            <motion.div animate={{ y: drag ? -6 : 0 }} className="grid size-14 place-items-center rounded-2xl border border-white/10 bg-white/[0.04]">
              {file ? <FileArchive className="size-6 text-iris" /> : <UploadCloud className="size-6 text-white/65" />}
            </motion.div>
            <div className="mt-3 text-[13.5px]">{file ? file.name : "Drop a workspace export ZIP"}</div>
            <div className="text-[12px] text-white/60">{file ? bytes(file.size) : "or click to choose · multipart, resumable"}</div>
          </motion.div>

          <AnimatePresence>
            {phase !== "idle" && (
              <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="mt-4 overflow-hidden">
                <div className="mb-1.5 flex justify-between text-[12px] text-white/55">
                  <span className="flex items-center gap-1.5">{phase === "hashing" ? <><Fingerprint className="size-3.5 text-iris" />SHA-256 in your browser…</> : phase === "uploading" ? "Uploading parts (Content-Digest per part)" : "Handed to ingest"}</span>
                  <span className="font-mono">{Math.round(pct * 100)}%</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
                  <motion.div className="h-full rounded-full bg-gradient-to-r from-iris via-cyan to-mint" animate={{ width: phase === "hashing" ? "12%" : `${pct * 100}%` }} />
                </div>
                {digest && <div className="mt-2 flex items-center gap-1 text-[11px] text-white/60">declared <Hash value={digest} /></div>}
              </motion.div>
            )}
          </AnimatePresence>

          <Button variant="primary" className="mt-4 w-full" disabled={!file || phase !== "idle"} loading={phase === "hashing" || phase === "uploading"} onClick={upload} icon={<UploadCloud className="size-4" />}>Upload & lock</Button>
        </Glass>

        <div className="space-y-4">
          {exports.map((x) => <ExportCard key={x.id} x={x} clientName={data?.clients.find((c) => c.id === x.client_id)?.name} />)}
          {data && exports.length === 0 && <Glass><Empty icon={<FileArchive />} title="No exports for this client" /></Glass>}
        </div>
      </div>
    </>
  );
}

export function ExportCard({ x, clientName }: { x: ExportOut; clientName?: string }) {
  const s = exportStatus(x.status);
  const findings = Object.entries(x.findings);
  return (
    <Glass className="p-6" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2"><span className="text-[15px] font-medium">{clientName ?? "Slack export"}</span><Hash value={x.id} n={4} /></div>
          <div className="mt-0.5 text-[12px] text-white/60">{bytes(x.declared_size)} · uploaded {ago(x.created_at)}{x.declared_plan && ` · declared ${x.declared_plan}`}</div>
        </div>
        <StatusPill tone={s.tone} label={s.label} title={s.hint} />
      </div>
      <div className="mt-6"><Pipeline status={x.status} rejectStage={(x.reject_detail?.stage as string) ?? undefined} /></div>

      {x.upload && (
        <div className="mt-5 font-mono text-[11.5px] text-white/65">
          {x.upload.parts_received} parts · {bytes(x.upload.bytes_received)} of {bytes(x.declared_size)} · session expires {ago(x.upload.expires_at)}
          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white/[0.06]"><div className="h-full bg-cyan" style={{ width: `${(x.upload.bytes_received / x.declared_size) * 100}%` }} /></div>
        </div>
      )}
      {x.status === "rejected" && (
        <div className="mt-5 rounded-xl border border-rose/25 bg-rose/[0.06] p-3.5 text-[12.5px]">
          <div className="font-medium text-rose">Rejected: {x.reject_reason}</div>
          {x.reject_detail && <div className="mt-1 font-mono text-[11px] text-white/65">{Object.entries(x.reject_detail).map(([k, v]) => `${k}=${String(v)}`).join("  ")}</div>}
          <div className="mt-1 text-[11.5px] text-white/60">The bytes stay locked as evidence of what was received.</div>
        </div>
      )}
      {(x.sha256 || x.entry_count != null) && (
        <div className="mt-5 grid grid-cols-2 gap-2 md:grid-cols-4">
          {x.sha256 && <div className="glass-sub p-3"><div className="text-[11px] text-white/60">SHA-256 (ours)</div><Hash value={x.sha256} n={5} className="!px-0" /></div>}
          {x.entry_count != null && <div className="glass-sub p-3"><div className="text-[11px] text-white/60">Entries</div><div className="font-mono text-[14px]">{num(x.entry_count)}</div></div>}
          {x.detected_tier && <div className="glass-sub p-3"><div className="text-[11px] text-white/60">Detected tier</div><div className="text-[13px]">{x.detected_tier}{x.tier_confirmed ? " ✓" : ""}</div></div>}
          {x.version_id && <div className="glass-sub p-3"><div className="text-[11px] text-white/60">Locked version</div><div className="truncate font-mono text-[11.5px]">{x.version_id}</div></div>}
        </div>
      )}
      {findings.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-1.5">
          {findings.map(([k, v]) => <span key={k} className="rounded-lg bg-white/[0.04] px-2 py-1 text-[11px] text-white/55"><span className="text-white/55">{k.replace(/_/g, " ")}</span> <span className="font-mono text-white/80">{String(v)}</span></span>)}
        </div>
      )}
    </Glass>
  );
}
