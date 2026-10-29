/** Cadastramento e credenciais com fetch simulado: vínculo correto,
 * validação/conflito/rede, bloqueio de envio duplicado, viewer
 * read-only, CSRF nas escritas, token só na apresentação temporária
 * (fora de storage/URL/erros), limpeza ao fechar/trocar e revogação. */

import { afterEach, describe, expect, it, vi } from "vitest";
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
  id: "ag-1",
  client_id: "cli-1",
  name: "vm-01",
  created_at: "2026-10-01T10:00:00Z",
  last_seen_at: "2026-10-26T09:59:00Z",
};
const CRED = {
  id: "cred-1",
  agent_id: "ag-1",
  label: "prod",
  created_at: "2026-10-26T10:00:00Z",
  revoked_at: null,
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
    "GET /api/v1/agents/ag-1/credentials": () => json([CRED]),
  };
}

let calls: ReturnType<typeof stubFetch>;
beforeEach(() => {
  document.cookie = "macip_csrf=csrf-test; Path=/";
});
afterEach(() => {
  vi.unstubAllGlobals();
  clearCookies();
});

describe("cadastro de cliente", () => {
  it("cria cliente e mostra link para abri-lo mesmo fora da página", async () => {
    const created = { id: "cli-new", name: "Zorba", created_at: "2026-10-28T00:00:00Z" };
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients": () => json(created, 201),
    });
    portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    await userEvent.type(screen.getByLabelText("Nome do cliente"), "Zorba");
    await userEvent.click(screen.getByRole("button", { name: "Cadastrar" }));
    const link = await screen.findByRole("link", { name: /Abrir o cliente/ });
    expect(link).toHaveAttribute("href", "/clients/cli-new");
    const post = calls.find((c) => c.key === "POST /api/v1/clients");
    expect(post?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
    expect(JSON.parse(String(post?.init?.body)).name).toBe("Zorba");
  });

  it("conflito 409 mostra o erro do servidor", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients": () => json({ detail: "cliente já existe" }, 409),
    });
    portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    await userEvent.type(screen.getByLabelText("Nome do cliente"), "Acme Ltda");
    await userEvent.click(screen.getByRole("button", { name: "Cadastrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("cliente já existe");
  });

  it("bloqueia submissão duplicada enquanto pendente", async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((res) => (release = res));
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients": () => gate,
    });
    portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    const input = screen.getByLabelText("Nome do cliente");
    await userEvent.type(input, "Zorba");
    const btn = screen.getByRole("button", { name: "Cadastrar" });
    await userEvent.click(btn);
    await userEvent.click(screen.getByRole("button", { name: "Cadastrando…" }));
    release(json({ id: "c2", name: "Zorba", created_at: "x" }, 201));
    await screen.findByText(/criado/);
    expect(calls.filter((c) => c.key === "POST /api/v1/clients")).toHaveLength(1);
  });

  it("viewer não vê ações de escrita", async () => {
    calls = stubFetch(baseRoutes(VIEWER));
    portal("/clients");
    await screen.findByText("Acme Ltda");
    expect(screen.queryByRole("button", { name: "Novo cliente" })).toBeNull();
  });
});

describe("cadastro de agente", () => {
  it("cria agente vinculado ao cliente exibido", async () => {
    const created = {
      id: "ag-2", client_id: "cli-1", name: "vm-02",
      created_at: "2026-10-28T00:00:00Z", last_seen_at: null,
    };
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients/cli-1/agents": () => json(created, 201),
    });
    portal("/clients/cli-1");
    await screen.findByText(/Acme Ltda/);
    await userEvent.click(screen.getByRole("button", { name: "Novo agente" }));
    expect(screen.getByText(/cliente/)).toHaveTextContent("Acme Ltda");
    await userEvent.type(screen.getByLabelText("Nome do agente"), "vm-02");
    await userEvent.click(screen.getByRole("button", { name: "Cadastrar" }));
    await screen.findByText(/vinculado/);
    const post = calls.find((c) => c.key === "POST /api/v1/clients/cli-1/agents");
    expect(post?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
  });

  it("viewer não vê 'Novo agente' nem 'Emitir'", async () => {
    calls = stubFetch(baseRoutes(VIEWER));
    portal("/clients/cli-1");
    await screen.findByText("vm-01");
    expect(screen.queryByRole("button", { name: "Novo agente" })).toBeNull();
    portal("/agents/ag-1");
    await screen.findByText("prod");
    expect(screen.queryByRole("button", { name: "Emitir credencial" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revogar" })).toBeNull();
  });
});

describe("emissão de credencial", () => {
  const ISSUED = { credential_id: "cred-9", agent_id: "ag-1", token: "segredo-xyz-789" };

  function routes(overrides = {}) {
    return {
      ...baseRoutes(),
      "POST /api/v1/agents/ag-1/credentials": () => json(ISSUED, 201),
      ...overrides,
    };
  }

  it("emite e mostra token só na apresentação temporária", async () => {
    calls = stubFetch(routes());
    portal("/agents/ag-1");
    await screen.findByText("prod");
    await userEvent.type(
      screen.getByLabelText("Descrição da credencial"),
      "srv-novo",
    );
    await userEvent.click(screen.getByRole("button", { name: "Emitir credencial" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("segredo-xyz-789");
    expect(dialog).toHaveTextContent(/não poderá ser consultado novamente/);
    // fora do diálogo, o token não aparece
    expect(document.body.textContent?.split("segredo-xyz-789").length).toBe(2);
    const post = calls.find((c) => c.key === "POST /api/v1/agents/ag-1/credentials");
    expect(post?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
    expect(JSON.parse(String(post?.init?.body)).label).toBe("srv-novo");
  });

  it("copia por gesto explícito e informa falha", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("clipboard off"));
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    calls = stubFetch(routes());
    portal("/agents/ag-1");
    await screen.findByText("prod");
    await userEvent.click(screen.getByRole("button", { name: "Emitir credencial" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Copiar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Não foi possível copiar/);
    expect(writeText).toHaveBeenCalledWith("segredo-xyz-789");
  });

  it("fecha a apresentação e o token some", async () => {
    calls = stubFetch(routes());
    portal("/agents/ag-1");
    await screen.findByText("prod");
    await userEvent.click(screen.getByRole("button", { name: "Emitir credencial" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Fechar" }));
    await waitFor(() =>
      expect(document.body.textContent ?? "").not.toContain("segredo-xyz-789"),
    );
  });

  it("trocar de agente limpa o token", async () => {
    calls = stubFetch({
      ...routes(),
      "GET /api/v1/agents/ag-2/credentials": () => json([]),
    });
    const { unmount } = portal("/agents/ag-1");
    await screen.findByText("prod");
    await userEvent.click(screen.getByRole("button", { name: "Emitir credencial" }));
    await screen.findByRole("dialog");
    unmount();
    portal("/agents/ag-2");
    await screen.findByText("Nenhuma credencial emitida.");
    expect(document.body.textContent ?? "").not.toContain("segredo-xyz-789");
  });

  it("falha de rede na emissão orienta conferir listagem", async () => {
    calls = stubFetch(
      routes({
        "POST /api/v1/agents/ag-1/credentials": () => Promise.reject(new TypeError("fetch")),
      }),
    );
    portal("/agents/ag-1");
    await screen.findByText("prod");
    await userEvent.click(screen.getByRole("button", { name: "Emitir credencial" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /consulte a listagem antes de emitir/,
    );
  });
});

describe("revogação", () => {
  it("confirma, revoga e atualiza o estado", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/credentials/cred-1/revoke": () => json({ id: "cred-1", revoked: true }),
    });
    portal("/agents/ag-1");
    await screen.findByText("Ativa");
    await userEvent.click(screen.getByRole("button", { name: "Revogar" }));
    expect(screen.getByText(/Revogar.*do agente/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirmar" }));
    await screen.findByText(/Revogada em/);
    const post = calls.find((c) => c.key === "POST /api/v1/credentials/cred-1/revoke");
    expect(post?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
    expect(screen.queryByRole("button", { name: "Revogar" })).toBeNull();
  });

  it("falha na revogação fica visível", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/credentials/cred-1/revoke": () => new Response(null, { status: 500 }),
    });
    portal("/agents/ag-1");
    await screen.findByText("Ativa");
    await userEvent.click(screen.getByRole("button", { name: "Revogar" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirmar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Erro interno da API/);
    expect(screen.getByText("Ativa")).toBeInTheDocument();
  });

  it("credencial já revogada não mostra ação", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/agents/ag-1/credentials": () =>
        json([{ ...CRED, revoked_at: "2026-10-27T10:00:00Z" }]),
    });
    portal("/agents/ag-1");
    await screen.findByText(/Revogada em/);
    expect(screen.queryByRole("button", { name: "Revogar" })).toBeNull();
  });
});

describe("respostas atrasadas", () => {
  it("POST de cliente antigo não reaparece após nova abertura do formulário", async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((res) => (release = res));
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients": () => gate,
    });
    const { unmount } = portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    await userEvent.type(screen.getByLabelText("Nome do cliente"), "Fantasma");
    await userEvent.click(screen.getByRole("button", { name: "Cadastrar" }));
    unmount(); // saiu da tela antes da resposta
    await act(async () => release(json({ id: "x", name: "Fantasma", created_at: "y" }, 201)));
    expect(document.body.textContent ?? "").not.toContain("Fantasma");
  });
});
