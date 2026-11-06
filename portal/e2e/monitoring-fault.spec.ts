/**
 * C15 — falha SELETIVA e SIMULADA de rede só no GET de monitoramento.
 *
 * NÃO é indisponibilidade real da API: `page.route` intercepta apenas
 * /api/v1/monitoring/* no navegador; tarefa e revisões continuam sendo
 * servidas pela stack real. O objetivo é provar que a seção
 * "Acompanhamento" isola o erro de carga sem fingir falha de backup.
 *
 * Executa contra o LABORATÓRIO (deploy/central), orquestrado pelo
 * roteiro — variáveis MACIP_HOMOLOG_* como em homologation.spec.ts,
 * mais MACIP_HOMOLOG_TASK=<id real da tarefa criada no laboratório>.
 * Sem as variáveis o arquivo inteiro é ignorado (skip).
 */

import { expect, test } from "@playwright/test";

const BASE_URL = process.env.MACIP_HOMOLOG_URL;
const OP_LOGIN = process.env.MACIP_HOMOLOG_LOGIN;
const OP_PASSWORD = process.env.MACIP_HOMOLOG_PASSWORD;
const TASK_ID = process.env.MACIP_HOMOLOG_TASK;

test.skip(
  !BASE_URL || !OP_LOGIN || !OP_PASSWORD || !TASK_ID,
  "C15 requer MACIP_HOMOLOG_{URL,LOGIN,PASSWORD,TASK} — ver docs/homologacao-monitoramento.md",
);

test.use({
  storageState: undefined,
  baseURL: process.env.MACIP_HOMOLOG_URL,
  launchOptions: process.env.MACIP_HOMOLOG_HOME
    ? { env: { ...process.env, HOME: process.env.MACIP_HOMOLOG_HOME } }
    : {},
});

test("C15: falha seletiva simulada no monitoramento não vira falha de backup", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel("Login").fill(OP_LOGIN!);
  await page.getByLabel("Senha").fill(OP_PASSWORD!);
  await page.getByRole("button", { name: "Entrar" }).click();
  await expect(page.getByRole("link", { name: /tarefas/i })).toBeVisible({
    timeout: 15_000,
  });

  // detalhe carregado de verdade ANTES da injeção da falha
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.getByRole("heading", { name: "Acompanhamento" })).toBeVisible();

  // SIMULAÇÃO seletiva: aborta SÓ as chamadas de monitoramento — as
  // demais APIs (tarefa, revisões) seguem atendidas pela stack real
  await page.route("**/api/v1/monitoring/**", (route) => route.abort());
  // nova consulta: recarregar é o mecanismo documentado de atualização
  await page.reload();

  // restante do detalhe permanece disponível (tarefa + revisões)
  await expect(page.getByRole("heading", { name: /tarefa/i }).first())
    .toBeVisible({ timeout: 15_000 });

  // a seção mostra ERRO DE CARGA — nunca selo de falha/missing do backup
  const section = page.getByRole("heading", { name: "Acompanhamento" })
    .locator("..");
  await expect(
    section.getByRole("alert"),
  ).toContainText(/Não foi possível carregar o acompanhamento/, {
    timeout: 15_000,
  });
  await expect(section.locator(".badge-failed")).toHaveCount(0);
  await expect(section.locator(".badge-missing")).toHaveCount(0);

  // removida a falha, nova consulta (reload) recupera a seção
  await page.unroute("**/api/v1/monitoring/**");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Acompanhamento" }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByText(/Não foi possível carregar o acompanhamento/),
  ).toHaveCount(0);
});
