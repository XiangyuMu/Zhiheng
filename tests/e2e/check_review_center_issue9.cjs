/* Issue #9: centralized review survives sessions and exposes safe write failures. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { randomUUID } = require("node:crypto");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback URL and output directory");
}
fs.mkdirSync(output, { recursive: true });
const tag = randomUUID().replaceAll("-", "").slice(0, 10);

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = { drafts: [], decisions: [], recovery: [] };
  page.on("pageerror", (error) => browserErrors.push(error.message));

  async function api(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((item) => item.trim())
        .find((item) => item.startsWith("zhiheng_csrf="))?.slice("zhiheng_csrf=".length) || "";
      const response = await fetch(url, {
        credentials: "same-origin", ...options,
        headers: { Accept: "application/json", "Content-Type": "application/json",
          "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID(), "If-Match": "*",
          ...(options.headers || {}) },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      const text = await response.text();
      let body = {};
      try { body = text ? JSON.parse(text) : {}; } catch (_) { body = { raw: text }; }
      if (!response.ok) {
        const error = new Error(body.detail || text || `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return body;
    }, { url, options });
  }
  async function login() {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }) });
      if (!response.ok) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");
  }
  async function createDraft(index) {
    const source = await api("/v1/conclusions/sources", { method: "POST", body: {
      text: `Issue 9 浏览器原文 ${tag}-${index}：固定条件下复习有效。`,
    } });
    return api("/v1/conclusions", { method: "POST", body: {
      source_id: source.id, title: `Issue 9 草稿 ${tag}-${index}`,
      claim: `固定条件下复习有效 ${tag}-${index}`, domain_id: "education_learning",
      premises: [{ text: "固定条件", confirmed: false }], excerpt: source.text,
      evidence: [{ text: source.text }],
    } });
  }
  try {
    await login();
    const drafts = await Promise.all([0, 1, 2].map(createDraft));
    evidence.drafts = drafts.map((draft) => draft.id);

    await page.goto(`${base}/review-center`);
    await page.locator("#total").waitFor({ state: "visible" });
    await page.locator("#queue").filter({ hasText: `Issue 9 草稿 ${tag}-0` }).waitFor();
    assert(Number(await page.locator("#conclusion-count").innerText()) >= 3);
    await page.locator(`button.queue-item[data-entry-id="${drafts[0].id}"]`).click();
    assert((await page.locator("#detail").innerText()).includes("固定条件"));

    // Closing the page and logging in again must leave an unapproved draft intact.
    await page.goto(`${base}/knowledge-agent#research`);
    assert.equal((await api(`/v1/conclusions/${drafts[0].id}`)).status, "draft");
    await login();
    await page.goto(`${base}/review-center`);
    await page.locator(`button.queue-item[data-entry-id="${drafts[0].id}"]`).waitFor();
    evidence.recovery.push("draft_restored_after_relogin");

    // Keep a stale detail open, mutate through the real API, then verify UI conflict and retry.
    await page.locator(`button.queue-item[data-entry-id="${drafts[1].id}"]`).click();
    const stale = await api(`/v1/conclusions/${drafts[1].id}`);
    await api(`/v1/conclusions/${drafts[1].id}`, { method: "PATCH", body: { claim: `外部更新 ${tag}` },
      headers: { "If-Match": stale.etag } });
    await page.getByRole("button", { name: "批准" }).click();
    await page.locator("#message").filter({ hasText: /变化|changed|版本/ }).waitFor();
    evidence.decisions.push("version_conflict_shown");
    await page.locator("#refresh").click();
    await page.locator(`button.queue-item[data-entry-id="${drafts[1].id}"]`).click();
    await page.getByRole("button", { name: "批准" }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    assert.equal((await api(`/v1/conclusions/${drafts[1].id}`)).status, "formal");
    evidence.decisions.push("retry_after_refresh_approved");

    // Closing a detail without choosing an action must never approve it.
    await page.goto(`${base}/knowledge-agent#research`);
    assert.equal((await api(`/v1/conclusions/${drafts[2].id}`)).status, "draft");
    evidence.recovery.push("close_without_approval_preserved_draft");
    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "review queue shows the draft count and full draft detail",
      "unapproved draft is restored after closing and re-login",
      "version conflict is shown in the browser and retry after refresh succeeds",
      "closing review without an action does not approve the draft",
    ], evidence, browserErrors }, null, 2));
    await page.screenshot({ path: path.join(output, "issue9-review-center.png"), fullPage: true });
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
