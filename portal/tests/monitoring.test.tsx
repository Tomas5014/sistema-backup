/** Acompanhamento no portal: selo na lista com tons corretos (ausência
 * de resultado NÃO é vermelho de falha), seção no detalhe com prazos e
 * explicação, erro da API de monitoring como erro de carga da seção —
 * nunca como falha do backup — e resposta atrasada descartada. */

import { beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "../src/App";
import { AuthProvider } from "../src/auth";
import {
  clearCookies,
  json,
  noContent,
  stubFetch,
  OPERATOR,
} from "./helpers";

const CLIENT = {
  id: "cli-1", name: "Acme Ltda", created_at: "2026-10-01T10:00:00Z",
};

const TASK = {
  id: "task-1", agent_id: "ag-1", client_id: "cli-1",
  name: "Financeiro diário", current_revision: 1,
  archived: false, archived_at: null,
  created_at: "2026-10-28T10:00:00Z", distributed: false,
};

const CONFIG = {
  name: "Financeiro diário",
  spec: {
    sources: [{ path: "/dados" }],
    exclusions: [],
    compression: "auto",
    destinations: [
      { id: "local", name: "Disco", kind: "local",
        repository: "/srv/b", password_environment: "PW",
        retention: { keep_last: 7 } },
    ],
  },
  schedule: { kind: "daily", timezone: "America/Sao_Paulo", hour: 2, minute: 15 },
};

const REV1 = {
  revision: 1, contract_version: 1, config: CONFIG,
  author_kind: "operator", author_ref: "anita",
  created_at: "2026-10-28T10:00:00Z",
};

function monitoring(over: Record<string, unknown> = {}) {
  return {
    task_id: "task-1",
    name: "Financeiro diário",
    agent_id: "ag-1",
    client_id: "cli-1",
    archived: false,
    current_revision: 1,
    operational_state: "scheduled",
    operational_reason: "agendamento confirmado como ativo pelo agente",
    result_state: "ok",
    reason_code: "result-success",
    detail: "resultado de sucesso recebido para a ocorrência",
    schedule: CONFIG.schedule,
    basis_revision: 1,
    last_execution: {
      id: "ex-1", execution_id: "uuid-ex-1", status: "success",
      started_at_utc: "2026-10-29T05:15:00Z",
      finished_at_utc: "2026-10-29T05:22:00Z",
      received_at: "2026-10-29T05:25:00Z",
    },
    last_success: {
      id: "ex-1", execution_id: "uuid-ex-1", status: "success",
      started_at_utc: "2026-10-29T05:15:00Z",
      finished_at_utc: "2026-10-29T05:22:00Z",
      received_at: "2026-10-29T05:25:00Z",
    },
    expected_occurrence_utc: "2026-10-29T05:15:00Z",
    result_deadline_utc: "2026-10-29T05:45:00Z",
    next_occurrence_utc: "2026-10-30T05:15:00Z",
    received_late: false,
    agent_last_seen_at: "2026-10-29T06:00:00Z",
    computed_at_utc: "2026-10-29T06:05:00Z",
    tolerance_seconds: 1800,
    ...over,
  };
}

function portal(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

function baseRoutes(mon: unknown = monitoring()) {
  return {
    "GET /api/v1/auth/me": () => json(OPERATOR),
    "GET /api/v1/auth/csrf": () => noContent(),
    "GET /api/v1/clients": () => json([CLIENT]),
    "GET /api/v1/agents": () => json([]),
    "GET /api/v1/tasks": () => json([TASK]),
    "GET /api/v1/monitoring/tasks": () =>
      json(Array.isArray(mon) ? mon : [mon]),
    "GET /api/v1/monitoring/tasks/task-1": () => json(mon),
    "GET /api/v1/tasks/task-1": () =>
      json({ ...TASK, revision: REV1 }),
    "GET /api/v1/tasks/task-1/recovery": () => json([]),
    "GET /api/v1/tasks/task-1/revisions": () => json([
      { revision: 1, contract_version: 1, author_kind: "operator",
        author_ref: "anita", created_at: "x", current: true },
    ]),
  };
}

beforeEach(() => {
  clearCookies();
  stubFetch(baseRoutes());
  document.cookie = "macip_csrf=csrf-test; Path=/";
});

describe("lista de tarefas — selo de acompanhamento", () => {
  it("mostra selo 'em dia' para sucesso", async () => {
    portal("/tasks");
    const badge = await screen.findByText("em dia");
    expect(badge).toHaveClass("badge-success");
  });

  it("ausência de resultado é âmbar, nunca o vermelho de falha", async () => {
    stubFetch(baseRoutes(monitoring({
      result_state: "missing",
      reason_code: "overdue",
      detail: "sem resultado recebido após o prazo — não prova falha do backup",
      last_execution: null,
      last_success: null,
    })));
    portal("/tasks");
    const badge = await screen.findByText("sem resultado");
    expect(badge).toHaveClass("badge-missing");
    expect(badge).not.toHaveClass("badge-failed");
    // e a página mostra o prazo objetivo, não um veredito de falha
    expect(screen.getByText(/prazo 29\/10/)).toBeInTheDocument();
  });

  it("'sem histórico' e 'parcial' têm tons próprios", async () => {
    stubFetch(baseRoutes([
      monitoring({ result_state: "no_history", last_execution: null,
                   last_success: null, task_id: "task-1", name: "A" }),
      monitoring({ result_state: "partial", reason_code: "result-partial",
                   task_id: "task-2", name: "B" }),
    ]));
    portal("/tasks");
    expect(await screen.findByText("sem histórico")).toHaveClass("badge-muted");
    expect(screen.getByText("parcial")).toHaveClass("badge-partial");
    // parcial NÃO entra como 'em dia'
    expect(screen.queryByText("em dia")).toBeNull();
  });
});

describe("detalhe — seção de acompanhamento", () => {
  it("mostra estados, última execução, prazo e caveats", async () => {
    portal("/tasks/task-1");
    expect(await screen.findByText("Acompanhamento")).toBeInTheDocument();
    expect(screen.getByText(/agendada —/)).toBeInTheDocument();
    expect(screen.getByText("em dia")).toBeInTheDocument();
    expect(screen.getByText(/Ocorrência esperada/)).toBeInTheDocument();
    expect(screen.getByText(/não prova disponibilidade/)).toBeInTheDocument();
    expect(screen.getByText(/não identifica disparo manual/)).toBeInTheDocument();
  });

  it("falha da API de monitoring é erro de carga, não do backup", async () => {
    stubFetch({
      ...baseRoutes(),
      "GET /api/v1/monitoring/tasks/task-1": () =>
        json({ detail: "banco indisponível" }, 503),
    });
    portal("/tasks/task-1");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/carregar o acompanhamento/);
    expect(alert).toHaveTextContent(/banco indisponível/);
    // a tarefa em si continua visível — erro não é "falha de backup"
    expect(screen.getByText("Financeiro diário")).toBeInTheDocument();
    expect(screen.queryByText(/backup falhou|falha do backup/)).toBeNull();
  });

  it("resposta atrasada de uma visita anterior não contamina a nova", async () => {
    let release!: (r: Response) => void;
    const pending = new Promise<Response>((res) => (release = res));
    stubFetch({
      ...baseRoutes(),
      "GET /api/v1/monitoring/tasks/task-1": () => pending,
    });
    const first = portal("/tasks/task-1");
    first.unmount();
    // segunda montagem com resposta rápida
    stubFetch(baseRoutes(monitoring({
      result_state: "missing", reason_code: "overdue",
      detail: "sem resultado recebido após o prazo",
      last_execution: null, last_success: null,
    })));
    portal("/tasks/task-1");
    expect(await screen.findByText("sem resultado")).toBeInTheDocument();
    // a resposta antiga chega depois — não deve sobrescrever
    release(json(monitoring()));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("sem resultado")).toBeInTheDocument();
  });
});
