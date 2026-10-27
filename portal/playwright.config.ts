import { defineConfig } from "@playwright/test";

// Prova REAL: API + PostgreSQL de verdade, navegador Chrome do sistema.
// Requer MACIP_API_TEST_DATABASE_URL (banco descartável) e a build
// gerada (npm run build). O preview encaminha /api à API de teste.
//
// Sem a variável: NENHUM servidor sobe e o teste é skipado no arquivo —
// Python/Chrome/PostgreSQL não são exigidos. Com a variável presente
// mas inválida/inacessível, o webServer falha claramente (não vira skip).
const realE2E = Boolean(process.env.MACIP_API_TEST_DATABASE_URL);

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:4173",
    channel: "chrome",
  },
  webServer: realE2E
    ? [
        {
          command:
            '.venv-api/bin/python -c "import uvicorn; uvicorn.run(\'macip_backup_api.app:app\', host=\'127.0.0.1\', port=8180)"',
          url: "http://127.0.0.1:8180/healthz",
          reuseExistingServer: false,
          timeout: 30_000,
          cwd: "..",
          env: {
            MACIP_API_DATABASE_URL:
              process.env.MACIP_API_TEST_DATABASE_URL ?? "",
            MACIP_API_ADMIN_TOKEN: "test-admin-e2e",
            // exceção local explícita: http no teste; produção usa Secure
            MACIP_API_SESSION_INSECURE_COOKIE: "1",
          },
        },
        {
          command:
            "env MACIP_API_ORIGIN=http://127.0.0.1:8180 npm run preview -- --port 4173 --strictPort",
          url: "http://127.0.0.1:4173",
          reuseExistingServer: false,
          timeout: 30_000,
        },
      ]
    : undefined,
});
