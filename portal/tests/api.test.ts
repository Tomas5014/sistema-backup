/** Contrato HTTP: CSRF, 204 sem JSON, classificação de erros. */

import { afterEach, describe, expect, it } from "vitest";
import { ApiError, csrfToken, login, logout, me } from "../src/api";
import { clearCookies, json, noContent, stubFetch, OPERATOR } from "./helpers";

afterEach(() => {
  clearCookies();
});

describe("csrfToken", () => {
  it("lê o cookie macip_csrf", () => {
    document.cookie = "macip_csrf=tok-1; Path=/";
    expect(csrfToken()).toBe("tok-1");
  });
  it("null sem o cookie", () => {
    expect(csrfToken()).toBeNull();
  });
});

describe("login", () => {
  it("pede CSRF antes do POST e envia X-CSRF-Token", async () => {
    const calls = stubFetch({
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => {
        document.cookie = "macip_csrf=csrf-b; Path=/"; // rotação
        return json(OPERATOR);
      },
    });
    const op = await login("ana", "segredo");
    expect(op.login).toBe("ana");
    expect(calls.map((c) => c.key)).toEqual([
      "GET /api/v1/auth/csrf",
      "POST /api/v1/auth/login",
    ]);
    expect(calls[1].init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-a" });
    expect(csrfToken()).toBe("csrf-b"); // valor rotacionado lido depois
  });

  it("401 → unauthenticated (não erro de rede)", async () => {
    stubFetch({
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => json({ detail: "x" }, 401),
    });
    await expect(login("ana", "errada")).rejects.toMatchObject({
      kind: "unauthenticated",
      status: 401,
    });
  });

  it("429 → rate_limited", async () => {
    stubFetch({
      "GET /api/v1/auth/csrf": () => noContent(),
      "POST /api/v1/auth/login": () => json({ detail: "x" }, 429),
    });
    await expect(login("ana", "x")).rejects.toMatchObject({ kind: "rate_limited" });
  });

  it("falha de transporte → network", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("fetch failed"))),
    );
    await expect(me()).rejects.toMatchObject({ kind: "network", status: null });
  });
});

describe("logout", () => {
  it("POST com X-CSRF-Token e tolera 204 sem corpo", async () => {
    document.cookie = "macip_csrf=csrf-z; Path=/";
    const calls = stubFetch({ "POST /api/v1/auth/logout": () => noContent() });
    await expect(logout()).resolves.toBeUndefined();
    expect(calls[0].init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-z" });
  });

  it("escrita sem cookie CSRF falha sem chamar a rede", async () => {
    const calls = stubFetch({});
    await expect(logout()).rejects.toBeInstanceOf(ApiError);
    expect(calls).toHaveLength(0);
  });
});
