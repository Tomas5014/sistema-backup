/**
 * Prova REAL do cadastro de tarefas: login → cliente/agente → criar
 * tarefa → editar (revisão 2) → consultar revisão anterior → exportar
 * → arquivar. API + PostgreSQL descartáveis
 * (MACIP_API_TEST_DATABASE_URL — mesmo padrão do portal.spec.ts).
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
  const login = `e2e-tasks-${stamp}`;
  const r = await fetch(`${API}/api/v1/operators`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${ADMIN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      login,
      name: "Operador Tarefas E2E",
      role: "admin",
      password: OPERATOR_PASSWORD,
    }),
  });
  if (!r.ok) throw new Error(`operator provision → ${r.status}`);
  return login;
}

test("cadastra, edita, revisa, exporta e arquiva tarefa", async ({
  page,
}) => {
  const login = await operatorLogin();
  const stamp = Date.now();
  const clientName = `Cliente E2E-T ${stamp}`;
  const agentName = `agente-et-${stamp}`;
  const taskName = `Tarefa E2E ${stamp}`;

  await page.goto("/login");
  await page.getByLabel("Login").fill(login);
  await page.getByLabel("Senha").fill(OPERATOR_PASSWORD);
  await page.getByRole("button", { name: "Entrar" }).click();
  await expect(page.getByText("Operador Tarefas E2E")).toBeVisible();

  // contexto real: cliente + agente pela UI
  await page.goto("/clients");
  await page.getByRole("button", { name: "Novo cliente" }).click();
  await page.getByLabel("Nome do cliente").fill(clientName);
  await page.getByRole("button", { name: "Cadastrar" }).click();
  await page.getByRole("link", { name: /Abrir o cliente/ }).click();
  await page.getByRole("button", { name: "Novo agente" }).click();
  await page.getByLabel("Nome do agente").fill(agentName);
  await page.getByRole("button", { name: "Cadastrar" }).click();
  await expect(page.getByText(/vinculado/)).toBeVisible();

  // nova tarefa a partir do agente
  await page.getByRole("link", { name: agentName }).click();
  await page.getByRole("link", { name: "nova tarefa" }).click();
  await expect(
    page.locator(".hint").filter({ hasText: "Cliente:" }),
  ).toContainText(clientName);
  await page.getByLabel("Nome da tarefa").fill(taskName);
  await page.getByLabel("Caminho da origem 1").fill("/srv/dados-e2e");
  await page.getByLabel("Identificador do destino 1").fill("local");
  await page.getByLabel("Nome do destino 1").fill("Disco local");
  await page.getByLabel("Repositório do destino 1").fill("/srv/backup-e2e");
  await page
    .getByLabel("password_environment do destino 1")
    .fill("MACIP_RESTIC_E2E");
  await page.getByRole("button", { name: "Cadastrar tarefa" }).click();

  // detalhe: cadastrada na central, NÃO distribuída
  await expect(
    page.getByText(/não distribuída ao agente/),
  ).toBeVisible();

  // editar → revisão 2 (otimista, esperada = 1)
  await page.getByRole("button", { name: "Editar configuração" }).click();
  const name = page.getByLabel("Nome da tarefa");
  await name.clear();
  await name.fill(`${taskName} v2`);
  await page.getByRole("button", { name: "Salvar nova revisão" }).click();
  await expect(page.getByText(`${taskName} v2`)).toBeVisible();

  // histórico: revisão anterior preservada + exportação real
  await expect(page.getByText(/Revisão 1/)).toBeVisible();
  const href = await page
    .getByRole("link", { name: "baixar tarefa.json" })
    .last()
    .getAttribute("href");
  expect(href).toContain("/revisions/1/export");
  const taskId = page.url().split("/").pop() as string;
  const doc = await page.evaluate(
    async (url) => (await fetch(url)).json(),
    href as string,
  );
  // mapeamento → TaskSpec: UUIDs centrais, nunca nomes
  expect(doc.id).toBe(taskId);
  expect(doc.name).toBe(taskName); // revisão 1, não a v2
  expect(doc.device_id).toBeTruthy();
  expect(doc.client.id).toBeTruthy();
  expect(doc.schedule).toBeUndefined();
  // sem sessão, o mesmo documento é negado
  const unauth = await fetch(
    `http://127.0.0.1:8180/api/v1/tasks/${taskId}/revisions/1/export`,
  );
  expect(unauth.status).toBe(401);

  // arquivar com confirmação
  await page.getByRole("button", { name: "Arquivar tarefa" }).click();
  await expect(page.getByText(/não interrompe/)).toBeVisible();
  await page
    .getByRole("button", { name: "Confirmar arquivamento" })
    .click();
  await expect(page.getByText("Arquivada na central")).toBeVisible();
});
