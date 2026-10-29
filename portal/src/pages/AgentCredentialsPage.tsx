/** Credenciais do agente: contexto (cliente+agente), listagem com
 * revogação, emissão com apresentação temporária do token.
 *
 * O token existe APENAS em estado React — nunca em storage, URL,
 * console ou mensagens de erro — e é descartado ao fechar a
 * apresentação, sair da tela ou trocar de agente. */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  createCredential,
  findAgent,
  findClient,
  listCredentials,
  revokeCredential,
  type AgentRow,
  type CredentialInfo,
  type CredentialOut,
} from "../api";
import { useAuth } from "../auth";
import { formatDate } from "../format";
import { EmptyState, ErrorState, Loading } from "../components/common";

export function AgentCredentialsPage() {
  const { agentId } = useParams<{ agentId: string }>();
  const { operator } = useAuth();
  const [agent, setAgent] = useState<AgentRow | null>(null);
  const [clientName, setClientName] = useState<string | null>(null);
  const [rows, setRows] = useState<CredentialInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issued, setIssued] = useState<CredentialOut | null>(null);
  const generation = useRef(0);

  async function load() {
    const gen = ++generation.current;
    const wanted = agentId;
    setError(null);
    try {
      // resolve o agente paginando — não há GET por id e a primeira
      // página pode não contê-lo
      const [found, creds] = await Promise.all([
        findAgent(wanted as string),
        listCredentials(wanted as string),
      ]);
      if (gen !== generation.current) return; // agente já mudou
      setAgent(found);
      if (found) {
        const client = await findClient(found.client_id);
        if (gen !== generation.current) return;
        setClientName(client?.name ?? null);
      } else {
        setClientName(null);
      }
      setRows(creds);
    } catch (err) {
      if (gen !== generation.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
      setRows(null);
      setAgent(null);
    }
  }

  useEffect(() => {
    setIssued(null); // trocou de agente → token do anterior some
    setRows(null);
    setAgent(null);
    setClientName(null);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // sair da tela → o token some com o componente
  useEffect(() => () => setIssued(null), []);

  return (
    <main className="page">
      <p>
        <Link to={agent ? `/clients/${agent.client_id}` : "/clients"}>
          ← Agentes do cliente
        </Link>
      </p>
      <h1>Credenciais do agente</h1>
      {rows !== null && agent === null && !error ? (
        <EmptyState message="Agente não encontrado." />
      ) : null}
      {agent ? (
        <p className="form-context">
          Agente <strong>{agent.name}</strong>{" "}
          <code>{agent.id}</code> — cliente{" "}
          <strong>{clientName ?? agent.client_id}</strong>
          {" — "}
          <Link to={`/tasks?agent_id=${agent.id}`}>tarefas do agente</Link>
          {operator?.role === "admin" ? (
            <>
              {" — "}
              <Link to={`/tasks/new?agent=${agent.id}`}>nova tarefa</Link>
            </>
          ) : null}
        </p>
      ) : null}
      {operator?.role === "admin" && agentId && agent ? (
        <IssueForm
          agentId={agentId}
          onIssued={(cred) => {
            setIssued(cred);
            void load();
          }}
        />
      ) : null}
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {rows === null && !error ? <Loading /> : null}
      {rows !== null && rows.length === 0 ? (
        <EmptyState message="Nenhuma credencial emitida." />
      ) : null}
      {rows !== null && rows.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Descrição</th>
              <th scope="col">Criação</th>
              <th scope="col">Estado</th>
              {operator?.role === "admin" ? <th scope="col">Ações</th> : null}
            </tr>
          </thead>
          <tbody>
            {rows.map((cred) => (
              <CredentialRow
                key={cred.id}
                cred={cred}
                agentName={agent?.name ?? null}
                admin={operator?.role === "admin"}
                onRevoked={(id) => {
                  setRows(
                    (cur) =>
                      cur?.map((c) =>
                        c.id === id
                          ? { ...c, revoked_at: new Date().toISOString() }
                          : c,
                      ) ?? cur,
                  );
                }}
              />
            ))}
          </tbody>
        </table>
      ) : null}
      {issued ? <TokenPanel issued={issued} onClose={() => setIssued(null)} /> : null}
    </main>
  );
}

function IssueForm({
  agentId,
  onIssued,
}: {
  agentId: string;
  onIssued: (cred: CredentialOut) => void;
}) {
  const [label, setLabel] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    if (pending) return; // sem submissão duplicada
    setPending(true);
    setError(null);
    try {
      const cred = await createCredential(agentId, label.trim() || null);
      if (!alive.current || cred.agent_id !== agentId) return;
      setLabel("");
      onIssued(cred);
    } catch (err) {
      if (!alive.current) return;
      // resultado incerto: a emissão pode ter acontecido no servidor —
      // NÃO repete o POST; orienta conferir a listagem antes
      setError(
        err instanceof ApiError && err.kind === "network"
          ? "Não foi possível confirmar a emissão — consulte a listagem antes de emitir outra credencial."
          : err instanceof ApiError
            ? err.message
            : "Falha inesperada.",
      );
    } finally {
      if (alive.current) setPending(false);
    }
  }

  return (
    <form className="form-inline" onSubmit={submit}>
      <label htmlFor="new-credential-label">Descrição da credencial</label>
      <input
        id="new-credential-label"
        value={label}
        onChange={(ev) => setLabel(ev.target.value)}
        placeholder="ex.: servidor de produção"
        disabled={pending}
      />
      <button type="submit" className="btn btn-primary" disabled={pending}>
        {pending ? "Emitindo…" : "Emitir credencial"}
      </button>
      {error ? <p className="error-state" role="alert">{error}</p> : null}
    </form>
  );
}

function TokenPanel({
  issued,
  onClose,
}: {
  issued: CredentialOut;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState<"idle" | "ok" | "fail">("idle");

  async function copy() {
    // gesto explícito do usuário; falha é informada, nunca silenciada
    try {
      await navigator.clipboard.writeText(issued.token);
      setCopied("ok");
    } catch {
      setCopied("fail");
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="token-title"
      >
        <h2 id="token-title">Credencial emitida</h2>
        <p>
          Copie o token agora — ele <strong>não poderá ser consultado
          novamente</strong> depois de fechar esta janela.
        </p>
        <div className="token-box">
          <code data-testid="token-value">{issued.token}</code>
          <button type="button" className="btn" onClick={copy}>
            Copiar
          </button>
        </div>
        {copied === "ok" ? (
          <p className="success-state" role="status">Copiado.</p>
        ) : null}
        {copied === "fail" ? (
          <p className="error-state" role="alert">
            Não foi possível copiar — selecione e copie manualmente.
          </p>
        ) : null}
        <button type="button" className="btn btn-primary" onClick={onClose}>
          Fechar
        </button>
      </div>
    </div>
  );
}

function CredentialRow({
  cred,
  agentName,
  admin,
  onRevoked,
}: {
  cred: CredentialInfo;
  agentName: string | null;
  admin: boolean;
  onRevoked: (id: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const revoked = cred.revoked_at !== null;

  async function doRevoke() {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await revokeCredential(cred.id);
      if (!alive.current) return;
      setConfirming(false);
      onRevoked(cred.id);
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
    } finally {
      if (alive.current) setPending(false);
    }
  }

  return (
    <tr>
      <td>{cred.label ?? "—"}</td>
      <td>{formatDate(cred.created_at)}</td>
      <td>
        {revoked ? (
          <>Revogada em {formatDate(cred.revoked_at)}</>
        ) : (
          "Ativa"
        )}
        {error ? (
          <p className="error-state" role="alert">{error}</p>
        ) : null}
      </td>
      {admin ? (
        <td>
          {revoked ? null : confirming ? (
            <span className="confirm-inline" role="group" aria-label="Confirmar revogação">
              Revogar <strong>{cred.label ?? cred.id}</strong> do agente{" "}
              {agentName ?? cred.agent_id}?{" "}
              <button
                type="button"
                className="btn"
                onClick={() => void doRevoke()}
                disabled={pending}
              >
                {pending ? "Revogando…" : "Confirmar"}
              </button>{" "}
              <button
                type="button"
                className="btn"
                onClick={() => setConfirming(false)}
                disabled={pending}
              >
                Cancelar
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="btn"
              onClick={() => setConfirming(true)}
            >
              Revogar
            </button>
          )}
        </td>
      ) : null}
    </tr>
  );
}
