/** Clientes: lista paginada + agentes do cliente selecionado. */

import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  listAgents,
  listClients,
  type AgentRow,
  type ClientRow,
} from "../api";
import { formatDate } from "../format";
import { EmptyState, ErrorState, Loading, Pagination } from "../components/common";

const PAGE = 50;

export function ClientsPage() {
  const [rows, setRows] = useState<ClientRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const generation = useRef(0);

  async function load(currentOffset: number) {
    const gen = ++generation.current;
    setError(null);
    try {
      const data = await listClients(PAGE, currentOffset);
      if (gen !== generation.current) return; // resposta antiga descartada
      setRows(data);
    } catch (err) {
      if (gen !== generation.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
      setRows(null);
    }
  }

  useEffect(() => {
    void load(offset);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset]);

  return (
    <main className="page">
      <h1>Clientes</h1>
      {error ? <ErrorState message={error} onRetry={() => void load(offset)} /> : null}
      {rows === null && !error ? <Loading /> : null}
      {rows !== null && rows.length === 0 && offset === 0 ? (
        <EmptyState message="Nenhum cliente cadastrado." />
      ) : null}
      {rows !== null && rows.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Nome</th>
              <th scope="col">Cadastro</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((client) => (
              <tr key={client.id}>
                <td>
                  <Link to={`/clients/${client.id}`}>{client.name}</Link>
                </td>
                <td>{formatDate(client.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {rows !== null ? (
        <Pagination offset={offset} limit={PAGE} count={rows.length} onChange={setOffset} />
      ) : null}
    </main>
  );
}

export function ClientAgentsPage() {
  const { clientId } = useParams<{ clientId: string }>();
  const [agents, setAgents] = useState<AgentRow[] | null>(null);
  const [clientName, setClientName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // respostas de um clientId antigo nunca substituem o atual
  const generation = useRef(0);

  async function load() {
    const gen = ++generation.current;
    const wanted = clientId;
    setError(null);
    try {
      const [clientRows, agentRows] = await Promise.all([
        listClients(500, 0),
        listAgents(wanted, 200, 0),
      ]);
      if (gen !== generation.current) return; // rota já mudou
      setClientName(
        clientRows.find((c) => c.id === wanted)?.name ?? null,
      );
      setAgents(agentRows);
    } catch (err) {
      if (gen !== generation.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
      setAgents(null);
    }
  }

  useEffect(() => {
    setAgents(null);
    setClientName(null); // outro cliente → dados do anterior somem
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId]);

  return (
    <main className="page">
      <p>
        <Link to="/clients">← Clientes</Link>
      </p>
      <h1>Agentes {clientName ? `de ${clientName}` : "do cliente"}</h1>
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {agents === null && !error ? <Loading /> : null}
      {agents !== null && agents.length === 0 ? (
        <EmptyState message="Nenhum agente neste cliente." />
      ) : null}
      {agents !== null && agents.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Nome</th>
              <th scope="col">Última comunicação</th>
              <th scope="col">Cadastro</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.id}>
                <td>{agent.name}</td>
                <td>{formatDate(agent.last_seen_at)}</td>
                <td>{formatDate(agent.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </main>
  );
}
