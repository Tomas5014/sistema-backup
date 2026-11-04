/** API + PostgreSQL + Chrome + Restic reais; arquivos e identidade descartáveis. */
import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const API = "http://127.0.0.1:8180";
const ADMIN = "test-admin-e2e";
const PASSWORD = "synthetic-recovery-operator-9";
test.skip(!process.env.MACIP_API_TEST_DATABASE_URL, "PostgreSQL descartável não configurado");

async function api(method: string, endpoint: string, body?: unknown) {
  const response = await fetch(`${API}/api/v1${endpoint}`, {
    method, headers: { Authorization: `Bearer ${ADMIN}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`API ${method} ${endpoint}: ${response.status}`);
  if (response.status === 204) return null;
  return response.json();
}

function findFile(root: string, name: string): string | undefined {
  for (const item of readdirSync(root, { withFileTypes: true })) {
    const candidate = path.join(root, item.name);
    if (item.isFile() && item.name === name) return candidate;
    if (item.isDirectory()) {
      const found = findFile(candidate, name);
      if (found) return found;
    }
  }
  return undefined;
}

test("portal solicita pontos e restaura conteúdo real de tarefa suspensa", async ({ page }) => {
  test.setTimeout(120_000);
  const temporary = mkdtempSync(path.join(tmpdir(), "MACIP recovery E2E "));
  const stamp = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const source = path.join(temporary, "source with spaces");
  const authorized = path.join(temporary, "authorized restore root");
  mkdirSync(source, { mode: 0o700 });
  mkdirSync(authorized, { mode: 0o700 });
  writeFileSync(path.join(source, "report.txt"), "real recovery content\n");
  writeFileSync(path.join(source, "ignored.tmp"), "must remain excluded\n");
  try {
    const login = `recovery-${stamp}`;
    await api("POST", "/operators", { login, name: "Operador Recuperação E2E", role: "admin", password: PASSWORD });
    const client = await api("POST", "/clients", { name: `Recovery client ${stamp}` });
    const agent = await api("POST", `/clients/${client.id}/agents`, { name: `Linux ${stamp}` });
    const credential = await api("POST", `/agents/${agent.id}/credentials`, { label: "discardable recovery" });
    const task = await api("POST", `/agents/${agent.id}/tasks`, {
      name: `Recovery task ${stamp}`,
      spec: { sources: [source], exclusions: ["*.tmp"], compression: "auto", destinations: [{
        id: "local", name: "Local descartável", kind: "local", repository: path.join(temporary, "repository"),
        password_environment: "MACIP_RECOVERY_E2E_PASSWORD",
      }] }, schedule: { kind: "manual" },
    });
    const config = await api("GET", `/tasks/${task.id}/revisions/1/export`);
    const taskFile = path.join(temporary, "task.json");
    const central = path.join(temporary, "central.json");
    const policy = path.join(temporary, "policy.json");
    const queue = path.join(temporary, "queue.db");
    writeFileSync(taskFile, JSON.stringify(config));
    writeFileSync(central, JSON.stringify({ url: API, agent_id: agent.id, token_environment: "MACIP_RECOVERY_E2E_TOKEN" }), { mode: 0o600 });
    writeFileSync(policy, JSON.stringify({ restore_root: authorized }), { mode: 0o600 });
    const root = path.resolve(process.cwd(), "..");
    const environment = { ...process.env, PYTHONPATH: path.join(root, "src"),
      MACIP_HOME: path.join(temporary, "program"), MACIP_STATE: path.join(temporary, "state"),
      MACIP_LOCK_DIR: path.join(temporary, "locks"), MACIP_CACHE: path.join(temporary, "cache"),
      MACIP_RECOVERY_E2E_TOKEN: credential.token, MACIP_RECOVERY_E2E_PASSWORD: "synthetic-restic-password-9" };
    const cli = (...args: string[]) => JSON.parse(execFileSync("python3", ["-m", "macip_backup.cli", ...args], {
      cwd: temporary, env: environment, encoding: "utf8", timeout: 60_000, stdio: ["ignore", "pipe", "pipe"],
    }));
    cli("run", taskFile);
    await api("POST", `/tasks/${task.id}/publish`, { revision: 1, expected_generation: 0 });
    await api("POST", `/tasks/${task.id}/suspend`, { expected_generation: 1 });
    const suspended = (await api("GET", `/tasks/${task.id}`)).distribution;
    expect(suspended.desired_action).toBe("suspend");
    const cycle = () => {
      cli("recovery-poll", "--central", central, "--queue", queue);
      cli("recovery-work", "--central", central, "--queue", queue, "--policy", policy);
      cli("recovery-poll", "--central", central, "--queue", queue);
    };
    await page.goto("/login");
    await page.getByLabel("Login").fill(login);
    await page.getByLabel("Senha").fill(PASSWORD);
    await page.getByRole("button", { name: "Entrar", exact: true }).click();
    await expect(page.getByText("Operador Recuperação E2E")).toBeVisible();
    await page.goto(`/tasks/${task.id}`);
    const section = page.locator("section").filter({ has: page.getByRole("heading", { name: "Recuperação no agente Linux" }) });
    await section.getByRole("button", { name: "Consultar pontos", exact: true }).click();
    await expect(section.getByRole("status")).toContainText("pendente");
    cycle();
    await section.getByRole("button", { name: "Atualizar recuperação", exact: true }).click();
    const points = section.getByLabel("Ponto de recuperação");
    await expect(points).toBeVisible();
    const snapshots = await api("GET", `/tasks/${task.id}/recovery`);
    const query = snapshots.find((operation: { kind: string }) => operation.kind === "snapshots");
    expect(query.state).toBe("succeeded");
    expect(query.result.snapshots).toHaveLength(1);
    await points.selectOption(query.result.snapshots[0].id);
    await section.getByRole("button", { name: "Solicitar restauração", exact: true }).click();
    await expect(section.getByText(/pasta nova e exclusiva/)).toBeVisible();
    await section.getByRole("button", { name: "Confirmar restauração", exact: true }).click();
    await expect(section.getByRole("list", { name: "Operações de recuperação" })).toContainText("Pendente");
    cycle();
    await section.getByRole("button", { name: "Atualizar recuperação", exact: true }).click();
    await expect(section.getByRole("list", { name: "Operações de recuperação" })).toContainText("Concluída pelo agente");
    const operations = await api("GET", `/tasks/${task.id}/recovery`);
    const restore = operations.find((operation: { kind: string }) => operation.kind === "restore");
    expect(restore.state).toBe("succeeded");
    expect(path.dirname(restore.result.target)).toBe(authorized);
    const report = findFile(restore.result.target, "report.txt");
    expect(report).toBeTruthy();
    expect(readFileSync(report!)).toEqual(readFileSync(path.join(source, "report.txt")));
    expect(findFile(restore.result.target, "ignored.tmp")).toBeUndefined();
    const after = (await api("GET", `/tasks/${task.id}`)).distribution;
    expect(after.generation).toBe(suspended.generation);
    expect(after.desired_action).toBe("suspend");
    await api("POST", `/credentials/${credential.credential_id}/revoke`);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});
