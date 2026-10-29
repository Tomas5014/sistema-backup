/**
 * Prova REAL do cadastramento: operador admin entra pela UI, cria
 * cliente e agente pela interface, emite credencial, a usa numa
 * chamada real do agente (heartbeat), revoga pela UI e confirma a
 * rejeição da MESMA credencial — contra API + PostgreSQL descartáveis
 * (mesma infraestrutura do portal.spec.ts: MACIP_API_TEST_DATABASE_URL).
 */

import { expect, test } from "@playwright/test";

const API = "http://127.0.0.1:8180";
const ADMIN = "test-admin-e2e";
const OPERATOR_PASSWORD = "senha-e2e-forte-9";

test.skip(
  !process.env.MACIP_API_TEST_DATABASE_URL,
  "prova real exige MACIP_API_TEST_DATABASE_URL (PostgreSQL)",
);

async function operatorLogin(): Promise<string> {
  const stamp = Date.now();
  const login = `e2e-prov-${stamp}`;
  const r = await fetch(`${API}/api/v1/operators`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${ADMIN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      login,
      name: "Operador Provisão E2E",
      role: "admin",
      password: OPERATOR_PASSWORD,
    }),
  });
  if (!r.ok) throw new Error(`operator provision → ${r.status}`);
  return login;
}

/** heartbeat com a credencial do AGENTE — uso real do token emitido. */
async function heartbeat(token: string): Promise<number> {
  const r = await fetch(`${API}/api/v1/agent/heartbeat`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  return r.status;
}

test("cadastra cliente, agente e credencial; revoga e credencial morre", async ({
  page,
}) => {
  const login = await operatorLogin();
  const stamp = Date.now();
  const clientName = `Cliente E2E ${stamp}`;
  const agentName = `agente-e2e-${stamp}`;

  await page.goto("/login");
  await page.getByLabel("Login").fill(login);
  await page.getByLabel("Senha").fill(OPERATOR_PASSWORD);
  await page.getByRole("button", { name: "Entrar" }).click();
  await expect(page.getByText("Operador Provisão E2E")).toBeVisible();

  // 1) cliente criado pela UI
  await page.goto("/clients");
  await page.getByRole("button", { name: "Novo cliente" }).click();
  await page.getByLabel("Nome do cliente").fill(clientName);
  await page.getByRole("button", { name: "Cadastrar" }).click();
  await expect(page.getByText(/criado/)).toBeVisible();
  await page.getByRole("link", { name: /Abrir o cliente/ }).click();
  await expect(page.getByRole("heading")).toContainText(clientName);

  // 2) agente criado no contexto do cliente
  await page.getByRole("button", { name: "Novo agente" }).click();
  await page.getByLabel("Nome do agente").fill(agentName);
  await page.getByRole("button", { name: "Cadastrar" }).click();
  await expect(page.getByText(/vinculado/)).toBeVisible();
  await page.getByRole("link", { name: agentName }).click();

  // 3) emite credencial — token visível só na apresentação
  await expect(page.locator(".form-context")).toContainText(clientName);
  await page.getByLabel("Descrição da credencial").fill("e2e");
  await page.getByRole("button", { name: "Emitir credencial" }).click();
  const token = await page
    .getByTestId("token-value")
    .textContent();
  expect(token).toBeTruthy();
  await page.getByRole("button", { name: "Fechar" }).click();
  await expect(page.getByTestId("token-value")).toHaveCount(0);

  // 4) a credencial funciona numa chamada real do agente
  expect(await heartbeat(token as string)).toBe(204);

  // 5) revoga pela UI — e a MESMA credencial é rejeitada
  await page.getByRole("button", { name: "Revogar" }).click();
  await page.getByRole("button", { name: "Confirmar" }).click();
  await expect(page.getByText(/Revogada em/)).toBeVisible();
  const status = await heartbeat(token as string);
  expect([401, 403]).toContain(status);
});
