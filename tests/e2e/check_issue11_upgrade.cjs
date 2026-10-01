/* Issue #11: upgraded legacy data and new review flow remain continuous. */
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
  page.on("pageerror", (error) => errors.push(error.message));

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
  try {
    await login();
    const before = await api("/v1/review/summary?limit=500");
    const legacy = before.conclusions.find((item) => item.id === "issue11-legacy-entry");
    assert(legacy, "legacy conclusion was lost during migration");
    assert.equal(legacy.status, "draft");
    const source = await api("/v1/conclusions/sources", { method: "POST", body: { text: "Issue 11 新审核原文" } });
    const draft = await api("/v1/conclusions", { method: "POST", body: {
      source_id: source.id, title: "Issue 11 新审核草稿", claim: "升级后仍可创建新草稿",
      domain_id: "education_learning", premises: [{ text: "迁移完成", confirmed: true }], excerpt: source.text,
      evidence: [{ text: source.text }],
    } });
    await page.goto(`${base}/review-center`);
    await page.locator(`button.queue-item[data-entry-id="${draft.id}"]`).waitFor();
    await page.locator(`button.queue-item[data-entry-id="issue11-legacy-entry"]`).waitFor();
    await page.locator(`button.queue-item[data-entry-id="issue11-legacy-entry"]`).click();
    const legacyDetail = await page.locator("#detail").innerText();
    assert(legacyDetail.includes("历史结论在升级后仍可读取"));
    await page.locator(`button.queue-item[data-entry-id="${draft.id}"]`).click();
    assert((await page.locator("#detail").innerText()).includes("升级后仍可创建新草稿"));
    const approvalResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/v1/conclusions/${draft.id}/approve`) && response.request().method() === "POST");
    await page.getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    const approvalBody = await (await approvalResponse).json();
    const approved = await api(`/v1/conclusions/${draft.id}`);
    assert.equal(approved.status, "formal");
    assert(approvalBody.knowledge_id);
    const after = await api("/v1/review/summary?limit=500");
    assert(after.conclusions.some((item) => item.id === "issue11-legacy-entry"));
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      status: "passed",
      checks: [
        "supported legacy schema data survives upgrade and remains in the review queue",
        "login creates a draft, approves it, and preserves upgraded history",
      ],
      evidence: {
        legacy_entry_id: legacy.id,
        legacy_source_id: legacy.source?.id || "issue11-legacy-source",
        new_draft_id: draft.id,
        review_action: "approve",
        approved_status: approved.status,
        approved_knowledge_id: approvalBody.knowledge_id,
        legacy_detail_visible: true,
      },
      browserErrors: errors,
    }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", error: error.stack || String(error), browserErrors: errors }, null, 2));
    throw error;
  } finally { await context.close(); await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
