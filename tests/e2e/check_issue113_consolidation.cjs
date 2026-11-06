/* Issue #113: idle-time background consolidation into suggestions and 整理发现.
 *
 * Everything runs against the real API and Worker. The live Worker already ran consolidation
 * once at start (nothing had happened yet), so its next run is a day away; the stage runs the
 * Worker's own consolidation step with a clock a day and two days later instead of waiting.
 * Later stages share this database, so every suggestion the runs made is decided here and our
 * formal knowledge is classified at the end.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback URL and output directory");
}
fs.mkdirSync(output, { recursive: true });

const ROOT = path.join(__dirname, "..", "..");
const PYTHON = fs.existsSync(path.join(ROOT, ".venv", "bin", "python"))
  ? path.join(ROOT, ".venv", "bin", "python") : "python3";
// Pairs share almost no characters, so each relates only to its own partner.
const DUPLICATE = "冰碛湖由冰川融水形成";
const CONFLICT = { older: "珊瑚白化源于海温升高", newer: "珊瑚白化不源于海温升高" };
const HOUR = 3600 * 1000;

function consolidateAt(offsetMs) {
  const at = new Date(Date.now() + offsetMs).toISOString();
  return JSON.parse(execFileSync(PYTHON, [path.join(__dirname, "run_consolidation_at.py"), at], {
    cwd: ROOT, encoding: "utf8", env: process.env,
  }));
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  const titles = [];
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
      const body = text ? JSON.parse(text) : {};
      if (!response.ok) throw new Error(`${url}: ${response.status} ${body.detail || text}`);
      return body;
    }, { url, options });
  }

  async function formal(title, claim) {
    const source = await api("/v1/conclusions/sources", { method: "POST", body: { text: `原文：${claim}。` } });
    const entry = await api("/v1/conclusions", { method: "POST", body: {
      source_id: source.id, title, claim, domain_id: "natural_sciences",
      premises: [{ text: "常见条件", confirmed: false }], excerpt: claim,
    } });
    const approved = await api(`/v1/conclusions/${entry.id}/approve`, { method: "POST", body: {},
      headers: { "If-Match": entry.etag } });
    titles.push(title);
    return approved;
  }

  const consolidation = async () =>
    (await api("/v1/review/items?type=relation_suggestion&deferred=include&limit=500")).items
      .filter((item) => item.detail.origin === "consolidation");
  const ours = (items, entries) => items.filter((item) =>
    entries.some((entry) => [item.detail.left_id, item.detail.right_id].includes(entry.id)));
  const total = async () => (await api("/v1/review/counts")).total;

  async function openDetail(item) {
    await page.locator(`button.queue-item[data-entry-id="${item.id}"]`).click();
    await page.locator(`#detail[data-ready="relation_suggestion:${item.id}"]`).waitFor();
    return page.locator("#detail").innerText();
  }

  async function decide(item, name) {
    await openDetail(item);
    await page.locator("#detail").getByRole("button", { name, exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
  }

  try {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }) });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");

    const olderDuplicate = await formal("issue113-duplicate-older", DUPLICATE);
    const newerDuplicate = await formal("issue113-duplicate-newer", DUPLICATE);
    const olderConflict = await formal("issue113-conflict-older", CONFLICT.older);
    const newerConflict = await formal("issue113-conflict-newer", CONFLICT.newer);
    const entries = [olderDuplicate, newerDuplicate, olderConflict, newerConflict];
    const pendingBefore = await total();

    const first = consolidateAt(25 * HOUR);
    assert.equal(first.ran, true, "a day after the Worker's own run, an idle run is due");
    const suggestions = await consolidation();
    const merge = ours(suggestions, [newerDuplicate]).find((item) => item.detail.kind === "duplicate");
    const conflict = ours(suggestions, [newerConflict]).find((item) => item.detail.kind === "conflict");
    assert(merge && conflict, "the run suggests the merge and the conflict");
    assert.equal(merge.detail.left_id, newerDuplicate.id, "the newer duplicate is the one to merge");
    // Only the suggestions are pending; findings never count.
    assert.equal(await total(), pendingBefore + suggestions.length);
    const findings = (await api("/v1/consolidation/findings")).items;
    const ourFindings = findings.filter((item) => entries.some((entry) => entry.id === item.subject_id));
    assert(ourFindings.some((item) => item.kind === "unconfirmed_assumption"),
      "conclusions resting on an unconfirmed premise are findings");
    assert.deepEqual(
      (await api(`/v1/conclusions/context?query=${encodeURIComponent(DUPLICATE)}`)).items.map((item) => item.id).sort(),
      [olderDuplicate.id, newerDuplicate.id].sort(), "nothing changes before approval");
    evidence.run = { ...first, suggestions: suggestions.length, ourFindings: ourFindings.map((item) => item.kind) };

    await page.goto(`${base}/review-center`);
    await page.locator(`button.queue-item[data-entry-id="${merge.id}"]`).waitFor();
    const mergeDetail = await openDetail(merge);
    assert(mergeDetail.includes("较新的正式知识") && mergeDetail.includes("较早的正式知识"));
    assert(mergeDetail.includes(DUPLICATE));
    await page.screenshot({ path: path.join(output, "merge-detail.png"), fullPage: true });
    const conflictDetail = await openDetail(conflict);
    assert(conflictDetail.includes("批准冲突后，两条结论在冲突解决前都不再用于回答。"));
    assert(conflictDetail.includes(CONFLICT.older) && conflictDetail.includes(CONFLICT.newer));
    await page.screenshot({ path: path.join(output, "conflict-detail.png"), fullPage: true });
    evidence.details = { merge: mergeDetail.slice(0, 400), conflict: conflictDetail.slice(0, 400) };

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await page.locator(`button.queue-item[data-entry-id="${conflict.id}"]`).click();
    await page.locator(`#detail[data-ready="relation_suggestion:${conflict.id}"]`).waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      "the comparison of two formal conclusions fits a narrow viewport");
    await page.screenshot({ path: path.join(output, "conflict-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });
    await page.reload();
    await page.locator(`button.queue-item[data-entry-id="${merge.id}"]`).waitFor();

    await decide(merge, "批准");
    assert.equal((await api(`/v1/conclusions/${newerDuplicate.id}`)).status, "merged");
    assert.equal((await api(`/v1/conclusions/${olderDuplicate.id}`)).status, "formal");
    assert.deepEqual(
      (await api(`/v1/conclusions/context?query=${encodeURIComponent(DUPLICATE)}`)).items.map((item) => item.id),
      [olderDuplicate.id], "after approval only the older duplicate serves answers");
    evidence.merge = { merged: newerDuplicate.id, kept: olderDuplicate.id };

    await decide(conflict, "拒绝");
    const second = consolidateAt(49 * HOUR);
    assert.equal(second.ran, true);
    assert.equal(second.suggestions_created, 0, "a second run adds nothing");
    assert.deepEqual(ours(await consolidation(), entries), [], "the rejected conflict stays rejected");
    evidence.second = second;

    const finding = ourFindings.find((item) => item.subject_id === olderDuplicate.id);
    const pendingMid = await total();
    const dismissed = await api(`/v1/consolidation/findings/${finding.id}/dismiss`, { method: "POST", body: {} });
    assert.equal(dismissed.status, "dismissed");
    assert(!(await api("/v1/consolidation/findings")).items.some((item) => item.id === finding.id));
    assert.equal(await total(), pendingMid, "dismissing a finding is not deferring anything");
    assert.equal((await api(`/v1/conclusions/${olderDuplicate.id}`)).status, "formal");
    evidence.dismissed = { id: finding.id, kind: finding.kind };

    // Later stages share this database: decide what the runs suggested and classify our knowledge.
    for (const item of await consolidation()) {
      await api(`/v1/review/items/relation_suggestion/${item.id}/reject`, { method: "POST", body: {},
        headers: { "If-Match": item.etag } });
    }
    for (const item of (await api("/v1/review/items?type=classification&limit=500")).items
      .filter((entry) => titles.includes(entry.title))) {
      await api(`/v1/knowledge/${item.id}/classifications`, { method: "PUT",
        body: { primary_domain_id: "natural_sciences", node_ids: [] } });
    }
    const left = (await api("/v1/review/items?deferred=include&limit=500")).items
      .filter((item) => titles.includes(item.title) || item.detail?.origin === "consolidation");
    assert.deepEqual(left, [], "the stage leaves nothing of its own in 待处理");

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "an idle consolidation run suggests merges and conflicts as pending items",
      "consolidation findings are listed but do not count as pending",
      "the pending detail compares two formal conclusions and warns before approving a conflict",
      "approving a merge retires only the newer duplicate",
      "a second run adds nothing and a rejected suggestion stays rejected",
      "dismissing a finding changes no pending item or knowledge",
      "the formal comparison fits a narrow viewport",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
