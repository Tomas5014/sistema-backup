/** Corridas de sessão e de rota com promises controladas —
 * a ordem de resolução define a prova, nunca sleeps. */

import { afterEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useNavigate,
} from "react-router-dom";
import { AuthProvider, useAuth } from "../src/auth";
import { ExecutionDetailPage } from "../src/pages/ExecutionDetailPage";
import { ClientAgentsPage } from "../src/pages/ClientsPage";
import { clearCookies, json, noContent, OPERATOR } from "./helpers";
import { vi } from "vitest";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** fetch controlado: cada chave guarda a fila de chamadas pendentes. */
function controlledFetch(map: Record<string, () => Promise<Response> | Response>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const key = `${init?.method ?? "GET"} ${url.pathname}`;
      calls.push(key);
      const handler = map[key] ?? map[`${key}*`];
      if (!handler) return Promise.resolve(json({ detail: `sem stub ${key}` }, 500));
      return Promise.resolve(handler());
    }),
  );
  return calls;
}

/** Consumidor mínimo do contexto para disparar as operações. */
function Probe() {
  const ctx = useAuth();
  return (
    <div>
      <output data-testid="state">{ctx.state}</output>
      <output data-testid="operator">{ctx.operator?.name ?? ""}</output>
      <button onClick={() => void ctx.refresh()}>refresh</button>
      <button onClick={() => void ctx.signIn("ana", "x").catch(() => {})}>
        signIn
      </button>
      <button
        onClick={() =>
          void ctx.signOut().then((m) => {
            if (m) setLastLogoutError(m);
          })
        }
      >
        signOut
      </button>
    </div>
  );
}

let lastLogoutError: string | null = null;
function setLastLogoutError(m: string) {
  lastLogoutError = m;
}

function NavButton({ to }: { to: string }) {
  const navigate = useNavigate();
  return (
    <button onClick={() => navigate(to)}>goto:{to}</button>
  );
}

function renderWithRoutes(initial: string, routes: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <AuthProvider>
        <Routes>{routes}</Routes>
        <NavButton to="/executions/B" />
        <NavButton to="/clients/cb" />
      </AuthProvider>
    </MemoryRouter>,
  );
}

function renderProbe() {
  return render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
}

afterEach(() => {
  clearCookies();
  lastLogoutError = null;
});

describe("sessão: respostas antigas não desfazem operações recentes", () => {
  it("me() antigo 401 não derruba um login que já concluiu", async () => {
    const me1 = deferred<Response>();
    const loginOk = deferred<Response>();
    const calls = controlledFetch({
      "GET /api/v1/auth/me": () => me1.promise,
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => {
        document.cookie = "macip_csrf=r; Path=/";
        return loginOk.promise;
      },
    });
    renderProbe();
    // refresh do mount ficou pendente…
    expect(calls).toContain("GET /api/v1/auth/me");
    // …o login conclui primeiro e autentica
    document.cookie = "macip_csrf=c; Path=/";
    await act(async () => {
      screen.getByRole("button", { name: "signIn" }).click();
      await new Promise((r) => setTimeout(r, 0));
      loginOk.resolve(json(OPERATOR));
      await loginOk.promise;
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("authenticated");
    expect(screen.getByTestId("operator").textContent).toBe("Ana");
    // o me() antigo retorna 401 depois — sessão NÃO pode virar anônima
    await act(async () => {
      me1.resolve(json({ detail: "x" }, 401));
      await me1.promise;
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("authenticated");
    expect(screen.getByTestId("operator").textContent).toBe("Ana");
  });

  it("me() antigo 200 não ressuscita sessão após logout", async () => {
    let meCount = 0;
    const mePendings: Deferred<Response>[] = [];
    const me = () => {
      meCount++;
      if (meCount === 1) return json(OPERATOR); // restore inicial
      const d = deferred<Response>();
      mePendings.push(d);
      return d.promise;
    };
    const out = deferred<Response>();
    controlledFetch({
      "GET /api/v1/auth/me": me,
      "POST /api/v1/auth/logout": () => out.promise,
    });
    renderProbe();
    await screen.findByText("authenticated", { selector: "output" });

    document.cookie = "macip_csrf=c; Path=/";
    // revalidação em voo (rota protegida)…
    await act(async () => {
      screen.getByRole("button", { name: "refresh" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(mePendings).toHaveLength(1);
    // …logout conclui primeiro
    await act(async () => {
      screen.getByRole("button", { name: "signOut" }).click();
      out.resolve(new Response(null, { status: 204 }));
      await out.promise;
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("anonymous");
    // o me() pendente retorna 200 depois — sessão NÃO pode voltar
    await act(async () => {
      mePendings[0].resolve(json(OPERATOR));
      await mePendings[0].promise;
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("anonymous");
    expect(lastLogoutError).toBeNull();
  });

  it("revalidações fora de ordem: só a mais recente vale", async () => {
    let meCount = 0;
    const pendings: Deferred<Response>[] = [];
    controlledFetch({
      "GET /api/v1/auth/me": () => {
        meCount++;
        if (meCount === 1) return json(OPERATOR);
        const d = deferred<Response>();
        pendings.push(d);
        return d.promise;
      },
    });
    renderProbe();
    await screen.findByText("authenticated", { selector: "output" });
    // duas revalidações em voo
    await act(async () => {
      screen.getByRole("button", { name: "refresh" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    await act(async () => {
      screen.getByRole("button", { name: "refresh" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(pendings).toHaveLength(2);
    // a mais nova diz 401 (sessão expirou de verdade)
    await act(async () => {
      pendings[1].resolve(json({ detail: "x" }, 401));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("anonymous");
    // a mais velha diz 200 depois — não pode ressuscitar
    await act(async () => {
      pendings[0].resolve(json(OPERATOR));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("anonymous");
  });

  it("falha no logout é visível e mantém a sessão", async () => {
    const out = deferred<Response>();
    controlledFetch({
      "GET /api/v1/auth/me": () => json(OPERATOR),
      "POST /api/v1/auth/logout": () => out.promise,
    });
    renderProbe();
    await screen.findByText("authenticated", { selector: "output" });
    document.cookie = "macip_csrf=c; Path=/";
    await act(async () => {
      screen.getByRole("button", { name: "signOut" }).click();
      out.resolve(json({ detail: "x" }, 500));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(lastLogoutError).toBeTruthy();
    expect(screen.getByTestId("state").textContent).toBe("authenticated");
  });
});

describe("detalhe da execução: dados presos à rota", () => {
  const detail = (task: string, dest: string) => ({
    id: task,
    agent_id: "a",
    client_id: "c",
    execution_id: `exec-${task}`,
    task_id: task,
    status: "success",
    started_at_utc: "2026-10-26T10:00:00Z",
    finished_at_utc: "2026-10-26T10:05:00Z",
    received_at: "2026-10-26T10:05:02Z",
    destinations: [
      {
        destination_id: dest,
        status: "success",
        attempt: 1,
        started_at_utc: "2026-10-26T10:00:00Z",
        finished_at_utc: "2026-10-26T10:04:00Z",
        duration_seconds: 240,
        retryable: false,
        snapshot_id: null,
        snapshots_available: null,
        files_processed: null,
        bytes_processed: null,
        error: null,
        exit_code: 0,
        failed_operation: null,
        timeout_seconds: null,
        attempts: [],
      },
    ],
  });

  function renderAt(path: string) {
    return renderWithRoutes(path, (
      <Route path="/executions/:id" element={<ExecutionDetailPage />} />
    ));
  }

  it("A pendente → navega para B → B conclui → A conclui: tela fica em B", async () => {
    const a = deferred<Response>();
    const b = deferred<Response>();
    controlledFetch({
      "GET /api/v1/executions/A": () => a.promise,
      "GET /api/v1/executions/B": () => b.promise,
    });
    renderAt("/executions/A");
    await act(async () => {
      screen.getByRole("button", { name: "goto:/executions/B" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    // enquanto B carrega, dados de A NÃO permanecem
    expect(screen.queryByText("tarefa-A")).toBeNull();
    expect(screen.getByText("Carregando…")).toBeInTheDocument();

    await act(async () => {
      b.resolve(json(detail("tarefa-B", "dest-B")));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("tarefa-B")).toBeInTheDocument();
    expect(screen.getByText("dest-B")).toBeInTheDocument();

    // A resolve depois — não pode vazar na rota B
    await act(async () => {
      a.resolve(json(detail("tarefa-A", "dest-A")));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("tarefa-B")).toBeInTheDocument();
    expect(screen.getByText("dest-B")).toBeInTheDocument();
    expect(screen.queryByText("tarefa-A")).toBeNull();
    expect(screen.queryByText("dest-A")).toBeNull();
  });

  it("A pendente → B conclui → A falha: B permanece sem erro", async () => {
    const a = deferred<Response>();
    const b = deferred<Response>();
    controlledFetch({
      "GET /api/v1/executions/A": () => a.promise,
      "GET /api/v1/executions/B": () => b.promise,
    });
    renderAt("/executions/A");
    await act(async () => {
      screen.getByRole("button", { name: "goto:/executions/B" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    await act(async () => {
      b.resolve(json(detail("tarefa-B", "dest-B")));
      await new Promise((r) => setTimeout(r, 0));
    });
    await act(async () => {
      a.resolve(json({ detail: "x" }, 500));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("tarefa-B")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("retry de A resolve depois da navegação — descartado", async () => {
    const aFirst = deferred<Response>();
    const aRetry = deferred<Response>();
    let aCalls = 0;
    controlledFetch({
      "GET /api/v1/executions/A": () => (aCalls++ === 0 ? aFirst.promise : aRetry.promise),
      "GET /api/v1/executions/B": () => json(detail("tarefa-B", "dest-B")),
    });
    renderAt("/executions/A");
    await act(async () => {
      aFirst.resolve(json({ detail: "x" }, 500));
      await new Promise((r) => setTimeout(r, 0));
    });
    // erro de A com retry
    const retry = await screen.findByRole("button", { name: "Tentar novamente" });
    await act(async () => {
      retry.click();
      await new Promise((r) => setTimeout(r, 0));
    });
    // navega para B antes do retry concluir
    await act(async () => {
      screen.getByRole("button", { name: "goto:/executions/B" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    await screen.findByText("tarefa-B");
    await act(async () => {
      aRetry.resolve(json(detail("tarefa-A", "dest-A")));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("tarefa-B")).toBeInTheDocument();
    expect(screen.queryByText("dest-A")).toBeNull();
  });
});

describe("agentes do cliente: dados presos à rota", () => {
  const client = (id: string, name: string) => ({
    id,
    name,
    created_at: "2026-10-01T10:00:00Z",
  });
  const agent = (id: string, clientId: string, name: string) => ({
    id,
    client_id: clientId,
    name,
    created_at: "2026-10-01T10:00:00Z",
    last_seen_at: null,
  });

  it("cliente A pendente → navega para B → B conclui → A conclui: B permanece", async () => {
    const agentsA = deferred<Response>();
    const agentsB = deferred<Response>();
    controlledFetch({
      "GET /api/v1/clients": () =>
        json([client("ca", "Cliente A"), client("cb", "Cliente B")]),
      "GET /api/v1/clients/ca/agents": () => agentsA.promise,
      "GET /api/v1/clients/cb/agents": () => agentsB.promise,
    });
    renderWithRoutes("/clients/ca", (
      <Route path="/clients/:clientId" element={<ClientAgentsPage />} />
    ));
    await act(async () => {
      screen.getByRole("button", { name: "goto:/clients/cb" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("Carregando…")).toBeInTheDocument();

    await act(async () => {
      agentsB.resolve(json([agent("b1", "cb", "agente-de-B")]));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("agente-de-B")).toBeInTheDocument();
    expect(screen.getByText(/Cliente B/)).toBeInTheDocument();

    await act(async () => {
      agentsA.resolve(json([agent("a1", "ca", "agente-de-A")]));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("agente-de-B")).toBeInTheDocument();
    expect(screen.queryByText("agente-de-A")).toBeNull();
    expect(screen.getByText(/Cliente B/)).toBeInTheDocument();
  });
});


describe("transição tem prioridade sobre revalidação", () => {
  it("login pendente + refresh durante → refresh suprimido, login vence", async () => {
    const loginPend = deferred<Response>();
    const calls = controlledFetch({
      "GET /api/v1/auth/me": () => json({ detail: "x" }, 401),
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => loginPend.promise,
    });
    renderProbe();
    await screen.findByText("anonymous", { selector: "output" }); // sem sessão
    const meBefore = calls.filter((c) => c === "GET /api/v1/auth/me").length;

    document.cookie = "macip_csrf=c; Path=/";
    await act(async () => {
      screen.getByRole("button", { name: "signIn" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    // refresh durante o login pendente — SUPRIMIDO, não vai à rede
    await act(async () => {
      screen.getByRole("button", { name: "refresh" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(
      calls.filter((c) => c === "GET /api/v1/auth/me").length,
    ).toBe(meBefore); // nenhuma requisição extra de revalidação

    await act(async () => {
      loginPend.resolve(json(OPERATOR));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("authenticated");
    expect(screen.getByTestId("operator").textContent).toBe("Ana");
  });

  it("logout pendente + refresh durante → suprimido, logout vence", async () => {
    const out = deferred<Response>();
    const calls = controlledFetch({
      "GET /api/v1/auth/me": () => json(OPERATOR),
      "POST /api/v1/auth/logout": () => out.promise,
    });
    renderProbe();
    await screen.findByText("authenticated", { selector: "output" });
    const meBefore = calls.filter((c) => c === "GET /api/v1/auth/me").length;

    document.cookie = "macip_csrf=c; Path=/";
    await act(async () => {
      screen.getByRole("button", { name: "signOut" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    await act(async () => {
      screen.getByRole("button", { name: "refresh" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(
      calls.filter((c) => c === "GET /api/v1/auth/me").length,
    ).toBe(meBefore);

    await act(async () => {
      out.resolve(new Response(null, { status: 204 }));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("anonymous");
    expect(lastLogoutError).toBeNull();
  });

  it("refresh iniciado ANTES do login responde depois — descartado", async () => {
    let meCount = 0;
    const pendings: Deferred<Response>[] = [];
    const loginPend = deferred<Response>();
    controlledFetch({
      "GET /api/v1/auth/me": () => {
        meCount++;
        if (meCount === 1) return json({ detail: "x" }, 401);
        const d = deferred<Response>();
        pendings.push(d);
        return d.promise;
      },
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => loginPend.promise,
    });
    renderProbe();
    await screen.findByText("anonymous", { selector: "output" });

    // refresh começa ANTES do login — fica pendente
    await act(async () => {
      screen.getByRole("button", { name: "refresh" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(pendings).toHaveLength(1);
    // login começa — o refresh pendente já está obsoleto
    document.cookie = "macip_csrf=c; Path=/";
    await act(async () => {
      screen.getByRole("button", { name: "signIn" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    // o refresh responde 401 DURANTE o login pendente — descartado
    await act(async () => {
      pendings[0].resolve(json({ detail: "x" }, 401));
      await new Promise((r) => setTimeout(r, 0));
    });
    // login conclui com sucesso — DEVE autenticar
    await act(async () => {
      loginPend.resolve(json(OPERATOR));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("authenticated");
  });

  it("revalidação volta a funcionar depois da transição", async () => {
    const loginPend = deferred<Response>();
    let meCount = 0;
    const pendings: Deferred<Response>[] = [];
    controlledFetch({
      "GET /api/v1/auth/me": () => {
        meCount++;
        if (meCount === 1) return json({ detail: "x" }, 401);
        const d = deferred<Response>();
        pendings.push(d);
        return d.promise;
      },
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => loginPend.promise,
    });
    renderProbe();
    await screen.findByText("anonymous", { selector: "output" });
    document.cookie = "macip_csrf=c; Path=/";
    await act(async () => {
      screen.getByRole("button", { name: "signIn" }).click();
      await new Promise((r) => setTimeout(r, 0));
      loginPend.resolve(json(OPERATOR));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("authenticated");

    // refresh NOVO, depois da transição — detecta expiração real
    await act(async () => {
      screen.getByRole("button", { name: "refresh" }).click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(pendings).toHaveLength(1); // a requisição realmente saiu
    await act(async () => {
      pendings[0].resolve(json({ detail: "x" }, 401));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("state").textContent).toBe("anonymous");
  });
});
