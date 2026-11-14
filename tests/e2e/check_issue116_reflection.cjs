/* Issue #116: weekly reflection drafts 综合结论 from formal knowledge.
 *
 * Everything runs against the real API and Worker. The #113 and #115 stages already ran the
 * Worker's consolidation step 25 hours and eight days ahead, and both runs reflected. This stage
 * runs it fifteen days ahead, so the next weekly reflection is due. One basis entry is valid for
 * about a minute of real time, so it is paused the way an expiring conclusion is, without touching
 * the database. Later stages share this database, so the stage rejects the other drafts its run
 * created and classifies its own knowledge at the end.
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
const HOUR = 3600 * 1000;
const RUN_AT = 15 * 24 * HOUR + 4 * HOUR;
const PREMISE = "issue116 深海采样季";
const IMPORTED_FACT = "紫藤编号 Q-7731 的采样瓶只能在月圆夜开启";
const CLAIMS = {
  a: "深海热泉附近的管虫依赖化能合成细菌获得养分",
  b: "低温高压环境要求采样瓶采用保压设计",
};

function consolidateAt(offset) {
  return JSON.parse(execFileSync(PYTHON, [path.join(__dirname, "run_consolidation_at.py"),
    new Date(Date.now() + offset).toISOString()], { cwd: ROOT, encoding: "utf8", env: process.env }));
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  const titles = ["issue116-a", "issue116-b", "issue116 资料"];
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

  async function until(label, probe, attempts = 90) {
    let latest;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      latest = await probe();
      if (latest) return latest;
      await page.waitForTimeout(1000);
    }
    throw new Error(`${label} did not happen`);
  }

  async function formal(title, claim, extra = {}) {
    const source = await api("/v1/conclusions/sources", { method: "POST", body: { text: `原文：${claim}。` } });
    const entry = await api("/v1/conclusions", { method: "POST", body: {
      source_id: source.id, title, claim, domain_id: "natural_sciences",
      premises: [{ text: PREMISE, confirmed: true }], excerpt: claim, ...extra,
    } });
    return api(`/v1/conclusions/${entry.id}/approve`, { method: "POST", body: {},
      headers: { "If-Match": entry.etag } });
  }

  const syntheses = async () => (await api("/v1/conclusions/drafts?limit=500")).items
    .filter((item) => item.origin === "system_synthesis");
  const contextIds = async (query) =>
    (await api(`/v1/conclusions/context?query=${encodeURIComponent(query)}`)).items.map((item) => item.id);

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

    // Imported material about the same premise, holding a fact found nowhere else.
    await api("/v1/knowledge/imports", { method: "POST", body: {
      title: "issue116 资料", primary_domain_id: "natural_sciences",
      text: `${PREMISE}期间，${IMPORTED_FACT}。${CLAIMS.a}。`,
    } });
    const a = await formal("issue116-a", CLAIMS.a);
    const b = await formal("issue116-b", CLAIMS.b,
      { valid_until: new Date(Date.now() + 60 * 1000).toISOString() });
    const draftsBefore = new Set((await syntheses()).map((item) => item.id));

    const run = consolidateAt(RUN_AT);
    assert.equal(run.ran, true, "a week after the #115 stage's run, an idle run is due");
    assert(run.reflection_drafts_created >= 1, "the weekly reflection drafts a synthesis");
    const created = (await syntheses()).filter((item) => !draftsBefore.has(item.id));
    const ours = created.filter((item) => item.basis.some((entry) => entry.entry_id === a.id));
    assert.equal(ours.length, 1, "one synthesis is drawn from our formal knowledge");
    const [draft] = ours;
    assert.deepEqual(draft.basis.map((entry) => entry.entry_id).sort(), [a.id, b.id].sort());
    assert.deepEqual(draft.evidence.map((entry) => entry.entry_id).sort(), [a.id, b.id].sort());
    evidence.run = { ...run, draft: draft.id, title: draft.title, claim: draft.claim };

    // Only formal knowledge is reflected on: the imported fact is in no reflection draft.
    for (const item of await syntheses()) {
      assert(!JSON.stringify(item).includes("Q-7731"), `${item.id} does not cite imported material`);
    }
    evidence.imported = { fact_absent_from_drafts: (await syntheses()).length };

    // The draft is reviewed one by one, shown as system generated, and not used in answers.
    assert(!(await contextIds(CLAIMS.a)).includes(draft.id), "an unapproved synthesis does not answer");
    await page.goto(`${base}/review-center?type=conclusion_draft`);
    await page.locator("#total").waitFor({ state: "visible" });
    await page.locator(`button.queue-item[data-entry-id="${draft.id}"]`).click();
    await page.locator(`#detail[data-ready="conclusion_draft:${draft.id}"]`).waitFor();
    const detail = await page.locator("#detail").innerText();
    assert(detail.includes("系统归纳（依据为下列正式知识，请逐条核对）"), detail);
    for (const claim of Object.values(CLAIMS)) assert(detail.includes(claim), `the basis ${claim} is cited`);
    await page.screenshot({ path: path.join(output, "synthesis-draft.png"), fullPage: true });

    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      "the synthesis detail fits a narrow viewport");
    await page.screenshot({ path: path.join(output, "synthesis-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });

    // Approval sends after the 5 second undo window; then it is formal and marked as synthesized.
    await page.locator("#detail").getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor({ timeout: 20000 });
    const approved = await until("synthesis approval", async () => {
      const item = await api(`/v1/conclusions/${draft.id}`);
      return item.status === "formal" ? item : null;
    });
    assert.equal(approved.origin, "system_synthesis");
    titles.push(draft.title);
    await page.goto(`${base}/knowledge-agent#library`);
    await page.locator("#library-search").fill(PREMISE);
    const row = page.locator("#knowledge-items li").filter({ hasText: draft.title }).first();
    await row.waitFor({ timeout: 30000 });
    await row.getByRole("button", { name: /阅读/ }).click();
    await page.locator("#knowledge-detail").waitFor({ state: "visible" });
    await page.waitForFunction((expected) => document.querySelector("#detail-title")?.textContent === expected, draft.title);
    assert.equal(await page.locator("#detail-basis").innerText(), "由系统归纳：依据为参与归纳的正式知识");
    await page.screenshot({ path: path.join(output, "synthesis-knowledge.png"), fullPage: true });
    await page.keyboard.press("Escape");
    assert((await contextIds(CLAIMS.a)).includes(draft.id), "the approved synthesis answers");

    // A basis whose validity runs out is paused, and the synthesis comes under review with it.
    const paused = await until("the basis pausing", async () => {
      const item = await api(`/v1/conclusions/${draft.id}`);
      return item.applicability.state === "suspended" ? item.applicability : null;
    });
    assert.equal(paused.reason, "basis_suspended");
    assert.equal(paused.evidence.basis_entry_id, b.id);
    assert(!(await contextIds(CLAIMS.a)).includes(draft.id), "a synthesis under review does not answer");
    const synthesisStatus = await api(`/v1/conclusions/${draft.id}`);
    assert.equal(synthesisStatus.status, "formal", "the synthesis itself is not rewritten");
    assert.equal(synthesisStatus.approved_version, 1);

    // Deleting a basis does the same.
    await api(`/v1/knowledge/${a.knowledge_id}/delete`, { method: "POST", body: {} });
    const removed = (await api(`/v1/conclusions/${draft.id}`)).applicability;
    assert.equal(removed.state, "suspended");
    assert(removed.events.some((event) => event.reason === "basis_removed"
      && event.evidence.basis_entry_id === a.id), "the deleted basis is recorded");
    evidence.applicability = { paused, removed: removed.events.map((event) => event.reason) };

    // Later stages share this database: restore our basis, reject the other drafts our run
    // created, and classify our knowledge.
    await api(`/v1/knowledge/${a.knowledge_id}/restore`, { method: "POST", body: {} });
    for (const other of created.filter((item) => item.id !== draft.id)) {
      await api(`/v1/conclusions/${other.id}/reject`, { method: "POST", body: {} });
    }
    for (const item of (await api("/v1/review/items?type=classification&limit=500")).items
      .filter((entry) => titles.includes(entry.title))) {
      await api(`/v1/knowledge/${item.id}/classifications`, { method: "PUT",
        body: { primary_domain_id: "natural_sciences", node_ids: [] } });
    }
    const left = (await api("/v1/review/items?deferred=include&limit=500")).items
      .filter((item) => titles.includes(item.title));
    assert.deepEqual(left, [], "the stage leaves nothing of its own in 待处理");

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "the weekly reflection drafts a synthesis from formal knowledge in the background",
      "a synthesis draft is marked system generated and cites only formal knowledge",
      "a fact only in imported material appears in no reflection draft",
      "an approved synthesis is formal knowledge marked as synthesized by the system",
      "a paused or deleted basis puts the synthesis under applicability review",
      "the synthesis detail fits a narrow viewport",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
