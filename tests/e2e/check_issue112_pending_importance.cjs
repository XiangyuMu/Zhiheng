/* Issue #112: 待处理 opens in importance order and offers a daily suggested amount.
 *
 * Everything runs against the real API and Worker. Later stages share this database, so the
 * items made here are rejected or classified at the end and the amount goes back to 10.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback URL and output directory");
}
fs.mkdirSync(output, { recursive: true });

// Claims share no characters across pairs, so each draft relates only to its own formal entry.
const PLAIN = { title: "issue112-plain", claim: "冰川侵蚀塑造峡湾地貌" };
const CONFLICT = { formal: "鹦鹉螺壳呈对数螺旋", draft: "鹦鹉螺壳不呈对数螺旋" };
const SUPPLEMENT = { formal: "琥珀能保存远古昆虫", draft: "琥珀能保存远古昆虫与花粉" };

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  const made = { drafts: [], formalTitles: [] };
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

  async function draft(title, claim) {
    const source = await api("/v1/conclusions/sources", { method: "POST", body: { text: `原文：${claim}。` } });
    const entry = await api("/v1/conclusions", { method: "POST", body: {
      source_id: source.id, title, claim, domain_id: "natural_sciences",
      premises: [{ text: "常见条件", confirmed: false }], excerpt: claim,
    } });
    made.drafts.push(entry.id);
    return entry;
  }

  async function relationToFormal(key, pair) {
    const formal = await draft(`${key}-formal`, pair.formal);
    await api(`/v1/conclusions/${formal.id}/approve`, { method: "POST", body: {},
      headers: { "If-Match": formal.etag } });
    made.drafts.splice(made.drafts.indexOf(formal.id), 1);
    made.formalTitles.push(`${key}-formal`);
    const entry = await draft(`${key}-draft`, pair.draft);
    const relations = (await api(`/v1/conclusions/${entry.id}/relations`)).items;
    assert.equal(relations.length, 1, `${key} draft relates to its formal entry only`);
    return relations[0];
  }

  const pending = async (params = "") =>
    (await api(`/v1/review/items?deferred=include&limit=500${params}`)).items;
  const queueIds = () => page.locator("#queue .queue-item")
    .evaluateAll((nodes) => nodes.map((node) => node.dataset.entryId));
  const progress = page.locator("#daily-progress");

  async function setAmount(value) {
    const saved = page.waitForResponse((response) =>
      response.url().endsWith("/v1/review/daily") && response.request().method() === "PUT");
    await page.locator("#daily-amount").fill(String(value));
    await page.locator("#daily-amount").press("Enter");
    const response = await saved;
    assert.equal(response.status(), 200);
    return response.json();
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

    // Made oldest first in the order a time sort would show them, least important first.
    const plain = await draft(PLAIN.title, PLAIN.claim);
    const conflict = await relationToFormal("issue112-conflict", CONFLICT);
    const supplement = await relationToFormal("issue112-supplement", SUPPLEMENT);
    const ours = [supplement.id, conflict.id, plain.id];

    await page.goto(`${base}/review-center`);
    await page.locator(`#queue .queue-item[data-entry-id="${plain.id}"]`).waitFor();
    assert.equal(await page.locator("#sort-order").inputValue(), "importance");
    const shown = (await queueIds()).filter((id) => ours.includes(id));
    assert.deepEqual(shown, ours, "affects knowledge, then conflict, then ordinary");
    const meta = (id) => page.locator(`#queue .queue-item[data-entry-id="${id}"] small`).innerText();
    assert((await meta(supplement.id)).includes("影响当前任务或已有知识"));
    assert((await meta(conflict.id)).includes("冲突与复核"));
    await page.screenshot({ path: path.join(output, "importance-order.png"), fullPage: true });
    evidence.importance = { order: shown, supplement: await meta(supplement.id), conflict: await meta(conflict.id) };

    await page.locator("#sort-order").selectOption("oldest");
    await page.waitForFunction((ids) => {
      const order = [...document.querySelectorAll("#queue .queue-item")].map((node) => node.dataset.entryId)
        .filter((id) => ids.includes(id));
      return order.join() === [...ids].reverse().join();
    }, ours);
    evidence.timeOrder = (await queueIds()).filter((id) => ours.includes(id));
    await page.locator("#sort-order").selectOption("importance");

    const before = await api("/v1/review/daily");
    assert.equal(before.suggested, 10, "the amount defaults to ten");
    await progress.filter({ hasText: `今天已处理 ${before.handled_today} / 10 项` }).waitFor();
    const target = before.handled_today + 1;
    const changed = await setAmount(target);
    assert.equal(changed.suggested, target);
    await progress.filter({ hasText: `/ ${target} 项` }).waitFor();
    evidence.amount = { default: before.suggested, changedTo: target, handledBefore: before.handled_today };

    const others = Object.fromEntries((await pending()).filter((item) => item.id !== plain.id)
      .map((item) => [`${item.type}:${item.id}`, item.etag]));
    await page.locator(`#queue .queue-item[data-entry-id="${plain.id}"]`).click();
    await page.keyboard.press("d");
    await progress.filter({ hasText: "今天可以停止了" }).waitFor({ timeout: 15000 });
    assert.equal(await progress.getAttribute("data-reached"), "true");
    const after = Object.fromEntries((await pending()).filter((item) => item.id !== plain.id)
      .map((item) => [`${item.type}:${item.id}`, item.etag]));
    assert.deepEqual(after, others, "reaching the amount changes no other item");
    await page.screenshot({ path: path.join(output, "daily-reached.png"), fullPage: true });
    evidence.reached = { text: await progress.innerText(), unchangedItems: Object.keys(after).length };

    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      "the daily note must fit a narrow viewport");
    await page.screenshot({ path: path.join(output, "daily-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });

    const off = await setAmount(0);
    assert.equal(off.suggested, 0);
    await page.getByText("已关闭每日建议处理量。").waitFor();
    assert(await progress.isHidden(), "the note is hidden at 0");
    await page.screenshot({ path: path.join(output, "daily-off.png"), fullPage: true });

    // Later stages share this database: take our items out of 待处理 and restore the default.
    const items = await pending();
    const drafts = items.filter((item) => item.type === "conclusion_draft" && made.drafts.includes(item.id));
    if (drafts.some((item) => item.deferred)) {
      await api("/v1/review/batch", { method: "POST", body: { action: "restore",
        items: drafts.filter((item) => item.deferred).map(({ type, id, etag }) => ({ type, id, etag })) } });
    }
    const fresh = (await pending("&type=conclusion_draft")).filter((item) => made.drafts.includes(item.id));
    await api("/v1/review/batch", { method: "POST", body: { action: "reject",
      items: fresh.map(({ type, id, etag }) => ({ type, id, etag })) } });
    for (const item of (await pending("&type=classification"))
      .filter((entry) => made.formalTitles.includes(entry.title))) {
      await api(`/v1/knowledge/${item.id}/classifications`, { method: "PUT",
        body: { primary_domain_id: "natural_sciences", node_ids: [] } });
    }
    const daily = await api("/v1/review/daily");
    await api("/v1/review/daily", { method: "PUT", body: { suggested: 10 },
      headers: { "If-Match": daily.etag } });
    const left = (await pending()).filter((item) => made.drafts.includes(item.id) || ours.includes(item.id)
      || made.formalTitles.includes(item.title));
    assert.deepEqual(left, [], "the stage leaves nothing of its own in 待处理");

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "the pending list opens in importance order and labels the tiers",
      "the pending list can switch to time order",
      "the daily suggested amount defaults to ten and can be changed",
      "reaching the daily amount says today can stop and changes no other item",
      "setting the daily amount to zero hides the note",
      "the daily note fits a narrow viewport",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
