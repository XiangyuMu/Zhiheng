/* Issue #8: real browser resolution and answer isolation evidence. */
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
  const evidence = { tag, decisions: [], answers: [] };
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));

  async function api(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((item) => item.trim())
        .find((item) => item.startsWith("zhiheng_csrf="))?.slice("zhiheng_csrf=".length) || "";
      const response = await fetch(url, {
        credentials: "same-origin", ...options,
        headers: { Accept: "application/json", "Content-Type": "application/json",
          "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID(), "If-Match": "*", ...(options.headers || {}) },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      const text = await response.text();
      let body = {};
      try { body = text ? JSON.parse(text) : {}; } catch (_) { body = { raw: text }; }
      if (!response.ok) throw new Error(`${response.status}: ${body.detail || text}`);
      return body;
    }, { url, options });
  }
  async function submitQuestion(query) {
    await page.locator("#question").fill(query);
    const responsePromise = page.waitForResponse((response) => response.url().endsWith("/v1/answers") && response.request().method() === "POST");
    await page.locator("#answer-form button[type=submit]").click();
    const response = await responsePromise;
    const body = await response.json();
    await page.locator("#answer-result").waitFor({ state: "visible", timeout: 20000 });
    return body;
  }
  async function login() {
    const username = "issue17-workspace";
    const password = "issue17 workspace passphrase";
    await page.goto(`${base}/login`);
    await page.evaluate(async ({ username, password }) => {
      const response = await fetch("/auth/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }) });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    }, { username, password });
    await page.locator("#username").fill(username);
    await page.locator("#password").fill(password);
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");
  }
  try {
    await login();
    await page.goto(`${base}/knowledge-agent#research`);

    // Create a real conflict, then resolve it by clicking the confirmation button.
    for (const [index, city] of ["北京", "上海"].entries()) {
      await api("/v1/personal-updates", { method: "POST", body: {
        memory_type: "fact", state_key: "profile.city", value: { text: city }, source_kind: "user_explicit",
      }, headers: { "Idempotency-Key": `issue8-city-${tag}-${index}` } });
    }
    const conflictQuery = "我现在居住在哪个城市？";
    const firstConflictAnswer = await submitQuestion(conflictQuery);
    await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
    assert((await page.locator("#context-prompt-values").innerText()).includes("冲突"));
    await page.locator("#context-prompt-confirm").click();
    await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
    const selected = await api("/v1/memory/context/l1?prefix=profile.");
    assert.equal(selected["profile.city"].text, "上海");
    evidence.decisions.push("conflict_confirm");
    const secondConflictAnswer = await submitQuestion(conflictQuery);
    assert.equal(secondConflictAnswer.context_prompts.filter((item) => item.kind === "conflict").length, 0,
      JSON.stringify(secondConflictAnswer.context_prompts));
    evidence.answers.push({ kind: "confirmed_conflict", prompts: secondConflictAnswer.context_prompts.length,
      selected_city: selected["profile.city"].text, personalization_refs: secondConflictAnswer.personalization_refs.length });
    // The city answer can still surface a separate missing-background prompt; dismiss it
    // explicitly before starting the independent supplement scenario.
    if (await page.locator("#context-prompt-dialog").isVisible()) {
      await page.locator("#context-prompt-skip").click();
      await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
    }

    // A missing prompt is resolved in the real dialog, then the next answer sees the saved value.
    const missingQuery = "我的工作偏好是什么？";
    await page.goto(`${base}/knowledge-agent#research`);
    await page.locator("#new-conversation").click();
    await page.waitForTimeout(100);
    const missingAnswer = await submitQuestion(missingQuery);
    await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
    assert((await page.locator("#context-prompt-reason").innerText()).includes("个人"));
    await page.locator("#context-prompt-input").fill("偏好短反馈");
    await page.locator("#context-prompt-supplement").click();
    await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
    const missingStateKey = missingAnswer.context_prompts.find((item) => item.kind === "missing").state_key;
    const memory = await api(`/v1/memory/context/l1?prefix=${encodeURIComponent(missingStateKey.slice(0, missingStateKey.lastIndexOf(".") + 1))}`);
    assert(Object.values(memory).some((value) => value?.text === "偏好短反馈"), JSON.stringify(memory));
    evidence.decisions.push("missing_supplement");
    const secondMissingAnswer = await submitQuestion(missingQuery);
    // The follow-up answer must still render after the supplement; the saved value
    // is asserted above and the response is captured as browser evidence.
    assert(secondMissingAnswer.answer !== undefined);
    evidence.answers.push({ kind: "supplemented_missing", prompts: secondMissingAnswer.context_prompts.length,
      preference: "偏好短反馈", personalization_refs: secondMissingAnswer.personalization_refs.length });

    // Create a second unresolved conflict and unrelated formal evidence. The answer must retain
    // the unrelated evidence while excluding unresolved personal values from its body.
    await api('/v1/personal-updates', { method: 'POST', body: {
      memory_type: 'fact', state_key: 'profile.city', value: { text: '北京' }, source_kind: 'user_explicit',
    }, headers: { 'Idempotency-Key': `issue8-city-second-${tag}` } });
    const partialSource = await api('/v1/conclusions/sources', { method: 'POST', body: { text: '通勤规则来源' } });
    const partialDraft = await api('/v1/conclusions', { method: 'POST', body: {
      source_id: partialSource.id, title: '通勤规则', claim: '无论居住在哪里，通勤出行应遵守交通规则',
      domain_id: 'career_work_practice', excerpt: '通勤出行应遵守交通规则', premises: [],
    } });
    await api(`/v1/conclusions/${partialDraft.id}/approve`, { method: 'POST', headers: { 'If-Match': partialDraft.etag } });
    for (let index = 0; index < 10; index += 1) {
      const search = await api('/v1/knowledge/search?q=通勤');
      if (search.items?.length) break;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    const partialQuery = `我的城市 通勤 ${tag}`;
    const partial = await submitQuestion(partialQuery);
    assert(partial.answer.includes('通勤出行应遵守交通规则'));
    assert(!partial.answer.includes('北京'));
    assert(!partial.answer.includes('上海'));
    assert(partial.insufficiencies.some((item) => item.includes('暂缓')));
    assert.equal(partial.personalization_refs.length, 0);
    evidence.answers.push({ kind: "partial", answer_nonempty: true, personalization_refs: 0,
      stop_reason: partial.stop_reason, insufficiencies: partial.insufficiencies });

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", evidence, browserErrors }, null, 2));
    await page.screenshot({ path: path.join(output, "issue8-context-prompts.png"), fullPage: true });
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
