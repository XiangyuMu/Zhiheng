/* Browser acceptance for Issue #17 delivery contracts.
 *
 * This script uses an authenticated browser context for every HTTP mutation.
 * Taxonomy uses the real taxonomy center UI for per-item migration decisions
 * and authenticated HTTP checks for durable state verification.
 *
 * node tests/e2e/check_delivery_contracts.cjs LOOPBACK_URL OUTPUT_DIR
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { randomUUID } = require("node:crypto");
const runTag = randomUUID().replaceAll("-", "").slice(0, 12);
const personalFinanceDomain = `issue17_personal_finance_${runTag}`;
const businessFinanceDomain = `issue17_business_finance_${runTag}`;
const mergedFinanceDomain = `issue17_finance_merged_${runTag}`;
const taxonomyTitleA = `分类迁移条目 A ${runTag}`;
const taxonomyTitleB = `分类迁移条目 B ${runTag}`;
const taxonomyTitleC = `分类合并条目 C ${runTag}`;
const taxonomyTitleD = `分类合并条目 D ${runTag}`;
const conclusionTitle = `Issue 17 条件结论 ${runTag}`;

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback server URL and screenshot output directory.");
}
fs.mkdirSync(output, { recursive: true });

const MINIMAL_PDF = Buffer.from(
  "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5ZGYpKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAuMCAwLjAgMjAwIDEwMCBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCnhyZWYKMCA1CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDU0IDAwMDAwIG4gCjAwMDAwMDAxMTMgMDAwMDAgbiAKMDAwMDAwMDE2MiAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9TaXplIDUKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjI1NgolJUVPRgo=",
  "base64",
);

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.BROWSER_CHANNEL || "chromium",
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const checks = [];
  const evidence = {};
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));

  async function check(label, work) {
    await work();
    checks.push(label);
    console.log("PASS", label);
  }
  async function shot(name) {
    await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true });
  }
  async function apiJson(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((item) => item.trim())
        .find((item) => item.startsWith("zhiheng_csrf="))
        ?.slice("zhiheng_csrf=".length) || "";
      const response = await fetch(url, {
        credentials: "same-origin",
        ...options,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": "*",
          ...(options.headers || {}),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      const raw = await response.text();
      let body = {};
      try { body = raw ? JSON.parse(raw) : {}; } catch (_) { body = { raw }; }
      if (!response.ok) throw new Error(`${response.status}: ${body.detail || raw}`);
      return body;
    }, { url, options });
  }
  async function apiRaw(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((item) => item.trim())
        .find((item) => item.startsWith("zhiheng_csrf="))?.slice("zhiheng_csrf=".length) || "";
      const response = await fetch(url, {
        credentials: "same-origin",
        ...options,
        headers: {
          Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID(), "If-Match": "*", ...(options.headers || {}),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      const raw = await response.text();
      let body = {};
      try { body = raw ? JSON.parse(raw) : {}; } catch (_) { body = { raw }; }
      return { status: response.status, body };
    }, { url, options });
  }
  async function contextPrompts(query) {
    return apiJson(`/v1/personal-updates/context-prompts?query=${encodeURIComponent(query)}`);
  }
  async function waitKnowledgeSearchable(knowledgeId) {
    let latest;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      latest = await apiJson(`/v1/knowledge/${encodeURIComponent(knowledgeId)}/processing`);
      if (
        latest.public_status === "succeeded"
        && latest.job_status === "completed"
        && latest.searchable === true
      ) return latest;
      await page.waitForTimeout(1000);
    }
    throw new Error(`knowledge object did not become searchable: ${JSON.stringify(latest)}`);
  }
  async function login() {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: "issue17-workspace",
          password: "issue17 workspace passphrase",
        }),
      });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");
  }
  async function makeKnowledge(title, domain) {
    const response = await apiJson("/v1/knowledge/imports", {
      method: "POST",
      body: {
        title,
        text: `${title} 原始材料，保留完整来源和分类迁移历史。`,
        primary_domain_id: domain,
        object_kind: "reflection",
      },
    });
    return response.result.knowledge_object_id;
  }
  async function makeConclusion(claim, premises, key, domain = "education_learning") {
    const source = await apiJson("/v1/conclusions/sources", {
      method: "POST",
      body: { text: `Issue 17 原始对话：${claim}` },
    });
    return apiJson("/v1/conclusions", {
      method: "POST",
      body: {
        source_id: source.id,
        title: conclusionTitle,
        claim,
        domain_id: domain,
        premises,
        excerpt: source.text,
        evidence: [{ text: source.text }],
        valid_until: key === "expired" ? "2000-01-01T00:00:00Z" : undefined,
      },
    });
  }

  let outcome = "passed";
  try {
    await login();

    await check("taxonomy migration preserves each item until its own approval", async () => {
      const first = await makeKnowledge(taxonomyTitleA, "economics_finance_business");
      const second = await makeKnowledge(taxonomyTitleB, "economics_finance_business");
      const proposal = await apiJson("/v1/taxonomy/proposals/domain", {
        method: "POST",
        body: {
          operation: "split",
          source_domain_ids: ["economics_finance_business"],
          new_domains: [
            { id: personalFinanceDomain, name: "Issue17 个人财务", sort_order: 900 },
            { id: businessFinanceDomain, name: "Issue17 商业金融", sort_order: 901 },
          ],
          reason: "Issue 17 browser item-by-item migration",
        },
      });
      const proposalResult = proposal.result;
      const proposalBeforeItemUpdate = await apiJson(`/v1/taxonomy/proposals/${proposalResult.id}`);
      const affectedIds = new Set(
        proposalResult.preview.affected_knowledge.map((item) => item.knowledge_object_id),
      );
      assert(affectedIds.has(first));
      assert(affectedIds.has(second));
      await page.goto(`${base}/taxonomy-center`);
      await page.locator("main").waitFor();
      const proposalRow = page.locator(".proposal").filter({ hasText: "Issue 17 browser item-by-item migration" }).first();
      const firstItem = proposalRow.locator(".migration-item").filter({ hasText: taxonomyTitleA });
      await firstItem.waitFor();
      await firstItem.locator("select").selectOption(personalFinanceDomain);
      await firstItem.getByRole("button", { name: "保存" }).click();
      await page.locator("#proposal-message").filter({ hasText: "迁移决定已保存" }).waitFor();
      const staleApproval = await apiRaw(`/v1/taxonomy/proposals/${proposalResult.id}/approve`, {
        method: "POST", headers: { "If-Match": proposalBeforeItemUpdate.etag },
      });
      assert.equal(staleApproval.status, 412);
      assert.match(String(staleApproval.body.detail), /stale/i);
      const approved = proposalRow.getByRole("button", { name: "批准已确认迁移" }).first();
      await approved.click();
      await page.locator("#proposal-message").filter({ hasText: /迁移已批准|仍有条目待处理/ }).waitFor();
      const finalProposal = await apiJson(`/v1/taxonomy/proposals/${proposalResult.id}`);
      const finalItems = finalProposal.preview.affected_knowledge || finalProposal.preview.items || [];
      assert.equal(
        finalItems.find((item) => item.knowledge_object_id === first)?.migration_status,
        "applied",
      );
      assert.notEqual(
        finalItems.find((item) => item.knowledge_object_id === second)?.migration_status,
        "applied",
      );
      assert.equal(
        (await apiJson(`/v1/knowledge/${first}/classifications`)).primary_domain_id,
        personalFinanceDomain,
      );
      assert.equal(
        (await apiJson(`/v1/knowledge/${second}/classifications`)).primary_domain_id,
        "economics_finance_business",
      );
      const history = await apiJson(`/v1/knowledge/${first}/classification-history`);
      const deferredHistory = await apiJson(`/v1/knowledge/${second}/classification-history`);
      assert(history.items.some((item) => item.action === "domain_migration_approved"));
      assert(!deferredHistory.items.some((item) => item.action === "domain_migration_approved"));
      evidence.taxonomy = {
        mode: "authenticated_browser_ui_with_http_verification",
        proposal_id: proposalResult.id,
        approved_item: first,
        deferred_item: second,
        approved_domain: personalFinanceDomain,
        deferred_domain: "economics_finance_business",
        history_entries: history.items.length,
        deferred_history_entries: deferredHistory.items.length,
        expired_etag_status: staleApproval.status,
      };
      let splitState = await apiJson(`/v1/taxonomy/proposals/${proposalResult.id}`);
      for (const item of splitState.preview.affected_knowledge || []) {
        if (item.migration_status === "applied") continue;
        splitState = await apiJson(`/v1/taxonomy/proposals/${proposalResult.id}`);
        await apiJson(`/v1/taxonomy/proposals/${proposalResult.id}/items/${item.knowledge_object_id}`, {
          method: "PATCH",
          headers: { "If-Match": splitState.etag },
          body: { target_domain_id: businessFinanceDomain },
        });
      }
      splitState = await apiJson(`/v1/taxonomy/proposals/${proposalResult.id}`);
      const splitApproval = await apiJson(`/v1/taxonomy/proposals/${proposalResult.id}/approve`, {
        method: "POST", headers: { "If-Match": splitState.etag },
      });
      assert.equal(splitApproval.result.status, "approved");
      const reviewedInNewDomain = await makeConclusion(
        "新增领域结论可以进入审核",
        [{ text: "新增领域已审核通过", confirmed: true }],
        "new-domain",
        personalFinanceDomain,
      );
      await page.goto(`${base}/review-center`);
      await page.locator(`button.queue-item[data-entry-id="${reviewedInNewDomain.id}"]`).click();
      await page.getByRole("button", { name: "批准", exact: true }).click();
      await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
      const reviewedDetail = await apiJson(`/v1/conclusions/${reviewedInNewDomain.id}`);
      assert.equal(reviewedDetail.status, "formal");
      assert.equal(reviewedDetail.domain_id, personalFinanceDomain);
      evidence.taxonomy.new_domain_review = {
        conclusion_id: reviewedInNewDomain.id,
        domain_id: reviewedDetail.domain_id,
        status: reviewedDetail.status,
      };
      const mergedFirst = await makeKnowledge(taxonomyTitleC, personalFinanceDomain);
      const mergedSecond = await makeKnowledge(taxonomyTitleD, businessFinanceDomain);
      const mergeProposal = await apiJson("/v1/taxonomy/proposals/domain", {
        method: "POST",
        body: {
          operation: "merge",
          source_domain_ids: [personalFinanceDomain, businessFinanceDomain],
          new_domains: [{ id: mergedFinanceDomain, name: "Issue17 合并金融", sort_order: 902 }],
          reason: "Issue 17 browser merge item-by-item migration",
        },
      });
      await page.goto(`${base}/taxonomy-center`);
      await page.locator("main").waitFor();
      const mergeProposalRow = page.locator(".proposal").filter({ hasText: "Issue 17 browser merge item-by-item migration" }).first();
      const mergeItem = mergeProposalRow.locator(".migration-item").filter({ hasText: taxonomyTitleC });
      await mergeItem.waitFor();
      await mergeItem.locator("select").selectOption(mergedFinanceDomain);
      await mergeItem.getByRole("button", { name: "保存" }).click();
      await page.locator("#proposal-message").filter({ hasText: "迁移决定已保存" }).waitFor();
      await mergeProposalRow.getByRole("button", { name: "批准已确认迁移" }).first().click();
      await page.locator("#proposal-message").filter({ hasText: /迁移已批准|仍有条目待处理/ }).waitFor();
      let mergeState = await apiJson(`/v1/taxonomy/proposals/${mergeProposal.result.id}`);
      for (const item of mergeState.preview.affected_knowledge || []) {
        if (item.migration_status === "applied") continue;
        mergeState = await apiJson(`/v1/taxonomy/proposals/${mergeProposal.result.id}`);
        await apiJson(`/v1/taxonomy/proposals/${mergeProposal.result.id}/items/${item.knowledge_object_id}`, {
          method: "PATCH",
          headers: { "If-Match": mergeState.etag },
          body: { target_domain_id: mergedFinanceDomain },
        });
      }
      mergeState = await apiJson(`/v1/taxonomy/proposals/${mergeProposal.result.id}`);
      const mergeApproval = await apiJson(`/v1/taxonomy/proposals/${mergeProposal.result.id}/approve`, {
        method: "POST", headers: { "If-Match": mergeState.etag },
      });
      assert.equal(mergeApproval.result.status, "approved");
      assert.equal(
        (await apiJson(`/v1/knowledge/${mergedFirst}/classifications`)).primary_domain_id,
        mergedFinanceDomain,
      );
      assert.equal(
        (await apiJson(`/v1/knowledge/${mergedSecond}/classifications`)).primary_domain_id,
        mergedFinanceDomain,
      );
      const mergeHistory = await apiJson(`/v1/knowledge/${mergedFirst}/classification-history`);
      const mergeSecondHistory = await apiJson(`/v1/knowledge/${mergedSecond}/classification-history`);
      assert(mergeHistory.items.some((item) => item.action === "domain_migration_approved"));
      assert(mergeSecondHistory.items.some((item) => item.action === "domain_migration_approved"));
      evidence.taxonomy.merge = {
        proposal_id: mergeProposal.result.id,
        approved_item: mergedFirst,
        completed_item: mergedSecond,
        approved_domain: mergedFinanceDomain,
        history_entries: mergeHistory.items.length,
      };
      const inactiveSource = await apiJson("/v1/conclusions/sources", {
        method: "POST", body: { text: `停用领域拒绝测试 ${runTag}` },
      });
      const inactiveDraft = await apiRaw("/v1/conclusions", {
        method: "POST",
        body: {
          source_id: inactiveSource.id,
          title: `停用领域结论 ${runTag}`,
          claim: "停用领域不能创建正式结论",
          domain_id: personalFinanceDomain,
          premises: [], excerpt: inactiveSource.text, evidence: [{ text: inactiveSource.text }],
        },
      });
      assert.equal(inactiveDraft.status, 400);
      assert.match(String(inactiveDraft.body.detail), /inactive|unknown/i);
      evidence.taxonomy.inactive_domain_rejection = {
        domain_id: personalFinanceDomain,
        http_status: inactiveDraft.status,
        error: inactiveDraft.body.detail,
      };
      checks.push("new domain conclusion is reviewed and approved through the browser");
      checks.push("inactive domain rejects conclusion creation after migration");
      checks.push("expired taxonomy ETag rejects approval");
      await shot("taxonomy-center");
    });

    await check("suspended conclusions stay out of context while assumptions remain conditional", async () => {
      const premise = {
        text: "在固定条件下",
        confirmed: false,
        state_key: "issue17.condition",
        value: { text: "固定条件" },
      };
      const conditional = await makeConclusion("固定条件下复习有效", [premise], "conditional");
      const expired = await makeConclusion("已过期的复习结论", [], "expired");
      const conditionalApproval = await apiJson(`/v1/conclusions/${conditional.id}/approve`, {
        method: "POST",
        headers: { "If-Match": conditional.etag },
      });
      await apiJson(`/v1/conclusions/${expired.id}/approve`, {
        method: "POST",
        headers: { "If-Match": expired.etag },
      });
      const processing = await waitKnowledgeSearchable(conditionalApproval.knowledge_id);
      const conditionalDetail = await apiJson(`/v1/conclusions/${conditional.id}`);
      const expiredDetail = await apiJson(`/v1/conclusions/${expired.id}`);
      assert.equal(conditionalDetail.status, "formal");
      assert.equal(expiredDetail.applicability.state, "suspended");
      const context = await apiJson("/v1/conclusions/context?query=复习");
      assert(context.items.some((item) => item.id === conditional.id));
      assert(!context.items.some((item) => item.id === expired.id));
      const reader = await apiJson(`/v1/knowledge/${conditionalApproval.knowledge_id}/reader`);
      assert.match(reader.text, /^如果在固定条件/);
      evidence.applicability = {
        conditional_id: conditional.id,
        conditional_context_visible: true,
        conditional_reader_prefix: reader.text.slice(0, 32),
        conditional_searchable: processing.searchable,
        suspended_id: expired.id,
        suspended_context_visible: false,
        suspended_state: expiredDetail.applicability.state,
      };
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator("#question").fill("固定条件下复习有效");
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#answer-result").waitFor({ state: "visible", timeout: 20000 });
      await page.locator("#citations .source-card").filter({ hasText: conclusionTitle }).waitFor({ timeout: 20000 });
      const answerText = await page.locator("#answer").innerText();
      const citationText = await page.locator("#citations").innerText();
      assert((answerText.includes("如果") || citationText.includes("如果")) && (answerText.includes("固定条件") || citationText.includes("固定条件")));
      assert(!answerText.includes("已过期的复习结论"));
      assert(!citationText.includes("已过期的复习结论"));
      await page.locator("#citations .source-card").filter({ hasText: conclusionTitle }).first().click();
      await page.locator("#citation-context-text").filter({ hasText: "如果在固定条件" }).waitFor();
      await page.keyboard.press("Escape");
      await shot("conditional-answer");
    });

    await check("missing information prompt supports defer, skip, and supplement on the real page", async () => {
      const deferredQuery = `我的工作偏好是什么？本轮${runTag}`;
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator("#question").fill(deferredQuery);
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
      assert((await page.locator("#context-prompt-reason").innerText()).includes("个人背景"));
      const deferredPrompt = (await contextPrompts(deferredQuery)).items.find((item) => item.kind === "missing" && item.status === "pending");
      assert(deferredPrompt);
      assert.equal(deferredPrompt.status, "pending");
      await page.locator("#context-prompt-defer").click();
      await page.locator("#toast").filter({ hasText: "待办" }).waitFor();
      const deferredState = (await contextPrompts(deferredQuery)).items
        .find((item) => item.id === deferredPrompt.id);
      assert.equal(deferredState?.status, "deferred");

      await page.locator("#question").fill("qzxv742919 未记录的天文学结论");
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#answer-limits").waitFor({ state: "visible" });
      assert(!await page.locator("#context-prompt-dialog").isVisible());

      const supplementQuery = `我的第二个工作偏好是什么？本轮${runTag}`;
      await page.locator("#question").fill(supplementQuery);
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
      const supplementPrompt = (await contextPrompts(supplementQuery)).items.find((item) => item.kind === "missing" && item.status === "pending");
      assert(supplementPrompt);
      await page.locator("#context-prompt-input").fill("偏好短反馈");
      await page.locator("#context-prompt-supplement").click();
      await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
      assert(!(await contextPrompts(supplementQuery)).items.some((item) => item.id === supplementPrompt.id));
      const supplementedMemory = await apiJson(`/v1/lookups/memory/${encodeURIComponent(supplementPrompt.state_key)}`);
      assert(supplementedMemory.rows.some((row) => JSON.stringify(row.value_json).includes("偏好短反馈")));

      const skipQuery = `我的第三个工作偏好是什么？本轮${runTag}`;
      await page.locator("#question").fill(skipQuery);
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
      const skipPrompt = (await contextPrompts(skipQuery)).items.find((item) => item.kind === "missing" && item.status === "pending");
      assert(skipPrompt);
      await page.locator("#context-prompt-skip").click();
      await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
      assert(!(await contextPrompts(skipQuery)).items.some((item) => item.id === skipPrompt.id));
      evidence.missing_information = {
        prompt_reason_visible: true,
        decisions: ["defer", "supplement", "skip"],
        supplement_value: "偏好短反馈",
        deferred_prompt_status: deferredState.status,
        unrelated_prompt_suppressed: true,
        supplemented_prompt_removed: true,
        skipped_prompt_removed: true,
      };
      await shot("missing-information");
    });

    await check("real Worker unsupported PDF failure is visible with stable code and recovery actions", async () => {
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator(".topbar [data-open-import]").first().click();
      await page.locator("#import-title").fill("未配置解析器 PDF");
      await page.locator("#import-file").setInputFiles({
        name: "unsupported.pdf",
        mimeType: "application/pdf",
        buffer: MINIMAL_PDF,
      });
      await page.locator("#import-submit").click();
      await page.waitForFunction(() => location.hash === "#library");
      await page.locator("#processing-section").waitFor({ state: "visible", timeout: 20000 });
      const item = page.locator(".processing-item").filter({ hasText: "unsupported_pdf_parser" }).first();
      await item.waitFor({ timeout: 30000 });
      assert((await item.innerText()).includes("configure a parser service"));
      assert(await item.getByRole("button", { name: "补充资料" }).isVisible());
      evidence.import_failure = {
        mode: "real_api_and_independent_worker",
        state: "unsupported",
        error_code: "unsupported_pdf_parser",
        recovery_action_visible: true,
      };
      await shot("import-unsupported");
    });

    assert.deepEqual(errors, [], "no uncaught browser errors");
  } catch (error) {
    outcome = "failed";
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      checks, evidence, browserErrors: errors, status: outcome, error: error.stack || String(error),
    }, null, 2));
    await shot("failure").catch(() => {});
    throw error;
  } finally {
    if (outcome === "passed") {
      fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
        checks, evidence, browserErrors: errors, status: outcome,
      }, null, 2));
    }
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
