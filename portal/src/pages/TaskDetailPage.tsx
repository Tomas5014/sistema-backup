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
  publishTask,
  suspendTask,
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
  const [publishOpen, setPublishOpen] = useState(false);
  const [suspendOpen, setSuspendOpen] = useState(false);
  const generation = useRef(0);
  const taskIdRef = useRef(taskId); // rota vigente — distingue callbacks atrasados
  // identidade da VISITA à rota: A→B→A são visitas diferentes — um
  // callback antigo não volta a valer quando a rota "retorna"
  const visitRef = useRef(0);
  // identidade da MUTAÇÃO em voo: só a mais recente libera `pending`
  const mutationRef = useRef(0);
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
    // metadados pertencem ao contexto — nunca "emprestados" da rota
    // anterior enquanto o novo carrega
    setAgentName(null);
    setClientName(null);
    setRevisions(null);
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
    taskIdRef.current = taskId;
    visitRef.current += 1; // cada visita à rota é uma identidade nova
    setEditing(false);
    setEditError(null);
    setArchiveOpen(false);
    setPublishOpen(false);
    setSuspendOpen(false);
    setPending(false); // operação pendente da rota anterior não trava a nova
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  // callbacks de escrita verificam: unmount, geração de carregamento
  // (qualquer load novo invalida), rota vigente E visita — um
  // callback de A não volta a valer após A→B→A
  function staleSave(gen: number, id: string, visit: number) {
    return (
      !alive.current ||
      gen !== generation.current ||
      taskIdRef.current !== id ||
      visitRef.current !== visit
    );
  }

  // `pending` pertence à mutação mais recente — um finally antigo
  // nunca libera uma operação nova; a mesma operação libera mesmo
  // depois do load() que ela própria disparou
  function releasePending(op: number) {
    if (alive.current && mutationRef.current === op) {
      setPending(false);
    }
  }

  async function saveEdit(config: Record<string, unknown>) {
    if (pending || !task) return;
    const gen = generation.current;
    const visit = visitRef.current;
    const op = ++mutationRef.current;
    const targetId = task.id;
    setPending(true);
    setEditError(null);
    try {
      await createRevision(targetId, task.current_revision, config);
      if (staleSave(gen, targetId, visit)) return;
      setEditing(false);
      await load();
    } catch (err) {
      if (staleSave(gen, targetId, visit)) return;
      // 409 preserva os valores digitados — explicamos a revisão nova
      setEditError(
        err instanceof ApiError ? err.message : "Falha inesperada.",
      );
    } finally {
      releasePending(op);
    }
  }

  async function confirmArchive() {
    if (pending || !task) return;
    const gen = generation.current;
    const visit = visitRef.current;
    const op = ++mutationRef.current;
    const targetId = task.id;
    setPending(true);
    setError(null);
    try {
      const updated = await archiveTask(targetId);
      if (staleSave(gen, targetId, visit)) return;
      setArchiveOpen(false);
      setTask((t) => (t?.id === targetId ? { ...t, ...updated } : t));
    } catch (err) {
      if (staleSave(gen, targetId, visit)) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      releasePending(op);
    }
  }

  async function confirmPublish() {
    if (pending || !task || !task.distribution) return;
    const gen = generation.current;
    const visit = visitRef.current;
    const op = ++mutationRef.current;
    const targetId = task.id;
    const expected = task.distribution.generation;
    const revision = task.current_revision;
    setPending(true);
    setError(null);
    try {
      const updated = await publishTask(targetId, revision, expected);
      if (staleSave(gen, targetId, visit)) return;
      setPublishOpen(false);
      setTask((t) => (t?.id === targetId ? { ...t, ...updated } : t));
    } catch (err) {
      if (staleSave(gen, targetId, visit)) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      releasePending(op);
    }
  }

  async function confirmSuspend() {
    if (pending || !task || !task.distribution) return;
    const gen = generation.current;
    const visit = visitRef.current;
    const op = ++mutationRef.current;
    const targetId = task.id;
    const expected = task.distribution.generation;
    setPending(true);
    setError(null);
    try {
      const updated = await suspendTask(targetId, expected);
      if (staleSave(gen, targetId, visit)) return;
      setSuspendOpen(false);
      setTask((t) => (t?.id === targetId ? { ...t, ...updated } : t));
    } catch (err) {
      if (staleSave(gen, targetId, visit)) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      releasePending(op);
    }
  }

  const admin = operator?.role === "admin";
  const shownTask = task?.id === taskId ? task : null;
  const dist = shownTask?.distribution;
  // aplicada = confirmação do agente para a geração vigente
  const appliedRevision =
    dist?.confirmed?.status === "applied" &&
    dist.confirmed.generation === dist.generation
      ? dist.confirmed.applied_revision
      : null;
  const awaitingApply =
    dist !== undefined &&
    dist !== null &&
    dist.desired_action === "apply" &&
    (dist.confirmed === null ||
      dist.confirmed.generation !== dist.generation);

  return (
    <section>
      <Link to="/tasks">← Tarefas</Link>
      <h1>Tarefa</h1>
      {error ? <p role="alert" className="error">{error}</p> : null}
      {!resolved && !error ? <p>Carregando…</p> : null}
      {resolved && !task && !error ? (
        <EmptyState message="Tarefa não encontrada." />
      ) : null}
      {shownTask ? (
        <>
          <dl>
            <dt>Nome</dt>
            <dd>{shownTask.name}</dd>
            <dt>Cliente</dt>
            <dd>{clientName ?? shownTask.client_id}</dd>
            <dt>Agente</dt>
            <dd>{agentName ?? shownTask.agent_id} (<code>{shownTask.agent_id}</code>)</dd>
            <dt>Revisão atual</dt>
            <dd>{shownTask.current_revision}</dd>
            <dt>Situação</dt>
            <dd>
              {shownTask.archived
                ? "Arquivada na central"
                : "Cadastrada na central — ainda não distribuída ao agente"}
            </dd>
          </dl>

          {admin && !shownTask.archived && !editing ? (
            <div className="row">
              <button type="button" onClick={() => setEditing(true)}>
                Editar configuração
              </button>
              <button type="button" onClick={() => setArchiveOpen(true)}>
                Arquivar tarefa
              </button>
            </div>
          ) : null}

          <h2>Distribuição gerenciada</h2>
          {dist ? (
            <>
              <dl>
                <dt>Revisão cadastrada</dt>
                <dd>{shownTask.current_revision}</dd>
                <dt>Revisão publicada</dt>
                <dd>{dist.published_revision ?? "nenhuma"}</dd>
                <dt>Revisão aplicada pelo agente</dt>
                <dd>{appliedRevision ?? "—"}</dd>
                <dt>Geração</dt>
                <dd>{dist.generation}</dd>
                <dt>Ação desejada</dt>
                <dd>
                  {dist.desired_action === "apply"
                    ? "agenda instalada"
                    : dist.desired_action === "suspend"
                      ? "agenda suspensa"
                      : "não publicada"}
                </dd>
                <dt>Última confirmação</dt>
                <dd>
                  {dist.confirmed ? (
                    <>
                      {dist.confirmed.status === "applied"
                        ? "aplicada"
                        : "falha"}{" "}
                      (geração {dist.confirmed.generation})
                      {dist.confirmed.received_at
                        ? ` — ${new Date(dist.confirmed.received_at).toLocaleString("pt-BR")}`
                        : ""}
                      {dist.confirmed.status === "failed" &&
                      dist.confirmed.detail
                        ? ` — ${dist.confirmed.detail}`
                        : ""}
                    </>
                  ) : (
                    "nenhuma confirmação ainda"
                  )}
                </dd>
              </dl>
              {awaitingApply ? (
                <p>
                  Aguardando a aplicação pelo agente — a publicação foi
                  registrada na central; a agenda só é efetiva quando o
                  agente confirmar.
                </p>
              ) : null}
              {admin && (!shownTask.archived || dist.published_revision !== null) ? (
                <div className="row">
                  {!shownTask.archived ? (
                    <button type="button" onClick={() => setPublishOpen(true)}>
                      Publicar revisão {shownTask.current_revision}
                    </button>
                  ) : null}
                  {dist.published_revision !== null ? (
                    <button type="button" onClick={() => setSuspendOpen(true)}>
                      Suspender agendamento
                    </button>
                  ) : null}
                </div>
              ) : null}
            </>
          ) : null}

          {publishOpen && dist ? (
            <fieldset className="box">
              <legend>Confirmar publicação</legend>
              <p>
                A revisão {shownTask.current_revision} será publicada para
                o agente (geração {dist.generation + 1}). O agente instala
                a configuração e a agenda na próxima consulta —
                publicar <strong>não executa</strong> backup imediato e
                não interrompe uma execução em andamento. Senhas e
                credenciais permanecem no agente.
              </p>
              <button type="button" disabled={pending} onClick={confirmPublish}>
                {pending ? "Publicando…" : "Confirmar publicação"}
              </button>
              <button type="button" onClick={() => setPublishOpen(false)}>
                Cancelar
              </button>
            </fieldset>
          ) : null}

          {suspendOpen && dist ? (
            <fieldset className="box">
              <legend>Confirmar suspensão</legend>
              <p>
                O agendamento gerenciado será suspenso na próxima consulta
                do agente (geração {dist.generation + 1}): o timer da
                tarefa é desabilitado, mas a configuração, os snapshots,
                o histórico e um backup em execução{" "}
                <strong>não são afetados</strong>. Suspensão não apaga a
                tarefa — uma nova publicação a reativa.
              </p>
              <button type="button" disabled={pending} onClick={confirmSuspend}>
                {pending ? "Suspendendo…" : "Confirmar suspensão"}
              </button>
              <button type="button" onClick={() => setSuspendOpen(false)}>
                Cancelar
              </button>
            </fieldset>
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
                initial={formFromConfig(shownTask.revision.config)}
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
                    href={exportRevisionUrl(shownTask.id, r.revision)}
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
