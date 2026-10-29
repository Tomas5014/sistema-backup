import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { ClientAgentsPage, ClientsPage } from "./pages/ClientsPage";
import { AgentCredentialsPage } from "./pages/AgentCredentialsPage";
import { ExecutionsPage } from "./pages/ExecutionsPage";
import { ExecutionDetailPage } from "./pages/ExecutionDetailPage";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<Layout />}>
        <Route index element={<ExecutionsPage />} />
        <Route path="/clients" element={<ClientsPage />} />
        <Route path="/clients/:clientId" element={<ClientAgentsPage />} />
        <Route path="/agents/:agentId" element={<AgentCredentialsPage />} />
        <Route path="/executions" element={<ExecutionsPage />} />
        <Route path="/executions/:id" element={<ExecutionDetailPage />} />
      </Route>
      <Route path="*" element={<Navigate404 />} />
    </Routes>
  );
}

function Navigate404() {
  return (
    <main className="center">
      <p>Página não encontrada.</p>
    </main>
  );
}
