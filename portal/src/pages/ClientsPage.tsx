/** Clientes: lista paginada + agentes do cliente selecionado. */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  downloadInstallationProfile,
  issueActivationCode,
  revokeActivationCode,
  type ActivationCode,
  createAgent,
  listPreparations,
  prepareClientInstallation,
  retryPreparation,
  listClientAgents,
  listClients,
  type AgentRow,
  type ClientRow,
  type PreparationRow,
} from "../api";
import { InstallationAlerts } from "../components/InstallationAlerts";
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

function newRequestId(): string {
  return crypto.randomUUID();
}

function NewClientForm({ onCreated }: { onCreated: (c: ClientRow) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [identifier, setIdentifier] = useState("");
  const [machine, setMachine] = useState("");
  const [suggestedTime, setSuggestedTime] = useState("22:00");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<PreparationRow | null>(null);
  const request = useRef<{ id: string; signature: string } | null>(null);
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
      const signature = JSON.stringify([
        name.trim(),
        identifier.trim(),
        machine.trim(),
        suggestedTime,
      ]);
      if (request.current?.signature !== signature) {
        request.current = { id: newRequestId(), signature };
      }
      const preparation = await prepareClientInstallation({
        id: request.current.id,
        client_name: name.trim(),
        installation_identifier: identifier.trim(),
        machine_name: machine.trim(),
        suggested_time: suggestedTime,
      });
      if (!alive.current) return;
      setCreated(preparation);
      onCreated({
        id: preparation.client_id,
        name: name.trim(),
        created_at: preparation.created_at,
      });
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
          Preparação registrada para <strong>{created.installation.machine_name}</strong>.{" "}
          <Link to={`/clients/${created.client_id}`}>Acompanhar preparação →</Link>{" "}
          <button
            type="button"
            className="btn"
            onClick={() => {
              setCreated(null);
              setOpen(false);
              request.current = null;
              setName("");
              setIdentifier("");
              setMachine("");
            }}
          >
            Fechar
          </button>
        </p>
      </div>
    );
  }
  return (
    <form className="prepare-form" onSubmit={submit}>
      <p className="form-context">
        Cadastre o cliente e envie uma preparação controlada ao servidor primário.
      </p>
      <label htmlFor="new-client-name">Nome do cliente
        <input id="new-client-name" value={name} onChange={(ev) => setName(ev.target.value)} required autoFocus disabled={pending} />
      </label>
      <label htmlFor="new-installation-id">Identificador da instalação
        <input id="new-installation-id" value={identifier} onChange={(ev) => setIdentifier(ev.target.value.toLowerCase())} pattern="[a-z0-9][a-z0-9._-]{2,79}" required disabled={pending} placeholder="pc-financeiro-01" />
      </label>
      <label htmlFor="new-machine-name">Máquina
        <input id="new-machine-name" value={machine} onChange={(ev) => setMachine(ev.target.value)} required disabled={pending} placeholder="PC-FINANCEIRO" />
      </label>
      <label htmlFor="new-suggested-time">Horário sugerido
        <input id="new-suggested-time" type="time" value={suggestedTime} onChange={(ev) => setSuggestedTime(ev.target.value)} required disabled={pending} />
      </label>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={pending || !name.trim() || !identifier.trim() || !machine.trim()}>
          {pending ? "Preparando solicitação…" : "Preparar instalação"}
        </button>
        <button type="button" className="btn" disabled={pending} onClick={() => setOpen(false)}>Cancelar</button>
      </div>
      {error ? <p className="error-state" role="alert">{error}</p> : null}
    </form>
  );
}

export function ClientAgentsPage() {
  const { clientId } = useParams<{ clientId: string }>();
  const { operator } = useAuth();
  const [agents, setAgents] = useState<AgentRow[] | null>(null);
  const [preparations, setPreparations] = useState<PreparationRow[] | null>(null);
  const [clientName, setClientName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // respostas de um clientId antigo nunca substituem o atual
  const generation = useRef(0);

  async function load() {
    const gen = ++generation.current;
    const wanted = clientId;
    setError(null);
    try {
      const [clientRows, agentRows, preparationRows] = await Promise.all([
        listClients(500, 0),
        listClientAgents(wanted as string, 200, 0),
        listPreparations(wanted as string),
      ]);
      if (gen !== generation.current) return; // rota já mudou
      setClientName(
        clientRows.find((c) => c.id === wanted)?.name ?? null,
      );
      setAgents(agentRows);
      setPreparations(preparationRows);
    } catch (err) {
      if (gen !== generation.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
      setAgents(null);
      setPreparations(null);
    }
  }

  useEffect(() => {
    setAgents(null);
    setClientName(null); // outro cliente → dados do anterior somem
    setPreparations(null);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId]);

  useEffect(() => {
    const active = preparations?.some(
      (item) => item.state !== "failed",
    );
    if (!active) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId, preparations?.map((item) => item.state).join(",")]);

  return (
    <main className="page">
      <p>
        <Link to="/clients">← Clientes</Link>
      </p>
      <h1>Agentes {clientName ? `de ${clientName}` : "do cliente"}</h1>
      {preparations && preparations.length > 0 ? (
        <section aria-labelledby="preparation-heading">
          <h2 id="preparation-heading">Preparação da instalação</h2>
          <div className="preparation-list">
            {preparations.map((item) => (
              <PreparationStatus
                key={item.installation.id}
                item={item}
                admin={operator?.role === "admin"}
                onChanged={() => void load()}
              />
            ))}
          </div>
        </section>
      ) : null}
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

const preparationLabels: Record<PreparationRow["state"], string> = {
  pending: "Aguardando",
  running: "Em preparação",
  primary_ready: "Estrutura primária pronta",
  failed: "Falhou",
};

const preparationErrors: Record<string, string> = {
  access_denied: "O executor não conseguiu acessar o recurso esperado em A.",
  secret_missing: "O segredo original não está disponível no cofre de laboratório.",
  repository_mismatch: "O repositório existente possui outra identidade.",
  key_mismatch: "A chave autorizada diverge da instalação registrada.",
  invalid_state: "O estado encontrado em A não pertence a esta instalação.",
  repository_error: "O repositório primário não pôde ser verificado.",
};

const protectionLabels: Record<PreparationRow["protection_state"], string> = {
  pending: "Preparação pendente",
  prepared: "Estrutura pronta · Prova de restauração pendente",
  proven: "Restauração comprovada em laboratório",
};

function PreparationStatus({
  item,
  admin,
  onChanged,
}: {
  item: PreparationRow;
  admin: boolean;
  onChanged: () => void;
}) {
  const [retrying, setRetrying] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [code, setCode] = useState<ActivationCode | null>(null);
  const [issuing, setIssuing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  async function retry() {
    if (retrying) return;
    setRetrying(true);
    setError(null);
    try {
      await retryPreparation(item.installation.id, newRequestId());
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      setRetrying(false);
    }
  }

  async function download() {
    if (!admin || !item.profile_available || downloading) return;
    setDownloading(true);
    setError(null);
    try {
      const profile = await downloadInstallationProfile(item.installation.id);
      if (!alive.current) return;
      const url = URL.createObjectURL(profile);
      const link = document.createElement("a");
      link.href = url;
      link.download = "perfil-macip.json";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err) {
      if (alive.current) setError(err instanceof ApiError ? err.message : "Falha no download.");
    } finally {
      if (alive.current) setDownloading(false);
    }
  }

  async function issueCode() {
    if (issuing) return;
    setIssuing(true);
    setError(null);
    try {
      const value = await issueActivationCode(item.installation.id);
      if (alive.current) setCode(value);
    } catch (err) {
      if (alive.current) setError(err instanceof ApiError ? err.message : "Falha ao gerar código.");
    } finally {
      if (alive.current) setIssuing(false);
    }
  }

  async function revokeCode() {
    if (!code || issuing) return;
    setIssuing(true);
    try {
      await revokeActivationCode(code.id);
      if (alive.current) setCode(null);
    } catch (err) {
      if (alive.current) setError(err instanceof ApiError ? err.message : "Falha ao revogar.");
    } finally {
      if (alive.current) setIssuing(false);
    }
  }

  const statusClass = item.state === "failed"
    ? "failed"
    : item.state === "primary_ready"
      ? "success"
      : "missing";
  return (
    <article className="preparation-status">
      <div className="preparation-heading">
        <div>
          <strong>{item.installation.machine_name}</strong>
          <span>{item.installation.identifier}</span>
        </div>
        <span className={`badge badge-${statusClass}`} role="status">
          {preparationLabels[item.state]}
        </span>
      </div>
      {item.state !== "primary_ready" ? (
        <p className="preparation-message">
          {item.state === "running"
            ? "O executor está reconciliando conta, chave e repositório."
            : item.state === "pending"
              ? "A solicitação aguarda o executor do servidor primário."
              : preparationErrors[item.error_code ?? ""] ?? "A preparação falhou sem expor detalhes internos."}
        </p>
      ) : null}
      <dl className="preparation-meta">
        <div><dt>Servidor B</dt><dd>{protectionLabels[item.protection_state]}</dd></div>
        <div>
          <dt>Última cópia confirmada</dt>
          <dd>{item.last_protected_copy ? formatDate(item.last_protected_copy.confirmed_at) : "Nenhuma confirmada"}</dd>
          {item.last_protected_copy ? (
            <dd>
              A <span title={item.last_protected_copy.snapshot_a}>{item.last_protected_copy.snapshot_a.slice(0, 8)}</span>
              {" → "}B <span title={item.last_protected_copy.snapshot_b}>{item.last_protected_copy.snapshot_b.slice(0, 8)}</span>
            </dd>
          ) : null}
        </div>
        <div><dt>Horário sugerido</dt><dd>{item.installation.suggested_time}</dd></div>
        <div><dt>Tentativa</dt><dd>{item.operation.attempt}</dd></div>
      </dl>
      <div className="form-actions">
        {admin && item.activation_available && !code ? (
          <button type="button" className="btn" disabled={issuing} onClick={() => void issueCode()}>
            {issuing ? "Gerando…" : "Gerar código de ativação"}
          </button>
        ) : null}
        {admin && item.state === "failed" ? (
          <button type="button" className="btn" onClick={() => void retry()} disabled={retrying}>
            {retrying ? "Solicitando…" : "Tentar novamente"}
          </button>
        ) : null}
        <button
          type="button"
          className="btn"
          disabled={!admin || !item.profile_available || downloading}
          onClick={() => void download()}
          title={item.profile_available ? undefined : "Disponível após comprovar a restauração em B"}
        >
          {downloading ? "Baixando…" : "Baixar perfil"}
        </button>
      </div>
      <InstallationAlerts id={item.installation.id} admin={admin} />
      {code ? (
        <div>
          <label htmlFor={`activation-code-${item.installation.id}`}>Código de ativação</label>
          <textarea id={`activation-code-${item.installation.id}`} readOnly value={code.activation_code}
            onFocus={(ev) => ev.currentTarget.select()} />
          <p>Uso único. Expira em {formatDate(code.expires_at)}. Entregue pelo canal privado do laboratório.</p>
          <div className="form-actions">
            <button type="button" className="btn" disabled={issuing} onClick={() => void revokeCode()}>Revogar código</button>
            <button type="button" className="btn" onClick={() => setCode(null)}>Ocultar código</button>
          </div>
        </div>
      ) : null}
      {error ? <p className="error-state" role="alert">{error}</p> : null}
    </article>
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
