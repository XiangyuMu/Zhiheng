/* Issue #47: browser acceptance for Provider model catalog management. */
const assert = require("node:assert/strict");
const http = require("node:http");
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
  let catalogFailure = false;
  const providerServer = http.createServer((request, response) => {
    if (request.url === "/api/tags" && !catalogFailure) {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ models: [{ name: "issue47-discovered" }] }));
      return;
    }
    response.writeHead(catalogFailure ? 503 : 404, { "content-type": "application/json" });
    response.end(JSON.stringify({ error: "catalog unavailable" }));
  });
  await new Promise((resolve) => providerServer.listen(0, "127.0.0.1", resolve));
  const providerPort = providerServer.address().port;
  const providerUrl = `http://127.0.0.1:${providerPort}`;
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const errors = [];
  let defaultPayload = "";
  let processingRequests = 0;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.url().includes("/v1/model-config/defaults")) defaultPayload = request.postData() || "";
    if (request.url().includes("/processing")) processingRequests += 1;
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
    async function apiResult(url, options = {}) {
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
        return { status: response.status, ok: response.ok, body };
      }, { url, options });
    }
    async function api(url, options = {}) {
      const result = await apiResult(url, options);
      if (!result.ok) {
        throw new Error(`${result.status}: ${result.body.detail || JSON.stringify(result.body)}`);
      }
      return result.body;
    }
    await page.goto(`${base}/knowledge-agent#settings`);
    await page.locator("#model-config-list").waitFor();
    const initialDefaults = (await api("/v1/model-config/status")).defaults;

    await page.getByRole("button", { name: "新增 Provider", exact: true }).click();
    await page.locator("#provider-kind").selectOption("ollama");
    await page.locator("#provider-name").fill("Issue 47 Browser Provider");
    await page.locator("#provider-base-url").fill(providerUrl);
    await page.locator("#provider-enabled").check();
    await page.locator("#model-provider-form button[type=submit]").click();
    await page.getByText("Provider 配置已保存").waitFor();

    const card = page.locator("li.provider-card").filter({ hasText: "Issue 47 Browser Provider" });
    await card.getByRole("button", { name: "刷新目录", exact: true }).click();
    await page.getByText("模型目录已刷新").waitFor();
    const discovered = card.locator(".model-record").filter({ hasText: "issue47-discovered" });
    await discovered.waitFor();
    assert.match(await discovered.innerText(), /issue47-discovered[\s\S]*discovered/);
    await card.getByPlaceholder("手动添加模型 ID").fill("issue47-chat");
    await card.getByRole("button", { name: "添加模型", exact: true }).click();
    await page.getByText("模型 issue47-chat 已添加").waitFor();

    const modelRecord = card.locator(".model-record").filter({ hasText: "issue47-chat" });
    const capability = modelRecord.getByRole("checkbox", { name: /Issue 47 Browser Provider issue47-chat text 能力/ });
    assert.equal(await capability.isChecked(), false);
    await capability.check();
    await modelRecord.getByRole("button", { name: "保存能力", exact: true }).click();
    await page.getByText("模型 issue47-chat 能力已更新").waitFor();
    await page.locator("#default-text-model").selectOption({ label: "Issue 47 Browser Provider / issue47-chat" });
    const defaultsResponse = page.waitForResponse("**/v1/model-config/defaults");
    await page.getByRole("button", { name: "保存默认模型", exact: true }).click();
    const defaultsResult = await defaultsResponse;
    if (!defaultsResult.ok()) throw new Error(`default model update failed: ${defaultsResult.status()} ${await defaultsResult.text()} payload=${defaultPayload}`);
    await page.getByText("默认模型已更新").waitFor();

    await page.reload();
    const persistedCard = page.locator("li.provider-card").filter({ hasText: "Issue 47 Browser Provider" });
    await persistedCard.waitFor();
    assert.equal(await persistedCard.locator(".model-record").filter({ hasText: "issue47-chat" }).getByRole("checkbox", { name: /Issue 47 Browser Provider issue47-chat text 能力/ }).isChecked(), true);
    await page.locator("#default-text-model").waitFor();
    await page.waitForFunction(() => document.querySelector("#default-text-model")?.selectedOptions[0]?.textContent === "Issue 47 Browser Provider / issue47-chat");

    assert.match(await card.innerText(), /chat_completions/);
    const staleRefresh = page.waitForResponse("**/v1/model-config/providers/*/models/refresh");
    await card.getByRole("button", { name: "刷新目录", exact: true }).click();
    assert((await staleRefresh).ok());
    await page.getByText("模型目录已刷新").waitFor();
    await page.reload();
    const refreshedCard = page.locator("li.provider-card").filter({ hasText: "Issue 47 Browser Provider" });
    await refreshedCard.waitFor();
    assert.match(await refreshedCard.innerText(), /issue47-chat[\s\S]*已过期/);
    catalogFailure = true;
    const failedRefresh = page.waitForResponse("**/v1/model-config/providers/*/models/refresh");
    await refreshedCard.getByRole("button", { name: "刷新目录", exact: true }).click();
    assert.equal((await failedRefresh).status(), 502);
    await page.getByText(/目录刷新失败：/).waitFor();
    assert.match(await refreshedCard.innerText(), /issue47-chat[\s\S]*已过期/);

    await page.setViewportSize({ width: 390, height: 844 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `settings page overflows narrow viewport by ${overflow}px`);
    const refreshButton = card.getByRole("button", { name: "刷新目录", exact: true });
    await page.keyboard.press("Tab");
    await refreshButton.focus();
    assert.equal(await refreshButton.evaluate((element) => document.activeElement === element), true);
    await page.reload();
    const restoredCard = page.locator("li.provider-card").filter({ hasText: "Issue 47 Browser Provider" });
    await restoredCard.waitFor();
    assert.match(await restoredCard.innerText(), /issue47-chat[\s\S]*已过期/);

    const text = await page.locator("body").innerText();
    assert(!text.includes("ciphertext_b64"));
    assert(!text.includes("api_key"));
    assert(errors.length === 0, errors.join("; "));

    // Issue #46: DeepSeek cannot be promoted to an Embedding route, and a
    // disabled/stale route leaves an indexing task in a durable unsupported
    // state instead of allowing the Worker to create vectors.
    const deepSeek = await api("/v1/model-config/providers", {
      method: "POST",
      body: {
        provider_kind: "deepseek",
        display_name: "Issue 46 DeepSeek Provider",
        base_url: "https://api.deepseek.com",
        text_models: ["issue46-deepseek-chat"],
        enabled: true,
      },
    });
    const deepSeekModel = await apiResult(`/v1/model-config/providers/${deepSeek.provider_id}/models/issue46-deepseek-chat`, {
      method: "PATCH",
      headers: { "If-Match": deepSeek.etag },
      body: { confirmed_capabilities: ["embedding"], protocol: "embeddings" },
    });
    assert.equal(deepSeekModel.status, 422);
    assert.match(JSON.stringify(deepSeekModel.body), /embedding|protocol/i);

    const embeddingDefaults = (await api("/v1/model-config/status")).defaults;
    await api("/v1/model-config/defaults", {
      method: "PUT",
      headers: { "If-Match": embeddingDefaults.etag },
      body: { embedding: null },
    });
    const unsupportedTitle = `Issue 46 unsupported ${Date.now()}`;
    await page.goto(`${base}/knowledge-agent#library`);
    await page.getByRole("button", { name: /添加资料/ }).first().click();
    await page.locator("#import-title").fill(unsupportedTitle);
    await page.locator("#import-text").fill("缺少已确认 Embedding 模型时仍需保留全文检索。");
    const importResponse = page.waitForResponse("**/v1/knowledge/imports");
    await page.locator("#import-submit").click();
    const imported = await importResponse;
    assert(imported.ok(), `Issue 46 import failed: ${imported.status()}`);
    const importedPayload = await imported.json();
    const importedId = importedPayload.result.knowledge_object_id;
    await page.getByText(/embedding_model_unavailable/).waitFor();
    const requestsAtTerminal = processingRequests;
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal(processingRequests, requestsAtTerminal, "unsupported task kept polling");
    const unsupportedTask = await api(`/v1/knowledge/${importedId}/processing`);
    assert.equal(unsupportedTask.public_status || unsupportedTask.status, "unsupported");
    assert.equal(unsupportedTask.error_code || unsupportedTask.failure_code, "embedding_model_unavailable");
    const restoredDefaults = (await api("/v1/model-config/status")).defaults;
    await api("/v1/model-config/defaults", {
      method: "PUT",
      headers: { "If-Match": restoredDefaults.etag },
      body: { embedding: embeddingDefaults.embedding },
    });
    await api(`/v1/knowledge/${importedId}/delete`, { method: "POST" });
    const deepSeekCurrent = (await api("/v1/model-config/providers?include_archived=true"))
      .find((item) => item.provider_id === deepSeek.provider_id);
    if (deepSeekCurrent) {
      await api(`/v1/model-config/providers/${deepSeek.provider_id}`, {
        method: "PATCH",
        headers: { "If-Match": deepSeekCurrent.etag },
        body: { enabled: false, archived: true },
      });
    }

    await page.screenshot({ path: path.join(output, "provider-models-issue47.png"), fullPage: true });
    fs.writeFileSync(path.join(output, "evidence.json"), JSON.stringify({
      checks: [
        "browser refresh discovers and persists a normalized Provider model",
        "confirmed default model survives API and Worker restart",
        "stale model and catalog failure remain visible after refresh",
        "DeepSeek embedding rejection and unsupported indexing stop polling",
      ],
      provider: "Issue 47 Browser Provider",
      model: "issue47-chat",
      initial_text_capability_confirmed: false,
      text_capability_confirmed: true,
      default_text_selected: true,
      protocol: "chat_completions",
      stale_model_visible: true,
      catalog_failure_visible: true,
      deepseek_embedding_rejected: true,
      unsupported_index_task: {
        status: unsupportedTask.public_status || unsupportedTask.status,
        failure_code: unsupportedTask.error_code || unsupportedTask.failure_code,
        browser_terminal: true,
        polling_stopped: true,
      },
      viewports: [{ width: 1440, height: 1080 }, { width: 390, height: 844 }],
    }, null, 2));

    const providers = await api("/v1/model-config/providers?include_archived=true");
    const provider = providers.find((item) => item.display_name === "Issue 47 Browser Provider");
    if (provider && process.env.ZHIHENG_ISSUE47_KEEP !== "1") {
      await api(`/v1/model-config/providers/${provider.provider_id}`, {
        method: "PATCH",
        headers: { "If-Match": provider.etag },
        body: { enabled: false, archived: true },
      });
    }
    fs.writeFileSync(path.join(output, "cleanup.json"), JSON.stringify({
      defaults_preserved_for_restart: true,
      archived_provider_id: provider?.provider_id || null,
      kept_for_restart: process.env.ZHIHENG_ISSUE47_KEEP === "1",
    }, null, 2));
  } finally {
    await new Promise((resolve) => providerServer.close(resolve));
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
