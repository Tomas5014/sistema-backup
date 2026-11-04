import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  createRecovery,
  listRecovery,
  type RecoveryOperation,
  type RecoveryRequest,
} from "../api";

interface Props {
  taskId: string;
  revision: number;
  destinations: { id: string; name: string }[];
  admin: boolean;
}

const states: Record<RecoveryOperation["state"], string> = {
  pending: "Pendente — aguardando o agente",
  running: "Em execução no agente",
  succeeded: "Concluída pelo agente",
  failed: "Falhou no agente",
  interrupted: "Interrompida — não concluída",
};

function explainError(message: string): string {
  const messages: Record<string, string> = {
    repository_error: "Não foi possível acessar ou verificar o repositório no agente.",
    snapshot_missing: "O snapshot não está mais disponível no repositório. Consulte os pontos novamente.",
    invalid_target: "A pasta de recuperação não atende às regras locais de segurança do agente.",
    credential_missing: "A credencial local do repositório está ausente no agente.",
    timeout: "A operação excedeu o limite de tempo no agente.",
    interrupted: "A operação foi interrompida; uma pasta parcial não representa recuperação concluída.",
    abandoned: "A execução anterior foi abandonada ou interrompida. Consulte o responsável pelo agente antes de uma nova solicitação.",
  };
  return messages[message] ?? message;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status === 401
      ? "Sessão expirada. Entre novamente para continuar."
      : explainError(error.message);
  }
  return "Não foi possível concluir a solicitação. Tente novamente.";
}

/** Operações fixadas por revisão; nunca interpreta caminhos ou erros como HTML. */
export default function RecoverySection({ taskId, revision, destinations, admin }: Props) {
  const [operations, setOperations] = useState<RecoveryOperation[]>([]);
  const [destination, setDestination] = useState(destinations[0]?.id ?? "");
  const [snapshotId, setSnapshotId] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const alive = useRef(false);
  const generation = useRef(0);
  const requestGeneration = useRef(0);
  const busy = useRef(false);
  const context = `${taskId}:${revision}`;
  const currentContext = useRef(context);
  currentContext.current = context;

  const refresh = useCallback(async () => {
    const visit = generation.current;
    const request = ++requestGeneration.current;
    setLoading(true);
    try {
      const result = await listRecovery(taskId);
      if (!alive.current || visit !== generation.current || currentContext.current !== context || request !== requestGeneration.current) return;
      setOperations(result);
      setError(null);
      for (const operation of result) {
        if (operation.kind !== "snapshots") continue;
        const key = `macip:recovery:${taskId}:${operation.revision}:snapshots:${operation.destination_id}`;
        if (window.sessionStorage.getItem(key) === operation.id) window.sessionStorage.removeItem(key);
      }
    } catch (err) {
      if (!alive.current || visit !== generation.current || currentContext.current !== context || request !== requestGeneration.current) return;
      setError(errorMessage(err));
    } finally {
      if (alive.current && visit === generation.current && currentContext.current === context && request === requestGeneration.current) setLoading(false);
    }
  }, [taskId, context]);

  useEffect(() => {
    alive.current = true;
    generation.current += 1;
    busy.current = false;
    setOperations([]);
    setDestination(destinations[0]?.id ?? "");
    setSnapshotId("");
    setConfirm(false);
    setSending(false);
    setError(null);
    void refresh();
    return () => {
      alive.current = false;
      generation.current += 1;
      requestGeneration.current += 1;
    };
    // The parent keys this section by task and revision; destinations belong
    // to that immutable context, not to a background detail refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, refresh]);

  const unfinished = operations.some((operation) => operation.state === "pending" || operation.state === "running");
  useEffect(() => {
    if (!unfinished) return;
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => window.clearInterval(timer);
  }, [unfinished, refresh]);

  // API lists newest operations first. A failed new query must not silently
  // present a previous query's catalogue as if the new one succeeded.
  const query = operations.find((operation) => operation.kind === "snapshots" && operation.destination_id === destination && operation.revision === revision);
  const catalogue = query?.state === "succeeded" ? query.result?.snapshots : undefined;
  const snapshots = catalogue ?? [];
  const chosen = snapshots.find((snapshot) => snapshot.id === snapshotId);
  const existingRestore = operations.find((operation) => operation.kind === "restore" && operation.query_id === query?.id && operation.snapshot_id === snapshotId);
  const queryBusy = query?.state === "pending" || query?.state === "running";

  useEffect(() => {
    setSnapshotId("");
    setConfirm(false);
  }, [query?.id]);

  async function submit(kind: "snapshots" | "restore") {
    if (!admin || busy.current || !destination || (kind === "restore" && (!query || !chosen || existingRestore))) return;
    busy.current = true;
    setSending(true);
    setError(null);
    const visit = generation.current;
    const wantedContext = context;
    const intent = kind === "snapshots"
      ? `${context}:snapshots:${destination}`
      : `${context}:restore:${query!.id}:${snapshotId}`;
    const key = `macip:recovery:${intent}`;
    try {
      // Persist before POST. Lost responses, retries and reloads reuse the
      // request UUID. Server additionally deduplicates query/snapshot restores.
      let id = window.sessionStorage.getItem(key);
      if (!id) {
        id = window.crypto.randomUUID();
        window.sessionStorage.setItem(key, id);
      }
      const request: RecoveryRequest = kind === "snapshots"
        ? { id, kind, destination_id: destination, revision }
        : { id, kind, destination_id: destination, query_id: query!.id, snapshot_id: snapshotId };
      const result = await createRecovery(taskId, request);
      if (!alive.current || visit !== generation.current || currentContext.current !== wantedContext) return;
      requestGeneration.current += 1;
      setLoading(false);
      setOperations((previous) => [result, ...previous.filter((operation) => operation.id !== result.id)]);
      setConfirm(false);
      // A deliberate new query after its completion needs a new identity.
      // Restore IDs remain stable; another restore requires another query.
      if (kind === "snapshots") window.sessionStorage.removeItem(key);
    } catch (err) {
      if (!alive.current || visit !== generation.current || currentContext.current !== wantedContext) return;
      setError(errorMessage(err));
    } finally {
      if (alive.current && visit === generation.current && currentContext.current === wantedContext) {
        busy.current = false;
        setSending(false);
      }
    }
  }

  return (
    <section aria-labelledby="recovery-heading">
      <h2 id="recovery-heading">Recuperação no agente Linux</h2>
      <p>A recuperação cria uma pasta nova sob a raiz autorizada localmente no agente. Não sobrescreve a origem nem reativa o agendamento.</p>
      <label>
        Destino da recuperação
        <select value={destination} disabled={sending} onChange={(event) => { setDestination(event.target.value); setSnapshotId(""); setConfirm(false); }}>
          {destinations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      {admin ? <button type="button" disabled={!destination || sending || queryBusy} onClick={() => void submit("snapshots")}>Consultar pontos</button> : null}
      <button type="button" disabled={loading} onClick={() => void refresh()}>Atualizar recuperação</button>
      {error ? <p role="alert">{error}</p> : null}
      {loading && operations.length === 0 ? <p role="status">Carregando operações de recuperação…</p> : null}
      {!query && !loading ? <p>Nenhuma consulta de pontos nesta revisão e destino.</p> : null}
      {queryBusy ? <p role="status">Consulta {query?.state === "running" ? "em execução" : "pendente"}. Aguardando o agente; se estiver indisponível, a solicitação permanece pendente.</p> : null}
      {query?.state === "failed" || query?.state === "interrupted" ? <p role="alert">Falha na consulta: {query.error ? explainError(query.error) : "O agente não concluiu a consulta."}</p> : null}
      {query?.state === "succeeded" && catalogue === undefined ? <p role="alert">Consulta confirmada sem catálogo de pontos. Atualize ou faça uma nova consulta.</p> : null}
      {query?.state === "succeeded" && catalogue !== undefined && snapshots.length === 0 ? <p>Consulta concluída: nenhum ponto disponível.</p> : null}
      {snapshots.length > 0 ? (
        <>
          <label>
            Ponto de recuperação
            <select value={snapshotId} onChange={(event) => { setSnapshotId(event.target.value); setConfirm(false); }}>
              <option value="">Selecione um snapshot</option>
              {snapshots.map((snapshot) => <option key={snapshot.id} value={snapshot.id}>{snapshot.id.slice(0, 12)} — {snapshot.time ?? "horário não informado"}{snapshot.hostname ? ` — ${snapshot.hostname}` : ""}</option>)}
            </select>
          </label>
          {admin ? <button type="button" disabled={!chosen || sending || Boolean(existingRestore)} onClick={() => setConfirm(true)}>Solicitar restauração</button> : null}
          {existingRestore ? <p>Restauração deste ponto já solicitada. Para outra recuperação deliberada, faça uma nova consulta de pontos.</p> : null}
        </>
      ) : null}
      {confirm && chosen && query ? (
        <fieldset>
          <legend>Confirmar restauração completa</legend>
          <p>Restaurar o snapshot {chosen.id} em pasta nova e exclusiva sob a raiz autorizada no agente? A conclusão só será exibida após confirmação do agente.</p>
          <button type="button" disabled={sending} onClick={() => void submit("restore")}>Confirmar restauração</button>
          <button type="button" disabled={sending} onClick={() => setConfirm(false)}>Cancelar restauração</button>
        </fieldset>
      ) : null}
      <ul aria-label="Operações de recuperação">
        {operations.filter((operation) => operation.kind === "restore").map((operation) => (
          <li key={operation.id}>
            Restauração — revisão {operation.revision}, destino {operation.destination_id}: {states[operation.state]}.
            {operation.error ? <p>{explainError(operation.error)}</p> : null}
            {operation.result?.target ? <p>Pasta no agente: {operation.result.target}</p> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
