import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * Errors the API reports by redirect (ADR 0016): `?auth_error=<code>` after a failed sign-in (it lands on
 * `/`) and `?install_error=<code>` after a failed source install. They are read once, removed from the
 * URL, and shown until dismissed. Only a code-shaped value is ever echoed: provider text never is.
 */
const AUTH_ERRORS: Record<string, string> = {
  denied: "Sign-in was cancelled or refused at your identity provider.",
  expired: "The sign-in took too long and expired. Please sign in again.",
  invalid: "The sign-in response could not be verified. Please sign in again.",
  unknown_user: "Your account is not registered for this workspace. Ask a tenant administrator for access.",
  inactive: "Your account has been deactivated for this workspace. Contact a tenant administrator.",
};
// ADR 0016 says install errors come "from a fixed list" but does not name the codes yet: every code gets
// the generic message plus the code until it does.
const INSTALL_ERRORS: Record<string, string> = {};

const CODE = /^[a-z0-9_]{1,40}$/i;

interface Notice {
  kind: "auth" | "install";
  message: string;
  code: string | null;
}

export function describeReturnError(kind: "auth" | "install", raw: string): Notice {
  const code = CODE.test(raw) ? raw : null;
  const known = code ? (kind === "auth" ? AUTH_ERRORS : INSTALL_ERRORS)[code] : undefined;
  if (known && Object.hasOwn(kind === "auth" ? AUTH_ERRORS : INSTALL_ERRORS, code!)) return { kind, message: known, code: null };
  return {
    kind,
    message: kind === "auth" ? "Sign-in did not complete." : "The source could not be connected. Nothing was changed.",
    code: code ?? "unrecognised",
  };
}

export function ReturnNotices() {
  const [params, setParams] = useSearchParams();
  const [notices, setNotices] = useState<Notice[]>([]);

  useEffect(() => {
    const auth = params.get("auth_error");
    const install = params.get("install_error");
    if (auth === null && install === null) return;
    const incoming = [...(auth !== null ? [describeReturnError("auth", auth)] : []), ...(install !== null ? [describeReturnError("install", install)] : [])];
    // Idempotent: the same notice is never shown twice (an effect may run more than once for one URL).
    const same = (a: Notice, b: Notice) => a.kind === b.kind && a.message === b.message && a.code === b.code;
    setNotices((n) => [...n, ...incoming.filter((x) => !n.some((y) => same(x, y)))]);
    setParams((p) => { p.delete("auth_error"); p.delete("install_error"); return p; }, { replace: true });
  }, [params, setParams]);

  return (
    <div className="pointer-events-none fixed inset-x-0 top-4 z-[75] flex flex-col items-center gap-2 px-4">
      <AnimatePresence>
        {notices.map((n, i) => (
          <motion.div key={`${n.kind}-${i}`} role="alert" initial={{ opacity: 0, y: -12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}
            className="glass pointer-events-auto flex w-full max-w-[560px] items-start gap-3 border-l-2 border-rose p-4">
            <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-rose" />
            <div className="min-w-0 flex-1 text-[13px]">
              <div className="font-medium">{n.kind === "auth" ? "Sign-in failed" : "Connection failed"}</div>
              <p className="mt-0.5 text-white/75">{n.message}</p>
              {n.code && <p className="mt-1 font-mono text-[11.5px] text-white/65">Code: {n.code}</p>}
            </div>
            <button type="button" aria-label="Dismiss" onClick={() => setNotices((all) => all.filter((x) => x !== n))}
              className="rounded-lg p-1 text-white/65 hover:bg-white/5 hover:text-white"><X aria-hidden className="size-4" /></button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
