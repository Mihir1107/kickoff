import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { api } from ".";
import type { Page } from "./types";

/** Query keys mirror the URL so invalidation reads like the API. */
export const qk = {
  me: ["me"] as const,
  session: ["session"] as const,
  myPermissions: ["me", "permissions"] as const,
  roles: ["roles"] as const,
  directory: (c: string) => ["connections", c, "directory"] as const,
  groups: ["groups"] as const,
  clients: ["clients"] as const,
  client: (id: string) => ["clients", id] as const,
  matters: (c: string) => ["clients", c, "matters"] as const,
  matter: (id: string) => ["matters", id] as const,
  workspaces: (m: string) => ["matters", m, "workspaces"] as const,
  clientConnections: (c: string) => ["clients", c, "connections"] as const,
  matterConnections: (m: string) => ["matters", m, "connections"] as const,
  jobs: (m: string) => ["matters", m, "jobs"] as const,
  job: (id: string) => ["jobs", id] as const,
  units: (id: string) => ["jobs", id, "units"] as const,
  recon: (id: string) => ["jobs", id, "reconciliation"] as const,
  chain: (id: string) => ["jobs", id, "custody", "events"] as const,
  exports: (c: string) => ["clients", c, "exports"] as const,
  export: (id: string) => ["exports", id] as const,
  principals: ["principals"] as const,
  assignments: ["role-assignments"] as const,
};

/** Drains every cursor page (lists here are small; use useInfiniteList for long ones). */
async function all<T>(fetchPage: (cursor: string | null) => Promise<Page<T>>): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | null = null;
  do {
    const p: Page<T> = await fetchPage(cursor);
    out.push(...p.items);
    cursor = p.next_cursor;
  } while (cursor);
  return out;
}

const ACTIVE = new Set(["pending", "running"]);

export const useSession = () => useQuery({ queryKey: qk.session, queryFn: () => api.session(), staleTime: 60_000, retry: false });
export const useMyPermissions = () => useQuery({ queryKey: qk.myPermissions, queryFn: () => api.myPermissions(), staleTime: 60_000 });
export const useRoleMatrix = () => useQuery({ queryKey: qk.roles, queryFn: () => api.roleMatrix(), staleTime: Infinity });
/** Optional route (no backend yet): `data` stays undefined and `available` is false in http mode. */
export const useDirectory = (connectionId: string | undefined) => ({
  available: !!api.directory,
  ...useQuery({ queryKey: qk.directory(connectionId ?? ""), queryFn: () => api.directory!(connectionId!), enabled: !!connectionId && !!api.directory }),
});
export const useGroups = () => useQuery({ queryKey: qk.groups, queryFn: () => api.listGroups!(), enabled: !!api.listGroups });
export const useMe = () => useQuery({ queryKey: qk.me, queryFn: () => api.me(), staleTime: Infinity });
export const useClients = () => useQuery({ queryKey: qk.clients, queryFn: () => all((cursor) => api.listClients({ cursor, limit: 200 })) });
export const useClient = (id: string) => useQuery({ queryKey: qk.client(id), queryFn: () => api.getClient(id) });
export const useMatters = (c: string | undefined) =>
  useQuery({ queryKey: qk.matters(c ?? ""), queryFn: () => all((cursor) => api.listMatters(c!, { cursor, limit: 200 })), enabled: !!c });
export const useMatter = (id: string) => useQuery({ queryKey: qk.matter(id), queryFn: () => api.getMatter(id) });
export const useWorkspaces = (m: string) => useQuery({ queryKey: qk.workspaces(m), queryFn: () => all((cursor) => api.listWorkspaces(m, { cursor, limit: 200 })) });
export const useClientConnections = (c: string | undefined) =>
  useQuery({ queryKey: qk.clientConnections(c ?? ""), queryFn: () => all((cursor) => api.listClientConnections(c!, { cursor, limit: 200 })), enabled: !!c });
export const useMatterConnections = (m: string) => useQuery({ queryKey: qk.matterConnections(m), queryFn: () => all((cursor) => api.listMatterConnections(m, { cursor, limit: 200 })) });
export const useJobs = (m: string | undefined) =>
  useQuery({
    queryKey: qk.jobs(m ?? ""),
    queryFn: () => all((cursor) => api.listJobs(m!, { cursor, limit: 200 })),
    enabled: !!m,
    refetchInterval: (q) => (q.state.data?.some((j) => ACTIVE.has(j.status)) ? 3000 : false),
  });
/** Polls while the job is live. A websocket/SSE feed would replace this (API gap). */
export const useJob = (id: string) =>
  useQuery({ queryKey: qk.job(id), queryFn: () => api.getJob(id), refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 2000 : false) });
export const useUnits = (id: string, live: boolean) =>
  useQuery({ queryKey: qk.units(id), queryFn: () => all((cursor) => api.listUnits(id, { cursor, limit: 200 })), refetchInterval: live ? 2500 : false });
export const useReconciliation = (id: string, live: boolean) =>
  useQuery({ queryKey: qk.recon(id), queryFn: () => api.reconciliation(id), refetchInterval: live ? 4000 : false });
export const useCustodyEvents = (id: string) =>
  useQuery({ queryKey: qk.chain(id), queryFn: () => (api.custodyEvents ? api.custodyEvents(id) : Promise.resolve([])) });
export const useExports = (c: string | undefined) =>
  useQuery({
    queryKey: qk.exports(c ?? ""),
    queryFn: () => all((cursor) => api.listExports(c!, { cursor, limit: 200 })),
    enabled: !!c,
    refetchInterval: (q) => (q.state.data?.some((x) => x.status === "locking" || x.status === "validating") ? 1500 : false),
  });
export const usePrincipals = () => useQuery({ queryKey: qk.principals, queryFn: () => all((cursor) => api.listPrincipals({ cursor, limit: 200 })) });
export const useAssignments = () => useQuery({ queryKey: qk.assignments, queryFn: () => all((cursor) => api.listAssignments({ cursor, limit: 200 })) });

/** A mutation that invalidates the given keys on success. */
export function useAction<A, R>(fn: (a: A) => Promise<R>, invalidate: (a: A, r: R) => QueryKey[]) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (r, a) => Promise.all(invalidate(a, r).map((queryKey) => qc.invalidateQueries({ queryKey }))),
  });
}


/**
 * Tenant-wide rollup for the dashboard: clients → matters → jobs. API GAP: there is no tenant-wide
 * jobs endpoint, so this walks the hierarchy (N+1 requests). Replace with one route when it exists.
 */
export function useRollup() {
  return useQuery({
    queryKey: ["rollup"],
    queryFn: async () => {
      const clients = await all((cursor) => api.listClients({ cursor, limit: 200 }));
      const matters = (await Promise.all(clients.map((c) => all((cursor) => api.listMatters(c.id, { cursor, limit: 200 }))))).flat();
      const jobs = (await Promise.all(matters.map((m) => all((cursor) => api.listJobs(m.id, { cursor, limit: 200 }))))).flat();
      const exports = (await Promise.all(clients.map((c) => all((cursor) => api.listExports(c.id, { cursor, limit: 200 }))))).flat();
      const connections = (await Promise.all(clients.map((c) => all((cursor) => api.listClientConnections(c.id, { cursor, limit: 200 }))))).flat();
      jobs.sort((a, b) => b.created_at.localeCompare(a.created_at));
      return { clients, matters, jobs, exports, connections };
    },
    refetchInterval: 4000,
  });
}
