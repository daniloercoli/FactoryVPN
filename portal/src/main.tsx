import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "./auth";
import Layout, { Allowed, GlobalAdmin } from "./Layout";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Profile from "./pages/Profile";
import Users, { UserDetail } from "./pages/Users";
import Groups, { GroupDetail } from "./pages/Groups";
import Permissions from "./pages/Permissions";
import Gateways from "./pages/Gateways";
import ProvisionGateway from "./pages/Provisioning";
import GatewayDetail from "./pages/GatewayDetail";
import Audit from "./pages/Audit";
import HistoryPage from "./pages/Diagnostics";
import TelemetryPage from "./pages/Telemetry";
import Backups from "./pages/Backups";
import ServerStatus from "./pages/ServerStatus";
import VPN, { VPNDetail } from "./pages/VPN";
import Clients from "./pages/Clients";
import { Loading, PageTitle } from "./ui";
import "./styles.css";

// La coda di fabbrica viene caricata soltanto quando un amministratore la apre.
const Enrollments = lazy(() => import("./pages/Enrollments"));
const EnrollmentDetail = lazy(() =>
  import("./pages/Enrollments").then((module) => ({
    default: module.EnrollmentDetail,
  })),
);

const client = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false, staleTime: 10000 },
    mutations: { retry: false },
  },
});
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter basename="/app/">
        <AuthProvider>
          <Routes>
            <Route path="login" element={<Login />} />
            <Route element={<Layout />}>
              <Route index element={<Overview />} />
              <Route path="backups" element={<Backups />} />
              <Route path="server" element={<ServerStatus />} />
              <Route path="profile" element={<Profile />} />
              <Route path="vpn" element={<VPN />} />
              <Route path="vpn/:id" element={<VPNDetail />} />
              <Route
                path="gateways/new"
                element={
                  <Allowed permission="gateways.provision">
                    <ProvisionGateway />
                  </Allowed>
                }
              />
              <Route path="gateways/:id" element={<GatewayDetail />} />
              <Route
                path="enrollments"
                element={
                  <GlobalAdmin>
                    <Suspense fallback={<Loading />}>
                      <Enrollments />
                    </Suspense>
                  </GlobalAdmin>
                }
              />
              <Route
                path="enrollments/:id"
                element={
                  <GlobalAdmin>
                    <Suspense fallback={<Loading />}>
                      <EnrollmentDetail />
                    </Suspense>
                  </GlobalAdmin>
                }
              />
              <Route path="vpn-history" element={<HistoryPage />} />
              <Route
                path="telemetry"
                element={
                  <Allowed permission="telemetry.read">
                    <TelemetryPage />
                  </Allowed>
                }
              />
              <Route
                path="vpn-supervision"
                element={<Navigate to="/clients" replace />}
              />
              <Route
                path="clients"
                element={
                  <Allowed permission="sessions.manage">
                    <Clients />
                  </Allowed>
                }
              />
              <Route
                path="users"
                element={
                  <Allowed permission="users.manage">
                    <Users />
                  </Allowed>
                }
              />
              <Route
                path="users/:id"
                element={
                  <Allowed permission="users.manage">
                    <UserDetail />
                  </Allowed>
                }
              />
              <Route path="groups" element={<Groups />} />
              <Route
                path="groups/:id"
                element={
                  <Allowed permission="groups.manage">
                    <GroupDetail />
                  </Allowed>
                }
              />
              <Route path="permissions" element={<Permissions />} />
              <Route
                path="gateways"
                element={
                  <Allowed permission="gateways.manage">
                    <Gateways />
                  </Allowed>
                }
              />
              <Route
                path="audit"
                element={
                  <Allowed permission="audit.read">
                    <Audit />
                  </Allowed>
                }
              />
              <Route
                path="*"
                element={
                  <>
                    <PageTitle
                      title="Pagina non trovata"
                      description="La pagina richiesta non è disponibile."
                    />
                    <Link className="button" to="/">
                      Torna alla panoramica
                    </Link>
                  </>
                }
              />
            </Route>
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
