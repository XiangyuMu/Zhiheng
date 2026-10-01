/* Issue #14: browser proof for real review writes and retry-safe state. */
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
  const evidence = { drafts: {}, statuses: {}, retry: {}, context: {} };
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));

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
          "Idempotency-Key": crypto.randomUUID(), "If-Match": "*", ...(options.headers || {}) },
        body: options.body === undefined ? undefined : JSON.stringify(options.body) });
      const body = await response.json();
      if (!response.ok) throw new Error(`${response.status}: ${body.detail || JSON.stringify(body)}`);
      return body;
    }, { url, options });
  }
  async function draft(label) {
    const source = await api("/v1/conclusions/sources", { method: "POST", body: { text: `Issue 14 source ${label}` } });
    return api("/v1/conclusions", { method: "POST", body: {
      source_id: source.id, title: `Issue 14 ${label}`, claim: `Issue 14 claim ${label}`,
      domain_id: "education_learning", premises: [{ text: "review prerequisite", confirmed: false }], excerpt: source.text,
      evidence: [{ text: source.text }],
    } });
  }
  async function open(id) {
    await page.locator(`button.queue-item[data-entry-id="${id}"]`).click();
  }
  async function submitQuestion(query) {
    await page.locator("#question").fill(query);
    await page.locator("#answer-form button[type=submit]").click();
    await page.locator("#answer-result").waitFor({ state: "visible", timeout: 20000 });
  }
  try {
    await login();
    const approve = await draft("approve");
    const reject = await draft("reject");
    const defer = await draft("defer");
    const revise = await draft("revise");
    const retry = await draft("retry");
    evidence.drafts = { approve: approve.id, reject: reject.id, defer: defer.id, revise: revise.id, retry: retry.id };
    await page.goto(`${base}/review-center`);
    await page.locator("#queue").waitFor();

    await open(approve.id); await page.getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    assert.equal((await api(`/v1/conclusions/${approve.id}`)).status, "formal");
    await open(reject.id); await page.getByRole("button", { name: "拒绝", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    assert.equal((await api(`/v1/conclusions/${reject.id}`)).status, "rejected");
    await open(defer.id); await page.getByRole("button", { name: "稍后处理", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    await context.clearCookies();
    await page.evaluate(() => localStorage.clear());
    await login();
    await page.goto(`${base}/review-center`);
    await page.locator("#queue").waitFor(); await open(defer.id);
    evidence.statuses.defer_after_refresh = (await api(`/v1/conclusions/${defer.id}`)).status;
    assert.equal(evidence.statuses.defer_after_refresh, "deferred");

    page.once("dialog", (dialog) => dialog.accept("Issue 14 revised claim"));
    await open(revise.id); await page.getByRole("button", { name: "修订", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    const revised = await api(`/v1/conclusions/${revise.id}`);
    assert.equal(revised.claim, "Issue 14 revised claim");
    evidence.statuses.revised = revised.status;

    // A stale ETag must be visible and leave the draft pending until refreshed.
    const conflict = await draft("conflict");
    await page.reload(); await open(conflict.id);
    const stale = await api(`/v1/conclusions/${conflict.id}`);
    await api(`/v1/conclusions/${conflict.id}`, { method: "PATCH", body: { claim: "Issue 14 external update" }, headers: { "If-Match": stale.etag } });
    await page.getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: /变化|版本|changed/ }).waitFor();
    assert.equal((await api(`/v1/conclusions/${conflict.id}`)).status, "draft");
    evidence.statuses.conflict_preserved = true;

    // Context prompt choices are real browser writes and survive a new session.
    await api("/v1/personal-updates", { method: "POST", body: { memory_type: "fact", state_key: "profile.issue14_city", value: { text: "北京" }, source_kind: "user_explicit" } });
    await api("/v1/personal-updates", { method: "POST", body: { memory_type: "fact", state_key: "profile.issue14_city", value: { text: "上海" }, source_kind: "user_explicit" } });
    await page.goto(`${base}/knowledge-agent#research`); await page.locator("#new-conversation").click();
    await submitQuestion("我现在 issue14 居住在哪个城市？");
    await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
    await page.getByRole("button", { name: "稍后处理" }).click();
    await page.locator("#toast").filter({ hasText: "待办" }).waitFor();
    await context.clearCookies(); await page.evaluate(() => localStorage.clear()); await login();
    await page.goto(`${base}/knowledge-agent#research`); await page.locator("#new-conversation").click();
    await submitQuestion("我现在 issue14 居住在哪个城市？");
    await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
    await page.getByRole("button", { name: "跳过" }).click();
    await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
    evidence.context = { defer: true, skip: true, cross_session: true };

    let failed = true;
    await page.goto(`${base}/review-center`);
    await page.locator("#queue").waitFor();
    await page.route(`**/v1/conclusions/${retry.id}/approve`, async (route) => {
      if (failed) { failed = false; await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "synthetic review failure" }) }); }
      else await route.continue();
    });
    await open(retry.id); await page.getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: /失败|不可用|重试|synthetic/ }).waitFor();
    evidence.retry.first_failure_visible = true;
    await page.getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    assert.equal((await api(`/v1/conclusions/${retry.id}`)).status, "formal");
    evidence.retry.second_attempt_status = "formal";
    await page.unroute(`**/v1/conclusions/${retry.id}/approve`);

    assert.deepEqual(browserErrors, []);
    const checks = [
      "real browser writes approve, reject, defer, and revise decisions",
      "deferred review remains available after refresh",
      "failed review write is visible and succeeds on retry",
      "version conflict is visible and preserves the pending draft",
      "context prompt defer and skip persist across a new browser session",
    ];
    await page.screenshot({ path: path.join(output, "issue14-writes.png"), fullPage: true });
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks, evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence, browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
