/** Clientes: lista paginada + agentes do cliente selecionado. */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  createAgent,
  createClient,
  listClientAgents,
  listClients,
  type AgentRow,
  type ClientRow,
} from "../api";
import { useAuth } from "../auth";
import { formatDate } from "../format";
import { EmptyState, ErrorState, Loading, Pagination } from "../components/common";

const PAGE = 50;

export function ClientsPage() {
  const { operator } = useAuth();
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
      {operator?.role === "admin" ? (
        <NewClientForm onCreated={() => void load(offset)} />
      ) : null}
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

function NewClientForm({ onCreated }: { onCreated: (c: ClientRow) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ClientRow | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    if (pending) return; // bloqueio de envio duplicado
    setPending(true);
    setError(null);
    try {
      const client = await createClient(name.trim());
      if (!alive.current) return;
      setCreated(client);
      setName("");
      onCreated(client);
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      if (alive.current) setPending(false);
    }
  }

  if (!open && created === null) {
    return (
      <p>
        <button type="button" className="btn btn-primary" onClick={() => setOpen(true)}>
          Novo cliente
        </button>
      </p>
    );
  }
  if (created !== null) {
    return (
      <div className="success-state" role="status">
        <p>
          Cliente <strong>{created.name}</strong> criado.{" "}
          <Link to={`/clients/${created.id}`}>Abrir o cliente →</Link>{" "}
          <button
            type="button"
            className="btn"
            onClick={() => {
              setCreated(null);
              setOpen(false);
            }}
          >
            Fechar
          </button>
        </p>
      </div>
    );
  }
  return (
    <form className="form-inline" onSubmit={submit}>
      <label htmlFor="new-client-name">Nome do cliente</label>
      <input
        id="new-client-name"
        value={name}
        onChange={(ev) => setName(ev.target.value)}
        required
        autoFocus
        disabled={pending}
      />
      <button type="submit" className="btn btn-primary" disabled={pending || !name.trim()}>
        {pending ? "Cadastrando…" : "Cadastrar"}
      </button>
      <button type="button" className="btn" disabled={pending} onClick={() => setOpen(false)}>
        Cancelar
      </button>
      {error ? <p className="error-state" role="alert">{error}</p> : null}
    </form>
  );
}

export function ClientAgentsPage() {
  const { clientId } = useParams<{ clientId: string }>();
  const { operator } = useAuth();
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
        listClientAgents(wanted as string, 200, 0),
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
      {operator?.role === "admin" && clientId ? (
        <NewAgentForm key={clientId} clientId={clientId} clientName={clientName} onCreated={() => void load()} />
      ) : null}
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
                <td>
                  <Link to={`/agents/${agent.id}`}>{agent.name}</Link>{" "}
                  <Link to={`/tasks?agent_id=${agent.id}`}>tarefas</Link>
                  {operator?.role === "admin" ? (
                    <>
                      {" "}
                      <Link to={`/tasks/new?agent=${agent.id}`}>+ tarefa</Link>
                    </>
                  ) : null}
                </td>
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

function NewAgentForm({
  clientId,
  clientName,
  onCreated,
}: {
  clientId: string;
  clientName: string | null;
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<AgentRow | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // o componente é remontado por key={clientId} ao trocar de cliente —
  // alive=false faz a resposta pendente de A ser descartada em B

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    if (pending) return; // bloqueio de envio duplicado
    setPending(true);
    setError(null);
    try {
      const agent = await createAgent(clientId, name.trim());
      if (!alive.current || agent.client_id !== clientId) return;
      setCreated(agent);
      setName("");
      onCreated();
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      if (alive.current) setPending(false);
    }
  }

  if (!open && created === null) {
    return (
      <p>
        <button type="button" className="btn btn-primary" onClick={() => setOpen(true)}>
          Novo agente
        </button>
      </p>
    );
  }
  if (created !== null) {
    return (
      <div className="success-state" role="status">
        <p>
          Agente <strong>{created.name}</strong> vinculado.{" "}
          <Link to={`/agents/${created.id}`}>Credenciais do agente →</Link>{" "}
          <button type="button" className="btn" onClick={() => setCreated(null)}>
            Fechar
          </button>
        </p>
      </div>
    );
  }
  return (
    <form className="form-inline" onSubmit={submit}>
      <p className="form-context">
        Novo agente para o cliente <strong>{clientName ?? clientId}</strong>
      </p>
      <label htmlFor="new-agent-name">Nome do agente</label>
      <input
        id="new-agent-name"
        value={name}
        onChange={(ev) => setName(ev.target.value)}
        required
        autoFocus
        disabled={pending}
      />
      <button type="submit" className="btn btn-primary" disabled={pending || !name.trim()}>
        {pending ? "Cadastrando…" : "Cadastrar"}
      </button>
      <button type="button" className="btn" disabled={pending} onClick={() => setOpen(false)}>
        Cancelar
      </button>
      {error ? <p className="error-state" role="alert">{error}</p> : null}
    </form>
  );
}
