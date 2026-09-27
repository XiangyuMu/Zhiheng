/* Browser acceptance for Issue #17 delivery contracts.
 *
 * This script uses an authenticated browser context for every HTTP mutation.
 * Taxonomy has a dedicated center but the item migration contract is asserted
 * through its authenticated API because the main workspace does not expose the
 * proposal editor as a visible navigation item.
 *
 * node tests/e2e/check_delivery_contracts.cjs LOOPBACK_URL OUTPUT_DIR
 */
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
  async function makeConclusion(claim, premises, key) {
    const source = await apiJson("/v1/conclusions/sources", {
      method: "POST",
      body: { text: `Issue 17 原始对话：${claim}` },
    });
    return apiJson("/v1/conclusions", {
      method: "POST",
      body: {
        source_id: source.id,
        title: "Issue 17 条件结论",
        claim,
        domain_id: "education_learning",
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
      const first = await makeKnowledge("分类迁移条目 A", "economics_finance_business");
      const second = await makeKnowledge("分类迁移条目 B", "economics_finance_business");
      const proposal = await apiJson("/v1/taxonomy/proposals/domain", {
        method: "POST",
        body: {
          operation: "split",
          source_domain_ids: ["economics_finance_business"],
          new_domains: [
            { id: "issue17_personal_finance", name: "Issue17 个人财务", sort_order: 900 },
            { id: "issue17_business_finance", name: "Issue17 商业金融", sort_order: 901 },
          ],
          reason: "Issue 17 browser item-by-item migration",
        },
      });
      const proposalResult = proposal.result;
      assert.deepEqual(
        new Set(proposalResult.preview.affected_knowledge.map((item) => item.knowledge_object_id)),
        new Set([first, second]),
      );
      await page.goto(`${base}/taxonomy-center`);
      await page.locator("main").waitFor();
      const migrationItems = page.locator(".migration-item");
      const firstItem = migrationItems.filter({ hasText: "分类迁移条目 A" });
      await firstItem.waitFor();
      await firstItem.locator("select").selectOption("issue17_personal_finance");
      await firstItem.getByRole("button", { name: "保存" }).click();
      await page.locator("#proposal-message").filter({ hasText: "迁移决定已保存" }).waitFor();
      const approved = page.getByRole("button", { name: "批准已确认迁移" });
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
        "issue17_personal_finance",
      );
      assert.equal(
        (await apiJson(`/v1/knowledge/${second}/classifications`)).primary_domain_id,
        "economics_finance_business",
      );
      const history = await apiJson(`/v1/knowledge/${first}/classification-history`);
      assert(history.items.some((item) => item.action === "domain_migration_approved"));
      evidence.taxonomy = {
        mode: "authenticated_browser_ui_with_http_verification",
        proposal_id: proposalResult.id,
        approved_item: first,
        deferred_item: second,
        approved_domain: "issue17_personal_finance",
        deferred_domain: "economics_finance_business",
        history_entries: history.items.length,
      };
      const mergedFirst = await makeKnowledge("分类合并条目 C", "issue17_personal_finance");
      const mergedSecond = await makeKnowledge("分类合并条目 D", "issue17_business_finance");
      const mergeProposal = await apiJson("/v1/taxonomy/proposals/domain", {
        method: "POST",
        body: {
          operation: "merge",
          source_domain_ids: ["issue17_personal_finance", "issue17_business_finance"],
          new_domains: [{ id: "issue17_finance_merged", name: "Issue17 合并金融", sort_order: 902 }],
          reason: "Issue 17 browser merge item-by-item migration",
        },
      });
      await page.goto(`${base}/taxonomy-center`);
      await page.locator("main").waitFor();
      const mergeProposalRow = page.locator(".proposal").filter({ hasText: "Issue 17 browser merge item-by-item migration" });
      const mergeItem = mergeProposalRow.locator(".migration-item").filter({ hasText: "分类合并条目 C" });
      await mergeItem.waitFor();
      await mergeItem.locator("select").selectOption("issue17_finance_merged");
      await mergeItem.getByRole("button", { name: "保存" }).click();
      await page.locator("#proposal-message").filter({ hasText: "迁移决定已保存" }).waitFor();
      await mergeProposalRow.getByRole("button", { name: "批准已确认迁移" }).click();
      await page.locator("#proposal-message").filter({ hasText: /迁移已批准|仍有条目待处理/ }).waitFor();
      assert.equal(
        (await apiJson(`/v1/knowledge/${mergedFirst}/classifications`)).primary_domain_id,
        "issue17_finance_merged",
      );
      assert.equal(
        (await apiJson(`/v1/knowledge/${mergedSecond}/classifications`)).primary_domain_id,
        "issue17_business_finance",
      );
      evidence.taxonomy.merge = {
        proposal_id: mergeProposal.result.id,
        approved_item: mergedFirst,
        deferred_item: mergedSecond,
        approved_domain: "issue17_finance_merged",
      };
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
      const conditionalDetail = await apiJson(`/v1/conclusions/${conditional.id}`);
      const expiredDetail = await apiJson(`/v1/conclusions/${expired.id}`);
      assert.equal(conditionalDetail.status, "formal");
      assert.equal(expiredDetail.applicability.state, "suspended");
      const context = await apiJson("/v1/conclusions/context?query=复习");
      assert(context.items.some((item) => item.id === conditional.id));
      assert(!context.items.some((item) => item.id === expired.id));
      const reader = await apiJson(`/v1/knowledge/${conditionalApproval.knowledge_id}/reader`);
      assert.match(reader.text, /^如果固定条件/);
      evidence.applicability = {
        conditional_id: conditional.id,
        conditional_context_visible: true,
        conditional_reader_prefix: reader.text.slice(0, 32),
        suspended_id: expired.id,
        suspended_context_visible: false,
        suspended_state: expiredDetail.applicability.state,
      };
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator("#question").fill("固定条件下复习是否有效？");
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#answer-result").waitFor({ state: "visible", timeout: 20000 });
      assert((await page.locator("#answer").innerText()).length > 0);
      await shot("conditional-answer");
    });

    await check("missing information prompt supports defer, skip, and supplement on the real page", async () => {
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator("#question").fill("我的工作偏好是什么？");
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
      assert((await page.locator("#context-prompt-reason").innerText()).includes("个人背景"));
      await page.locator("#context-prompt-defer").click();
      await page.locator("#toast").filter({ hasText: "待办" }).waitFor();
      await page.locator("#question").fill("我的第二个工作偏好是什么？");
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
      await page.locator("#context-prompt-input").fill("偏好短反馈");
      await page.locator("#context-prompt-supplement").click();
      await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
      await page.locator("#question").fill("我的第三个工作偏好是什么？");
      await page.locator("#answer-form button[type=submit]").click();
      await page.locator("#context-prompt-dialog").waitFor({ state: "visible", timeout: 20000 });
      await page.locator("#context-prompt-skip").click();
      await page.locator("#toast").filter({ hasText: "提示已处理" }).waitFor();
      evidence.missing_information = {
        prompt_reason_visible: true,
        decisions: ["defer", "supplement", "skip"],
        supplement_value: "偏好短反馈",
      };
      await shot("missing-information");
    });

    await check("real Worker unsupported PDF failure is visible with stable code and recovery actions", async () => {
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator("[data-open-import]").click();
      await page.locator("#import-title").fill("未配置解析器 PDF");
      await page.locator("#import-file").setInputFiles({
        name: "unsupported.pdf",
        mimeType: "application/pdf",
        buffer: MINIMAL_PDF,
      });
      await page.locator("#import-submit").click();
      await page.waitForFunction(() => location.hash === "#library");
      await page.locator("#processing-section").waitFor({ state: "visible", timeout: 20000 });
      await page.locator(".processing-item").filter({ hasText: "unsupported_pdf_parser" }).waitFor({ timeout: 30000 });
      const item = page.locator(".processing-item").filter({ hasText: "unsupported_pdf_parser" }).first();
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
