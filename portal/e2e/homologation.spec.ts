/**
 * Prova integrada da stack HTTPS (deploy/central): PostgreSQL + API +
 * proxy TLS + navegador REAIS, sessão por cookie e persistência após
 * restart. TLS é verificado de verdade — a CA de laboratório entra num
 * NSS DB de HOME temporário (confiança explícita, sem tocar o SO);
 * se o certificado não validar, o navegador falha o teste.
 *
 * Orquestrado por deploy/central/scripts/prove.sh — exige as variáveis
 * MACIP_HOMOLOG_* documentadas em docs/homologacao-central.md.
 */

import { expect, test } from "@playwright/test";
import { execSync } from "node:child_process";

const BASE_URL = process.env.MACIP_HOMOLOG_URL;
const OP_LOGIN = process.env.MACIP_HOMOLOG_LOGIN;
const OP_PASSWORD = process.env.MACIP_HOMOLOG_PASSWORD;
const TASK = process.env.MACIP_HOMOLOG_TASK;
const AGENT_TASK = process.env.MACIP_HOMOLOG_AGENT_TASK; // opcional
const COMPOSE = process.env.MACIP_HOMOLOG_COMPOSE; // comando p/ restart
const READY = `${BASE_URL}/readyz`;

test.skip(
  !BASE_URL || !OP_LOGIN || !OP_PASSWORD || !TASK || !COMPOSE,
  "prova integrada requer MACIP_HOMOLOG_* (ver docs/homologacao-central.md)",
);

test.use({
  storageState: undefined,
  baseURL: process.env.MACIP_HOMOLOG_URL,
  launchOptions: process.env.MACIP_HOMOLOG_HOME
    ? { env: { ...process.env, HOME: process.env.MACIP_HOMOLOG_HOME } }
    : {},
});

test("central HTTPS: sessão, dados, restart e revogação", async ({
  page,
  context,
  browser,
}) => {
  // 1. login pela interface HTTPS — cert+hostname já validados aqui:
  //    CA de laboratório num nssdb de HOME temporário (prove.sh)
  await page.goto("/login");
  const csrfBefore = await context.cookies();
  await page.getByLabel("Login").fill(OP_LOGIN!);
  await page.getByLabel("Senha").fill(OP_PASSWORD!);
  await page.getByRole("button", { name: "Entrar" }).click();

  await expect(page.getByText(/e2e|Homolog|Operador/).first()).toBeVisible({
    timeout: 15_000,
  });

  // 2. cookie de sessão HttpOnly+Secure e rotação do CSRF
  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === "macip_session");
  expect(session, "macip_session emitido").toBeTruthy();
  expect(session!.httpOnly, "sessão HttpOnly").toBe(true);
  expect(session!.secure, "sessão Secure em HTTPS").toBe(true);
  expect(session!.sameSite).toBe("Lax");
  const csrfAfter = cookies.find((c) => c.name === "macip_csrf");
  expect(csrfAfter?.httpOnly, "csrf legível pelo JS").toBe(false);
  const before = csrfBefore.find((c) => c.name === "macip_csrf")?.value;
  expect(csrfAfter?.value).not.toBe(before); // rotação no login

  // 3. execução do agente (provisionada no prove.sh) → detalhe
  await page.goto(`/executions?task_id=${TASK}`);
  await expect(page.getByText(TASK!)).toBeVisible();
  await page.getByRole("link", { name: "Detalhes" }).click();
  await expect(page.getByText("dest-homolog")).toBeVisible();
  await page.getByText("1 tentativa(s)").click();
  await expect(page.getByText("snapshot-homolog")).toBeVisible();

  // 4. reload — sessão restaurada do cookie HttpOnly
  await page.reload();
  await expect(page.getByRole("link", { name: "Clientes" })).toBeVisible();

  // 5. logout: revoga no servidor E remove o cookie local
  await page.getByRole("button", { name: "Sair" }).click();
  await expect(page.getByRole("button", { name: "Entrar" })).toBeVisible();
  const afterLogout = await context.cookies();
  expect(
    afterLogout.find((c) => c.name === "macip_session"),
    "cookie removido localmente",
  ).toBeUndefined();
  // prova da revogação no servidor: novo contexto COM o cookie antigo
  const stolen = await browser.newContext();
  await stolen.addCookies([
    {
      name: "macip_session",
      value: session!.value,
      domain: new URL(BASE_URL!).hostname,
      path: "/",
      secure: true,
      httpOnly: true,
    },
  ]);
  const meRes = await stolen.request.get(`${BASE_URL}/api/v1/auth/me`);
  expect(meRes.status(), "sessão revogada no servidor").toBe(401);
  await stolen.close();

  // 6. restart da stack — dados persistem no volume
  execSync(COMPOSE!, { stdio: "pipe" });
  const deadline = Date.now() + 90_000;
  let ready = false;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(READY);
      if (r.ok) {
        ready = true;
        break;
      }
    } catch { /* proxy ainda subindo */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
  expect(ready, "API voltou saudável após restart").toBe(true);

  await page.goto("/login");
  await page.getByLabel("Login").fill(OP_LOGIN!);
  await page.getByLabel("Senha").fill(OP_PASSWORD!);
  await page.getByRole("button", { name: "Entrar" }).click();
  // espera a área protegida antes de navegar — goto durante o
  // login corrido renderia a rota ainda anônima e cairia em /login
  await expect(page.getByRole("link", { name: "Clientes" })).toBeVisible({
    timeout: 15_000,
  });
  await page.goto(`/executions?task_id=${TASK}`);
  await expect(page.getByText(TASK!)).toBeVisible();

  // 7. fluxo do agente real, se prove.sh executou backup→outbox→sync
  if (AGENT_TASK) {
    await page.goto(`/executions?task_id=${AGENT_TASK}`);
    await expect(page.getByText(AGENT_TASK)).toBeVisible();
  }
});
