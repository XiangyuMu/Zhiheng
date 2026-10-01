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
  const evidence = { drafts: [], decisions: [], recovery: [], navigation: {}, details: {}, revision: {} };
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
    const beforeReview = await api("/v1/review/summary?limit=500");
    const drafts = await Promise.all([0, 1, 2].map(createDraft));
    evidence.drafts = drafts.map((draft) => draft.id);
    const afterCreate = await api("/v1/review/summary?limit=500");

    // A draft must be visible as a count on the ordinary conversation page, without
    // redirecting the user into the review center or opening a detail automatically.
    await page.goto(`${base}/knowledge-agent#research`);
    await page.locator("#screen-research").waitFor({ state: "visible" });
    await page.locator("#review-nav-count").waitFor({ state: "visible" });
    assert(Number(await page.locator("#review-nav-count").innerText()) >= 3);
    assert.equal(
      await page.locator("#review-nav-count").innerText(),
      String(afterCreate.counts.total),
    );
    assert(!page.url().includes("/review-center"));
    assert.equal(await page.locator("#detail").count(), 0);
    evidence.navigation = {
      research_url: page.url(),
      review_badge: Number(await page.locator("#review-nav-count").innerText()),
      review_summary_total: afterCreate.counts.total,
      review_not_opened_automatically: true,
    };

    await page.goto(`${base}/review-center`);
    await page.locator("#total").waitFor({ state: "visible" });
    await page.locator("#queue").filter({ hasText: `Issue 9 草稿 ${tag}-0` }).waitFor();
    assert.equal(
      Number(await page.locator("#conclusion-count").innerText()),
      Number(afterCreate.counts?.conclusions || 0),
    );
    assert(Number(await page.locator("#conclusion-count").innerText())
      >= Number(beforeReview.counts?.conclusions || 0) + 3);
    await page.locator(`button.queue-item[data-entry-id="${drafts[0].id}"]`).click();
    const firstDetail = await page.locator("#detail").innerText();
    assert(firstDetail.includes(`Issue 9 草稿 ${tag}-0`));
    assert(firstDetail.includes(`固定条件下复习有效 ${tag}-0`));
    assert(firstDetail.includes("假设：固定条件"));
    assert(firstDetail.includes(`Issue 9 浏览器原文 ${tag}-0：固定条件下复习有效。`));
    assert(firstDetail.includes("education_learning"));
    assert(firstDetail.includes("暂无关系建议"));
    evidence.details = {
      title: `Issue 9 草稿 ${tag}-0`,
      claim: `固定条件下复习有效 ${tag}-0`,
      premise: "固定条件",
      source: `Issue 9 浏览器原文 ${tag}-0：固定条件下复习有效。`,
      domain: "education_learning",
      relation_section: "暂无关系建议",
    };

    // Closing the page and logging in again must leave an unapproved draft intact.
    await page.goto(`${base}/knowledge-agent#research`);
    assert.equal((await api(`/v1/conclusions/${drafts[0].id}`)).status, "draft");
    await context.clearCookies();
    await page.evaluate(() => localStorage.clear());
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
    assert((await page.locator("#total").innerText()) !== "0");
    evidence.decisions.push("retry_after_refresh_approved");

    // Open the third draft in the real review UI, cancel revision, wait for a bounded
    // idle period, and then leave the page without selecting an approval action.
    await page.locator(`button.queue-item[data-entry-id="${drafts[2].id}"]`).click();
    const thirdDetail = await page.locator("#detail").innerText();
    assert(thirdDetail.includes(`Issue 9 草稿 ${tag}-2`));
    assert(thirdDetail.includes(`固定条件下复习有效 ${tag}-2`));
    const thirdBefore = await api(`/v1/conclusions/${drafts[2].id}`);
    page.once("dialog", async (dialog) => {
      assert.equal(dialog.type(), "prompt");
      await dialog.dismiss();
    });
    await page.getByRole("button", { name: "修订" }).click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    const thirdAfterCancel = await api(`/v1/conclusions/${drafts[2].id}`);
    assert.equal(thirdAfterCancel.status, "draft");
    assert.equal(thirdAfterCancel.claim, thirdBefore.claim);
    assert.equal(thirdAfterCancel.etag, thirdBefore.etag);
    evidence.revision = {
      opened_detail: true,
      prompt_cancelled: true,
      bounded_idle_ms: 500,
      before: { status: thirdBefore.status, claim: thirdBefore.claim, etag: thirdBefore.etag },
      after_cancel: {
        status: thirdAfterCancel.status,
        claim: thirdAfterCancel.claim,
        etag: thirdAfterCancel.etag,
      },
    };

    // Navigating away acts as closing the detail. The server-side draft must remain
    // unchanged, and a fresh session must still be able to reopen it.
    await page.goto(`${base}/knowledge-agent#research`);
    const thirdAfterClose = await api(`/v1/conclusions/${drafts[2].id}`);
    assert.equal(thirdAfterClose.status, "draft");
    assert.equal(thirdAfterClose.claim, thirdBefore.claim);
    assert.equal(thirdAfterClose.etag, thirdBefore.etag);
    evidence.recovery.push("close_without_approval_preserved_draft");
    evidence.recovery.push("revision_cancel_and_idle_preserved_draft");
    await context.clearCookies();
    await page.evaluate(() => localStorage.clear());
    await login();
    await page.goto(`${base}/review-center`);
    await page.locator(`button.queue-item[data-entry-id="${drafts[2].id}"]`).waitFor();
    const thirdAfterRelogin = await api(`/v1/conclusions/${drafts[2].id}`);
    assert.equal(thirdAfterRelogin.status, "draft");
    assert.equal(thirdAfterRelogin.claim, thirdBefore.claim);
    evidence.recovery.push("closed_draft_restored_after_relogin");
    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "review queue shows the draft count and full draft detail",
      "ordinary conversation page shows the pending review badge without auto-opening review",
      "unapproved draft is restored after closing and re-login",
      "version conflict is shown in the browser and retry after refresh succeeds",
      "closing review without an action does not approve the draft",
      "cancelled revision and bounded idle preserve the pending draft",
    ], evidence, browserErrors }, null, 2));
    await page.screenshot({ path: path.join(output, "issue9-review-center.png"), fullPage: true });
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
