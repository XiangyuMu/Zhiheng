/* Verify that a PDF reader span opens its quote and authenticated original page.
 * Usage: node tests/e2e/test_pdf_reader_navigation.cjs URL OUTPUT_DIR [KNOWLEDGE_ID]
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");

const [base, output, requestedId] = process.argv.slice(2);
const outputExists = Boolean(output && fs.existsSync(output));
if (output && !outputExists) fs.mkdirSync(output, { mode: 0o700 });

const report = { scope: "pdf-reader-navigation", status: "running" };
const reportPath = output
  ? path.join(output, outputExists ? `failure-${Date.now()}.json` : "report.json")
  : null;
const save = () => {
  if (reportPath) fs.writeFileSync(reportPath, JSON.stringify(report, null, 2), { mode: 0o600 });
};

(async () => {
  let browser;
  let page;
  try {
    assert(base && output, "Expected URL and output directory");
    assert(["localhost", "127.0.0.1"].includes(new URL(base).hostname), "Local service only");
    assert(process.env.ZHIHENG_TEST_USERNAME && process.env.ZHIHENG_TEST_PASSWORD, "Missing credentials");
    assert(!outputExists, `output directory already exists: ${output}`);
    report.sha = execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    report.dirty = Boolean(execFileSync("git", ["status", "--porcelain"], { encoding: "utf8" }).trim());
    assert.equal(report.dirty, false, "final browser evidence requires a clean worktree");
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.goto(`${base}/login?next=${encodeURIComponent("/knowledge-agent#library")}`);
    await page.locator("#username").fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator("#password").fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent*");

    const reader = await page.evaluate(async (knowledgeId) => {
      let id = knowledgeId;
      if (!id) {
        const response = await fetch("/v1/knowledge/items?limit=100", { credentials: "same-origin" });
        if (!response.ok) throw new Error(`knowledge list failed: ${response.status}`);
        const items = (await response.json()).items || [];
        id = items.find((item) => item.media_type === "application/pdf" || item.source_type === "pdf")?.knowledge_object_id;
      }
      if (!id) throw new Error("No PDF knowledge object available; pass KNOWLEDGE_ID");
      const response = await fetch(`/v1/knowledge/${encodeURIComponent(id)}/reader`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`reader failed: ${response.status}`);
      return await response.json();
    }, requestedId);
    assert.equal(reader.media_type, "application/pdf");
    const spans = reader.spans || reader.citations || [];
    assert(spans.length > 0, "PDF reader must expose at least one citation span");
    const span = spans.find((item) => item.page_no != null) || spans[0];
    const pageNumber = Number(span.page_no);
    assert(Number.isFinite(pageNumber), "PDF citation span must expose a page number");
    report.reader = {
      knowledge_object_id: reader.knowledge_object_id,
      knowledge_version_id: reader.knowledge_version_id,
      span_count: spans.length,
      page_no: pageNumber,
      source_sha256: reader.source?.sha256,
    };

    await page.goto(`${base}/knowledge-agent#library`);
    await page.locator("#library-search").fill(reader.title);
    await page.locator("#knowledge-items li").filter({ hasText: reader.title }).first()
      .getByRole("button", { name: /阅读/ }).click();
    await page.locator("#knowledge-detail").waitFor({ state: "visible", timeout: 20_000 });
    const citation = page.locator('[data-reader-citation="true"]').first();
    await citation.waitFor({ state: "visible", timeout: 20_000 });
    await citation.click();
    await page.locator("#citation-context").waitFor({ state: "visible", timeout: 10_000 });
    const quote = page.locator('#citation-context-text [data-reader-quote="true"]');
    await quote.waitFor({ state: "visible", timeout: 10_000 });
    assert((await quote.innerText()).trim(), "reader citation must show the original quote");
    const pageLink = page.locator("#citation-page-link");
    await pageLink.waitFor({ state: "visible", timeout: 10_000 });
    const expected = `${new URL(base).origin}/v1/knowledge/${encodeURIComponent(reader.knowledge_object_id)}/export?format=original&disposition=inline&version_id=${encodeURIComponent(reader.knowledge_version_id)}#page=${pageNumber}`;
    assert.equal(await pageLink.getAttribute("href"), expected.replace(new URL(base).origin, ""));
    assert.equal(await pageLink.getAttribute("target"), "_blank");
    const original = await page.request.get(new URL(await pageLink.getAttribute("href"), base).href);
    assert.equal(original.status(), 200);
    assert.match(original.headers()["content-type"] || "", /application\/pdf/i);
    report.ui = {
      detail_opened: true,
      clicked_selector: '[data-reader-citation="true"]',
      quote_selector: '#citation-context-text [data-reader-quote="true"]',
      page_link_selector: "#citation-page-link",
      page_link: await pageLink.getAttribute("href"),
      page_link_status: original.status(),
      quote: (await quote.innerText()).trim(),
    };
    report.status = "passed";
  } catch (error) {
    report.status = "failed";
    report.failure = String(error.stack || error);
    process.exitCode = 1;
  } finally {
    if (page) {
      try { await page.screenshot({ path: path.join(output, "final.png"), fullPage: true }); } catch (error) {
        report.status = "failed";
        report.screenshot_failure = String(error.stack || error);
        process.exitCode = 1;
      }
    }
    save();
    if (browser) await browser.close();
  }
})();
