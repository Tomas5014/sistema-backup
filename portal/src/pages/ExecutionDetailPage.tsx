/** Detalhe da execução: destinos e tentativas. Erros como texto puro —
 * o JSX já escapa, e erros extensos ficam em bloco rolável. */

import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { ApiError, getExecution, type ExecutionDetail } from "../api";
import { formatBytes, formatDate, formatDuration } from "../format";
import { EmptyState, ErrorState, Loading, StatusBadge } from "../components/common";

function Cell({ value }: { value: string | number | null | undefined }) {
  return <>{value === null || value === undefined || value === "" ? "—" : value}</>;
}

export function ExecutionDetailPage() {
  const { id } = useParams<{ id: string }>(); // UUID CENTRAL, não execution_id
  const location = useLocation();
  const [detail, setDetail] = useState<ExecutionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const back = (location.state as { back?: string } | null)?.back ?? "";

  async function load() {
    setError(null);
    try {
      setDetail(await getExecution(id!));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Falha inesperada.");
      setDetail(null);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  return (
    <main className="page">
      <p>
        <Link to={`/executions${back}`}>← Execuções</Link>
      </p>
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {detail === null && !error ? <Loading /> : null}
      {detail ? (
        <>
          <h1>
            Execução <StatusBadge status={detail.status} />
          </h1>
          <dl className="detail-grid">
            <dt>Cliente</dt>
            <dd className="mono">{detail.client_id}</dd>
            <dt>Agente</dt>
            <dd className="mono">{detail.agent_id}</dd>
            <dt>Tarefa</dt>
            <dd>{detail.task_id}</dd>
            <dt>Início</dt>
            <dd>{formatDate(detail.started_at_utc)}</dd>
            <dt>Fim</dt>
            <dd>{formatDate(detail.finished_at_utc)}</dd>
            <dt>Recebida</dt>
            <dd>{formatDate(detail.received_at)}</dd>
            <dt>ID da execução (agente)</dt>
            <dd className="mono">{detail.execution_id}</dd>
          </dl>

          <h2>Destinos</h2>
          {detail.destinations.length === 0 ? (
            <EmptyState message="Nenhum destino registrado." />
          ) : (
            detail.destinations.map((dest) => (
              <section key={dest.destination_id} className="card">
                <h3>
                  {dest.destination_id} <StatusBadge status={dest.status} />
                </h3>
                <dl className="detail-grid">
                  <dt>Duração</dt>
                  <dd>{formatDuration(dest.duration_seconds)}</dd>
                  <dt>Snapshot</dt>
                  <dd className="mono">
                    <Cell value={dest.snapshot_id} />
                    {dest.snapshots_available !== null ? (
                      <> ({dest.snapshots_available} disponíveis)</>
                    ) : null}
                  </dd>
                  <dt>Arquivos / bytes</dt>
                  <dd>
                    <Cell value={dest.files_processed} /> /{" "}
                    {formatBytes(dest.bytes_processed)}
                  </dd>
                  <dt>Operação que falhou</dt>
                  <dd>
                    <Cell value={dest.failed_operation} />
                  </dd>
                  <dt>Código de saída</dt>
                  <dd>
                    <Cell value={dest.exit_code} />
                  </dd>
                  <dt>Timeout</dt>
                  <dd>
                    <Cell value={dest.timeout_seconds} />
                  </dd>
                </dl>
                {dest.error ? (
                  <pre className="error-block">{dest.error}</pre>
                ) : null}

                {dest.attempts.length > 0 ? (
                  <details>
                    <summary>{dest.attempts.length} tentativa(s)</summary>
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th scope="col">#</th>
                          <th scope="col">Status</th>
                          <th scope="col">Duração</th>
                          <th scope="col">Retentável</th>
                          <th scope="col">Erro</th>
                        </tr>
                      </thead>
                      <tbody>
                        {dest.attempts.map((attempt) => (
                          <tr key={attempt.attempt}>
                            <td>{attempt.attempt}</td>
                            <td>
                              <StatusBadge status={attempt.status} />
                            </td>
                            <td>{formatDuration(attempt.duration_seconds)}</td>
                            <td>{attempt.retryable ? "Sim" : "Não"}</td>
                            <td className="attempt-error">
                              <Cell value={attempt.error} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>
                ) : null}
              </section>
            ))
          )}
        </>
      ) : null}
    </main>
  );
}
