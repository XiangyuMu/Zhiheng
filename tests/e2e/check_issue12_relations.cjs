/* Issue #12: HTTP evidence for stale relation approval, history, and answer authority. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) throw new Error("Pass a disposable loopback server URL and output directory.");
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext();
  const page = await context.newPage();
  const checks = [];
  const evidence = { stale: {}, approved: {}, answer: {} };
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  async function login() {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }) });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent");
  }
  async function request(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith("zhiheng_csrf="))?.slice("zhiheng_csrf=".length) || "";
      const response = await fetch(url, { credentials: "same-origin", ...options, headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID(), "If-Match": "*", ...(options.headers || {}) }, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
      let body; try { body = await response.json(); } catch { body = {}; }
      return { status: response.status, body };
    }, { url, options });
  }
  async function ok(url, options = {}) {
    const result = await request(url, options);
    if (result.status < 200 || result.status >= 300) throw new Error(`${result.status}: ${JSON.stringify(result.body)}`);
    return result.body;
  }
  async function draft(label, claim) {
    const source = await ok("/v1/conclusions/sources", { method: "POST", body: { text: `Issue 12 原始材料：${label}` } });
    return ok("/v1/conclusions", { method: "POST", body: { source_id: source.id, title: `Issue 12 ${label}`, claim, domain_id: "education_learning", premises: [{ text: "固定关系前提", confirmed: true }], excerpt: claim, evidence: [{ text: source.text }] } });
  }
  try {
    await login();
    const anchor = await draft("基准", "关系审核基准结论可用于复习");
    const anchorApproval = await ok(`/v1/conclusions/${anchor.id}/approve`, { method: "POST", headers: { "If-Match": anchor.etag } });
    const staleDraft = await draft("过期关系", "关系审核基准结论不能用于复习");
    const staleRelation = (await ok(`/v1/conclusions/${staleDraft.id}/relations`)).items.find((item) => item.kind === "conflict");
    assert(staleRelation, "expected a conflict relation");
    const changed = await ok(`/v1/conclusions/${staleDraft.id}`, { method: "PATCH", headers: { "If-Match": staleDraft.etag }, body: { claim: "关系审核基准结论在新版本中不能用于复习" } });
    const staleApproval = await request(`/v1/conclusions/relations/${staleRelation.id}/approve`, { method: "POST" });
    assert.equal(staleApproval.status, 409);
    assert.match(String(staleApproval.body.detail), /stale|过期|refresh/i);
    const staleCurrent = await ok(`/v1/conclusions/${staleDraft.id}`);
    const staleDetails = (await ok(`/v1/conclusions/${staleDraft.id}/relations`)).items.find((item) => item.id === staleRelation.id);
    assert.equal(staleCurrent.status, "draft");
    assert.equal(staleCurrent.version, 2);
    assert.equal(staleDetails.status, "proposed");
    assert.equal(staleDetails.left_version, 1);
    assert.equal(staleDetails.right_version, 1);
    assert.equal(staleDetails.history.length, 1);
    assert.equal(staleDetails.history[0].to_status, "proposed");
    evidence.stale = { relation_id: staleRelation.id, left_id: staleDraft.id, right_id: anchor.id, proposed_left_version: staleRelation.left_version, current_left_version: changed.version, right_version: staleRelation.right_version, approve_http_status: staleApproval.status, approve_error: staleApproval.body.detail, history: staleDetails.history };
    checks.push("HTTP approval rejects a relation after the left conclusion version changes");
    checks.push("stale relation HTTP details preserve both versions and proposal history");

    const supplement = await draft("补充关系", "关系审核基准结论可用于复习并巩固理解");
    const relation = (await ok(`/v1/conclusions/${supplement.id}/relations`)).items.find((item) => item.kind === "supplement" || item.kind === "revision");
    assert(relation, "expected supplement or revision relation");
    const approved = await ok(`/v1/conclusions/relations/${relation.id}/approve`, { method: "POST" });
    const relationAfter = (await ok(`/v1/conclusions/${supplement.id}/relations`)).items.find((item) => item.id === relation.id);
    assert.equal(relationAfter.status, "approved");
    assert.deepEqual(relationAfter.history.map((event) => event.to_status), ["proposed", "approved"]);
    const formal = await ok(`/v1/conclusions/${supplement.id}`);
    assert.equal(formal.status, "formal");
    assert.equal(formal.approved_version, relationAfter.left_version);
    assert.equal(relationAfter.left_version, 1);
    assert.equal(relationAfter.right_version, 1);
    assert.equal(relationAfter.history[1].left_source_id, relationAfter.left_source_id);
    assert.equal(relationAfter.history[1].right_source_id, relationAfter.right_source_id);
    const reader = await ok(`/v1/knowledge/${approved.knowledge_id}/reader`);
    const answer = await ok("/v1/answers", { method: "POST", body: { query: "关系审核基准结论可用于复习并巩固理解" } });
    const citation = answer.citations.find((item) => item.source_id === approved.knowledge_id);
    const memoryRef = answer.personalization_refs.find((item) => item.formal_memory_id === supplement.id);
    assert(citation || memoryRef, "answer must expose the relation's published authority");
    if (citation) {
      assert.equal(citation.source_version_id, reader.knowledge_version_id);
      assert.equal(citation.content_version_id, reader.content_version_id);
    }
    evidence.approved = { relation_id: relation.id, kind: relationAfter.kind, knowledge_id: approved.knowledge_id, left_version: relationAfter.left_version, right_version: relationAfter.right_version, history: relationAfter.history, formal_status: formal.status };
    evidence.answer = {
      stop_reason: answer.stop_reason,
      citation: citation ? { source_id: citation.source_id, source_version_id: citation.source_version_id, content_version_id: citation.content_version_id } : null,
      formal_memory_ref: memoryRef || null,
      reader_version: reader.knowledge_version_id,
      reader_content_version: reader.content_version_id,
    };
    checks.push("approved relation HTTP history records the exact source and version pair");
    checks.push("answer citations use the approved relation's current knowledge and content version");
    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks, evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", checks, evidence, browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
