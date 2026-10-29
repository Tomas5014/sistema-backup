/** Jar de cookies falso + fetch stub roteável para os testes de UI. */

import { vi } from "vitest";

type Handler = (
  init?: RequestInit,
  url?: URL,
) => Response | Promise<Response>;

export const OPERATOR = {
  id: "op-1",
  login: "ana",
  name: "Ana",
  role: "admin" as const,
  active: true,
  created_at: "2026-10-01T10:00:00Z",
  last_login_at: null,
};

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function noContent(): Response {
  return new Response(null, { status: 204 });
}

export function clearCookies() {
  for (const name of ["macip_csrf", "macip_session"]) {
    document.cookie = `${name}=; Max-Age=0; Path=/`;
  }
}

/** Stub de fetch: roteia por "METHOD /path" e simula o jar ao receber
 * CSRF/login (o cookie real é escrito pelo TestClient só no e2e). */
export function stubFetch(routes: Record<string, Handler>) {
  const calls: { key: string; url: URL; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const method = init?.method ?? "GET";
      const key = `${method} ${url.pathname}`;
      calls.push({ key, url, init });
      // simula o jar: rotas reais que emitem cookies
      if (key === "GET /api/v1/auth/csrf") {
        document.cookie = "macip_csrf=csrf-a; Path=/";
      }
      const handler = routes[key] ?? routes[`${method} *`];
      if (!handler) return json({ detail: `rota não simulada: ${key}` }, 500);
      return handler(init, url);
    }),
  );
  return calls;
}
