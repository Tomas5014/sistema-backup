// Invocado pela prova Restic em api/tests/test_protected_copy.py.
const assert = require("node:assert/strict");
const { readFile } = require("node:fs/promises");
const { join } = require("node:path");
const { chromium } = require("playwright");
const { expect } = require("@playwright/test");

async function main() {
  const [origin, clientId, state, evidenceDir] = process.argv.slice(2);
  const browser = await chromium.launch({ channel: "chrome" });
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
    await page.goto(origin + "/login");
    await page.getByLabel("Login").fill(process.env.MACIP_TEST_BROWSER_LOGIN);
    await page.getByLabel("Senha").fill(process.env.MACIP_TEST_BROWSER_PASSWORD);
    await page.getByRole("button", { name: "Entrar" }).click();
    await expect(page.getByText("Operador de prova B")).toBeVisible();
    await page.goto(origin + "/clients/" + clientId);
    await expect(page.getByText("Estrutura primária pronta")).toBeVisible();
    await expect(page.getByText("Última cópia confirmada")).toBeVisible();
    const button = page.getByRole("button", { name: "Baixar perfil" });
    if (state === "proven") {
      await expect(page.getByText("Restauração comprovada em laboratório")).toBeVisible();
      const downloadPromise = page.waitForEvent("download");
      await button.click();
      const download = await downloadPromise;
      assert.equal(download.suggestedFilename(), "perfil-macip.json");
      const profile = JSON.parse(await readFile(await download.path(), "utf8"));
      assert.equal(profile.repositorio_id, process.env.MACIP_TEST_EXPECTED_REPO_ID);
    } else {
      await expect(page.getByText("Estrutura pronta · Prova de restauração pendente")).toBeVisible();
      await expect(button).toBeDisabled();
    }
    if (evidenceDir) {
      await page.screenshot({ path: join(evidenceDir, state + "-desktop.png") });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: join(evidenceDir, state + "-mobile.png") });
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
