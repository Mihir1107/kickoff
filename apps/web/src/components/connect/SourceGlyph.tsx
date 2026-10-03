/** Source mark for connection cards and pickers. */
export function SourceGlyph({ source }: { source: string }) {
  const map: Record<string, [string, string]> = {
    slack: ["#", "linear-gradient(135deg,#e01e5a,#ecb22e 50%,#2eb67d)"],
    slack_export: ["Z", "linear-gradient(135deg,#8b7cff,#5ad8ff)"],
    teams: ["T", "linear-gradient(135deg,#5b5fc7,#7b83eb)"],
    dummy: ["◆", "linear-gradient(135deg,#7cf5d2,#5ad8ff)"],
  };
  const [g, bg] = map[source] ?? ["?", "#333"];
  // A logo mark beside the source name (decorative, exempt from contrast as a logotype).
  return <div aria-hidden className="grid size-10 place-items-center rounded-xl text-[17px] font-bold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.3)]" style={{ background: bg }}>{g}</div>;
}
