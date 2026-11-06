/**
 * C15 — falha SELETIVA e SIMULADA de rede só no GET de monitoramento.
 *
 * NÃO é indisponibilidade real da API: `page.route` aborta apenas
 * /api/v1/monitoring/* no navegador; tarefa e revisões continuam sendo
 * servidas pela stack real (verificado em DADOS, não só em título).
 * A prova exige: seção isolando o erro, ausência de selos de falha,
 * controle de que consulta ainda falhando NÃO satisfaz recuperação, e
 * recuperação somente após resposta 200 real da tarefa esperada.
 *
 * Roteiro e variáveis: docs/homologacao-monitoramento.md (C15).
 * Sem MACIP_HOMOLOG_{URL,LOGIN,PASSWORD,TASK} o arquivo inteiro skipa
 * — coleta skipada NÃO conta como aprovação.
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

const MON_PATH = "/api/v1/monitoring/**";
const MON_URL = `/api/v1/monitoring/tasks/${TASK_ID}`;

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

  // detalhe + acompanhamento REALMENTE carregados antes de injetar a
  // falha — o título sozinho não prova consulta concluída
  await page.goto(`/tasks/${TASK_ID}`);
  const section = page.getByRole("heading", { name: "Acompanhamento" })
    .locator("..");
  await expect(section.locator(".badge").first()).toBeVisible({
    timeout: 15_000,
  });

  // SIMULAÇÃO seletiva: aborta SÓ monitoramento — demais APIs reais
  await page.route(MON_PATH, (route) => route.abort());
  await page.reload();  // mecanismo documentado de nova consulta

  // tarefa e revisões seguem disponíveis COM DADOS REAIS — não apenas
  // um título: a API ainda responde fora da rota interceptada
  const taskResp = await page.request.get(`/api/v1/tasks/${TASK_ID}`);
  expect(taskResp.status()).toBe(200);
  const taskBody = await taskResp.json();
  expect(taskBody.id).toBe(TASK_ID);
  const revResp = await page.request.get(
    `/api/v1/tasks/${TASK_ID}/revisions`,
  );
  expect(revResp.status()).toBe(200);
  expect((await revResp.json()).length).toBeGreaterThan(0);
  await expect(page.getByRole("heading").first()).toBeVisible();

  // a seção mostra ERRO DE CARGA — nunca selo de falha/missing
  await expect(section.getByRole("alert")).toContainText(
    /Não foi possível carregar o acompanhamento/,
    { timeout: 15_000 },
  );
  await expect(section.locator(".badge-failed")).toHaveCount(0);
  await expect(section.locator(".badge-missing")).toHaveCount(0);

  // CONTROLE: consulta ainda falhando não satisfaz recuperação —
  // manter o aborto e consultar de novo mantém o erro (heading vazio
  // ou ausência momentânea de erro NÃO valem como recuperação)
  await page.reload();
  await expect(section.getByRole("alert")).toBeVisible({
    timeout: 15_000,
  });

  // recuperação real: resposta 200 do endpoint da TAREFA ESPERADA e
  // conteúdo renderizado — ausência de erro não basta
  await page.unroute(MON_PATH);
  const [resp] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes(MON_URL) && r.status() === 200,
      { timeout: 15_000 },
    ),
    page.reload(),
  ]);
  const body = await resp.json();
  expect(body.task_id).toBe(TASK_ID);

  await expect(section.locator(".badge").first()).toBeVisible({
    timeout: 15_000,
  });
  await expect(
    page.getByText(/Não foi possível carregar o acompanhamento/),
  ).toHaveCount(0);
});
