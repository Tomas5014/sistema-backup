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
import { useAuth } from "../auth";

/** Nova tarefa — cliente e agente identificados; o vínculo com o
 * cliente é DERIVADO do agente no servidor, nunca do formulário. */
export default function TaskNewPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { operator } = useAuth();
  const agentId = params.get("agent") ?? "";
  const agentIdRef = useRef(agentId);
  // cada visita a ?agent= tem identidade própria — A→B→A não
  // ressuscita o POST da visita anterior
  const visitRef = useRef(0);
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
    agentIdRef.current = agentId;
    visitRef.current += 1;
    setPending(false); // um POST antigo não pode travar o novo contexto
    setSubmitError(null);
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
    const submittedAgent = agent.id;
    const visit = visitRef.current;
    setPending(true);
    setSubmitError(null);
    // conclusão de um cadastro de OUTRA visita não redireciona nem
    // toca o contexto atual — A→B→A mantém identidades distintas
    const stale = () =>
      !alive.current ||
      visitRef.current !== visit ||
      agentIdRef.current !== submittedAgent;
    try {
      const task = await createTask(submittedAgent, config);
      if (stale()) return;
      navigate(`/tasks/${task.id}`, { replace: true });
    } catch (err) {
      if (stale()) return;
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
      {operator?.role === "admin" ? (
        <TaskForm
          initial={emptyTask()}
          onSubmit={submit}
          pending={pending}
          error={submitError}
          submitLabel="Cadastrar tarefa"
        />
      ) : (
        <p role="alert" className="error">
          Perfil de consulta — o cadastro exige operador administrador.
        </p>
      )}
    </section>
  );
}
