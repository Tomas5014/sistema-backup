/** Componentes compartilhados: status, estados vazios/erro, paginação. */

import { formatDuration } from "../format";

export function StatusBadge({ status }: { status: string }) {
  const labels: Record<string, string> = {
    success: "Sucesso",
    partial: "Parcial",
    failed: "Falha",
    interrupted: "Interrompida",
  };
  return (
    <span className={`badge badge-${status}`} role="status">
      {labels[status] ?? status}
    </span>
  );
}

export function EmptyState({ message }: { message: string }) {
  return <p className="empty-state">{message}</p>;
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="error-state" role="alert">
      <p>{message}</p>
      {onRetry ? (
        <button type="button" className="btn" onClick={onRetry}>
          Tentar novamente
        </button>
      ) : null}
    </div>
  );
}

export function Loading() {
  return (
    <p className="loading" role="status" aria-live="polite">
      Carregando…
    </p>
  );
}

export function Pagination({
  offset,
  limit,
  count,
  onChange,
}: {
  offset: number;
  limit: number;
  count: number; // itens DESTA página — a API não devolve total global
  onChange: (offset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  return (
    <nav className="pagination" aria-label="Paginação">
      <button
        type="button"
        className="btn"
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - limit))}
      >
        Anterior
      </button>
      <span className="pagination-info">
        Página {page}
      </span>
      <button
        type="button"
        className="btn"
        disabled={count < limit} // página cheia → pode haver próxima
        onClick={() => onChange(offset + limit)}
      >
        Próxima
      </button>
    </nav>
  );
}

export function Duration({ seconds }: { seconds: number | null }) {
  return <>{formatDuration(seconds)}</>;
}
