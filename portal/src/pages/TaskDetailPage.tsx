import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  archiveTask,
  createRevision,
  exportRevisionUrl,
  findAgent,
  findClient,
  getTask,
  listRevisions,
  type RevisionSummary,
  type TaskDetail,
} from "../api";
import { useAuth } from "../auth";
import { EmptyState } from "../components/common";
import TaskForm, { formFromConfig } from "./TaskForm";

/** Detalhe + edição com concorrência otimista + histórico + exportar. */
export default function TaskDetailPage() {
  const { taskId } = useParams<{ taskId: string }>();
  const { operator } = useAuth();
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [agentName, setAgentName] = useState<string | null>(null);
  const [clientName, setClientName] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<RevisionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const generation = useRef(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function load() {
    const gen = ++generation.current;
    const wanted = taskId;
    setError(null);
    setResolved(false);
    try {
      const [detail, revs] = await Promise.all([
        getTask(wanted as string),
        listRevisions(wanted as string),
      ]);
      if (gen !== generation.current) return;
      setTask(detail);
      setRevisions(revs);
      const agent = await findAgent(detail.agent_id);
      if (gen !== generation.current) return;
      setAgentName(agent?.name ?? null);
      if (agent) {
        const client = await findClient(agent.client_id);
        if (gen !== generation.current) return;
        setClientName(client?.name ?? null);
      }
      setResolved(true);
    } catch (err) {
      if (gen !== generation.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
      setTask(null);
      setResolved(true);
    }
  }

  useEffect(() => {
    setEditing(false);
    setEditError(null);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  async function saveEdit(config: Record<string, unknown>) {
    if (pending || !task) return;
    setPending(true);
    setEditError(null);
    try {
      await createRevision(
        task.id,
        task.current_revision,
        config,
      );
      if (!alive.current) return;
      setEditing(false);
      await load();
    } catch (err) {
      if (!alive.current) return;
      // 409 preserva os valores digitados — explicamos a revisão nova
      setEditError(
        err instanceof ApiError ? err.message : "Falha inesperada.",
      );
    } finally {
      if (alive.current) setPending(false);
    }
  }

  async function confirmArchive() {
    if (pending || !task) return;
    setPending(true);
    setError(null);
    try {
      await archiveTask(task.id);
      if (!alive.current) return;
      setArchiveOpen(false);
      await load();
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      if (alive.current) setPending(false);
    }
  }

  const admin = operator?.role === "admin";

  return (
    <section>
      <Link to="/tasks">← Tarefas</Link>
      <h1>Tarefa</h1>
      {error ? <p role="alert" className="error">{error}</p> : null}
      {!resolved && !error ? <p>Carregando…</p> : null}
      {resolved && !task && !error ? (
        <EmptyState message="Tarefa não encontrada." />
      ) : null}
      {task ? (
        <>
          <dl>
            <dt>Nome</dt>
            <dd>{task.name}</dd>
            <dt>Cliente</dt>
            <dd>{clientName ?? task.client_id}</dd>
            <dt>Agente</dt>
            <dd>{agentName ?? task.agent_id} (<code>{task.agent_id}</code>)</dd>
            <dt>Revisão atual</dt>
            <dd>{task.current_revision}</dd>
            <dt>Situação</dt>
            <dd>
              {task.archived
                ? "Arquivada na central"
                : "Cadastrada na central — ainda não distribuída ao agente"}
            </dd>
          </dl>

          {admin && !task.archived && !editing ? (
            <div className="row">
              <button type="button" onClick={() => setEditing(true)}>
                Editar configuração
              </button>
              <button type="button" onClick={() => setArchiveOpen(true)}>
                Arquivar tarefa
              </button>
            </div>
          ) : null}

          {archiveOpen ? (
            <fieldset className="box">
              <legend>Confirmar arquivamento</legend>
              <p>
                Arquivar tira a tarefa da lista de ativas e bloqueia novas
                revisões. As revisões já gravadas são preservadas e o
                cadastro NÃO é apagado. Arquivar na central{" "}
                <strong>não interrompe</strong> um backup em execução nem
                desativa agendamentos já instalados no agente.
              </p>
              <button type="button" disabled={pending} onClick={confirmArchive}>
                {pending ? "Arquivando…" : "Confirmar arquivamento"}
              </button>
              <button type="button" onClick={() => setArchiveOpen(false)}>
                Cancelar
              </button>
            </fieldset>
          ) : null}

          {editing ? (
            <>
              <h2>Editar — cria uma nova revisão</h2>
              <TaskForm
                initial={formFromConfig(task.revision.config)}
                onSubmit={saveEdit}
                pending={pending}
                error={editError}
                submitLabel="Salvar nova revisão"
              />
            </>
          ) : null}

          <h2>Revisões</h2>
          {revisions === null ? <p>Carregando…</p> : null}
          {revisions !== null && revisions.length === 0 ? (
            <EmptyState message="Sem revisões." />
          ) : null}
          {revisions && revisions.length > 0 ? (
            <ul>
              {revisions.map((r) => (
                <li key={r.revision}>
                  Revisão {r.revision}
                  {r.current ? " (atual)" : ""}
                  {" — "}
                  {r.author_kind === "operator"
                    ? `operador ${r.author_ref}`
                    : "automação"}
                  {" — "}
                  {new Date(r.created_at).toLocaleString("pt-BR")}
                  {" — "}
                  <a
                    href={exportRevisionUrl(task.id, r.revision)}
                    download={`tarefa-rev${r.revision}.json`}
                  >
                    baixar tarefa.json
                  </a>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
