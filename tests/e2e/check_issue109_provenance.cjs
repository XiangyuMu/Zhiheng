/* Issue #109: drafts name who said them, assistant-only knowledge says so, originals stay unchanged. */
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
const tag = `p${randomUUID().replaceAll("-", "").slice(0, 8)}`;

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = { material: {}, drafts: {}, details: {}, formal: {} };
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
      if (!response.ok) throw new Error(`${url}: HTTP ${response.status} ${text}`);
      return body;
    }, { url, options });
  }
  async function login() {
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
  }
  async function until(label, probe, attempts = 60) {
    let latest;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      latest = await probe();
      if (latest) return latest;
      await page.waitForTimeout(1000);
    }
    throw new Error(`${label} did not happen`);
  }
  async function openDraft(id) {
    await page.locator(`button.queue-item[data-entry-id="${id}"]`).click();
    await page.locator(`#detail[data-ready="conclusion_draft:${id}"]`).waitFor();
    return page.locator("#detail").innerText();
  }
  async function openKnowledge(title) {
    await page.goto(`${base}/knowledge-agent#library`);
    await page.locator("#library-search").fill(tag);
    const row = page.locator("#knowledge-items li").filter({ hasText: title }).first();
    await row.waitFor({ timeout: 30000 });
    await row.getByRole("button", { name: /阅读/ }).click();
    await page.locator("#knowledge-detail").waitFor({ state: "visible" });
    await page.waitForFunction((expected) => document.querySelector("#detail-title")?.textContent === expected, title);
  }

  try {
    await login();
    // The excerpt-mode answer quotes this material back: that quote is assistant output.
    // Full-text search requires every query term, so the material holds all of the question's.
    // The extractor needs a boundary before each sentence, so the quoted one starts a new line.
    const materialTitle = `Issue 109 资料 ${tag}`;
    const original = `结论：每天复习能提高记忆 ${tag}。\n间隔练习能提高长期记忆 ${tag}。`;
    const imported = await api("/v1/knowledge/imports", { method: "POST", body: {
      title: materialTitle, text: original, primary_domain_id: "education_learning",
      object_kind: "note",
    } });
    const materialId = imported.result.knowledge_object_id;
    await until("material indexing", async () => {
      const state = await api(`/v1/knowledge/${encodeURIComponent(materialId)}/processing`);
      return state.public_status === "succeeded" && state.searchable === true;
    });
    const before = await api(`/v1/knowledge/${encodeURIComponent(materialId)}/reader`);
    assert.equal(before.text.trim(), original);

    // Ask through the real page; the question itself states a conclusion of the user's own.
    await page.goto(`${base}/knowledge-agent#research`);
    const answered = page.waitForResponse((response) =>
      response.url().endsWith("/v1/answers") && response.request().method() === "POST");
    await page.locator("#question").fill(`结论：每天复习能提高记忆 ${tag}。间隔练习 长期记忆 ${tag}`);
    await page.locator("#answer-form button[type=submit]").click();
    const answer = await (await answered).json();
    evidence.answer = { text: answer.answer, cited: (answer.citations || []).map((c) => c.source_id) };
    await page.locator("#answer-result").waitFor({ state: "visible" });
    const cited = (answer.citations || []).filter((citation) => citation.source_id === materialId);
    assert(cited.length > 0, `the answer must cite the material: ${JSON.stringify(answer.citations)}`);
    assert(answer.answer.includes(`间隔练习能提高长期记忆 ${tag}`), answer.answer);

    const drafts = await until("draft extraction", async () => {
      const items = (await api("/v1/conclusions/drafts")).items.filter((item) => item.claim.includes(tag));
      const user = items.find((item) => item.claim.includes("每天复习"));
      const assistant = items.find((item) => item.claim.includes("间隔练习"));
      return user && assistant ? { user, assistant } : null;
    });
    evidence.drafts = {
      user: { id: drafts.user.id, claim: drafts.user.claim, origin: drafts.user.origin },
      assistant: { id: drafts.assistant.id, claim: drafts.assistant.claim, origin: drafts.assistant.origin },
    };
    assert.equal(drafts.user.origin, "user_statement");
    assert.equal(drafts.assistant.origin, "assistant_output");

    await page.goto(`${base}/review-center`);
    await page.locator("#total").waitFor({ state: "visible" });
    const userDetail = await openDraft(drafts.user.id);
    assert(userDetail.includes("出自") && userDetail.includes("个人陈述"), userDetail);
    assert(!userDetail.includes("助手输出"), userDetail);
    const assistantDetail = await openDraft(drafts.assistant.id);
    assert(assistantDetail.includes("出自") && assistantDetail.includes("助手输出（不是依据，请逐条核对）"), assistantDetail);
    assert(assistantDetail.includes("关联原文"), assistantDetail);
    await page.screenshot({ path: path.join(output, "assistant-draft-detail.png"), fullPage: true });
    evidence.details = {
      user_origin_label: "出自 个人陈述",
      assistant_origin_label: "出自 助手输出（不是依据，请逐条核对）",
      assistant_material_link_shown: true,
    };

    // Each draft is still approved on its own; approval sends after the 5 second undo window.
    await page.locator("#detail").getByRole("button", { name: "批准", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor({ timeout: 20000 });
    await until("assistant draft approval", async () =>
      (await api(`/v1/conclusions/${drafts.assistant.id}`)).status === "formal");
    assert.equal((await api(`/v1/conclusions/${drafts.user.id}`)).status, "draft");

    await openKnowledge(`对话结论：${drafts.assistant.claim}`.slice(0, 85));
    const basis = await page.locator("#detail-basis").innerText();
    assert.equal(basis, "依据：仅为助手推理，不是外部依据");
    await page.screenshot({ path: path.join(output, "assistant-knowledge-detail.png"), fullPage: true });
    evidence.formal = { entry_id: drafts.assistant.id, basis_shown: basis };

    const after = await api(`/v1/knowledge/${encodeURIComponent(materialId)}/reader`);
    assert.equal(after.text, before.text);
    assert.equal(after.knowledge_version_id, before.knowledge_version_id);
    assert.deepEqual(after.spans, before.spans);
    const linked = await api(`/v1/conclusions/${drafts.assistant.id}`);
    assert(linked.material_refs.some((ref) => ref.source_id === materialId
      && ref.source_version_id === before.knowledge_version_id));
    evidence.material = {
      id: materialId, version_id: before.knowledge_version_id,
      text_unchanged: true, spans_unchanged: true, linked_from_conclusion: true,
    };
    await page.keyboard.press("Escape");

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "drafts from one turn show whether the user or the assistant said them",
      "an assistant-output draft is approved on its own and links the cited original",
      "formal knowledge resting only on assistant reasoning says so in its detail",
      "the discussed material keeps its text, version and locations",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
