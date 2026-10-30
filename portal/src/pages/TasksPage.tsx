import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ApiError,
  listClients,
  listTasks,
  type ClientRow,
  type TaskRow,
} from "../api";
import { EmptyState } from "../components/common";

/** Cadastro central de tarefas — NENHUMA é distribuída ao agente
 * nesta etapa (sem estados fictícios de "aplicada"/"agendada").
 * Clientes e tarefas são consultas INDEPENDENTES: cada uma tem sua
 * própria geração de invalidação e seu próprio retry. */
export default function TasksPage() {
  const [params, setParams] = useSearchParams();
  const [rows, setRows] = useState<TaskRow[] | null>(null);
  const [clients, setClients] = useState<ClientRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [clientsError, setClientsError] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  const [clientsRetry, setClientsRetry] = useState(0);
  const tasksGeneration = useRef(0);
  const clientsGeneration = useRef(0);

  const clientId = params.get("client_id") ?? "";
  const agentId = params.get("agent_id") ?? "";
  const archived = params.get("archived") ?? "false";
  const offset = Number(params.get("offset") ?? "0");

  // carga dos clientes: geração própria — a busca de tarefas não a
  // invalida; falha aparece explicitamente e tem retry próprio
  useEffect(() => {
    const gen = ++clientsGeneration.current;
    setClientsError(null);
    void listClients(500, 0)
      .then((c) => {
        if (gen === clientsGeneration.current) setClients(c);
      })
      .catch((err) => {
        if (gen !== clientsGeneration.current) return;
        setClientsError(
          err instanceof ApiError ? err.message : "Falha inesperada.",
        );
      });
  }, [clientsRetry]);

  useEffect(() => {
    const gen = ++tasksGeneration.current;
    setError(null);
    setRows(null);
    const filters: Record<string, string | boolean> = {};
    if (clientId) filters.client_id = clientId;
    if (agentId) filters.agent_id = agentId;
    if (archived !== "all") filters.archived = archived === "true";
    listTasks(filters, 51, offset)
      .then((page) => {
        if (gen !== tasksGeneration.current) return;
        setRows(page.slice(0, 50));
        setNextOffset(page.length > 50 ? offset + 50 : 0);
      })
      .catch((err) => {
        if (gen !== tasksGeneration.current) return;
        setError(err instanceof ApiError ? err.message : "Falha inesperada.");
        setRows([]);
      });
  }, [clientId, agentId, archived, offset, retry]);

  function patch(next: Record<string, string>) {
    setParams((prev) => {
      const p = new URLSearchParams(prev);
      for (const [key, value] of Object.entries(next)) {
        if (value) p.set(key, value);
        else p.delete(key);
      }
      return p;
    });
  }

  const clientName = (id: string) =>
    clients.find((c) => c.id === id)?.name ?? id.slice(0, 8);

  return (
    <section>
      <h1>Tarefas cadastradas</h1>
      <p className="hint">
        Configurações versionadas na central — ainda <strong>não
        distribuídas</strong> aos agentes (etapa posterior).
      </p>
      <div className="filters">
        <select
          aria-label="Filtrar por cliente"
          value={clientId}
          onChange={(e) => patch({ client_id: e.target.value, offset: "" })}
        >
          <option value="">Todos os clientes</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <input
          aria-label="Filtrar por agente"
          placeholder="UUID do agente"
          value={agentId}
          onChange={(e) => patch({ agent_id: e.target.value, offset: "" })}
        />
        <select
          aria-label="Arquivamento"
          value={archived}
          onChange={(e) => patch({ archived: e.target.value, offset: "" })}
        >
          <option value="false">Ativas</option>
          <option value="true">Arquivadas</option>
          <option value="all">Todas</option>
        </select>
      </div>
      {clientsError ? (
        <p role="alert" className="error">
          Não foi possível listar os clientes para o filtro: {clientsError}{" "}
          <button type="button" onClick={() => setClientsRetry((n) => n + 1)}>
            Tentar novamente
          </button>
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="error">
          {error}{" "}
          <button type="button" onClick={() => setRetry((n) => n + 1)}>
            Tentar novamente
          </button>
        </p>
      ) : null}
      {rows === null && !error ? <p>Carregando…</p> : null}
      {rows !== null && rows.length === 0 && !error ? (
        <EmptyState message="Nenhuma tarefa neste filtro." />
      ) : null}
      {rows && rows.length > 0 ? (
        <ul>
          {rows.map((t) => (
            <li key={t.id}>
              <Link to={`/tasks/${t.id}`}>{t.name}</Link>
              {" — "}
              {clientName(t.client_id)}
              {" — revisão "}
              {t.current_revision}
              {t.archived ? " — (arquivada)" : ""}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="pager">
        {offset > 0 ? (
          <button type="button" onClick={() =>
            patch({ offset: String(Math.max(0, offset - 50)) })
          }>← Anteriores</button>
        ) : null}
        {nextOffset > 0 ? (
          <button type="button" onClick={() =>
            patch({ offset: String(nextOffset) })
          }>Próximas →</button>
        ) : null}
      </div>
    </section>
  );
}
