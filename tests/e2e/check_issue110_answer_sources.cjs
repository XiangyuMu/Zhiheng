/* Issue #110: answers label general knowledge and say when the knowledge base has nothing relevant.
 *
 * The answer API responses below are injected at the browser boundary so the page renders
 * fixed claim kinds; the API's own validation of claim kinds is covered by the pytest suite.
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

const notice = "知识库中没有与这个问题相关的内容。";
const budget = { rounds: 1, subqueries: 1, retrieval_calls: 1, model_calls: 1, context_chunks: 1,
  input_tokens: 1, output_tokens: 1, wall_clock_ms: 1 };
function answer(fields) {
  return { route: { route: "hybrid", reason: "injected" }, conflicts: [], assumptions: [],
    insufficiencies: [], stop_reason: "completed", budget_usage: budget, rows: [],
    personalization_refs: [], memory_context_digest: null, context_prompts: [], ...fields };
}
const citation = {
  citation_id: "issue110-citation", source_type: "knowledge", source_id: "k1", source_version_id: "v1",
  chunk_id: "c1", evidence_object_id: "e1", content_version_id: "cv1", content_span_id: "s1",
  content_span: [0, 4], offset: [0, 4], page_no: null, section_path: null, quote_hash: "q",
  title: "检索笔记", text: "资料原文",
};
const responses = {
  insufficient: answer({
    answer: `${notice}一般来说，检索系统会结合关键词和向量召回。`,
    claims: [{ text: "检索系统通常结合关键词和向量召回。", citation_ids: [], kind: "general_knowledge" }],
    citations: [],
    insufficiencies: ["知识库中没有与该问题相关的资料。"],
  }),
  mixed: answer({
    answer: "资料记录了检索笔记；另外按通用知识补充一点。",
    claims: [
      { text: "资料记录了检索笔记。", citation_ids: [citation.citation_id], kind: "cited" },
      { text: "向量召回适合语义相近的问题。", citation_ids: [], kind: "general_knowledge" },
    ],
    citations: [citation],
  }),
};

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  page.on("pageerror", (error) => browserErrors.push(error.message));
  let next = "insufficient";
  await page.route("**/v1/answers", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(responses[next]),
  }));

  async function ask(kind, question) {
    next = kind;
    await page.locator("#question").fill(question);
    await page.locator("#answer-form button[type=submit]").click();
    await page.locator("#answer-result").waitFor({ state: "visible" });
    await page.locator("#asked-question").filter({ hasText: question }).waitFor();
    return {
      route: await page.locator("#route").innerText(),
      answer: await page.locator("#answer").innerText(),
      claims: await page.locator("#answer-claims li").evaluateAll((items) => items.map((item) => ({
        label: item.querySelector(".badge")?.textContent || "", text: item.textContent || "",
      }))),
    };
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

    const empty = await ask("insufficient", "知识库里没有的问题");
    assert(empty.answer.startsWith(notice), empty.answer);
    assert.equal(empty.route, "基于通用知识");
    assert.deepEqual(empty.claims.map((claim) => claim.label), ["通用知识"]);
    await page.screenshot({ path: path.join(output, "insufficient-general-knowledge.png"), fullPage: true });
    evidence.insufficient = empty;

    const mixed = await ask("mixed", "资料和通用知识都涉及的问题");
    assert.deepEqual(mixed.claims.map((claim) => claim.label), ["正式知识", "通用知识"]);
    assert(!mixed.answer.startsWith(notice), mixed.answer);
    assert.equal(await page.locator("#citations li").count(), 1);
    await page.screenshot({ path: path.join(output, "mixed-claim-sources.png"), fullPage: true });
    evidence.mixed = mixed;

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "an answer with nothing relevant in the knowledge base opens by saying so",
      "uncited claims are labelled as general knowledge",
      "cited claims are labelled with their source beside general knowledge",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
