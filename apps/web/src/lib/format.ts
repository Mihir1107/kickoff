const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31_536_000], ["month", 2_592_000], ["week", 604_800], ["day", 86_400], ["hour", 3_600], ["minute", 60], ["second", 1],
];

export function ago(isoOrMs: string | number): string {
  const s = (new Date(isoOrMs).getTime() - Date.now()) / 1000;
  for (const [u, n] of UNITS) if (Math.abs(s) >= n || u === "second") return rtf.format(Math.round(s / n), u);
  return "";
}

/** Always shown in UTC: the system never assumes a local zone (edisc_core.time). */
export const utc = (iso: string, withTime = true) =>
  new Date(iso).toLocaleString("en-GB", {
    timeZone: "UTC", year: "numeric", month: "short", day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }) + (withTime ? " UTC" : "");

export const day = (iso: string) => iso.slice(0, 10);

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

export const num = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("en-US"));
export const short = (h: string, n = 6) => (h.length > n * 2 + 1 ? `${h.slice(0, n)}…${h.slice(-n)}` : h);
export const shortId = (id: string) => id.slice(-8);

export function duration(ms: number): string {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
}

export const newKey = () => crypto.randomUUID();
