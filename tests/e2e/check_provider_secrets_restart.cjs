/* Issue #40: prove migrated Provider state survives real API/Worker restart. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output) throw new Error("Pass a service URL and output directory");
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }) });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");
    const state = await page.evaluate(async () => {
      const response = await fetch("/v1/model-config/providers", { credentials: "same-origin" });
      return { status: response.status, body: await response.json() };
    });
    assert.equal(state.status, 200);
    const provider = state.body.find((item) => item.display_name === "Issue 40 Legacy Provider");
    assert(provider);
    assert.equal(provider.secret_source, "local");
    assert.equal(provider.secret_status, "configured");
    assert.equal(typeof provider.secret_fingerprint, "string");
    assert(!JSON.stringify(state.body).includes("ZHIHENG_PRIVATE_ISSUE40_LEGACY"));
    const search = await page.evaluate(async () => {
      const response = await fetch("/v1/knowledge/search?sort=updated_desc", { credentials: "same-origin" });
      return response.status;
    });
    assert.equal(search, 200);
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ status: "passed", checks: [
      "migrated Provider remains configured after API and Worker restart",
      "legacy environment reference is absent after restart",
      "knowledge search remains available after Provider restart",
    ], evidence: { provider_id: provider.provider_id, secret_source: provider.secret_source,
      secret_status: provider.secret_status, fingerprint: provider.secret_fingerprint, search_status: search } }, null, 2));
  } catch (error) {
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ status: "failed", error: error.stack || String(error) }, null, 2));
    throw error;
  } finally { await context.close(); await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
