/* Issue #47: browser acceptance for Provider model catalog management. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback server URL and output directory");
}
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const errors = [];
  let defaultPayload = "";
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.url().includes("/v1/model-config/defaults")) defaultPayload = request.postData() || "";
  });
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
    await page.goto(`${base}/knowledge-agent#settings`);
    await page.locator("#model-config-list").waitFor();
    const initialDefaults = (await api("/v1/model-config/status")).defaults;

    await page.getByRole("button", { name: "新增 Provider", exact: true }).click();
    await page.locator("#provider-kind").selectOption("ollama");
    await page.locator("#provider-name").fill("Issue 47 Browser Ollama");
    await page.locator("#provider-base-url").fill("http://127.0.0.1:9");
    await page.locator("#provider-enabled").check();
    await page.locator("#model-provider-form button[type=submit]").click();
    await page.getByText("Provider 配置已保存").waitFor();

    const card = page.locator("li.provider-card").filter({ hasText: "Issue 47 Browser Ollama" });
    await card.getByPlaceholder("手动添加模型 ID").fill("issue47-chat");
    await card.getByRole("button", { name: "添加模型", exact: true }).click();
    await page.getByText("模型 issue47-chat 已添加").waitFor();

    const capability = card.getByRole("checkbox", { name: /Issue 47 Browser Ollama issue47-chat text 能力/ });
    assert.equal(await capability.isChecked(), false);
    await capability.check();
    await card.getByRole("button", { name: "保存能力", exact: true }).click();
    await page.getByText("模型 issue47-chat 能力已更新").waitFor();
    await page.locator("#default-text-model").selectOption({ label: "Issue 47 Browser Ollama / issue47-chat" });
    const defaultsResponse = page.waitForResponse("**/v1/model-config/defaults");
    await page.getByRole("button", { name: "保存默认模型", exact: true }).click();
    const defaultsResult = await defaultsResponse;
    if (!defaultsResult.ok()) throw new Error(`default model update failed: ${defaultsResult.status()} ${await defaultsResult.text()} payload=${defaultPayload}`);
    await page.getByText("默认模型已更新").waitFor();

    const text = await page.locator("body").innerText();
    assert(!text.includes("ciphertext_b64"));
    assert(!text.includes("api_key"));
    assert(errors.length === 0, errors.join("; "));
    await page.screenshot({ path: path.join(output, "provider-models-issue47.png"), fullPage: true });
    fs.writeFileSync(path.join(output, "evidence.json"), JSON.stringify({
      provider: "Issue 47 Browser Ollama",
      model: "issue47-chat",
      initial_text_capability_confirmed: false,
      text_capability_confirmed: true,
      default_text_selected: true,
      viewport: { width: 1440, height: 1080 },
    }, null, 2));

    // Leave the shared acceptance database in its pre-test route state so the
    // following restart and provider-secret scenarios remain independent.
    const currentDefaults = (await api("/v1/model-config/status")).defaults;
    await api("/v1/model-config/defaults", {
      method: "PUT",
      headers: { "If-Match": currentDefaults.etag },
      body: { text: null, multimodal: null, embedding: null },
    });
    const restoredDefaults = (await api("/v1/model-config/status")).defaults;
    const providers = await api("/v1/model-config/providers?include_archived=true");
    const provider = providers.find((item) => item.display_name === "Issue 47 Browser Ollama");
    if (provider) {
      await api(`/v1/model-config/providers/${provider.provider_id}`, {
        method: "PATCH",
        headers: { "If-Match": provider.etag },
        body: { enabled: false, archived: true },
      });
    }
    fs.writeFileSync(path.join(output, "cleanup.json"), JSON.stringify({
      restored_defaults: restoredDefaults,
      archived_provider_id: provider?.provider_id || null,
    }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
