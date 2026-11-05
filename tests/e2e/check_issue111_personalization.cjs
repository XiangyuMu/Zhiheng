/* Issue #111: the user chooses primary domains whose answers use no personal memory.
 *
 * The setting is saved through the real API. The answer below is injected at the browser
 * boundary so the page renders a fixed excluded-domain judgment; the API's gating of personal
 * memory is covered by the pytest suite.
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

const budget = { rounds: 1, subqueries: 1, retrieval_calls: 1, model_calls: 1, context_chunks: 1,
  input_tokens: 1, output_tokens: 1, wall_clock_ms: 1 };
const excludedAnswer = {
  route: { route: "hybrid", reason: "injected" }, answer: "睡眠不足会降低注意力。",
  claims: [{ text: "睡眠不足会降低注意力。", citation_ids: [], kind: "general_knowledge" }],
  citations: [], conflicts: [], assumptions: [], insufficiencies: [], stop_reason: "completed",
  budget_usage: budget, rows: [], personalization_refs: [], memory_context_digest: null,
  context_prompts: [],
  personalization: { question_domain_id: "medicine_health", question_domain_name: "医学与健康",
    matched_keywords: ["睡眠"], method: "keyword-rule-v2", applied: false, reason: "domain_excluded",
    matched_domains: [{ domain_id: "medicine_health", domain_name: "医学与健康", keywords: ["睡眠"] }] },
};

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  page.on("pageerror", (error) => browserErrors.push(error.message));
  const domainBox = (id) => page.locator(`#personalization-domains input[value="${id}"]`);

  async function saveExclusions(ids) {
    for (const input of await page.locator("#personalization-domains input").all()) {
      await input.setChecked(ids.includes(await input.getAttribute("value")));
    }
    const saved = page.waitForResponse((response) =>
      response.url().endsWith("/v1/memory/personalization") && response.request().method() === "PUT");
    await page.locator("#personalization-save").click();
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

    await page.goto(`${base}/memory-center`);
    await domainBox("medicine_health").waitFor();
    assert.equal(await page.locator("#personalization-domains input").count(), 14);

    const saved = await saveExclusions(["medicine_health", "economics_finance_business"]);
    assert.deepEqual(saved.excluded_domain_ids, ["economics_finance_business", "medicine_health"]);
    await page.reload();
    await domainBox("medicine_health").waitFor();
    assert(await domainBox("medicine_health").isChecked());
    assert(await domainBox("economics_finance_business").isChecked());
    await page.screenshot({ path: path.join(output, "personalization-settings.png"), fullPage: true });

    const changed = await saveExclusions(["medicine_health"]);
    assert.deepEqual(changed.excluded_domain_ids, ["medicine_health"]);
    evidence.settings = { saved: saved.excluded_domain_ids, changed: changed.excluded_domain_ids };

    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      "the personalization settings must fit a narrow viewport");
    await page.screenshot({ path: path.join(output, "personalization-settings-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });

    await page.route("**/v1/answers", (route) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify(excludedAnswer),
    }));
    await page.goto(`${base}/knowledge-agent`);
    await page.locator("#question").fill("睡眠不足有什么影响？");
    await page.locator("#answer-form button[type=submit]").click();
    await page.locator("#memory-impact-section").waitFor({ state: "visible" });
    const note = await page.locator("#memory-impact-list").innerText();
    assert(note.includes("未做个性化") && note.includes("医学与健康"), note);
    await page.screenshot({ path: path.join(output, "excluded-domain-answer.png"), fullPage: true });
    evidence.answerNote = note;

    // Later stages share this database; leave every domain personalized again.
    await page.goto(`${base}/memory-center`);
    await domainBox("medicine_health").waitFor();
    await saveExclusions([]);

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "the memory center lists the fourteen primary domains for personalization",
      "chosen domains are saved and survive a reload",
      "chosen domains can be changed later",
      "an answer in an excluded domain says personal memory was not used",
      "personalization settings fit a narrow viewport",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
