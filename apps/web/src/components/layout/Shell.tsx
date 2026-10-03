import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { Command, LogOut, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, API_MODE } from "@/api";
import { useMe, useMyPermissions, useRollup } from "@/api/hooks";
import { CommandPalette } from "./CommandPalette";
import { Logo } from "./Logo";
import { NAV } from "./nav";

export function Shell() {
  const loc = useLocation();
  const nav = useNavigate();
  const [palette, setPalette] = useState(false);
  const me = useMe();
  const perms = useMyPermissions();
  // Server-side session: GET /v1/me without one is a 401, which means "sign in" (we never see a token).
  useEffect(() => {
    if (me.isError) nav(`/login?from=${encodeURIComponent(loc.pathname)}`, { replace: true });
  }, [me.isError, nav, loc.pathname]);
  const name = me.data?.subject ?? "…";
  const initials = name.replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase();
  const scopes = perms.data?.scopes ?? [];
  const tenantWide = scopes.some((s) => s.scope_type === "tenant" && s.permissions.length > 0);
  const rollup = useRollup();
  const live = rollup.data?.jobs.filter((j) => j.status === "running").length ?? 0;
  const attention = rollup.data?.jobs.filter((j) => j.status === "paused_awaiting_reauth" || j.status === "failed").length ?? 0;

  return (
    <div className="flex h-full">
      <aside className="relative z-20 hidden w-[264px] shrink-0 flex-col border-r border-white/[0.06] bg-ink-950/40 px-4 py-5 backdrop-blur-2xl lg:flex">
        <div className="mb-8 flex items-center gap-3 px-2">
          <Logo />
          <div>
            <div className="font-display text-[22px] leading-none tracking-tight">Vault</div>
            <div className="eyebrow mt-1 !text-[9.5px]">Defensible collection</div>
          </div>
        </div>

        <button onClick={() => setPalette(true)}
          className="focus-ring mb-6 flex items-center gap-2 rounded-xl border border-white/[0.07] bg-white/[0.025] px-3 py-2 text-[12.5px] text-white/60 transition hover:border-white/15 hover:text-white/70">
          <Search className="size-3.5" /> Jump to…
          <span className="ml-auto flex items-center gap-0.5 rounded-md border border-white/10 px-1.5 py-0.5 font-mono text-[10px]"><Command className="size-2.5" />K</span>
        </button>

        <div className="eyebrow mb-2 px-3">Workspace</div>
        <nav className="flex flex-col gap-0.5">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={"end" in n}
              className={({ isActive }) => clsx("focus-ring group relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-[13.5px] transition-colors", isActive ? "text-white" : "text-white/65 hover:text-white/85")}>
              {({ isActive }) => (
                <>
                  {isActive && (
                    <motion.span layoutId="nav-pill" transition={{ type: "spring", stiffness: 400, damping: 34 }}
                      className="absolute inset-0 rounded-xl border border-white/[0.08] bg-[linear-gradient(100deg,rgba(124,245,210,0.12),rgba(139,124,255,0.06)_60%,transparent)] shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                      <span className="absolute -left-4 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-mint shadow-[0_0_12px_#7cf5d2]" />
                    </motion.span>
                  )}
                  <n.icon className={clsx("relative size-[17px] transition", isActive ? "text-mint" : "group-hover:scale-110")} strokeWidth={1.8} />
                  <span className="relative">{n.label}</span>
                  {n.to === "/collections" && live > 0 && (
                    <span className="relative ml-auto flex items-center gap-1.5 rounded-full bg-cyan/10 px-2 py-0.5 font-mono text-[10px] text-cyan">
                      <span className="size-1.5 animate-pulse rounded-full bg-cyan" />{live}
                    </span>
                  )}
                  {n.to === "/" && attention > 0 && (
                    <span className="relative ml-auto rounded-full bg-amber/10 px-2 py-0.5 font-mono text-[10px] text-amber">{attention}</span>
                  )}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto space-y-3">
          <div className="glass-sub p-3">
            <div className="flex items-center justify-between">
              <span className="eyebrow">Tenant</span>
              <span className={clsx("rounded-md px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-wider", API_MODE === "demo" ? "bg-iris/15 text-iris" : "bg-mint/15 text-mint")}>
                {API_MODE === "demo" ? "demo data" : "live api"}
              </span>
            </div>
            <div className="mt-1.5 text-[13px] font-medium">Halcyon Legal</div>
            <div className="font-mono text-[11px] text-white/55">halcyon.edisc.localhost</div>
          </div>
          <div className="flex items-center gap-3 rounded-xl px-2 py-1.5">
            <div aria-hidden className="grid size-8 place-items-center rounded-full bg-[linear-gradient(135deg,#7cf5d2,#8b7cff)] text-[12px] font-semibold text-ink-950">{initials}</div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px]">{name}</div>
              <div className="truncate font-mono text-[10.5px] text-white/55">{perms.data ? (tenantWide ? "tenant-wide access" : `${scopes.length} scope${scopes.length === 1 ? "" : "s"}`) : perms.isError ? "permissions unavailable" : "…"}</div>
            </div>
            <button onClick={() => void api.logout().finally(() => nav("/login"))} className="focus-ring rounded-lg p-1.5 text-white/60 hover:bg-white/5 hover:text-white" title="Sign out" aria-label="Sign out"><LogOut className="size-4" /></button>
          </div>
        </div>
      </aside>

      <main className="relative z-10 flex-1 overflow-y-auto overflow-x-hidden">
        <div className="sticky top-0 z-30 border-b border-white/[0.06] bg-ink-950/70 backdrop-blur-xl lg:hidden">
          <div className="flex items-center gap-3 px-4 pt-3">
            <Logo size={28} />
            <span className="font-display text-[20px]">Vault</span>
            <button onClick={() => setPalette(true)} className="focus-ring ml-auto rounded-lg border border-white/10 p-2 text-white/65"><Search className="size-4" /></button>
          </div>
          <nav className="flex gap-1 overflow-x-auto px-3 py-2">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={"end" in n}
                className={({ isActive }) => clsx("flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px]", isActive ? "bg-white/[0.08] text-white" : "text-white/65")}>
                <n.icon className="size-3.5" />{n.label}
              </NavLink>
            ))}
          </nav>
        </div>
        <AnimatePresence mode="wait">
          <motion.div key={loc.pathname} className="mx-auto max-w-[1440px] px-5 pb-24 pt-8 md:px-10"
            initial={{ opacity: 0, y: 14, filter: "blur(10px)" }} animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
            exit={{ opacity: 0, y: -8, filter: "blur(6px)" }} transition={{ duration: 0.38, ease: [0.16, 1, 0.3, 1] }}>
            <Outlet />
          </motion.div>
        </AnimatePresence>
      </main>
      <CommandPalette open={palette} setOpen={setPalette} />
    </div>
  );
}
