/* Issue #47: verify the model catalog survives an API/Worker restart. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output) throw new Error("Pass the acceptance base URL and output directory");
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  async function api(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((value) => value.trim())
        .find((value) => value.startsWith("zhiheng_csrf="))?.slice(13) || "";
      const response = await fetch(url, {
        credentials: "same-origin",
        ...options,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID(),
          ...(options.headers || {}),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(`${response.status}: ${body.detail || JSON.stringify(body)}`);
      return body;
    }, { url, options });
  }
  try {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }),
      });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");
    await page.goto(`${base}/knowledge-agent#settings`);
    await page.locator("#model-config-list").waitFor();

    const providers = await api("/v1/model-config/providers?include_archived=true");
    const provider = providers.find((item) => item.display_name === "Issue 47 Browser Provider");
    assert(provider, "Issue 47 provider missing after process restart");
    const model = (provider.model_records || []).find((item) => item.model_id === "issue47-chat");
    assert(model, "Issue 47 model missing after process restart");
    assert.deepEqual(model.confirmed_capabilities, ["text"]);
    assert.equal(model.stale, true);
    assert.equal(model.enabled, false);
    assert.equal(provider.catalog_status, "failed");
    const defaults = (await api("/v1/model-config/status")).defaults;
    assert.deepEqual(defaults.text, {
      provider_id: provider.provider_id,
      model_id: "issue47-chat",
    });

    const card = page.locator("li.provider-card").filter({ hasText: "Issue 47 Browser Provider" });
    await card.waitFor();
    assert.match(await card.innerText(), /issue47-chat[\s\S]*已过期/);
    assert((await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 1);
    await page.screenshot({ path: path.join(output, "provider-models-restart.png"), fullPage: true });

    await api(`/v1/model-config/providers/${provider.provider_id}`, {
      method: "PATCH",
      headers: { "If-Match": provider.etag },
      body: { enabled: false, archived: true },
    });
    const currentDefaults = (await api("/v1/model-config/status")).defaults;
    await api("/v1/model-config/defaults", {
      method: "PUT",
      headers: { "If-Match": currentDefaults.etag },
      body: { text: null, multimodal: null, embedding: null },
    });
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      status: "passed",
      checks: [
        "confirmed default model survives API and Worker restart",
        "stale model and catalog failure remain visible after refresh",
      ],
      provider_id: provider.provider_id,
      model_id: model.model_id,
      confirmed_capabilities: model.confirmed_capabilities,
      stale: model.stale,
      enabled: model.enabled,
      catalog_status: provider.catalog_status,
      default_text: defaults.text,
      default_text_survived_restart: true,
    }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
