/** Cadastro de tarefas com fetch simulado: lista/filtros/paginação,
 * criação com contexto, edição com concorrência otimista e 409
 * preservando valores, arquivamento com confirmação, exportar por
 * link, viewer read-only e pendência que não bloqueia navegação. */

import { beforeEach, describe, expect, it } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
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

const CLIENT = { id: "cli-1", name: "Acme Ltda", created_at: "2026-10-01T10:00:00Z" };
const AGENT = {
  id: "ag-1", client_id: "cli-1", name: "vm-01",
  created_at: "2026-10-01T10:00:00Z", last_seen_at: null,
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
    sources: [{ path: "/dados/financeiro" }],
    exclusions: ["*.tmp"],
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
const REV2 = {
  ...REV1, revision: 2,
  config: { ...CONFIG, name: "Financeiro diário v2" },
};

const TASK_DETAIL = { ...TASK, revision: REV1 };
const TASK_DETAIL_V2 = {
  ...TASK, name: "Financeiro diário v2",
  current_revision: 2, revision: REV2,
};

function portal(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

function baseRoutes(op: typeof OPERATOR | typeof VIEWER = OPERATOR) {
  return {
    "GET /api/v1/auth/me": () => json(op),
    "GET /api/v1/auth/csrf": () => noContent(),
    "GET /api/v1/clients": () => json([CLIENT]),
    "GET /api/v1/agents": () => json([AGENT]),
    "GET /api/v1/clients/cli-1/agents": () => json([AGENT]),
    "GET /api/v1/tasks": () => json([TASK]),
    "GET /api/v1/tasks/task-1": () => json(TASK_DETAIL),
    "GET /api/v1/tasks/task-1/revisions": () => json([
      { revision: 1, contract_version: 1, author_kind: "operator",
        author_ref: "anita", created_at: "x", current: true },
    ]),
  };
}

let calls: ReturnType<typeof stubFetch>;
beforeEach(() => {
  clearCookies();
  calls = stubFetch(baseRoutes());
  document.cookie = "macip_csrf=csrf-test; Path=/";
});

describe("lista de tarefas", () => {
  it("lista com nome, cliente, revisão e aviso de não distribuída", async () => {
    portal("/tasks");
    expect(await screen.findByText("Financeiro diário")).toBeInTheDocument();
    expect(screen.getByText(/não\s*distribuídas/)).toBeInTheDocument();
    expect(screen.getByText(/revisão 1/)).toBeInTheDocument();
    expect(screen.queryByText(/aplicada|agendada no dispositivo/)).toBeNull();
  });

  it("filtros viram query e estado vazio é explícito", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/tasks": () => json([]),
    });
    portal("/tasks?client_id=cli-1&archived=true");
    await screen.findByText("Nenhuma tarefa neste filtro.");
    const call = calls.find((c) => c.key === "GET /api/v1/tasks");
    expect(call?.url.searchParams.get("client_id")).toBe("cli-1");
    expect(call?.url.searchParams.get("archived")).toBe("true");
  });
});

describe("criação", () => {
  it("identifica cliente e agente e cadastra com CSRF", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/agents/ag-1/tasks": () => json(TASK_DETAIL, 201),
    });
    portal("/tasks/new?agent=ag-1");
    expect(await screen.findByText(/vm-01/)).toBeInTheDocument();
    await userEvent.type(
      screen.getByLabelText("Nome da tarefa"), "Backups gerais",
    );
    await userEvent.type(
      screen.getByLabelText("Caminho da origem 1"), "/srv/dados",
    );
    await userEvent.type(
      screen.getByLabelText("Identificador do destino 1"), "local",
    );
    await userEvent.type(
      screen.getByLabelText("Nome do destino 1"), "Disco local",
    );
    await userEvent.type(
      screen.getByLabelText("Repositório do destino 1"), "/srv/backup",
    );
    await userEvent.type(
      screen.getByLabelText("password_environment do destino 1"),
      "MACIP_RESTIC",
    );
    await userEvent.clear(screen.getByLabelText("Hora"));
    await userEvent.type(screen.getByLabelText("Hora"), "3");
    await userEvent.clear(screen.getByLabelText("Minuto"));
    await userEvent.type(screen.getByLabelText("Minuto"), "30");
    await userEvent.click(
      screen.getByRole("button", { name: "Cadastrar tarefa" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Tarefa" }))
        .toBeInTheDocument(),
    );
    const post = calls.find(
      (c) => c.key === "POST /api/v1/agents/ag-1/tasks",
    );
    expect(post?.init?.headers).toMatchObject({
      "X-CSRF-Token": "csrf-test",
    });
    const body = JSON.parse(String(post?.init?.body));
    expect(body.spec.destinations[0].kind).toBe("local");
    expect(body.spec.destinations[0].password_environment).toBe("MACIP_RESTIC");
    expect(body.schedule).toMatchObject({
      kind: "daily", timezone: "America/Sao_Paulo", hour: 3, minute: 30,
    });
    // a tela segue explicando o estágio — nada de "distribuída"
    expect(document.body.textContent ?? "").toMatch(/não distribuída/);
  });
});

describe("detalhe, edição e 409", () => {
  it("mostra contexto, revisão atual e link de exportação", async () => {
    portal("/tasks/task-1");
    await screen.findByText("Financeiro diário");
    expect(screen.getByText(/não distribuída/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "baixar tarefa.json" });
    expect(link).toHaveAttribute(
      "href", "/api/v1/tasks/task-1/revisions/1/export",
    );
  });

  it("409 na edição preserva os valores e explica a revisão nova", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/tasks/task-1/revisions": () =>
        json({ detail: "a tarefa já está na revisão 2" }, 409),
    });
    portal("/tasks/task-1");
    await screen.findByText("Financeiro diário");
    await userEvent.click(
      screen.getByRole("button", { name: "Editar configuração" }),
    );
    const nameInput = screen.getByLabelText("Nome da tarefa");
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "Nome digitado pela operadora");
    await userEvent.click(
      screen.getByRole("button", { name: "Salvar nova revisão" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/revisão 2/);
    // o campo preserva o que foi digitado — nada de sobrescrever
    expect(screen.getByLabelText("Nome da tarefa"))
      .toHaveValue("Nome digitado pela operadora");
  });

  it("edição válida envia expected_revision e cria a revisão 2", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/tasks/task-1/revisions": () => json(TASK_DETAIL_V2, 201),
    });
    portal("/tasks/task-1");
    await screen.findByText("Financeiro diário");
    await userEvent.click(
      screen.getByRole("button", { name: "Editar configuração" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Salvar nova revisão" }),
    );
    await waitFor(() => {
      const post = calls.find(
        (c) => c.key === "POST /api/v1/tasks/task-1/revisions",
      );
      expect(post).toBeTruthy();
      expect(JSON.parse(String(post?.init?.body)).expected_revision).toBe(1);
    });
  });
});

describe("arquivamento", () => {
  it("confirma, explica o alcance e marca a tarefa", async () => {
    portal("/tasks/task-1");
    await screen.findByText("Financeiro diário");
    await userEvent.click(
      screen.getByRole("button", { name: "Arquivar tarefa" }),
    );
    expect(screen.getByText(/não interrompe/)).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Confirmar arquivamento" }),
    );
    await waitFor(() =>
      expect(
        calls.find((c) => c.key === "POST /api/v1/tasks/task-1/archive"),
      ).toBeTruthy(),
    );
  });
});

describe("viewer", () => {
  it("consulta tarefa e revisões sem ações de escrita", async () => {
    calls = stubFetch(baseRoutes(VIEWER));
    portal("/tasks/task-1");
    await screen.findByText("Financeiro diário");
    expect(screen.getByRole("link", { name: "baixar tarefa.json" }))
      .toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Editar configuração" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Arquivar tarefa" }),
    ).toBeNull();
  });
});

describe("pendência e respostas atrasadas", () => {
  it("resposta atrasada da tarefa A não sobrescreve a tarefa B", async () => {
    let releaseA!: (r: Response) => void;
    const gateA = new Promise<Response>((res) => (releaseA = res));
    const taskB = { ...TASK, id: "task-2", name: "Tarefa B" };
    const detailB = {
      ...TASK_DETAIL, id: "task-2", name: "Tarefa B",
      revision: { ...REV1, config: { ...CONFIG, name: "Tarefa B" } },
    };
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/tasks": () => json([TASK, taskB]),
      "GET /api/v1/tasks/task-1": () => gateA,
      "GET /api/v1/tasks/task-1/revisions": () => json([]),
      "GET /api/v1/tasks/task-2": () => json(detailB),
      "GET /api/v1/tasks/task-2/revisions": () => json([
        { revision: 1, contract_version: 1, author_kind: "admin",
          author_ref: "bearer", created_at: "x", current: true },
      ]),
    });
    portal("/tasks");
    // abre A → PENDENTE → volta → abre B (mesma árvore)
    await userEvent.click(
      await screen.findByRole("link", { name: "Financeiro diário" }),
    );
    await userEvent.click(
      await screen.findByRole("link", { name: "← Tarefas" }),
    );
    await userEvent.click(
      await screen.findByRole("link", { name: "Tarefa B" }),
    );
    await screen.findByText(/automação/);
    // libera A — a tela continua sendo B
    await act(async () => releaseA(json(TASK_DETAIL)));
    expect(document.body.textContent ?? "").toContain("Tarefa B");
    expect(document.body.textContent ?? "").toContain("automação");
  });
});
