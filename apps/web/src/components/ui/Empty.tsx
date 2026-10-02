import type { ReactNode } from "react";

export function Empty({ icon, title, body, action }: { icon: ReactNode; title: string; body?: ReactNode; action?: ReactNode }) {
  return (
    <div className="grid place-items-center gap-3 py-14 text-center">
      <div className="grid size-12 place-items-center rounded-2xl border border-white/10 bg-white/[0.03] text-white/65">{icon}</div>
      <div className="text-[14px] font-medium text-white/80">{title}</div>
      {body && <div className="max-w-sm text-[12.5px] text-white/60">{body}</div>}
      {action}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={`skeleton ${className ?? ""}`} />;
}
