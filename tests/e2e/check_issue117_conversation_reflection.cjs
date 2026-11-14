/* Issue #117: weekly reflection drafts judgments past conversations left unextracted.
 *
 * Everything runs against the real API and Worker. One question is asked through the real page,
 * and the Worker extracts its judgment as usual. A second turn stands for an extraction job that
 * failed (tests/e2e/seed_unextracted_turn.py): it repeats that judgment, states a new one, carries
 * an assistant judgment, and cites imported material holding a fact found nowhere else. The #116
 * stage ran the Worker's consolidation step fifteen days ahead; this stage runs it twenty-two days
 * ahead, so the next weekly reflection is due. Later stages share this database, so the stage
 * rejects the drafts it does not approve, and the other drafts its run created, and classifies
 * its own knowledge at the end.
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
const RUN_AT = 22 * 24 * HOUR + 4 * HOUR;
const TAG = "issue117";
const IMPORTED_FACT = "银杏编号 K-5520 的错题本只能用蓝色墨水";
const CLAIMS = {
  extracted: `${TAG} 晨间复盘能减少重复错误。`,
  missedUser: `${TAG} 错题本需要每周整理。`,
  missedAssistant: `${TAG} 番茄钟适合短时专注。`,
};

function python(script, ...args) {
  return JSON.parse(execFileSync(PYTHON, [path.join(__dirname, script), ...args],
    { cwd: ROOT, encoding: "utf8", env: process.env }));
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  const titles = [`${TAG} 资料`];
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

  const drafts = async () => (await api("/v1/conclusions/drafts?limit=500")).items;
  const ours = async () => (await drafts()).filter((item) => item.claim.includes(TAG));
  const supplements = async () => (await api("/v1/review/items?type=extraction_supplement&deferred=include&limit=500"))
    .items.map((item) => item.id);
  const contextIds = async (query) =>
    (await api(`/v1/conclusions/context?query=${encodeURIComponent(query)}`)).items.map((item) => item.id);

  async function openDraft(id) {
    await page.locator(`button.queue-item[data-entry-id="${id}"]`).click();
    await page.locator(`#detail[data-ready="conclusion_draft:${id}"]`).waitFor();
    return page.locator("#detail").innerText();
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

    // A judgment the user states through the real page; the Worker extracts it as usual.
    await page.goto(`${base}/knowledge-agent#research`);
    const answered = page.waitForResponse((response) =>
      response.url().endsWith("/v1/answers") && response.request().method() === "POST");
    await page.locator("#question").fill(`结论：${CLAIMS.extracted}`);
    await page.locator("#answer-form button[type=submit]").click();
    await answered;
    const extracted = await until("the turn's own extraction", async () =>
      (await ours()).find((item) => item.claim === CLAIMS.extracted && !item.drafted_by));
    assert.equal(extracted.origin, "user_statement");

    // A past turn whose extraction failed, citing imported material.
    const imported = await api("/v1/knowledge/imports", { method: "POST", body: {
      title: `${TAG} 资料`, primary_domain_id: "education_learning",
      text: `整理错题时，${IMPORTED_FACT}。`,
    } });
    const materialId = imported.result.knowledge_object_id;
    const conversation = await api("/v1/conversations", { method: "POST", body: { title: `${TAG} 聊天` } });
    const { history_id: historyId } = python("seed_unextracted_turn.py", conversation.id,
      JSON.stringify({ answer: `结论：${CLAIMS.missedAssistant}`,
        citations: [{ source_type: "knowledge", source_id: materialId, quote: IMPORTED_FACT }] }),
      `我认为${CLAIMS.extracted}结论：${CLAIMS.missedUser}`);
    const runId = `conversation-extraction:${historyId}`;
    assert((await supplements()).includes(runId), "the failed turn waits in 待处理 to be supplemented");
    const draftsBefore = new Set((await drafts()).map((item) => item.id));

    const run = python("run_consolidation_at.py", new Date(Date.now() + RUN_AT).toISOString());
    assert.equal(run.ran, true, "a week after the #116 stage's run, an idle run is due");
    assert(run.reflection_drafts_created >= 2, "the weekly reflection drafts the missed judgments");
    const created = (await drafts()).filter((item) => !draftsBefore.has(item.id));
    const missed = created.filter((item) => item.evidence.some((entry) => entry.history_id === historyId));
    assert.deepEqual(missed.map((item) => item.claim).sort(),
      [CLAIMS.missedUser, CLAIMS.missedAssistant].sort(), "only the judgments never extracted");
    const user = missed.find((item) => item.claim === CLAIMS.missedUser);
    const assistant = missed.find((item) => item.claim === CLAIMS.missedAssistant);
    assert.equal(user.origin, "user_statement");
    assert.equal(assistant.origin, "assistant_output");
    for (const item of missed) assert.equal(item.drafted_by, "reflection");
    assert(!created.some((item) => item.claim === CLAIMS.extracted),
      "the judgment already extracted is not drafted again");
    assert(!(await supplements()).includes(runId), "the drafted turn no longer asks to be supplemented");
    evidence.run = { ...run, history: historyId,
      drafts: missed.map((item) => ({ id: item.id, claim: item.claim, origin: item.origin })) };

    // Reflection reads no imported material, not even the material the turn cited.
    for (const item of created) {
      const text = JSON.stringify(item);
      assert(!text.includes("K-5520") && !text.includes(materialId), `${item.id} reads no imported material`);
    }
    evidence.imported = { fact_absent_from_drafts: created.length };

    // Each draft is reviewed one by one, shown as found by reflection, and not used in answers.
    assert(!(await contextIds(CLAIMS.missedUser)).includes(user.id), "an unapproved draft does not answer");
    assert(!(await contextIds(CLAIMS.missedAssistant)).includes(assistant.id), "an unapproved draft does not answer");
    await page.goto(`${base}/review-center?type=conclusion_draft`);
    await page.locator("#total").waitFor({ state: "visible" });
    const assistantDetail = await openDraft(assistant.id);
    assert(assistantDetail.includes("助手输出（不是依据，请逐条核对）"), assistantDetail);
    assert(assistantDetail.includes("系统反思时从过往对话补提"), assistantDetail);
    const userDetail = await openDraft(user.id);
    assert(userDetail.includes("个人陈述") && !userDetail.includes("助手输出"), userDetail);
    assert(userDetail.includes("系统反思时从过往对话补提"), userDetail);
    await page.screenshot({ path: path.join(output, "reflection-draft.png"), fullPage: true });
    evidence.details = { user_origin: "个人陈述", assistant_origin: "助手输出（不是依据，请逐条核对）",
      drafted_by: "系统反思时从过往对话补提" };

    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      "the reflection draft detail fits a narrow viewport");
    await page.screenshot({ path: path.join(output, "reflection-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });

    // Approval sends after the 5 second undo window; then the judgment answers.
    await page.locator("#detail").getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor({ timeout: 20000 });
    const approved = await until("approval", async () => {
      const item = await api(`/v1/conclusions/${user.id}`);
      return item.status === "formal" ? item : null;
    });
    assert.equal(approved.origin, "user_statement");
    titles.push(approved.title);
    assert((await contextIds(CLAIMS.missedUser)).includes(user.id), "the approved draft answers");
    evidence.approved = { id: user.id, status: approved.status };

    // Later stages share this database: reject what we did not approve, and the other drafts our
    // run created, and classify our knowledge. The live answer to our question can hold text the
    // extractor sets aside for review; resolve that request too, then reject its draft.
    for (const item of (await api("/v1/review/items?type=extraction_supplement&deferred=include&limit=500")).items
      .filter((entry) => JSON.stringify(entry).includes(TAG))) {
      await api(`/v1/conclusions/drafts/${encodeURIComponent(item.id)}/supplement`, { method: "POST", body: {
        title: `${TAG} 人工补充`, claim: `${TAG} 人工补充：无需记录`,
        domain_id: "education_learning", excerpt: TAG,
      } });
    }
    for (const item of [...await ours(), ...created.filter((entry) => entry.id !== user.id)]) {
      const current = await api(`/v1/conclusions/${item.id}`);
      if (["draft", "deferred"].includes(current.status)) {
        await api(`/v1/conclusions/${item.id}/reject`, { method: "POST", body: {} });
      }
    }
    for (const item of (await api("/v1/review/items?type=classification&limit=500")).items
      .filter((entry) => titles.includes(entry.title))) {
      await api(`/v1/knowledge/${item.id}/classifications`, { method: "PUT",
        body: { primary_domain_id: "education_learning", node_ids: [] } });
    }
    const left = (await api("/v1/review/items?deferred=include&limit=500")).items
      .filter((item) => titles.includes(item.title) || JSON.stringify(item).includes(TAG));
    assert.deepEqual(left, [], "the stage leaves nothing of its own in 待处理");

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "reflection drafts judgments a failed extraction left in a past turn",
      "a judgment already extracted is not drafted again",
      "a reflection draft shows its user or assistant origin and reflection",
      "reflection reads no imported material, not even material the turn cited",
      "an unapproved reflection draft does not answer, and answers once approved",
      "the reflection draft detail fits a narrow viewport",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
