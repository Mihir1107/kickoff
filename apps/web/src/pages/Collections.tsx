import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useRollup } from "@/api/hooks";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { Empty, Skeleton } from "@/components/ui/Empty";
import { Glass } from "@/components/ui/Glass";
import { Tabs } from "@/components/ui/Tabs";
import { Radar } from "lucide-react";
import { JobRow } from "./JobRow";

type Filter = "all" | "live" | "clean" | "attention" | "closed";
const GROUP: Record<Exclude<Filter, "all">, readonly string[]> = {
  live: ["pending", "running"],
  clean: ["completed"],
  attention: ["paused_awaiting_reauth", "failed", "completed_with_gaps", "completed_unverified", "completed_against_archive"],
  closed: ["cancelled"],
};

export function Collections() {
  const { data, isLoading } = useRollup();
  const nav = useNavigate();
  const [f, setF] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const name = useMemo(() => new Map(data?.matters.map((m) => [m.id, m.name])), [data]);
  const rows = (data?.jobs ?? []).filter((j) => (f === "all" || GROUP[f].includes(j.status)) && (!q || `${name.get(j.matter_id)} ${j.id}`.toLowerCase().includes(q.toLowerCase())));
  const count = (k: Exclude<Filter, "all">) => data?.jobs.filter((j) => GROUP[k].includes(j.status)).length;

  return (
    <>
      <PageHeader eyebrow="Collection jobs · tenant-wide" title={<>Collect<em className="text-gradient">ions</em></>}
        subtitle="Each job splits into conversation × day units. A unit is clean only when collected equals expected; the job is clean only when every unit is."
        actions={<Button variant="primary" icon={<Plus className="size-4" />} onClick={() => nav("/collections/new")}>New collection</Button>} />
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <Tabs id="jobs" value={f} onChange={setF} options={[
          { value: "all", label: "All", count: data?.jobs.length },
          { value: "live", label: "Live", count: count("live") },
          { value: "attention", label: "Attention", count: count("attention") },
          { value: "clean", label: "Clean", count: count("clean") },
          { value: "closed", label: "Cancelled", count: count("closed") },
        ]} />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by matter or job id…" className="input !w-72" />
      </div>
      <Glass className="p-3" spotlight={false}>
        <div className="space-y-2">
          {isLoading && [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-20" />)}
          {rows.map((j, i) => <JobRow key={j.id} j={j} matterName={name.get(j.matter_id)} i={i} />)}
          {data && rows.length === 0 && <Empty icon={<Radar />} title="No jobs match" />}
        </div>
      </Glass>
    </>
  );
}
