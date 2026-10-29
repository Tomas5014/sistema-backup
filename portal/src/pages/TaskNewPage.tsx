import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  ApiError,
  createTask,
  findAgent,
  findClient,
  type AgentRow,
} from "../api";
import TaskForm, { emptyTask } from "./TaskForm";
import { EmptyState } from "../components/common";

/** Nova tarefa — cliente e agente identificados; o vínculo com o
 * cliente é DERIVADO do agente no servidor, nunca do formulário. */
export default function TaskNewPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const agentId = params.get("agent") ?? "";
  const [agent, setAgent] = useState<AgentRow | null>(null);
  const [clientName, setClientName] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [resolved, setResolved] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const generation = useRef(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    const gen = ++generation.current;
    setResolved(false);
    setAgent(null);
    setLoadError(null);
    if (!agentId) {
      setLoadError("agente não informado na URL");
      setResolved(true);
      return;
    }
    void (async () => {
      try {
        const found = await findAgent(agentId);
        if (gen !== generation.current) return;
        setAgent(found);
        if (found) {
          const client = await findClient(found.client_id);
          if (gen !== generation.current) return;
          setClientName(client?.name ?? null);
        }
      } catch (err) {
        if (gen !== generation.current) return;
        setLoadError(
          err instanceof ApiError ? err.message : "Falha inesperada.",
        );
      }
      if (gen === generation.current) setResolved(true);
    })();
  }, [agentId]);

  async function submit(config: Record<string, unknown>) {
    if (pending || !agent) return;
    setPending(true);
    setSubmitError(null);
    try {
      const task = await createTask(agent.id, config);
      if (!alive.current) return;
      navigate(`/tasks/${task.id}`, { replace: true });
    } catch (err) {
      if (!alive.current) return;
      setSubmitError(
        err instanceof ApiError ? err.message : "Falha inesperada.",
      );
      setPending(false);
    }
  }

  if (!resolved) return <section><p>Carregando…</p></section>;
  if (loadError || !agent) {
    return (
      <section>
        <Link to="/tasks">← Tarefas</Link>
        <h1>Nova tarefa</h1>
        {loadError ? <p role="alert" className="error">{loadError}</p> : (
          <EmptyState message="Agente não encontrado." />
        )}
      </section>
    );
  }

  return (
    <section>
      <Link to="/tasks">← Tarefas</Link>
      <h1>Nova tarefa</h1>
      <p className="hint">
        Cliente: <strong>{clientName ?? agent.client_id}</strong> — Agente:{" "}
        <strong>{agent.name}</strong> (<code>{agent.id}</code>)
      </p>
      <p className="hint">
        A configuração será cadastrada na central — ainda <strong>não
        distribuída</strong> ao agente nesta etapa.
      </p>
      <TaskForm
        initial={emptyTask()}
        onSubmit={submit}
        pending={pending}
        error={submitError}
        submitLabel="Cadastrar tarefa"
      />
    </section>
  );
}
