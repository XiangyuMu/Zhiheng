/* Issue #114: the fixed five-question first-use questionnaire.
 *
 * Runs against the real API. The home page offers the questionnaire once; skipping it
 * keeps it from appearing again, and it can still be completed from 个人上下文. Earlier
 * stages may already have remembered an occupation or goal for this user, so an answer
 * either becomes personal memory or, when it contradicts what is stored, waits for
 * confirmation as the auto-memory rules require.
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

const suffix = Date.now().toString(36);
const ANSWERS = {
  occupation: { key: "role.current", text: `软件工程师 ${suffix}` },
  interests: { key: "interest.primary", text: `历史和音乐 ${suffix}` },
  current_goal: { key: "goal.personal", text: `今年完成一个开源项目 ${suffix}` },
  answer_style: { key: "preference.response_style", text: `简洁，先给结论 ${suffix}` },
};

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  page.on("pageerror", (error) => browserErrors.push(error.message));
  const api = (url, init = {}) => page.evaluate(async ({ url, init }) => {
    const csrf = document.cookie.split(";").map((part) => part.trim())
      .find((part) => part.startsWith("zhiheng_csrf="))?.slice("zhiheng_csrf=".length) || "";
    const headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf,
      "Idempotency-Key": crypto.randomUUID(), "If-Match": "*", ...(init.headers || {}) };
    const response = await fetch(url, { ...init, headers });
    return { status: response.status, body: await response.json().catch(() => null) };
  }, { url, init });
  const onboardingLoaded = () => page.waitForResponse((response) =>
    response.url().endsWith("/v1/memory/onboarding") && response.request().method() === "GET");

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

    const initial = await api("/v1/memory/onboarding", { method: "GET" });
    assert.equal(initial.body.status, "not_started", "no earlier stage may answer the questionnaire");
    const before = await api("/v1/memory/formal?limit=500", { method: "GET" });
    const priorValues = Object.fromEntries(before.body.items.map((item) => [item.state_key, item.value?.text]));
    const priorExcluded = (await api("/v1/memory/personalization", { method: "GET" })).body.excluded_domain_ids;

    // First use: the home page offers the questionnaire without blocking the composer.
    await page.locator("#onboarding-card").waitFor({ state: "visible" });
    assert(await page.locator("#question").isEditable());
    await page.screenshot({ path: path.join(output, "first-use-card.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      "the first-use card must fit a narrow viewport");
    await page.screenshot({ path: path.join(output, "first-use-card-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });

    // Skipping the whole questionnaire keeps it from appearing on its own again.
    const skipped = page.waitForResponse((response) => response.url().endsWith("/v1/memory/onboarding/skip"));
    await page.locator("#onboarding-card [data-onboarding-skip]").click();
    assert.equal((await skipped).status(), 200);
    await page.locator("#onboarding-card").waitFor({ state: "hidden" });
    const reloaded = onboardingLoaded();
    await page.reload();
    await reloaded;
    assert(await page.locator("#onboarding-card").isHidden(), "a skipped questionnaire must not reappear");
    evidence.afterSkip = (await api("/v1/memory/onboarding", { method: "GET" })).body.status;
    assert.equal(evidence.afterSkip, "skipped");

    // It can still be completed later from 个人上下文.
    await page.goto(`${base}/memory-center`);
    await page.locator("#onboarding-entry-open").waitFor({ state: "visible" });
    await page.locator("#onboarding-entry-open").click();
    const dialog = page.locator("#onboarding-dialog");
    await dialog.waitFor({ state: "visible" });
    assert.equal(await dialog.locator("input[type=text]").count(), 4);
    assert.equal(await dialog.locator("input[name=excluded_domains]").count(), 14);
    for (const [name, answer] of Object.entries(ANSWERS)) {
      await dialog.locator(`input[name=${name}]`).fill(answer.text);
    }
    await dialog.locator('input[value="sports_games_leisure"]').check();
    await page.screenshot({ path: path.join(output, "questionnaire.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
      "the questionnaire must fit a narrow viewport");
    await page.screenshot({ path: path.join(output, "questionnaire-mobile.png") });
    await page.setViewportSize({ width: 1440, height: 1080 });

    const candidateCount = page.locator('[data-count="candidates"]');
    const candidatesBefore = Number(await candidateCount.innerText());
    const submitted = page.waitForResponse((response) =>
      response.url().endsWith("/v1/memory/onboarding") && response.request().method() === "POST");
    await dialog.locator("#onboarding-submit").click();
    const response = await submitted;
    assert.equal(response.status(), 200);
    const result = await response.json();
    assert.equal(result.status, "completed");
    await dialog.waitFor({ state: "detached" });
    await page.locator("[data-onboarding-result]").first().waitFor({ state: "visible" });
    evidence.resultNote = await page.locator("[data-onboarding-result]").first().innerText();
    assert(await page.locator("#onboarding-entry-open").isHidden(), "a completed questionnaire is not offered again");
    // The memory center reloads, so the new inferences are counted without a page reload.
    await page.waitForFunction(
      (before) => Number(document.querySelector('[data-count="candidates"]').textContent) >= before + 2,
      candidatesBefore);

    // Explicit answers follow the auto-memory rules; the current goal is volatile.
    const formal = Object.fromEntries((await api("/v1/memory/formal?limit=500", { method: "GET" }))
      .body.items.map((item) => [item.state_key, item]));
    const explicit = result.answers.filter((item) => !item.inferred);
    evidence.answers = {};
    for (const [name, answer] of Object.entries(ANSWERS)) {
      const outcome = explicit.find((item) => item.question_id === name);
      if (priorValues[answer.key] === undefined) {
        assert.equal(outcome.disposition, "auto_confirmed", name);
        assert.equal(formal[answer.key].value.text, answer.text, name);
      } else {
        assert.equal(outcome.disposition, "conflict_pending", name);
        assert.equal(formal[answer.key].value.text, priorValues[answer.key], name);
      }
      evidence.answers[name] = outcome.disposition;
    }
    if (formal["goal.personal"].value.text === ANSWERS.current_goal.text) {
      assert.equal(formal["goal.personal"].time_sensitivity, "volatile");
    }

    // What the system infers from the answers only awaits confirmation.
    const inferredIds = result.answers.filter((item) => item.inferred).map((item) => item.candidate_id);
    const candidates = (await api("/v1/memory/candidates?limit=500", { method: "GET" })).body.items;
    const inferred = candidates.filter((item) => inferredIds.includes(item.id));
    assert.deepEqual(inferred.map((item) => item.state_key).sort(),
      ["inference.interest_domains", "inference.work_domain"]);
    assert(inferred.every((item) => item.candidate_type === "inferred" && item.status === "pending_confirmation"));
    assert(!formal["inference.work_domain"] && !formal["inference.interest_domains"]);
    // The fifth answer is the primary-domain setting.
    const settings = (await api("/v1/memory/personalization", { method: "GET" })).body;
    assert.deepEqual(settings.excluded_domain_ids, ["sports_games_leisure"]);
    assert(await page.locator('#personalization-domains input[value="sports_games_leisure"]').isChecked());
    await page.screenshot({ path: path.join(output, "questionnaire-completed.png"), fullPage: true });

    await page.goto(`${base}/review-center?type=memory_candidate`);
    await page.locator("#queue").getByText("inference.work_domain").last().click();
    await page.getByText(`由首次引导的回答“${ANSWERS.occupation.text}”推断`).first()
      .waitFor({ state: "visible" });
    await page.screenshot({ path: path.join(output, "inferences-awaiting-confirmation.png"), fullPage: true });
    evidence.inferred = inferred.map((item) => item.value.text);

    const home = onboardingLoaded();
    await page.goto(`${base}/knowledge-agent`);
    await home;
    assert(await page.locator("#onboarding-card").isHidden());

    // Later stages share this database: undo what this stage created.
    for (const candidate of inferred) {
      const rejected = await api(`/v1/memory/candidates/${candidate.id}/reject`, { method: "POST",
        headers: { "If-Match": candidate.etag }, body: JSON.stringify({}) });
      assert.equal(rejected.status, 200, JSON.stringify(rejected.body));
    }
    for (const outcome of explicit) {
      if (outcome.conflict_id) {
        const candidate = candidates.find((item) => item.id === outcome.candidate_id);
        const rejected = await api(`/v1/personal-updates/conflicts/${outcome.conflict_id}/decision`, {
          method: "POST", headers: { "If-Match": candidate.etag }, body: JSON.stringify({ decision: "reject" }) });
        assert.equal(rejected.status, 200, JSON.stringify(rejected.body));
      } else if (outcome.formal_memory_id) {
        const item = formal[outcome.state_key];
        const deleted = await api(`/v1/memory/items/${item.id}`, { method: "DELETE",
          headers: { "If-Match": item.etag }, body: JSON.stringify({ reason: "issue114 stage cleanup" }) });
        assert.equal(deleted.status, 200, JSON.stringify(deleted.body));
      }
    }
    const current = (await api("/v1/memory/personalization", { method: "GET" })).body;
    const restored = await api("/v1/memory/personalization", { method: "PUT",
      headers: { "If-Match": current.etag }, body: JSON.stringify({ excluded_domain_ids: priorExcluded }) });
    assert.equal(restored.status, 200, JSON.stringify(restored.body));

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "the home page offers the questionnaire on first use without blocking the composer",
      "skipping the whole questionnaire keeps it from appearing again",
      "a skipped questionnaire can still be completed from the memory center",
      "explicit answers become personal memory under the auto-memory rules",
      "inferences from the answers await confirmation",
      "the current goal is remembered as volatile",
      "the fifth answer becomes the domains without personalization",
      "the questionnaire fits a narrow viewport",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
