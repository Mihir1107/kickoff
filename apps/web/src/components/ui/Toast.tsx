import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Info } from "lucide-react";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type Kind = "ok" | "error" | "info";
interface T { id: number; kind: Kind; title: string; body?: string }
const Ctx = createContext<(kind: Kind, title: string, body?: string) => void>(() => {});
export const useToast = () => useContext(Ctx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<T[]>([]);
  const push = useCallback((kind: Kind, title: string, body?: string) => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, kind, title, body }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), 4200);
  }, []);
  const icon = { ok: <CheckCircle2 className="size-4 text-mint" />, error: <AlertTriangle className="size-4 text-rose" />, info: <Info className="size-4 text-cyan" /> };
  return (
    <Ctx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-[70] flex w-[340px] flex-col gap-2">
        <AnimatePresence>
          {items.map((t) => (
            <motion.div key={t.id} layout initial={{ opacity: 0, x: 40, scale: 0.95 }} animate={{ opacity: 1, x: 0, scale: 1 }} exit={{ opacity: 0, x: 40, scale: 0.95 }}
              transition={{ type: "spring", stiffness: 420, damping: 32 }} className="glass pointer-events-auto flex gap-3 p-3.5">
              <div className="mt-0.5">{icon[t.kind]}</div>
              <div>
                <div className="text-[13px] font-medium">{t.title}</div>
                {t.body && <div className="mt-0.5 text-[12px] text-white/65">{t.body}</div>}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </Ctx.Provider>
  );
}
