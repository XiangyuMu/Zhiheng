/* Verify that a PDF reader span opens its quote and authenticated original page.
 * Usage: node tests/e2e/test_pdf_reader_navigation.cjs URL OUTPUT_DIR [KNOWLEDGE_ID] [EXPECTATION_JSON] [PDF]
 */
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const {
  initializeEvidence,
  loadExpectation,
  sanitizedExpectation,
  sha256File,
  validateExpectationAgainstPdf,
  verifyClickedCitation,
} = require("./pdf_evidence_helpers.cjs");

const [base, output, ...optionalArgs] = process.argv.slice(2);
let [requestedId, expectationPath, pdfFile] = optionalArgs;
if (requestedId && /\.json$/i.test(requestedId)) {
  pdfFile = expectationPath;
  expectationPath = requestedId;
  requestedId = null;
}

const report = {
  started: new Date().toISOString(),
  scope: "pdf-reader-navigation",
  status: "running",
};
const evidence = initializeEvidence("pdf-reader-navigation", output, report);

(async () => {
  let browser;
  let page;
  try {
    assert(!evidence.setupError, evidence.setupError);
    assert(evidence.outputReady, "output directory must be newly created for this evidence run");
    assert(base && output, "Expected URL and output directory");
    assert(["localhost", "127.0.0.1"].includes(new URL(base).hostname), "Local service only");
    assert(process.env.ZHIHENG_TEST_USERNAME && process.env.ZHIHENG_TEST_PASSWORD, "Missing credentials");
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
    const expectedSha = pdfFile ? sha256File(pdfFile) : reader.source?.sha256;
    const expectation = loadExpectation(expectationPath, expectedSha);
    const localPdfCheck = validateExpectationAgainstPdf(pdfFile, expectation);
    report.expectation = sanitizedExpectation(expectation, localPdfCheck);
    const spans = reader.spans || reader.citations || [];
    assert(spans.length > 0, "PDF reader must expose at least one citation span");
    const span = expectation
      ? spans.find((item) => Number(item.page_no) === expectation.page_no)
      : spans.find((item) => item.page_no != null) || spans[0];
    assert(span, "PDF reader must expose the expected citation page");
    const pageNumber = Number(span.page_no);
    assert(Number.isFinite(pageNumber), "PDF citation span must expose a page number");
    report.reader = {
      knowledge_object_id: reader.knowledge_object_id,
      knowledge_version_id: reader.knowledge_version_id,
      span_count: spans.length,
      page_no: pageNumber,
      source_sha256: reader.source?.sha256,
    };
    if (expectation) {
      assert.equal(pageNumber, expectation.page_no, "reader API must expose expected citation page");
    }

    await page.goto(`${base}/knowledge-agent#library`);
    await page.locator("#library-search").fill(reader.title);
    await page.locator("#knowledge-items li").filter({ hasText: reader.title }).first()
      .getByRole("button", { name: /阅读/ }).click();
    await page.locator("#knowledge-detail").waitFor({ state: "visible", timeout: 20_000 });
    report.ui = {
      detail_opened: true,
      citation: await verifyClickedCitation({
        page,
        base,
        reader,
        expectation,
        expectedPdfSha: expectedSha,
      }),
    };
    report.status = "passed";
  } catch (error) {
    report.status = "failed";
    report.failure = String(error.stack || error);
    process.exitCode = 1;
  } finally {
    report.finished = new Date().toISOString();
    if (page && evidence.outputReady) {
      try {
        await page.screenshot({ path: `${evidence.outputPath}/final.png`, fullPage: true });
      } catch (error) {
        report.status = "failed";
        report.screenshot_failure = String(error.stack || error);
        process.exitCode = 1;
      }
    }
    try {
      evidence.save();
    } catch (error) {
      process.exitCode = 1;
      console.error(`failed to write evidence report: ${error.stack || error}`);
    }
    if (browser) await browser.close();
  }
})();
