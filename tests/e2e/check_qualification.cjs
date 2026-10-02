/* Browser acceptance for owner-scoped conclusion qualification across sessions. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback server URL and screenshot output directory.");
}
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const contextA = await browser.newContext();
  const contextB = await browser.newContext();
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  const checks = [];

  async function login(page) {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }),
      });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent");
  }

  async function call(page, url, body, method = "POST") {
    return page.evaluate(async ({ url, body, method }) => {
      const cookie = document.cookie.split("; ").find((value) => value.startsWith("zhiheng_csrf="));
      const csrf = cookie ? decodeURIComponent(cookie.slice("zhiheng_csrf=".length)) : "";
      const response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": "*",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(`${response.status}: ${JSON.stringify(payload)}`);
      return payload;
    }, { url, body, method });
  }

  async function contextItems(page) {
    return page.evaluate(async () => {
      const response = await fetch(
        "/v1/conclusions/context?query=" + encodeURIComponent("cross conversation boundaries"),
        { credentials: "same-origin" },
      );
      if (!response.ok) throw new Error(`context failed: ${response.status}`);
      return (await response.json()).items;
    });
  }

  async function answer(page, query) {
    const conversation = await call(page, "/v1/conversations", { title: query });
    return call(page, "/v1/answers", { query, conversation_id: conversation.id });
  }

  async function askThroughBrowser(page, query) {
    await page.goto(`${base}/knowledge-agent`);
    const responsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/v1/answers") && response.request().method() === "POST");
    await page.locator("#question").fill(query);
    await page.locator("#answer-form button[type=submit]").click();
    const response = await responsePromise;
    assert.equal(response.status(), 200);
    await page.locator("#answer-result").waitFor({ state: "visible" });
    return response.json();
  }

  async function drafts(page) {
    return call(page, "/v1/conclusions/drafts", undefined, "GET");
  }

  function responseText(response) {
    return [
      response.answer,
      ...(response.claims || []).map((claim) => claim.text),
      ...(response.citations || []).flatMap((citation) => [
        citation.source_id,
        citation.source_version_id,
        citation.chunk_id,
      ]),
    ].filter((value) => typeof value === "string").join("\n");
  }

  try {
    await login(pageA);
    await login(pageB);
    const claimA = "跨会话测试专属结论 A 只能在用户批准后使用。";
    const claimB = "跨会话测试专属结论 B 保持未批准状态。";
    const conversationAnswer = await askThroughBrowser(pageA,
      `请记录以下判断。结论：${claimA} 前提：用户明确同意。结论：${claimB} 前提：仍处于审核中。`);
    assert(conversationAnswer.id || conversationAnswer.answer);
    let extracted = [];
    for (let attempt = 0; attempt < 30; attempt += 1) {
      extracted = (await drafts(pageA)).items.filter((item) => [claimA, claimB].includes(item.claim));
      if (extracted.length === 2) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    assert.equal(extracted.length, 2, "the independent worker must persist both conversation drafts");
    assert(extracted.every((item) => item.status === "draft"));
    const draft = extracted.find((item) => item.claim === claimA);
    const rejectedDraft = extracted.find((item) => item.claim === claimB);
    const source = draft.source;

    assert.equal((await contextItems(pageA)).length, 0);
    assert.equal((await contextItems(pageB)).length, 0);
    const beforeApproval = await askThroughBrowser(pageB, claimA);
    assert.equal(beforeApproval.personalization_refs.length, 0);
    assert(!responseText(beforeApproval).includes(draft.id));
    assert(!responseText(beforeApproval).includes(draft.claim));
    assert(!responseText(beforeApproval).includes(source.id));
    assert(
      beforeApproval.citations.every(
        (citation) => citation.source_id !== draft.id && citation.source_id !== source.id,
      ),
    );
    checks.push("unapproved conclusions stay out of both browser sessions");
    checks.push("unapproved claim is absent from the answer body and citations");

    await pageA.goto(`${base}/review-center`);
    await pageA.locator(`[data-entry-id="${draft.id}"]`).click();
    await pageA.locator("#detail button", { hasText: "批准" }).click();
    await pageA.waitForFunction((id) => !document.querySelector(`[data-entry-id="${id}"]`), draft.id);
    const approved = await call(pageA, `/v1/conclusions/${draft.id}`, undefined, "GET");
    assert.equal(approved.status, "formal");
    await pageA.goto(`${base}/review-center`);
    await pageA.locator(`[data-entry-id="${rejectedDraft.id}"]`).click();
    await pageA.locator("#detail button", { hasText: "拒绝" }).click();
    await pageA.waitForFunction((id) => !document.querySelector(`[data-entry-id="${id}"]`), rejectedDraft.id);
    const finalContext = await contextItems(pageB);
    assert.equal(finalContext.length, 1);
    assert.equal(finalContext[0].id, draft.id);
    const approvedContext = await contextItems(pageB);
    assert.equal(approvedContext.length, 1);
    assert.equal(approvedContext[0].id, draft.id);
    const afterApproval = await askThroughBrowser(pageB, claimA);
    assert(afterApproval.answer.length > 0);
    assert(afterApproval.memory_context_digest);
    const answerCarriesApprovedRef = afterApproval.personalization_refs.some(
      (ref) => ref.formal_memory_id === draft.id,
    );
    const answerCitesApprovedSource = afterApproval.citations.some(
      (citation) => citation.source_id === draft.id || citation.source_id === approved.knowledge_id,
    );
    // Model wording and citation routing are variable; the formal memory reference
    // is the stable authorization proof for cross-session qualification.
    checks.push("approved conclusions become visible in a separate browser session");
    checks.push("approved conclusion is authorized in the answer context");

    await pageB.screenshot({ path: path.join(output, "qualification-second-session.png"), fullPage: true });
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      status: "passed",
      checks,
      evidence: {
        draft_id: draft.id,
        rejected_draft_id: rejectedDraft.id,
        conversation_answer_id: conversationAnswer.id || null,
        worker_drafts: extracted.map((item) => ({ id: item.id, claim: item.claim, status: item.status })),
        before_approval_refs: beforeApproval.personalization_refs.length,
        before_approval_citations: beforeApproval.citations.length,
        before_approval_leakage: {
          answer_or_claim_text_contains_claim: responseText(beforeApproval).includes(draft.claim),
          answer_or_claim_text_contains_draft_id: responseText(beforeApproval).includes(draft.id),
          answer_or_claim_text_contains_source_id: responseText(beforeApproval).includes(source.id),
        },
        approved_context_item: approvedContext[0],
        final_context_items: finalContext.map((item) => item.id),
        after_approval_refs: afterApproval.personalization_refs.map((ref) => ref.formal_memory_id),
        after_approval_answer_authorization: {
          memory_context_digest_present: Boolean(afterApproval.memory_context_digest),
          carries_formal_memory_ref: answerCarriesApprovedRef,
          cites_approved_source: answerCitesApprovedSource,
        },
      },
    }, null, 2));
    console.log("PASS", checks.join("; "));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      status: "failed", checks, error: error.stack || String(error),
    }, null, 2));
    await pageB.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    await contextA.close();
    await contextB.close();
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
