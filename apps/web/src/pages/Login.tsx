import { motion } from "framer-motion";
import { ArrowRight, Fingerprint, KeyRound, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, API_MODE } from "@/api";
import { Logo } from "@/components/layout/Logo";
import { Button } from "@/components/ui/Button";
import { useReducedMotion } from "@/lib/motion";

const HEX = "0123456789abcdef";

/** Columns of drifting hex: every item is hashed at collection. */
function HashRain() {
  const reduced = useReducedMotion();
  const [cols] = useState(() => Array.from({ length: 22 }, (_, i) => ({
    x: (i / 22) * 100 + Math.random() * 2,
    dur: 14 + Math.random() * 18,
    delay: -Math.random() * 20,
    text: Array.from({ length: 64 }, () => HEX[Math.floor(Math.random() * 16)]).join(""),
    o: 0.05 + Math.random() * 0.12,
  })));
  if (reduced) return null;
  return (
    <div aria-hidden data-decorative-motion className="pointer-events-none absolute inset-0 overflow-hidden [mask-image:radial-gradient(ellipse_at_center,#000_20%,transparent_70%)]">
      {cols.map((c, i) => (
        <motion.div key={i} className="absolute top-0 font-mono text-[12px] leading-[1.15] text-mint [writing-mode:vertical-rl]"
          style={{ left: `${c.x}%`, opacity: c.o }} initial={{ y: "-100%" }} animate={{ y: "100vh" }} transition={{ duration: c.dur, delay: c.delay, repeat: Infinity, ease: "linear" }}>
          {c.text}
        </motion.div>
      ))}
    </div>
  );
}

export function Login() {
  const nav = useNavigate();
  // Only a same-site path may be a return target: never an absolute or protocol-relative URL.
  const raw = new URLSearchParams(useLocation().search).get("from") ?? "/";
  const from = raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/\\") ? raw : "/";
  const [busy, setBusy] = useState(false);
  // Live: the tenant IS this page's subdomain (ADR 0013); switching tenant means opening another address.
  const [host, setHost] = useState(() => (API_MODE === "http" ? window.location.hostname.split(".")[0] ?? "" : "halcyon"));
  useEffect(() => { document.title = "Sign in · Vault"; return () => { document.title = "Vault · Defensible Collection"; }; }, []);
  return (
    <div className="relative grid h-full place-items-center overflow-hidden px-4">
      <HashRain />
      <motion.div initial={{ opacity: 0, y: 30, scale: 0.96, filter: "blur(12px)" }} animate={{ opacity: 1, y: 0, scale: 1, filter: "blur(0px)" }} transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }}
        className="glass relative w-full max-w-[440px] p-8">
        <div className="flex items-center gap-3"><Logo size={40} /><span className="font-display text-[26px]">Vault</span></div>
        <h1 className="mt-8 font-display text-[46px] leading-[0.95] tracking-tight">Collect <em className="text-gradient">defensibly</em>.</h1>
        <p className="mt-3 text-[13.5px] leading-relaxed text-white/65">Hash-chained custody, WORM evidence, reconciliation on every conversation-day. Nothing is lost quietly.</p>

        <div className="mt-8">
          <label className="mb-1.5 block text-[12px] text-white/55">Tenant</label>
          <div data-field className="flex items-center rounded-xl border border-[var(--control-border)] bg-white/[0.03] pr-3 focus-within:border-mint">
            <input id="tenant" aria-label="Tenant" readOnly={API_MODE === "http"} value={host} onChange={(e) => setHost(e.target.value.replace(/[^a-z0-9-]/g, ""))} className="h-11 flex-1 bg-transparent px-3.5 font-mono text-[13.5px] outline-none" />
            <span className="font-mono text-[12.5px] text-white/55">.edisc.localhost</span>
          </div>
          <p className="mt-1.5 text-[11.5px] text-white/55">Your tenant comes from the address, never from a form field the server trusts.</p>
        </div>

        <Button variant="primary" className="mt-6 w-full !h-11" loading={busy} icon={<KeyRound className="size-4" />}
          onClick={() => {
            setBusy(true);
            // Demo: enter directly. Live: the backend runs the IdP flow, sets the HttpOnly session cookie
            // and redirects back here; the browser never receives a token.
            if (API_MODE === "demo") setTimeout(() => nav(from), 900);
            else window.location.assign(api.loginUrl(from));
          }}>
          Continue with SSO <ArrowRight className="size-4" />
        </Button>

        <div className="mt-8 grid grid-cols-3 gap-2 text-center text-[10.5px] text-white/60">
          {[[ShieldCheck, "RLS isolated"], [Fingerprint, "SHA-256 chained"], [KeyRound, "Envelope-encrypted"]].map(([I, l], i) => {
            const Icon = I as typeof ShieldCheck;
            return <div key={i} className="glass-sub flex flex-col items-center gap-1.5 py-3"><Icon className="size-4 text-mint/80" />{l as string}</div>;
          })}
        </div>
      </motion.div>
    </div>
  );
}
