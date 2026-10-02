import { Route, Routes } from "react-router-dom";
import { Shell } from "@/components/layout/Shell";
import { Access } from "@/pages/Access";
import { ClientDetail } from "@/pages/ClientDetail";
import { Clients } from "@/pages/Clients";
import { Collections } from "@/pages/Collections";
import { Custody } from "@/pages/Custody";
import { Dashboard } from "@/pages/Dashboard";
import { Exports } from "@/pages/Exports";
import { JobDetail } from "@/pages/JobDetail";
import { Login } from "@/pages/Login";
import { MatterDetail } from "@/pages/MatterDetail";
import { NewJob } from "@/pages/NewJob";
import { NotFound } from "@/pages/NotFound";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Shell />}>
        <Route index element={<Dashboard />} />
        <Route path="clients" element={<Clients />} />
        <Route path="clients/:id" element={<ClientDetail />} />
        <Route path="matters/:id" element={<MatterDetail />} />
        <Route path="collections" element={<Collections />} />
        <Route path="collections/new" element={<NewJob />} />
        <Route path="jobs/:id" element={<JobDetail />} />
        <Route path="exports" element={<Exports />} />
        <Route path="custody" element={<Custody />} />
        <Route path="access" element={<Access />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
