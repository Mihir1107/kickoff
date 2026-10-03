import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ArrowRight, ExternalLink, KeyRound, ShieldCheck } from "lucide-react";
import { useRef, useState } from "react";
import { api } from "@/api";
import { qk } from "@/api/hooks";
import type { ConnectionOut } from "@/api/types";
import { Button, ButtonLink } from "@/components/ui/Button";
import { Field, Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { CONNECT_OPTIONS, type SourceOption } from "@/lib/sources";
import { SourceGlyph } from "./SourceGlyph";

/**
 * Connect a source, or re-authorize an existing connection (`reauth`). Only the Slack internal-app tier
 * involves a credential in the browser; every other source starts a server-side flow or an upload.
 */
export function ConnectSourceModal({ open, onClose, clientId, reauth }: { open: boolean; onClose: () => void; clientId: string; reauth?: ConnectionOut }) {
  const options = CONNECT_OPTIONS.filter((o) => (reauth ? o.source === reauth.source && o.method !== "upload" : true));
  const [key, setKey] = useState<string | null>(null);
  const choice = options.find((o) => o.key === key) ?? (options.length === 1 ? options[0] : undefined);
  const close = () => { setKey(null); onClose(); };

  return (
    <Modal open={open} onClose={close} width={600}
      title={reauth ? "Re-authorize connection" : "Connect a source"}
      subtitle={reauth ? <>Paused jobs on <span className="font-mono">{reauth.external_org_id}</span> resume from their checkpoints once access is restored.</> : "Credentials never pass through this page, except an internal Slack app's one-time token."}>
      <div className="grid grid-cols-2 gap-2">
        {options.map((o) => (
          <button type="button" key={o.key} onClick={() => setKey(o.key)} aria-pressed={choice?.key === o.key}
            className={clsx("focus-ring flex items-start gap-3 rounded-2xl border p-3 text-left transition", choice?.key === o.key ? "border-mint/40 bg-mint/[0.06]" : "border-white/[0.07] bg-white/[0.02] hover:border-white/15")}>
            <SourceGlyph source={o.source} />
            <span>
              <span className="block text-[13px] font-medium">{o.name}</span>
              <span className="block text-[11.5px] leading-snug text-white/65">{o.hint}</span>
            </span>
          </button>
        ))}
      </div>
      <div className="mt-5">
        {!choice && <p className="text-[12.5px] text-white/60">Choose how this source connects.</p>}
        {choice?.method === "install" && <InstallPanel option={choice} clientId={clientId} reauth={reauth} />}
        {choice?.method === "token" && <InternalTokenForm key={reauth?.id ?? "new"} clientId={clientId} reauth={reauth} onDone={close} />}
        {choice?.method === "upload" && (
          <div className="glass-sub flex items-center justify-between gap-4 p-4 text-[12.5px] text-white/70">
            Exports become a connection once the uploaded archive is locked and validated.
            <ButtonLink to={`/exports?client=${clientId}`} onClick={close} size="sm" variant="primary">Upload export <ArrowRight aria-hidden className="size-3.5" /></ButtonLink>
          </div>
        )}
      </div>
    </Modal>
  );
}

/**
 * OAuth-capable sources (M17 plan section 6): the API creates a state-bound pending install and answers
 * with the provider's URL; the browser only follows redirects. Nothing secret passes through here.
 */
function InstallPanel({ option, clientId, reauth }: { option: SourceOption; clientId: string; reauth?: ConnectionOut }) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const provider = option.source === "teams" ? "Microsoft" : "Slack";
  return (
    <div className="glass-sub space-y-3 p-4">
      <p className="text-[12.5px] leading-relaxed text-white/70">
        You will sign in at {provider} and {option.source === "teams" ? "a Microsoft 365 admin grants consent" : "approve our app"}.
        {" "}{provider} sends the grant to our API, which encrypts it per tenant (ADR 0009) and brings you back here.
        This page never sees a token.
      </p>
      <Button variant="primary" loading={busy} icon={<ExternalLink className="size-4" />}
        onClick={async () => {
          setBusy(true);
          try {
            const { authorize_url } = await api.startInstall(clientId, option.source === "teams" ? "teams" : "slack", reauth?.id);
            const target = new URL(authorize_url, location.origin);
            // Follow only a provider URL over TLS (or our own origin, as the demo does): never a downgrade or a script URL.
            if (target.protocol !== "https:" && target.origin !== location.origin) throw new Error("Unexpected install URL from the API");
            window.location.assign(target.href);
          } catch (err) {
            setBusy(false);
            toast("error", "Could not start the install", err instanceof Error ? err.message : "Unexpected error");
          }
        }}>
        Continue to {provider}
      </Button>
    </div>
  );
}

/**
 * Slack internal-app tier: the one place a credential is typed in. The token
 * - lives only in this uncontrolled password input (never React state, query cache, URL or logs);
 * - has no `name`, so no native form submission could ever serialize it;
 * - is read once at submit, sent in the POST body over TLS, and the field is cleared on success.
 * The API never echoes credentials back (its test suite scans every response for them).
 */
function InternalTokenForm({ clientId, reauth, onDone }: { clientId: string; reauth?: ConnectionOut; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const formRef = useRef<HTMLFormElement>(null);
  const tokenRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const input = tokenRef.current;
    if (!input) return;
    if (!input.value.startsWith("xoxb-")) {
      setProblem("That is not a Slack bot token (they start with xoxb-). User tokens are not accepted.");
      return;
    }
    setProblem(null);
    setBusy(true);
    try {
      // M17 plan section 7: POST .../slack/token (or PUT /connections/{id}/token to replace). The API
      // validates it with Slack (auth.test, which also yields the team id) and encrypts it on receipt.
      await api.submitSlackToken(clientId, input.value, reauth?.id);
      input.value = "";
      formRef.current?.reset();
      await Promise.all([qc.invalidateQueries({ queryKey: qk.clientConnections(clientId) }), qc.invalidateQueries({ queryKey: ["rollup"] })]);
      toast("ok", reauth ? "Connection re-authorized" : "Connection created", "The token was encrypted on receipt and will not be shown again.");
      onDone();
    } catch (err) {
      // ApiError carries only the API's code and detail, which never include the credential.
      toast("error", reauth ? "Re-authorization failed" : "Could not connect", err instanceof Error ? err.message : "Unexpected error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form ref={formRef} onSubmit={submit} autoComplete="off" className="space-y-4">
      <div className="glass-sub flex gap-3 p-4 text-[12.5px] leading-relaxed text-white/70">
        <KeyRound className="mt-0.5 size-4 shrink-0 text-amber" aria-hidden />
        <p>
          <span className="font-medium text-white/90">Why a token?</span> An internal Slack app belongs to your workspace,
          so Slack has no install flow that grants it to us. A workspace admin copies the app&apos;s <span className="font-mono">xoxb-</span> bot
          token once. It goes over TLS straight to our API, which checks it with Slack, encrypts it per tenant, and never
          displays, logs or returns it again. Keep token rotation off for this app (ADR 0009).
        </p>
      </div>
      <Field label="Bot token" hint={problem ? <span className="text-rose" role="alert">{problem}</span> : "Write-only. Cleared from this page as soon as it is accepted."}>
        <input ref={tokenRef} type="password" required autoComplete="off" spellCheck={false} autoCapitalize="off" autoCorrect="off"
          data-1p-ignore data-lpignore="true" aria-label="Bot token" className="input font-mono" placeholder="xoxb-…" />
      </Field>
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[11.5px] text-white/60"><ShieldCheck className="size-3.5 text-mint" aria-hidden />Never stored in this browser</span>
        <Button variant="primary" loading={busy}>{reauth ? "Replace token" : "Connect"}</Button>
      </div>
    </form>
  );
}
