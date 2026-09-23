/* Browser acceptance for relation review and formal knowledge serving.
 * PLAYWRIGHT_MODULE_PATH=/path/to/playwright node tests/e2e/check_review_relations.cjs URL OUTPUT_DIR
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback server URL and screenshot output directory.");
}
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.BROWSER_CHANNEL || "chromium",
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const checks = [];
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));

  async function check(label, work) {
    await work();
    checks.push(label);
    console.log("PASS", label);
  }
  async function apiJson(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const cookie = document.cookie.split(";").map((value) => value.trim())
        .find((value) => value.startsWith("zhiheng_csrf="));
      const csrf = cookie ? cookie.slice("zhiheng_csrf=".length) : "";
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
        body: options.body ? JSON.stringify(options.body) : undefined,
      });
      const body = await response.json();
      if (!response.ok) throw new Error(`${response.status}: ${body.detail || "request failed"}`);
      return body;
    }, { url, options });
  }
  async function makeDraft(title, claim, premise) {
    const source = await apiJson("/v1/conclusions/sources", {
      method: "POST",
      body: { text: `浏览器关系来源：${title}` },
    });
    return apiJson("/v1/conclusions", {
      method: "POST",
      body: {
        source_id: source.id,
        title,
        claim,
        domain_id: "education_learning",
        premises: [{ text: premise, confirmed: false }],
        excerpt: `浏览器关系摘录：${claim}`,
        evidence: [{ text: `浏览器关系来源：${title}` }],
      },
    });
  }

  try {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      await fetch("/auth/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: "issue17-workspace",
          password: "issue17 workspace passphrase",
        }),
      });
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");

    const old = await makeDraft("关系审核旧结论", "每天复习有效", "固定时间");
    await apiJson(`/v1/conclusions/${old.id}/approve`, { method: "POST", headers: { "If-Match": old.etag } });
    const deferred = await makeDraft("关系审核待办结论", "每天复习无效", "固定时间");
    const rejected = await makeDraft("关系审核拒绝结论", "每天复习不能提高记忆", "固定时间");
    const approved = await makeDraft("关系审核批准结论", "每天复习需要更多样本", "固定时间");
    for (const draft of [deferred, rejected, approved]) {
      await apiJson(`/v1/conclusions/${draft.id}/relations/suggest`, { method: "POST" });
    }

    await page.goto(`${base}/review-center`);
    await page.locator(".queue-item").filter({ hasText: "关系建议" }).filter({ hasText: "关系审核" }).nth(2).waitFor();
    await check("relation suggestions appear in the central review queue", async () => {
      assert.equal(await page.locator(".queue-item").filter({ hasText: "关系建议" }).count(), 3);
      await page.locator(".queue-item").filter({ hasText: "关系建议" }).filter({ hasText: "每天复习无效" }).click();
      const detail = await page.locator("#detail").innerText();
      assert(detail.includes("每天复习无效"));
      assert(detail.includes("每天复习有效"));
      assert(detail.includes("固定时间"));
      assert(detail.includes("浏览器关系来源"));
      assert(detail.includes("v1"));
      await page.screenshot({ path: path.join(output, "relation-detail.png"), fullPage: true });
    });

    await page.getByRole("button", { name: "稍后处理", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    await check("deferred relation remains reviewable", async () => {
      assert.equal(await apiJson(`/v1/conclusions/${deferred.id}/relations`).then((body) => body.items[0].status), "deferred");
      await page.reload();
      await page.locator(".queue-item").filter({ hasText: "关系建议" }).filter({ hasText: "关系审核" }).nth(2).waitFor();
    });

    await page.locator(".queue-item").filter({ hasText: "关系建议" }).filter({ hasText: "每天复习不能提高记忆" }).click();
    await page.getByRole("button", { name: "拒绝关系", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    await check("reject relation records the real API decision", async () => {
      assert.equal(await apiJson(`/v1/conclusions/${rejected.id}/relations`).then((body) => body.items[0].status), "rejected");
      assert.equal(await page.locator("#relation-count").innerText(), "2");
    });

    await page.locator(".queue-item").filter({ hasText: "关系建议" }).filter({ hasText: "每天复习需要更多样本" }).click();
    await page.getByRole("button", { name: "批准关系", exact: true }).click();
    await page.locator("#message").filter({ hasText: "操作已保存" }).waitFor();
    await check("approved relation enters formal searchable knowledge", async () => {
      const relation = (await apiJson(`/v1/conclusions/${approved.id}/relations`)).items[0];
      assert.equal(relation.status, "approved");
      const formal = await apiJson(`/v1/conclusions/${approved.id}`);
      assert.equal(formal.status, "formal");
      assert((await apiJson("/v1/conclusions/context?query=更多样本")).items.length > 0);
    });

    assert.deepEqual(errors, [], "no uncaught browser errors");
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ checks, browserErrors: errors }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
