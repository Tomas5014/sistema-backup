/** Rótulos e tons do acompanhamento — fonte única para lista e detalhe.
 * O texto curto do badge NUNCA substitui a explicação: `detail` da API
 * acompanha o estado no detalhe e no title da lista. */

import type {
  OperationalState,
  ResultState,
} from "./api";

export const OPERATIONAL_LABELS: Record<OperationalState, string> = {
  unpublished: "não publicada",
  pending_apply: "aguardando aplicação",
  manual: "manual",
  scheduled: "agendada",
  pending_suspend: "suspensão pendente",
  suspended: "suspensa",
};

export interface ResultBadge {
  label: string;
  /** classe CSS — 'missing' é atenção (âmbar), NUNCA o vermelho de falha;
   * estados sem dado suficiente ficam neutros, nunca verdes. */
  className: string;
}

export const RESULT_BADGES: Record<ResultState, ResultBadge> = {
  no_history: { label: "sem histórico", className: "badge-muted" },
  awaiting: { label: "aguardando", className: "badge-muted" },
  pending: { label: "dentro do prazo", className: "badge-muted" },
  ok: { label: "em dia", className: "badge-success" },
  partial: { label: "parcial", className: "badge-partial" },
  failed: { label: "falha", className: "badge-failed" },
  missing: { label: "sem resultado", className: "badge-missing" },
};

export function resultBadge(state: ResultState): ResultBadge {
  return RESULT_BADGES[state] ?? { label: state, className: "badge-muted" };
}

/** Formata instante UTC com fuso visível — o operador precisa saber
 * qual fuso o relógio usou (a agenda em si é IANA no contrato). */
export function formatUtc(instant: string | null | undefined): string {
  if (!instant) return "—";
  return new Date(instant).toLocaleString("pt-BR", {
    timeZoneName: "short",
  });
}
