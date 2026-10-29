import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { ClientAgentsPage, ClientsPage } from "./pages/ClientsPage";
import { AgentCredentialsPage } from "./pages/AgentCredentialsPage";
import { ExecutionsPage } from "./pages/ExecutionsPage";
import TasksPage from "./pages/TasksPage";
import TaskNewPage from "./pages/TaskNewPage";
import TaskDetailPage from "./pages/TaskDetailPage";
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
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/tasks/new" element={<TaskNewPage />} />
        <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
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
