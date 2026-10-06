/* Issue #40: browser acceptance for encrypted Provider secret lifecycle. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const https = require("node:https");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback server URL and output directory");
}
fs.mkdirSync(output, { recursive: true });
const syntheticKey = "sk-issue40-browser-synthetic-key";
const rotatedKey = `${syntheticKey}-rotated`;
const legacyKey = "issue40-browser-legacy-key";
const legacyRef = "env:ZHIHENG_PRIVATE_ISSUE40_LEGACY";

(async () => {
  let receivedAuth = [];
  const providerServer = https.createServer({
    key: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_KEY),
    cert: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_CERT),
  }, (request, response) => {
    if (request.url === "/models") {
      receivedAuth.push(request.headers.authorization || "");
      if (!request.headers.authorization) {
        response.writeHead(401, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: "missing authorization" }));
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
  const providerPort = providerServer.address().port;
  const providerUrl = `https://127.0.0.1:${providerPort}`;
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const errors = [];
  const responses = [];
  const responseBodyReads = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    responses.push(response);
    if (["xhr", "fetch"].includes(response.request().resourceType())) {
      responseBodyReads.push(response.text().then((body) => ({ url: response.url(), body }))
        .catch((error) => ({ url: response.url(), error })));
    }
  });
  async function login() {
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
  }
  async function api(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((v) => v.trim()).find((v) => v.startsWith("zhiheng_csrf="))?.slice(13) || "";
      const response = await fetch(url, { credentials: "same-origin", ...options,
        headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID(), ...(options.headers || {}) },
        body: options.body === undefined ? undefined : JSON.stringify(options.body) });
      const body = await response.json();
      if (!response.ok) throw new Error(`${response.status}: ${body.detail || JSON.stringify(body)}`);
      return body;
    }, { url, options });
  }
  const evidence = {};
  try {
    await login();
    await page.goto(`${base}/knowledge-agent#settings`);
    await page.locator("#model-provider-form").waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "新增 Provider", exact: true }).click();
    await page.locator("#provider-name").fill("Issue 40 Browser Provider");
    await page.locator("#provider-kind").selectOption("openai-compatible");
    await page.locator("#provider-base-url").fill(providerUrl);
    await page.locator("#provider-api-key").fill(syntheticKey);
    await page.locator("#provider-text-models").fill("model-a");
    await page.locator("#provider-enabled").check();
    await page.locator("#model-provider-form button[type=submit]").click();
    await page.getByText("Provider 配置已保存").waitFor();
    const pageAfterCreate = await page.locator("body").innerText();
    assert(!pageAfterCreate.includes(syntheticKey));
    const providers = await api("/v1/model-config/providers");
    const provider = providers.find((item) => item.display_name === "Issue 40 Browser Provider");
    assert(provider && provider.secret_status === "configured");
    assert(!JSON.stringify(providers).includes(syntheticKey));
    evidence.create = { provider_id: provider.provider_id, secret_status: provider.secret_status, fingerprint: provider.secret_fingerprint };
    const connectivity = await api(`/v1/model-config/providers/${provider.provider_id}/connectivity-test`, { method: "POST" });
    assert.equal(connectivity.status, "succeeded");
    assert.deepEqual(receivedAuth.at(-1), `Bearer ${syntheticKey}`);
    evidence.connection = { status: connectivity.status, diagnostic_code: connectivity.diagnostic_code || null };
    await page.reload();
    const refreshed = await api("/v1/model-config/providers");
    const refreshedProvider = refreshed.find((item) => item.provider_id === provider.provider_id);
    assert.equal(refreshedProvider.secret_status, "configured");
    assert.equal(refreshedProvider.secret_fingerprint, provider.secret_fingerprint);
    assert(!JSON.stringify(refreshedProvider).includes(syntheticKey));
    evidence.refresh = { secret_status: refreshedProvider.secret_status, fingerprint: refreshedProvider.secret_fingerprint };

    const createdCard = page.locator("li.provider-card").filter({ hasText: "Issue 40 Browser Provider" });
    await createdCard.getByRole("button", { name: "编辑", exact: true }).click();
    await page.locator("#provider-api-key").fill(rotatedKey);
    await page.locator("#model-provider-form button[type=submit]").click();
    await page.getByText("Provider 配置已保存").waitFor();
    const rotated = (await api("/v1/model-config/providers")).find((item) => item.provider_id === provider.provider_id);
    assert.equal(rotated.secret_version, 2);
    assert.equal((await page.locator("body").innerText()).includes(rotatedKey), false);
    evidence.rotation = { secret_version: rotated.secret_version, fingerprint: rotated.secret_fingerprint };

    const card = page.locator("li.provider-card").filter({ hasText: "Issue 40 Browser Provider" });
    page.once("dialog", (dialog) => dialog.accept());
    await card.getByRole("button", { name: "删除密钥", exact: true }).click();
    await page.getByText("密钥已删除，Provider 已停用").waitFor();
    const deleted = (await api("/v1/model-config/providers?include_archived=true")).find((item) => item.provider_id === provider.provider_id);
    assert.equal(deleted.enabled, false);
    assert.equal(deleted.secret_status, "missing");
    evidence.deletion = { enabled: deleted.enabled, secret_status: deleted.secret_status };

    const legacy = await api("/v1/model-config/providers", { method: "POST", body: {
      provider_kind: "openai-compatible", display_name: "Issue 40 Legacy Provider",
      base_url: providerUrl, secret_ref: legacyRef,
      text_models: ["model-a"], enabled: true,
    }});
    await page.reload();
    const legacyCard = page.locator("li.provider-card").filter({ hasText: "Issue 40 Legacy Provider" });
    await legacyCard.getByRole("button", { name: "迁移到本地加密", exact: true }).click();
    await page.getByText("密钥已迁移到本地加密存储").waitFor();
    const migrated = (await api("/v1/model-config/providers")).find((item) => item.provider_id === legacy.provider_id);
    assert.equal(migrated.secret_source, "local");
    assert.equal(migrated.secret_version, 1);
    assert(!JSON.stringify(migrated).includes(legacyRef));
    const migratedConnectivity = await api(`/v1/model-config/providers/${migrated.provider_id}/connectivity-test`, { method: "POST" });
    assert.equal(migratedConnectivity.status, "succeeded");
    assert.deepEqual(receivedAuth.at(-1), `Bearer ${legacyKey}`);
    evidence.migration = { provider_id: migrated.provider_id, secret_source: migrated.secret_source, secret_version: migrated.secret_version };
    const audit = await api(`/v1/model-config/audits?provider_id=${migrated.provider_id}`);
    assert(!JSON.stringify(audit).includes(syntheticKey));
    evidence.audit = { entries: Array.isArray(audit) ? audit.length : (audit.items || []).length };

    const storage = await page.evaluate(() => ({ local: JSON.stringify(localStorage), session: JSON.stringify(sessionStorage) }));
    assert(!storage.local.includes(syntheticKey) && !storage.session.includes(syntheticKey));
    const visible = await page.locator("body").innerText();
    assert(!visible.includes(syntheticKey) && !visible.includes("ciphertext_b64"));
    for (const result of await Promise.all(responseBodyReads)) {
      if (result.error) {
        throw new Error(`could not read response body for ${result.url}: ${result.error}`);
      }
      const body = result.body;
      assert(!body.includes(syntheticKey) && !body.includes(rotatedKey) && !body.includes(legacyKey));
      assert(!body.includes("ciphertext_b64"));
    }
    const screenshotPath = path.join(output, "provider-secrets.png");
    await page.screenshot({ path: screenshotPath, fullPage: true });
    const acceptanceRoot = process.env.ZHIHENG_ACCEPTANCE_OUTPUT || output;
    const logContents = ["api.log", "worker.log"].map((name) => {
      const file = path.join(acceptanceRoot, name);
      return fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
    }).join("\n");
    assert(fs.existsSync(path.join(acceptanceRoot, "api.log")) && fs.existsSync(path.join(acceptanceRoot, "worker.log")));
    assert(!logContents.includes(syntheticKey) && !logContents.includes(rotatedKey) && !logContents.includes(legacyKey));
    const databaseUrl = process.env.ZHIHENG_DATABASE_URL || "";
    const databasePath = databaseUrl.startsWith("sqlite:///") ? databaseUrl.slice("sqlite:///".length) : "";
    assert(databasePath && fs.existsSync(databasePath));
    const databaseFiles = [databasePath, `${databasePath}-wal`, `${databasePath}-shm`]
      .filter((file) => fs.existsSync(file));
    const databaseBytes = Buffer.concat(databaseFiles.map((file) => fs.readFileSync(file)));
    for (const key of [syntheticKey, rotatedKey, legacyKey]) {
      assert(!databaseBytes.includes(Buffer.from(key)));
    }
    assert(fs.statSync(screenshotPath).size > 0);
    evidence.leakage = {
      page: true,
      storage: true,
      http_responses: responses.length,
      api_log: true,
      worker_log: true,
      database: true,
      screenshot: "provider-secrets.png",
      audit: true,
      legacy_key_scanned: true,
      model_config_responses: responses.filter((response) => response.url().includes("/v1/model-config")).length,
    };
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "real browser creates encrypted Provider key without rendering plaintext",
      "refresh and API listing retain only configured state and short fingerprint",
      "browser completes a real Provider connectivity test with a stable diagnostic",
      "browser rotation creates a new version and deletion disables the Provider",
      "browser deletion disables the Provider and removes the active secret",
      "browser migrates a legacy env reference to local encrypted storage",
      "synthetic key and complete ciphertext are absent from page, storage, and model-config responses",
    ], evidence, browserErrors: errors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", error: error.stack || String(error), evidence, browserErrors: errors }, null, 2));
    throw error;
  } finally { await context.close(); await browser.close(); await new Promise((resolve) => providerServer.close(resolve)); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
