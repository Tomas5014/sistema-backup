/**
 * Prova REAL do acompanhamento de tarefas: API + PostgreSQL +
 * navegador Chrome de verdade (webServer do playwright.config.ts).
 * Fixtures preparadas via REST administrativo/token de agente —
 * execuções inseridas assim são dados de teste, NÃO prova de backup
 * Restic real. O que o navegador exibe é o produto rodando.
 *
 *   MACIP_API_TEST_DATABASE_URL='postgresql+psycopg://.../macip_test' \
 *     npm run build && npm run test:e2e -- monitoring
 */

import { expect, test } from "@playwright/test";

const API = "http://127.0.0.1:8180";
const ADMIN = "test-admin-e2e";
const OPERATOR_PASSWORD = "senha-e2e-forte-9";

test.skip(
  !process.env.MACIP_API_TEST_DATABASE_URL,
  "prova real exige MACIP_API_TEST_DATABASE_URL (PostgreSQL)",
);

async function post(path: string, body: unknown, token = ADMIN) {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`POST ${path} → ${r.status}: ${await r.text()}`);
  return r.json();
}

function taskDoc(name: string, schedule: Record<string, unknown>) {
  return {
    name,
    spec: {
      sources: ["/dados/e2e-monitoring"],
      destinations: [
        {
          id: "local",
          name: "Disco local",
          kind: "local",
          repository: "/srv/backup/e2e-monitoring",
          password_environment: "MACIP_RESTIC_E2E",
          retention: { keep_last: 7 },
        },
      ],
    },
    schedule,
  };
}

/** Fluxo real de distribuição: publica revisão 1 e o agente confirma
 * aplicação (gen 1) — last_effect 'apply' e âncora registradas. */
async function applyTask(
  agentId: string,
  agentToken: string,
  doc: unknown,
) {
  const task = await post(`/api/v1/agents/${agentId}/tasks`, doc);
  await post(`/api/v1/tasks/${task.id}/publish`, {
    revision: 1,
    expected_generation: 0,
  });
  await post(
    `/api/v1/agent/tasks/${task.id}/confirmations`,
    { generation: 1, attempt: 1, status: "applied", applied_revision: 1 },
    agentToken,
  );
  return task;
}

async function provision() {
  const stamp = Date.now();
  const operatorLogin = `e2e-mon-${stamp}`;
  await post("/api/v1/operators", {
    login: operatorLogin,
    name: "Operador Monitoramento E2E",
    role: "admin",
    password: OPERATOR_PASSWORD,
  });
  const client = await post("/api/v1/clients", {
    name: `Cliente E2E-M ${stamp}`,
  });
  const agent = await post(`/api/v1/clients/${client.id}/agents`, {
    name: `agente-em-${stamp}`,
  });
  const cred = await post(`/api/v1/agents/${agent.id}/credentials`, {
    label: "e2e",
  });

  // Agenda HORÁRIA cuja ocorrência cai ~100 s no futuro: é a única
  // janela em que uma ocorrência é POSTERIOR à âncora de aplicação —
  // sem relógio falso, 'missing' exige a ocorrência vencer + tolerância
  // (5 s na stack descartável) em tempo real, ~2 min de espera.
  const due = new Date(Date.now() + 100_000);
  const hourly = { kind: "hourly", timezone: "UTC", minute: due.getUTCMinutes() };
  const daily = {
    kind: "daily", timezone: "UTC",
    hour: due.getUTCHours(), minute: due.getUTCMinutes(),
  };

  // tarefa SEM resultado recebido → 'missing' (não é falha!)
  const missingTask = await applyTask(
    agent.id,
    cred.token,
    taskDoc(`SemResultado E2E ${stamp}`, hourly),
  );

  // tarefa com falha RECEBIDA → 'failed': início alegado ~10 s após a
  // ocorrência (futuro breve dentro do skew de 120 s no POST — válido)
  const failedTask = await applyTask(
    agent.id,
    cred.token,
    taskDoc(`Falha E2E ${stamp}`, hourly),
  );
  const started = new Date(due.getTime() + 10_000);
  await post(
    "/api/v1/agent/executions",
    {
      execution_id: `e2e-mon-f-${stamp}`,
      task_id: failedTask.id,
      status: "failed",
      started_at_utc: started.toISOString(),
      finished_at_utc: new Date(started.getTime() + 120_000).toISOString(),
      destinations: [
        {
          destination_id: "local",
          status: "failed",
          started_at_utc: started.toISOString(),
          finished_at_utc: new Date(
            started.getTime() + 120_000,
          ).toISOString(),
        },
      ],
    },
    cred.token,
  );

  // tarefa suspensa (gen 2 confirmada) + NOVA suspensão pendente (gen 3)
  // → last_effect 'suspend'; a explicação NÃO pode dizer "agenda
  // anterior continua ativa" — esse foi o ajuste A da revisão.
  const suspendedTask = await applyTask(
    agent.id,
    cred.token,
    taskDoc(`Suspensa E2E ${stamp}`, daily),
  );
  const susp1 = await post(`/api/v1/tasks/${suspendedTask.id}/suspend`, {
    expected_generation: 1,
  });
  const gen2 = susp1.distribution.generation;
  await post(
    `/api/v1/agent/tasks/${suspendedTask.id}/confirmations`,
    { generation: gen2, attempt: 1, status: "applied",
      applied_revision: null },
    cred.token,
  );
  const susp2 = await post(`/api/v1/tasks/${suspendedTask.id}/suspend`, {
    expected_generation: gen2,
  });
  expect(susp2.distribution.generation).toBe(gen2 + 1);

  return { operatorLogin, stamp, due, missingTask, failedTask, suspendedTask };
}

test("acompanhamento real: lista, detalhe, suspensão e missing≠falha",
  async ({ page }) => {
    // espera real da ocorrência + tolerância (~2 min) — sem relógio falso
    test.setTimeout(300_000);
    const { operatorLogin, stamp, due, missingTask, failedTask, suspendedTask } =
      await provision();

    // login de operador pela interface real
    await page.goto("/login");
    await page.getByLabel("Login").fill(operatorLogin);
    await page.getByLabel("Senha").fill(OPERATOR_PASSWORD);
    await page.getByRole("button", { name: "Entrar" }).click();
    await expect(
      page.getByText("Operador Monitoramento E2E"),
    ).toBeVisible();

    // 'missing' só existe DEPOIS da ocorrência + tolerância: espera real
    // de até ~2 min — sem controlar o relógio do servidor
    const waitMs = due.getTime() + 15_000 - Date.now();
    if (waitMs > 0) await page.waitForTimeout(waitMs);

    // lista: três selos distintos — ausência NÃO é falha
    await page.goto("/tasks");
    const rowMissing = page.locator("li", {
      hasText: `SemResultado E2E ${stamp}`,
    });
    await expect(rowMissing.locator(".badge")).toHaveText("sem resultado");
    await expect(rowMissing.locator(".badge")).toHaveClass(/badge-missing/);
    await expect(rowMissing).toContainText("esperado"); // ocorrência + prazo

    const rowFailed = page.locator("li", {
      hasText: `Falha E2E ${stamp}`,
    });
    await expect(rowFailed.locator(".badge")).toHaveText("falha");
    await expect(rowFailed.locator(".badge")).toHaveClass(/badge-failed/);

    const rowSuspended = page.locator("li", {
      hasText: `Suspensa E2E ${stamp}`,
    });
    await expect(rowSuspended.locator(".badge")).toHaveText(
      "sem histórico",
    );

    // detalhe da suspensa-pendente: explicação coerente com o efeito
    // CONFIRMADO (suspensa) — nunca "agenda anterior continua ativa"
    await page.goto(`/tasks/${suspendedTask.id}`);
    await expect(
      page.getByRole("heading", { name: "Acompanhamento" }),
    ).toBeVisible();
    await expect(page.getByText("suspensão pendente")).toBeVisible();
    const reason = page.locator(".hint").filter({ hasText: "suspens" });
    await expect(reason.first()).toContainText("suspensa");
    await expect(
      page.getByText("agenda anterior continua ativa"),
    ).toHaveCount(0);

    // detalhe do missing: prazo/exp. e a ressalva "não prova falha"
    await page.goto(`/tasks/${missingTask.id}`);
    await expect(page.locator(".badge.badge-missing")).toHaveText(
      "sem resultado",
    );
    await expect(page.getByText(/não prova falha/)).toBeVisible();

    // detalhe do failed: vermelho real — resultado recebido, não ausência
    await page.goto(`/tasks/${failedTask.id}`);
    await expect(page.locator(".badge.badge-failed")).toHaveText("falha");
    await expect(
      page.getByText(/resultado com falha recebido/),
    ).toBeVisible();
  },
);

test("viewer lê acompanhamento e segue sem permissão de escrita",
  async ({ page }) => {
    // seeding admin + operador viewer separado — permissões reais
    const { stamp, suspendedTask } = await provision();
    const viewerLogin = `e2e-mon-viewer-${stamp}`;
    await post("/api/v1/operators", {
      login: viewerLogin,
      name: "Viewer Monitoramento E2E",
      role: "viewer",
      password: OPERATOR_PASSWORD,
    });

    await page.goto("/login");
    await page.getByLabel("Login").fill(viewerLogin);
    await page.getByLabel("Senha").fill(OPERATOR_PASSWORD);
    await page.getByRole("button", { name: "Entrar" }).click();
    await expect(
      page.getByText("Viewer Monitoramento E2E"),
    ).toBeVisible();

    // leitura preservada: lista e detalhe mostram o acompanhamento
    await page.goto("/tasks");
    await expect(
      page.locator("li", { hasText: `Suspensa E2E ${stamp}` }),
    ).toBeVisible();
    await page.goto(`/tasks/${suspendedTask.id}`);
    await expect(
      page.getByRole("heading", { name: "Acompanhamento" }),
    ).toBeVisible();

    // escrita segue negada pela SESSÃO (mesma credencial do navegador)
    const denied = await page.request.post(
      `/api/v1/tasks/${suspendedTask.id}/publish`,
      { data: { revision: 1, expected_generation: 3 } },
    );
    expect(denied.status()).toBe(403);
  },
);
