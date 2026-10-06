/* Issue #40: prove migrated Provider state survives real API/Worker restart. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const https = require("node:https");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output) throw new Error("Pass a service URL and output directory");
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const context = await browser.newContext();
  const page = await context.newPage();
  const receivedAuth = [];
  const providerServer = https.createServer({
    key: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_KEY),
    cert: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_CERT),
  }, (request, response) => {
    if (request.url === "/models") {
      receivedAuth.push(request.headers.authorization || "");
      if (request.headers.authorization !== "Bearer sk-issue40-reentry-key") {
        response.writeHead(401, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: "invalid authorization" }));
        return;
      }
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: "model-a" }] }));
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => providerServer.listen(0, "127.0.0.1", resolve));
  const providerUrl = `https://127.0.0.1:${providerServer.address().port}`;
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
    assert.equal(provider.secret_status, "unavailable");
    assert(!JSON.stringify(state.body).includes("ZHIHENG_PRIVATE_ISSUE40_LEGACY"));
    const csrf = await page.evaluate(() => document.cookie.split(";").map((v) => v.trim())
      .find((v) => v.startsWith("zhiheng_csrf="))?.slice(13) || "");
    const repaired = await page.evaluate(async ({ providerId, etag, csrf, providerUrl }) => {
      const response = await fetch(`/v1/model-config/providers/${providerId}`, {
        method: "PATCH", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
          "If-Match": etag, "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ api_key: "sk-issue40-reentry-key", base_url: providerUrl, enabled: true }),
      });
      return { status: response.status, body: await response.json() };
    }, { providerId: provider.provider_id, etag: provider.etag, csrf, providerUrl });
    assert.equal(repaired.status, 200);
    assert.equal(repaired.body.secret_status, "configured");
    const connectivity = await page.evaluate(async ({ providerId, csrf }) => {
      const response = await fetch(`/v1/model-config/providers/${providerId}/connectivity-test`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      });
      return { status: response.status, body: await response.json() };
    }, { providerId: provider.provider_id, csrf });
    assert.equal(connectivity.status, 200, JSON.stringify(connectivity.body));
    assert.equal(connectivity.body.status, "succeeded");
    assert.deepEqual(receivedAuth.at(-1), "Bearer sk-issue40-reentry-key");
    const search = await page.evaluate(async () => {
      const response = await fetch("/v1/knowledge/search?sort=updated_desc", { credentials: "same-origin" });
      return response.status;
    });
    assert.equal(search, 200);
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ status: "passed", checks: [
      "tampered Provider is unavailable after API and Worker restart",
      "Provider key can be re-entered after recovery",
      "re-entered Provider succeeds through a real authenticated HTTPS probe",
      "legacy environment reference is absent after restart",
      "knowledge search remains available after Provider restart",
    ], evidence: { provider_id: provider.provider_id, pre_reentry_status: "unavailable",
      post_reentry_status: repaired.body.secret_status, connectivity: connectivity.body.status,
      search_status: search } }, null, 2));
  } catch (error) {
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ status: "failed", error: error.stack || String(error) }, null, 2));
    throw error;
  } finally { await context.close(); await browser.close(); await new Promise((resolve) => providerServer.close(resolve)); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
