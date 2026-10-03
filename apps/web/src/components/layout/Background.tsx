/** Aurora blobs + masked grid + film grain. Purely decorative, fixed behind everything. */
export function Background() {
  return (
    <div aria-hidden className="noise pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      <div className="absolute inset-0 bg-[radial-gradient(1200px_600px_at_70%_-10%,rgba(139,124,255,0.16),transparent_60%),radial-gradient(900px_500px_at_0%_10%,rgba(124,245,210,0.10),transparent_60%)]" />
      <div className="absolute -left-[20%] -top-[30%] h-[70vh] w-[70vw] animate-aurora rounded-full bg-[conic-gradient(from_90deg,rgba(124,245,210,0.22),rgba(90,216,255,0.10),rgba(139,124,255,0.22),rgba(124,245,210,0.22))] opacity-50 blur-[120px]" />
      <div className="absolute -right-[15%] top-[30%] h-[60vh] w-[50vw] animate-aurora rounded-full bg-[conic-gradient(from_200deg,rgba(139,124,255,0.25),rgba(255,107,139,0.08),rgba(90,216,255,0.18),rgba(139,124,255,0.25))] opacity-40 blur-[130px] [animation-delay:-9s]" />
      <div className="grid-bg absolute inset-0" />
      <div className="absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-ink-950 to-transparent" />
    </div>
  );
}
