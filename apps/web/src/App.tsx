import { useEffect } from "react";
import { Route, Routes, useNavigate } from "react-router-dom";
import { onUnauthenticated } from "@/api";
import { ReturnNotices } from "@/components/layout/ReturnNotices";
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
  const navigate = useNavigate();
  // No session → the sign-in page by client-side navigation, so app state (e.g. a sign-in error notice
  // read from ?auth_error=) survives; a full reload would drop it.
  useEffect(() => {
    onUnauthenticated(() => {
      if (location.pathname !== "/login") navigate(`/login?from=${encodeURIComponent(location.pathname + location.search)}`, { replace: true });
    });
  }, [navigate]);
  return (
    <>
    <ReturnNotices />
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
    </>
  );
}
