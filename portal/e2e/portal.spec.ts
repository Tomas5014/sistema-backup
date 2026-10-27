/**
 * Prova REAL em navegador: API + PostgreSQL reais (subidos pelo
 * webServer do playwright.config.ts), operador provisionado pelo
 * endpoint administrativo existente, login/logout pela interface.
 *
 *   MACIP_API_TEST_DATABASE_URL='postgresql+psycopg://.../macip_test' \
 *     npm run build && npm run test:e2e
 *
 * Exige: .venv-api (FastAPI instalado), Chrome do sistema, dist/ gerado.
 */

import { expect, test } from "@playwright/test";

const API = "http://127.0.0.1:8180";
const ADMIN = "test-admin-e2e";

test.skip(
  !process.env.MACIP_API_TEST_DATABASE_URL,
  "prova real exige MACIP_API_TEST_DATABASE_URL (PostgreSQL)",
);

const OPERATOR_PASSWORD = "senha-e2e-forte-9";

async function provision() {
  const admin = { Authorization: `Bearer ${ADMIN}` };
  const post = async (path: string, body: unknown, token = ADMIN) => {
    const r = await fetch(`${API}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`provision ${path} → ${r.status}`);
    return r.json();
  };

  const stamp = Date.now();
  const operatorLogin = `e2e-${stamp}`;
  await post("/api/v1/operators", {
    login: operatorLogin,
    name: "Operador E2E",
    role: "admin",
    password: OPERATOR_PASSWORD,
  });

  // caminho real do agente: cliente → agente → credencial → execução
  const client = await post("/api/v1/clients", { name: `Cliente E2E ${stamp}` });
  const agent = await post(`/api/v1/clients/${client.id}/agents`, {
    name: "agente-e2e",
  });
  const cred = await post(`/api/v1/agents/${agent.id}/credentials`, {
    label: "e2e",
  });
  await post(
    "/api/v1/agent/executions",
    {
      execution_id: `e2e-${stamp}`,
      task_id: `tarefa-e2e-${stamp}`,
      status: "partial",
      started_at_utc: "2026-10-26T10:00:00Z",
      finished_at_utc: "2026-10-26T10:05:00Z",
      destinations: [
        {
          destination_id: "dest-e2e",
          status: "success",
          started_at_utc: "2026-10-26T10:00:00Z",
          finished_at_utc: "2026-10-26T10:04:00Z",
          snapshot_id: "snap-e2e",
          files_processed: 10,
          bytes_processed: 4096,
          attempts: [
            {
              attempt: 1,
              status: "success",
              started_at_utc: "2026-10-26T10:00:00Z",
              finished_at_utc: "2026-10-26T10:04:00Z",
              duration_seconds: 240,
              snapshot_id: "snap-e2e",
              files_processed: 10,
              bytes_processed: 4096,
            },
          ],
        },
      ],
    },
    cred.token, // Bearer do AGENTE — nunca o admin
  );
  return { operatorLogin, stamp };
}

test("operador entra, consulta execução e sai", async ({ page }) => {
  const { operatorLogin, stamp } = await provision();

  await page.goto("/login");
  await page.getByLabel("Login").fill(operatorLogin);
  await page.getByLabel("Senha").fill(OPERATOR_PASSWORD);
  await page.getByRole("button", { name: "Entrar" }).click();

  await expect(page.getByText("Operador E2E")).toBeVisible();
  // filtro pela tarefa única desta execução — linha única garantida
  await page.goto(`/executions?task_id=tarefa-e2e-${stamp}`);
  await expect(page.getByText(`tarefa-e2e-${stamp}`)).toBeVisible();

  // detalhe pelo UUID central — destino e tentativa reais do agente
  await page.getByRole("link", { name: "Detalhes" }).first().click();
  await expect(page.getByText("dest-e2e")).toBeVisible();
  await page.getByText("1 tentativa(s)").click();
  await expect(page.getByText("snap-e2e")).toBeVisible();

  // logout revoga no servidor: rota protegida volta ao login
  await page.getByRole("button", { name: "Sair" }).click();
  await expect(page.getByRole("button", { name: "Entrar" })).toBeVisible();
  await page.goto("/executions");
  await expect(page.getByRole("button", { name: "Entrar" })).toBeVisible();
});
