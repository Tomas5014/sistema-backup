/** Execuções: filtros em URL (voltar preserva), paginação limit/offset,
 * descarte de respostas antigas quando os filtros mudam rápido. */

import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ApiError,
  listAgents,
  listClients,
  listExecutions,
  type AgentRow,
  type ClientRow,
  type ExecutionSummary,
} from "../api";
import { formatDate, statusLabel } from "../format";
import { EmptyState, ErrorState, Loading, Pagination, StatusBadge } from "../components/common";

const PAGE = 25;
const STATUSES = ["success", "partial", "failed", "interrupted"];

/** datetime-local → ISO com timezone (a API exige tz explícita). */
function toIso(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function readFilters(params: URLSearchParams) {
  const offset = Number.parseInt(params.get("offset") ?? "0", 10);
  return {
    client_id: params.get("client_id") ?? "",
    agent_id: params.get("agent_id") ?? "",
    task_id: params.get("task_id") ?? "",
    status: params.get("status") ?? "",
    since: params.get("since") ?? "", // datetime-local
    until: params.get("until") ?? "",
    offset: Number.isFinite(offset) && offset >= 0 ? offset : 0,
  };
}

export function ExecutionsPage() {
  const [params, setParams] = useSearchParams();
  const filters = readFilters(params);
  const [rows, setRows] = useState<ExecutionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clients, setClients] = useState<ClientRow[]>([]);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const generation = useRef(0);

  // listas de apoio para os filtros — tolerante a falha (filtro segue textual)
  useEffect(() => {
    listClients(500, 0).then(setClients).catch(() => {});
    listAgents(undefined, 500, 0).then(setAgents).catch(() => {});
  }, []);

  async function load() {
    const gen = ++generation.current;
    setError(null);
    try {
      const data = await listExecutions({
        client_id: filters.client_id || undefined,
        agent_id: filters.agent_id || undefined,
        task_id: filters.task_id || undefined,
        status: filters.status || undefined,
        since: toIso(filters.since),
        until: toIso(filters.until),
        limit: PAGE,
        offset: filters.offset,
      });
      if (gen !== generation.current) return; // busca mais nova venceu
      setRows(data);
    } catch (err) {
      if (gen !== generation.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
      setRows(null);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  function applyFilters(next: Partial<ReturnType<typeof readFilters>>) {
    const merged = { ...filters, ...next, offset: next.offset ?? 0 };
    const next_ = new URLSearchParams();
    for (const [key, value] of Object.entries(merged)) {
      if (value !== "" && value !== 0) next_.set(key, String(value));
    }
    setParams(next_);
  }

  const clientName = (id: string) =>
    clients.find((c) => c.id === id)?.name ?? id;
  const agentName = (id: string) =>
    agents.find((a) => a.id === id)?.name ?? id;

  return (
    <main className="page">
      <h1>Execuções</h1>

      <form
        className="filters"
        onSubmit={(e) => e.preventDefault()}
        aria-label="Filtros de execuções"
      >
        <label>
          Cliente
          <select
            value={filters.client_id}
            onChange={(e) => applyFilters({ client_id: e.target.value })}
          >
            <option value="">Todos</option>
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Agente
          <select
            value={filters.agent_id}
            onChange={(e) => applyFilters({ agent_id: e.target.value })}
          >
            <option value="">Todos</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Tarefa
          <input
            value={filters.task_id}
            onChange={(e) => applyFilters({ task_id: e.target.value })}
            placeholder="id da tarefa"
          />
        </label>
        <label>
          Status
          <select
            value={filters.status}
            onChange={(e) => applyFilters({ status: e.target.value })}
          >
            <option value="">Todos</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {statusLabel(s)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Início a partir de
          <input
            type="datetime-local"
            value={filters.since}
            onChange={(e) => applyFilters({ since: e.target.value })}
          />
        </label>
        <label>
          Início até
          <input
            type="datetime-local"
            value={filters.until}
            onChange={(e) => applyFilters({ until: e.target.value })}
          />
        </label>
      </form>

      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {rows === null && !error ? <Loading /> : null}
      {rows !== null && rows.length === 0 ? (
        <EmptyState message="Nenhuma execução encontrada para os filtros." />
      ) : null}
      {rows !== null && rows.length > 0 ? (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Início</th>
                <th scope="col">Cliente</th>
                <th scope="col">Agente</th>
                <th scope="col">Tarefa</th>
                <th scope="col">Status</th>
                <th scope="col">Recebida</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{formatDate(row.started_at_utc)}</td>
                  <td>{clientName(row.client_id)}</td>
                  <td>{agentName(row.agent_id)}</td>
                  <td>{row.task_id}</td>
                  <td>
                    <StatusBadge status={row.status} />
                  </td>
                  <td>{formatDate(row.received_at)}</td>
                  <td>
                    <Link
                      to={`/executions/${row.id}`}
                      state={{ back: `?${params.toString()}` }}
                    >
                      Detalhes
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {rows !== null ? (
        <Pagination
          offset={filters.offset}
          limit={PAGE}
          count={rows.length}
          onChange={(offset) => applyFilters({ offset })}
        />
      ) : null}
    </main>
  );
}
