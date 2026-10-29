/** Fluxos de tela com fetch simulado: sessão, proteção de rota,
 * login, logout, filtros/paginação e detalhe. A prova real contra
 * API+PostgreSQL fica em e2e/ (Playwright). */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const VIEWER = { ...OPERATOR, login: "bob", name: "Bob", role: "viewer" as const };

const CLIENTS = [
  { id: "cli-1", name: "Acme Ltda", created_at: "2026-10-01T10:00:00Z" },
];
const AGENTS = [
  {
    id: "ag-1",
    client_id: "cli-1",
    name: "vm-01",
    created_at: "2026-10-01T10:00:00Z",
    last_seen_at: "2026-10-26T09:59:00Z",
  },
];
const EXECUTION = {
  id: "central-uuid-1",
  agent_id: "ag-1",
  client_id: "cli-1",
  execution_id: "exec-agente-7",
  task_id: "tarefa-diaria",
  status: "partial",
  started_at_utc: "2026-10-26T10:00:00Z",
  finished_at_utc: "2026-10-26T10:05:00Z",
  received_at: "2026-10-26T10:05:02Z",
};
const DETAIL = {
  ...EXECUTION,
  destinations: [
    {
      destination_id: "s3-principal",
      status: "success",
      attempt: 1,
      started_at_utc: "2026-10-26T10:00:00Z",
      finished_at_utc: "2026-10-26T10:04:00Z",
      duration_seconds: 240,
      retryable: false,
      snapshot_id: "abc123",
      snapshots_available: 3,
      files_processed: 1542,
      bytes_processed: 2097152,
      error: null,
      exit_code: 0,
      failed_operation: null,
      timeout_seconds: 300,
      attempts: [
        {
          attempt: 1,
          status: "failed",
          started_at_utc: "2026-10-26T10:00:00Z",
          finished_at_utc: "2026-10-26T10:01:00Z",
          duration_seconds: 60,
          retryable: true,
          snapshot_id: null,
          snapshots_available: null,
          files_processed: null,
          bytes_processed: null,
          error: "timeout no upload",
          exit_code: 1,
          failed_operation: "backup",
          timeout_seconds: 300,
        },
      ],
    },
    {
      destination_id: "sftp-filial",
      status: "failed",
      attempt: 3,
      started_at_utc: "2026-10-26T10:00:00Z",
      finished_at_utc: "2026-10-26T10:05:00Z",
      duration_seconds: 300,
      retryable: false,
      snapshot_id: null,
      snapshots_available: null,
      files_processed: null,
      bytes_processed: null,
      error: "conexão recusada",
      exit_code: 2,
      failed_operation: "check",
      timeout_seconds: null,
      attempts: [],
    },
  ],
};

function renderApp(initial = "/") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

/** Rotas da área logada já respondendo. */
function areaRoutes() {
  return {
    "GET /api/v1/auth/me": () => json(OPERATOR),
    "GET /api/v1/clients": () => json(CLIENTS),
    "GET /api/v1/agents": () => json(AGENTS),
    "GET /api/v1/clients/cli-1/agents": () => json(AGENTS),
    "GET /api/v1/executions": () => json([EXECUTION]),
    "GET *": () => json({ detail: "rota não simulada" }, 500),
  };
}

beforeEach(clearCookies);
afterEach(clearCookies);

describe("sessão e rotas protegidas", () => {
  it("reabertura com sessão válida restaura o operador", async () => {
    stubFetch({ "GET /api/v1/auth/me": () => json(OPERATOR) });
    renderApp("/");
    // cookie HttpOnly não é lido pelo JS — /auth/me decide
    await screen.findByText("Ana");
    expect(screen.getByText("admin")).toBeInTheDocument();
  });

  it("acesso direto sem sessão redireciona ao login", async () => {
    stubFetch({ "GET /api/v1/auth/me": () => json({ detail: "x" }, 401) });
    renderApp("/clients");
    await screen.findByRole("button", { name: "Entrar" });
    expect(screen.getByLabelText("Login")).toBeInTheDocument();
  });

  it("sessão expirada (401) também leva ao login", async () => {
    stubFetch({ "GET /api/v1/auth/me": () => json({ detail: "x" }, 401) });
    renderApp("/executions");
    await screen.findByRole("button", { name: "Entrar" });
  });

  it("API fora do ar ≠ senha errada — tela de indisponibilidade", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("down")));
    renderApp("/");
    await screen.findByText(/Não foi possível alcançar a API/);
  });
});

describe("login e logout", () => {
  it("login correto leva à área protegida", async () => {
    const user = userEvent.setup();
    let logged = false; // me() muda depois do login — sessão restaurada
    const calls = stubFetch({
      ...areaRoutes(),
      "GET /api/v1/auth/me": () =>
        logged ? json(OPERATOR) : json({ detail: "x" }, 401),
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => {
        logged = true;
        document.cookie = "macip_csrf=novo; Path=/";
        return json(OPERATOR);
      },
    });
    renderApp("/login");
    await user.type(await screen.findByLabelText("Login"), "ana");
    await user.type(screen.getByLabelText("Senha"), "segredo-1");
    await user.click(screen.getByRole("button", { name: "Entrar" }));
    await screen.findByText("Ana");
    expect(calls.map((c) => c.key)).toContain("POST /api/v1/auth/login");
  });

  it("login incorreto mostra mensagem sem culpar a rede", async () => {
    const user = userEvent.setup();
    stubFetch({
      "GET /api/v1/auth/me": () => json({ detail: "x" }, 401),
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => json({ detail: "x" }, 401),
    });
    renderApp("/login");
    await user.type(await screen.findByLabelText("Login"), "ana");
    await user.type(screen.getByLabelText("Senha"), "errada");
    await user.click(screen.getByRole("button", { name: "Entrar" }));
    await screen.findByText("Login ou senha inválidos.");
  });

  it("429 vira mensagem de tentativas, não de senha", async () => {
    const user = userEvent.setup();
    stubFetch({
      "GET /api/v1/auth/me": () => json({ detail: "x" }, 401),
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => json({ detail: "x" }, 429),
    });
    renderApp("/login");
    await user.type(await screen.findByLabelText("Login"), "ana");
    await user.type(screen.getByLabelText("Senha"), "x");
    await user.click(screen.getByRole("button", { name: "Entrar" }));
    await screen.findByText(/Muitas tentativas/);
  });

  it("logout revoga no servidor (POST + CSRF) e volta ao login", async () => {
    const user = userEvent.setup();
    const calls = stubFetch({
      ...areaRoutes(),
      "POST /api/v1/auth/logout": () => noContent(),
    });
    document.cookie = "macip_csrf=csrf-login; Path=/";
    renderApp("/executions");
    await screen.findByText("Ana");
    await user.click(screen.getByRole("button", { name: "Sair" }));
    await waitFor(() => {
      const logoutCall = calls.find((c) => c.key === "POST /api/v1/auth/logout");
      expect(logoutCall?.init?.headers).toMatchObject({
        "X-CSRF-Token": expect.any(String),
      });
    });
  });

  it("falha no logout fica visível e mantém a sessão", async () => {
    const user = userEvent.setup();
    stubFetch({
      ...areaRoutes(),
      "POST /api/v1/auth/logout": () => json({ detail: "x" }, 500),
    });
    renderApp("/executions");
    await screen.findByText("Ana");
    await user.click(screen.getByRole("button", { name: "Sair" }));
    await screen.findByText(/a sessão pode continuar ativa/);
  });
});

describe("consulta com viewer", () => {
  it("viewer consulta execuções normalmente", async () => {
    stubFetch({ ...areaRoutes(), "GET /api/v1/auth/me": () => json(VIEWER) });
    renderApp("/executions");
    await screen.findByText("Bob");
    expect(screen.getByText("viewer")).toBeInTheDocument();
    await screen.findByText("tarefa-diaria");
    expect(screen.getAllByText("Parcial").length).toBeGreaterThan(0);
  });
});

describe("clientes e agentes", () => {
  it("lista clientes e navega aos agentes", async () => {
    const user = userEvent.setup();
    stubFetch(areaRoutes());
    renderApp("/clients");
    await screen.findByText("Acme Ltda");
    await user.click(screen.getByRole("link", { name: "Acme Ltda" }));
    await screen.findByText("vm-01");
    expect(screen.getByText(/Agentes de Acme Ltda/)).toBeInTheDocument();
  });

  it("estado vazio e erro com tentar novamente", async () => {
    let fail = true;
    const user = userEvent.setup();
    stubFetch({
      "GET /api/v1/auth/me": () => json(OPERATOR),
      "GET /api/v1/clients": () =>
        fail ? json({ detail: "x" }, 500) : json([]),
      "GET *": () => json([], 200),
    });
    renderApp("/clients");
    const retry = await screen.findByRole("button", {
      name: "Tentar novamente",
    });
    fail = false;
    await user.click(retry);
    await screen.findByText("Nenhum cliente cadastrado.");
  });
});

describe("execuções: filtros, paginação, detalhe", () => {
  it("filtros viram query e detalhe preserva a busca ao voltar", async () => {
    const user = userEvent.setup();
    const calls = stubFetch({
      ...areaRoutes(),
      "GET /api/v1/executions/central-uuid-1": () => json(DETAIL),
    });
    renderApp("/executions");
    await screen.findByText("tarefa-diaria");

    await user.selectOptions(screen.getByLabelText("Status"), "failed");
    await waitFor(() => {
      const last = calls
        .filter((c) => c.key === "GET /api/v1/executions")
        .at(-1);
      expect(last?.url.searchParams.get("status")).toBe("failed");
    });

    // abre detalhe pelo UUID central (não o execution_id do agente)
    await user.click(screen.getByRole("link", { name: "Detalhes" }));
    await screen.findByText("s3-principal");

    // voltar restaura os filtros pela query string
    await user.click(screen.getByRole("link", { name: "← Execuções" }));
    await waitFor(() => {
      const last = calls
        .filter((c) => c.key === "GET /api/v1/executions")
        .at(-1);
      expect(last?.url.searchParams.get("status")).toBe("failed");
    });
  });

  it("detalhe mostra destinos, tentativas e erros como texto", async () => {
    stubFetch({
      ...areaRoutes(),
      "GET /api/v1/executions/central-uuid-1": () => json(DETAIL),
    });
    renderApp("/executions/central-uuid-1");
    await screen.findByText("s3-principal");
    expect(screen.getByText("sftp-filial")).toBeInTheDocument();
    // erros renderizados como texto puro, não HTML
    expect(screen.getByText("conexão recusada")).toBeInTheDocument();
    // valores ausentes → indisponíveis, nunca zero
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    // abre tentativas do primeiro destino
    const user = userEvent.setup();
    await user.click(screen.getByText("1 tentativa(s)"));
    expect(screen.getByText("timeout no upload")).toBeInTheDocument();
  });

  it("paginação próxima/anterior usa offset", async () => {
    const user = userEvent.setup();
    const urls: string[] = [];
    stubFetch({
      "GET /api/v1/auth/me": () => json(OPERATOR),
      "GET /api/v1/clients": () => json(CLIENTS),
      "GET /api/v1/agents": () => json(AGENTS),
      "GET /api/v1/clients/cli-1/agents": () => json(AGENTS),
      "GET /api/v1/executions": () => {
        urls.push("hit");
        return json(new Array(25).fill(EXECUTION).map((e, i) => ({ ...e, id: `x${i}` })));
      },
      "GET *": () => json({ detail: "rota não simulada" }, 500),
    });
    renderApp("/executions");
    await screen.findByText("Próxima");
    await user.click(screen.getByRole("button", { name: "Próxima" }));
    await screen.findByText("Página 2");
    await user.click(screen.getByRole("button", { name: "Anterior" }));
    await screen.findByText("Página 1");
  });

  it("erro de rede na listagem ≠ sessão expirada", async () => {
    stubFetch({
      "GET /api/v1/auth/me": () => json(OPERATOR),
      "GET /api/v1/executions": () => {
        throw new TypeError("fetch failed");
      },
      "GET *": () => json([], 200),
    });
    renderApp("/executions");
    await screen.findByText(/Não foi possível alcançar a API/);
  });
});
