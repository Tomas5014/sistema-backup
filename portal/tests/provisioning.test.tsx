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
const PREP = {
  client_id: "cli-1",
  installation: {
    id: "ag-1",
    identifier: "pc-financeiro-01",
    machine_name: "PC-FINANCEIRO",
    platform: "windows",
    suggested_time: "22:00",
  },
  state: "pending",
  step: "queued",
  protection_state: "pending",
  last_protected_copy: null,
  repository_id: null,
  error_code: null,
  profile_available: false,
  created_at: "2026-10-26T10:00:00Z",
  updated_at: "2026-10-26T10:00:00Z",
  operation: {
    id: "op-1",
    kind: "prepare_primary",
    attempt: 1,
    state: "pending",
    step: "queued",
    error_code: null,
    created_at: "2026-10-26T10:00:00Z",
    started_at: null,
    finished_at: null,
  },
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
    "GET /api/v1/clients/cli-1/preparations": () => json([]),
    "GET /api/v1/agents/ag-1/credentials": () => json([CRED]),
  };
}

let calls: ReturnType<typeof stubFetch>;
beforeEach(() => {
  document.cookie = "macip_csrf=csrf-test; Path=/";
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearCookies();
});

describe("cadastro de cliente", () => {
  it("cadastra cliente e instalação e inicia preparação", async () => {
    const created = {
      ...PREP,
      client_id: "cli-new",
      installation: { ...PREP.installation, id: "ag-new", machine_name: "PC-ZORBA" },
    };
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients/prepare": () => json(created, 202),
    });
    portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    await userEvent.type(screen.getByLabelText("Nome do cliente"), "Zorba");
    await userEvent.type(screen.getByLabelText("Identificador da instalação"), "pc-zorba-01");
    await userEvent.type(screen.getByLabelText("Máquina"), "PC-ZORBA");
    await userEvent.click(screen.getByRole("button", { name: "Preparar instalação" }));
    const link = await screen.findByRole("link", { name: /Acompanhar preparação/ });
    expect(link).toHaveAttribute("href", "/clients/cli-new");
    const post = calls.find((c) => c.key === "POST /api/v1/clients/prepare");
    expect(post?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
    expect(JSON.parse(String(post?.init?.body))).toMatchObject({
      client_name: "Zorba",
      installation_identifier: "pc-zorba-01",
      machine_name: "PC-ZORBA",
      suggested_time: "22:00",
    });
  });

  it("conflito 409 mostra o erro do servidor", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients/prepare": () => json({ detail: "instalação já existe" }, 409),
    });
    portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    await userEvent.type(screen.getByLabelText("Nome do cliente"), "Acme Ltda");
    await userEvent.type(screen.getByLabelText("Identificador da instalação"), "pc-acme-01");
    await userEvent.type(screen.getByLabelText("Máquina"), "PC-ACME");
    await userEvent.click(screen.getByRole("button", { name: "Preparar instalação" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("instalação já existe");
  });

  it("bloqueia submissão duplicada enquanto pendente", async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((res) => (release = res));
    calls = stubFetch({
      ...baseRoutes(),
      "POST /api/v1/clients/prepare": () => gate,
    });
    portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    const input = screen.getByLabelText("Nome do cliente");
    await userEvent.type(input, "Zorba");
    await userEvent.type(screen.getByLabelText("Identificador da instalação"), "pc-zorba-01");
    await userEvent.type(screen.getByLabelText("Máquina"), "PC-ZORBA");
    const btn = screen.getByRole("button", { name: "Preparar instalação" });
    await userEvent.click(btn);
    await userEvent.click(screen.getByRole("button", { name: "Preparando solicitação…" }));
    release(json({ ...PREP, client_id: "c2" }, 202));
    await screen.findByText(/Preparação registrada/);
    expect(calls.filter((c) => c.key === "POST /api/v1/clients/prepare")).toHaveLength(1);
  });

  it("viewer não vê ações de escrita", async () => {
    calls = stubFetch(baseRoutes(VIEWER));
    portal("/clients");
    await screen.findByText("Acme Ltda");
    expect(screen.queryByRole("button", { name: "Novo cliente" })).toBeNull();
  });
});

describe("preparação da instalação", () => {
  it("emite código com CSRF, oculta e revoga sem storage", async () => {
    const activationCode = "https://central.lab#macip-activation=" + "a".repeat(43);
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/clients/cli-1/preparations": () => json([{
        ...PREP, state: "primary_ready", activation_available: true, profile_available: true,
      }]),
      "POST /api/v1/activation-codes": () => json({
        id: "code-1", activation_code: activationCode, expires_at: "2026-11-18T12:00:00Z",
      }, 201),
      "POST /api/v1/activation-codes/code-1/revoke": () => json({ state: "revoked" }),
    });
    portal("/clients/cli-1");
    await userEvent.click(await screen.findByRole("button", { name: "Gerar código de ativação" }));
    expect(await screen.findByLabelText("Código de ativação")).toHaveValue(activationCode);
    const post = calls.find((call) => call.key === "POST /api/v1/activation-codes");
    expect(post?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "Revogar código" }));
    await waitFor(() => expect(screen.queryByLabelText("Código de ativação")).toBeNull());
    expect(calls.find((call) => call.key.endsWith("/revoke"))?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
  });

  it("separa primário pronto da cópia protegida e bloqueia perfil", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/clients/cli-1/preparations": () => json([
        {
          ...PREP,
          state: "primary_ready",
          step: "verified",
          repository_id: "a".repeat(64),
          operation: { ...PREP.operation, state: "succeeded", step: "verified" },
        },
      ]),
    });
    portal("/clients/cli-1");
    expect(await screen.findByText("Estrutura primária pronta")).toBeVisible();
    expect(screen.getByText("Preparação pendente")).toBeVisible();
    expect(screen.getByRole("button", { name: "Baixar perfil" })).toBeDisabled();
    expect(calls.some((call) => call.key.includes("/profile"))).toBe(false);
  });

  it.each([
    ["prepared", false, OPERATOR],
    ["proven", false, OPERATOR],
    ["proven", true, VIEWER],
    ["proven", true, OPERATOR],
  ])("usa o estado de B e a permissão da API para download (%s, %s, %j)", async (state, available, operator) => {
    const profile = { nome: "Cliente fictício", repositorio: "sftp://user@backup.lab//repository" };
    calls = stubFetch({
      ...baseRoutes(operator),
      "GET /api/v1/clients/cli-1/preparations": () => json([{
        ...PREP, state: "primary_ready", protection_state: state, profile_available: available,
        last_protected_copy: {
          snapshot_a: "a".repeat(64), snapshot_b: "b".repeat(64),
          confirmed_at: "2026-10-26T12:00:00Z",
        },
      }]),
      "GET /api/v1/installations/ag-1/profile": () => json(profile),
    });
    portal("/clients/cli-1");
    expect(await screen.findByText("Última cópia confirmada")).toBeVisible();
    expect(screen.getByText("aaaaaaaa")).toBeVisible();
    expect(screen.getByText("bbbbbbbb")).toBeVisible();
    const button = screen.getByRole("button", { name: "Baixar perfil" });
    if (!available || operator.role === "viewer") {
      expect(button).toBeDisabled();
      expect(calls.some((call) => call.key.endsWith("/profile"))).toBe(false);
      return;
    }
    const create = vi.fn(() => "blob:perfil");
    const OriginalURL = URL;
    vi.stubGlobal("URL", class extends OriginalURL {
      static createObjectURL = create;
      static revokeObjectURL = vi.fn();
    });
    let filename: string | undefined;
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      filename = this.download;
    });
    await userEvent.click(button);
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(create).toHaveBeenCalledOnce();
    expect(filename).toBe("perfil-macip.json");
    expect(calls.some((call) => call.key === "GET /api/v1/installations/ag-1/profile")).toBe(true);
  });

  it("mostra erro sanitizado e admin tenta novamente com CSRF", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/clients/cli-1/preparations": () => json([
        { ...PREP, state: "failed", error_code: "repository_mismatch", operation: { ...PREP.operation, state: "failed" } },
      ]),
      "POST /api/v1/installations/ag-1/preparation/retry": () => json(PREP, 202),
    });
    portal("/clients/cli-1");
    expect(await screen.findByText(/outra identidade/)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
    await waitFor(() => expect(calls.some((call) => call.key === "POST /api/v1/installations/ag-1/preparation/retry")).toBe(true));
    const post = calls.find((call) => call.key === "POST /api/v1/installations/ag-1/preparation/retry");
    expect(post?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test" });
  });

  it("viewer acompanha, mas não vê retry", async () => {
    calls = stubFetch({
      ...baseRoutes(VIEWER),
      "GET /api/v1/clients/cli-1/preparations": () => json([
        { ...PREP, state: "failed", error_code: "access_denied", operation: { ...PREP.operation, state: "failed" } },
      ]),
    });
    portal("/clients/cli-1");
    expect(await screen.findByText(/não conseguiu acessar/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Tentar novamente" })).toBeNull();
    expect(screen.getByRole("button", { name: "Baixar perfil" })).toBeDisabled();
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
      "POST /api/v1/clients/prepare": () => gate,
    });
    const { unmount } = portal("/clients");
    await userEvent.click(await screen.findByRole("button", { name: "Novo cliente" }));
    await userEvent.type(screen.getByLabelText("Nome do cliente"), "Fantasma");
    await userEvent.type(screen.getByLabelText("Identificador da instalação"), "pc-fantasma-01");
    await userEvent.type(screen.getByLabelText("Máquina"), "PC-FANTASMA");
    await userEvent.click(screen.getByRole("button", { name: "Preparar instalação" }));
    unmount(); // saiu da tela antes da resposta
    await act(async () => release(json({ ...PREP, client_id: "x" }, 202)));
    expect(document.body.textContent ?? "").not.toContain("Fantasma");
  });
});

describe("cadastro pendente entre clientes", () => {
  it("POST pendente do cliente A não altera a tela do cliente B", async () => {
    const clientB = { id: "cli-2", name: "Cliente B", created_at: "2026-10-02T10:00:00Z" };
    const agentB = { ...AGENT, id: "ag-b", client_id: "cli-2", name: "vm-b" };
    let releasePostA!: (r: Response) => void;
    const gatePostA = new Promise<Response>((res) => (releasePostA = res));
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/clients": () => json([CLIENT, clientB]),
      "GET /api/v1/clients/cli-2/agents": () => json([agentB]),
      "GET /api/v1/clients/cli-2/preparations": () => json([]),
      "POST /api/v1/clients/cli-1/agents": () => gatePostA,
    });
    portal("/clients/cli-1");
    await screen.findByText(/Acme Ltda/);
    // cadastro de A iniciado e PENDENTE
    await userEvent.click(screen.getByRole("button", { name: "Novo agente" }));
    await userEvent.type(screen.getByLabelText("Nome do agente"), "vm-a-pendente");
    await userEvent.click(screen.getByRole("button", { name: "Cadastrar" }));
    // navegação REAL para o cliente B (mesma árvore de componentes)
    await userEvent.click(screen.getByRole("link", { name: "← Clientes" }));
    await screen.findByText("Cliente B");
    await userEvent.click(screen.getByRole("link", { name: "Cliente B" }));
    expect(await screen.findByRole("heading")).toHaveTextContent(/Cliente B/);
    await screen.findByText("vm-b");
    // libera a resposta atrasada do POST de A — tela de B permanece
    await act(async () =>
      releasePostA(
        json({ id: "ag-a", client_id: "cli-1", name: "vm-a-pendente",
               created_at: "x", last_seen_at: null }, 201),
      ),
    );
    expect(screen.getByRole("heading")).toHaveTextContent(/Cliente B/);
    expect(screen.getByText("vm-b")).toBeInTheDocument();
    expect(screen.queryByText(/vinculado/)).toBeNull();
  });
});

describe("agente além da primeira página", () => {
  const farClient = { id: "cli-far", name: "Cliente Distante", created_at: "x" };
  const farAgent = {
    id: "ag-far", client_id: "cli-far", name: "vm-501",
    created_at: "x", last_seen_at: null,
  };
  const filler = (n: number, mk: (i: number) => object) =>
    new Array(n).fill(null).map((_, i) => mk(i));

  /** Stub paginado real: corta por limit/offset como a API faz. */
  function paginated<T extends object>(rows: T[]) {
    return (_init?: RequestInit, url?: URL) => {
      const limit = Number(url?.searchParams.get("limit") ?? 50);
      const offset = Number(url?.searchParams.get("offset") ?? 0);
      return json(rows.slice(offset, offset + limit));
    };
  }

  const manyAgents = [
    ...filler(500, (i) => ({ ...AGENT, id: `filler-${i}`, name: `f${i}` })),
    farAgent,
  ];
  const manyClients = [
    ...filler(500, (i) => ({ ...CLIENT, id: `cf-${i}`, name: `cf${i}` })),
    farClient,
  ];

  it("resolve agente na posição 501 com nome, cliente e emissão", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/agents": paginated(manyAgents),
      "GET /api/v1/clients": paginated(manyClients),
      "GET /api/v1/agents/ag-far/credentials": () => json([CRED]),
    });
    portal("/agents/ag-far");
    expect(await screen.findByText(/Cliente Distante/)).toBeInTheDocument();
    expect(screen.getByText("vm-501")).toBeInTheDocument();
    expect(screen.getByText("ag-far")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Emitir credencial" }),
    ).toBeInTheDocument();
  });

  it("agente inexistente mostra estado explícito", async () => {
    calls = stubFetch({
      ...baseRoutes(),
      "GET /api/v1/agents": paginated(manyAgents.slice(0, 500)),
      "GET /api/v1/agents/ag-fantasma/credentials": () => json([]),
    });
    portal("/agents/ag-fantasma");
    await waitFor(() =>
      expect(screen.getByText("Agente não encontrado.")).toBeInTheDocument(),
    );
  });
});
