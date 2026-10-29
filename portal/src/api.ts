/**
 * Cliente HTTP da API central — mesma origem, sessão por cookie.
 *
 * Nada de segredos no JavaScript: a sessão vive no cookie HttpOnly
 * `macip_session`; o token CSRF é lido do cookie `macip_csrf` (não
 * HttpOnly) e repetido no cabeçalho `X-CSRF-Token`. Nem senha nem
 * Bearer administrativo passam por aqui.
 */

export interface Operator {
  id: string;
  login: string;
  name: string;
  role: "admin" | "viewer";
  active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface ClientRow {
  id: string;
  name: string;
  created_at: string;
}

export interface AgentRow {
  id: string;
  client_id: string;
  name: string;
  created_at: string;
  last_seen_at: string | null;
}

export type ExecutionStatus =
  | "success"
  | "partial"
  | "failed"
  | "interrupted";

export interface ExecutionSummary {
  id: string; // UUID central — não confundir com execution_id do agente
  agent_id: string;
  client_id: string;
  execution_id: string;
  task_id: string;
  status: ExecutionStatus;
  started_at_utc: string;
  finished_at_utc: string;
  received_at: string;
}

export interface AttemptResult {
  attempt: number;
  status: string;
  started_at_utc: string;
  finished_at_utc: string;
  duration_seconds: number | null;
  retryable: boolean;
  snapshot_id: string | null;
  snapshots_available: number | null;
  files_processed: number | null;
  bytes_processed: number | null;
  error: string | null;
  exit_code: number | null;
  failed_operation: string | null;
  timeout_seconds: number | null;
}

export interface DestinationResult extends AttemptResult {
  destination_id: string;
  attempts: AttemptResult[];
}

export interface ExecutionDetail extends ExecutionSummary {
  destinations: DestinationResult[];
}

export interface ExecutionFilters {
  client_id?: string;
  agent_id?: string;
  task_id?: string;
  status?: string;
  since?: string; // ISO 8601 com timezone
  until?: string;
  limit: number;
  offset: number;
}

export type ApiErrorKind =
  | "unauthenticated" // 401 — sessão ausente/expirada
  | "forbidden" // 403 — sem permissão / CSRF
  | "rate_limited" // 429 — muitas tentativas
  | "invalid" // 422 — validação
  | "not_found" // 404
  | "network" // falha de transporte — API indisponível
  | "server"; // 5xx e outros

export class ApiError extends Error {
  constructor(
    readonly kind: ApiErrorKind,
    readonly status: number | null,
    detail?: string,
  ) {
    super(detail ?? defaultMessage(kind));
    this.name = "ApiError";
  }
}

function defaultMessage(kind: ApiErrorKind): string {
  switch (kind) {
    case "unauthenticated":
      return "Sessão ausente ou expirada — entre novamente.";
    case "forbidden":
      return "Acesso não permitido para esta credencial.";
    case "rate_limited":
      return "Muitas tentativas — aguarde antes de tentar novamente.";
    case "invalid":
      return "Dados inválidos — revise os campos informados.";
    case "not_found":
      return "Registro não encontrado.";
    case "network":
      return "Não foi possível alcançar a API — verifique a conexão.";
    case "server":
      return "Erro interno da API — tente novamente mais tarde.";
  }
}

function kindOf(status: number): ApiErrorKind {
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 429) return "rate_limited";
  if (status === 422 || status === 400) return "invalid";
  if (status === 404) return "not_found";
  return "server";
}

/** Valor ATUAL do cookie CSRF — a API o rotaciona a cada login. */
export function csrfToken(): string | null {
  const entry = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("macip_csrf="));
  return entry ? decodeURIComponent(entry.slice("macip_csrf=".length)) : null;
}

async function parseDetail(response: Response): Promise<string | undefined> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    /* corpo sem JSON — não é obrigatório */
  }
  return undefined;
}

async function apiFetch(
  path: string,
  options: { method?: string; body?: unknown; csrf?: boolean } = {},
): Promise<Response> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) {
    const token = csrfToken();
    if (!token) throw new ApiError("forbidden", null, "Proteção CSRF ausente — recarregue a página.");
    headers["X-CSRF-Token"] = token;
  }
  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError("network", null);
  }
  if (!response.ok) {
    // 429 usa mensagem própria — o detalhe bruto do servidor não ajuda
    const detail =
      response.status === 429 ? undefined : await parseDetail(response);
    throw new ApiError(kindOf(response.status), response.status, detail);
  }
  return response;
}

export interface Session {
  operator: Operator;
}

/** Emite o cookie CSRF pré-sessão (204 — sem corpo). */
export async function fetchCsrf(): Promise<void> {
  await apiFetch("/api/v1/auth/csrf");
}

export async function login(loginName: string, password: string): Promise<Operator> {
  await fetchCsrf(); // par cookie+cabeçalho obrigatório no login
  const response = await apiFetch("/api/v1/auth/login", {
    method: "POST",
    body: { login: loginName, password },
    csrf: true,
  });
  return (await response.json()) as Operator;
}

export async function me(): Promise<Operator> {
  const response = await apiFetch("/api/v1/auth/me");
  return (await response.json()) as Operator;
}

export async function logout(): Promise<void> {
  await apiFetch("/api/v1/auth/logout", { method: "POST", csrf: true });
}

export async function listClients(limit: number, offset: number): Promise<ClientRow[]> {
  const r = await apiFetch(`/api/v1/clients?limit=${limit}&offset=${offset}`);
  return (await r.json()) as ClientRow[];
}

export async function listAgents(
  clientId?: string,
  limit = 200,
  offset = 0,
): Promise<AgentRow[]> {
  const suffix = clientId ? `&client_id=${encodeURIComponent(clientId)}` : "";
  const r = await apiFetch(`/api/v1/agents?limit=${limit}&offset=${offset}${suffix}`);
  return (await r.json()) as AgentRow[];
}

export async function listExecutions(
  filters: ExecutionFilters,
): Promise<ExecutionSummary[]> {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit));
  params.set("offset", String(filters.offset));
  for (const key of ["client_id", "agent_id", "task_id", "status", "since", "until"] as const) {
    const value = filters[key];
    if (value) params.set(key, value);
  }
  const r = await apiFetch(`/api/v1/executions?${params}`);
  return (await r.json()) as ExecutionSummary[];
}

export async function getExecution(id: string): Promise<ExecutionDetail> {
  const r = await apiFetch(`/api/v1/executions/${encodeURIComponent(id)}`);
  return (await r.json()) as ExecutionDetail;
}

// ---- cadastramento e credenciais (escrita = admin + CSRF) ---------

export interface CredentialInfo {
  id: string;
  agent_id: string;
  label: string | null;
  created_at: string;
  revoked_at: string | null;
}

export interface CredentialOut {
  credential_id: string;
  agent_id: string;
  token: string; // entregue UMA vez — nunca persista
}

export async function createClient(name: string): Promise<ClientRow> {
  const r = await apiFetch("/api/v1/clients", {
    method: "POST",
    body: { name },
    csrf: true,
  });
  return (await r.json()) as ClientRow;
}

export async function createAgent(
  clientId: string,
  name: string,
): Promise<AgentRow> {
  const r = await apiFetch(
    `/api/v1/clients/${encodeURIComponent(clientId)}/agents`,
    { method: "POST", body: { name }, csrf: true },
  );
  return (await r.json()) as AgentRow;
}

export async function listClientAgents(
  clientId: string,
  limit = 200,
  offset = 0,
): Promise<AgentRow[]> {
  const r = await apiFetch(
    `/api/v1/clients/${encodeURIComponent(clientId)}/agents?limit=${limit}&offset=${offset}`,
  );
  return (await r.json()) as AgentRow[];
}

export async function listCredentials(
  agentId: string,
): Promise<CredentialInfo[]> {
  const r = await apiFetch(
    `/api/v1/agents/${encodeURIComponent(agentId)}/credentials`,
  );
  return (await r.json()) as CredentialInfo[];
}

export async function createCredential(
  agentId: string,
  label: string | null,
): Promise<CredentialOut> {
  const r = await apiFetch(
    `/api/v1/agents/${encodeURIComponent(agentId)}/credentials`,
    { method: "POST", body: { label }, csrf: true },
  );
  return (await r.json()) as CredentialOut;
}

export async function revokeCredential(
  credentialId: string,
): Promise<void> {
  await apiFetch(
    `/api/v1/credentials/${encodeURIComponent(credentialId)}/revoke`,
    { method: "POST", csrf: true },
  );
}
