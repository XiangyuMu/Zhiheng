/* Real-browser PDF index/search/citation acceptance for Issue #53.
 * Usage: node .../check_pdf_search_issue53.cjs URL OUTPUT_DIR PDF
 * The API and worker are real; this script does not call internal functions.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const [base, output, file] = process.argv.slice(2);
assert(base && output && file, 'Expected URL, output directory and PDF');
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname), 'Local service only');
assert(process.env.ZHIHENG_TEST_USERNAME && process.env.ZHIHENG_TEST_PASSWORD, 'Missing credentials');
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const sourceSha = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const report = {
  started: new Date().toISOString(),
  scope: 'issue53-real-pdf-index-search-citation',
  sha: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
  dirty: Boolean(execFileSync('git', ['status', '--porcelain'], { encoding: 'utf8' }).trim()),
  input: { name: path.basename(file), sha256: sourceSha },
  states: [], search: [], status: 'running',
};
const save = () => fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });

(async () => {
  let browser;
  let page;
  try {
    browser = await chromium.launch({ headless: true });
    report.browser = browser.version();
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(() => {
      const fetchImpl = window.fetch;
      window.__pdfReceipt = null;
      window.fetch = async (...args) => {
        const response = await fetchImpl.apply(window, args);
        const request = args[0];
        const url = new URL(request instanceof Request ? request.url : String(request), location.href);
        const method = args[1]?.method || (request instanceof Request ? request.method : 'GET');
        if (url.pathname === '/v1/knowledge/pdf-imports' && method.toUpperCase() === 'POST') {
          try { window.__pdfReceipt = { http: response.status, body: await response.clone().json() }; } catch {}
        }
        return response;
      };
    });
    await page.goto(`${base}/login?next=${encodeURIComponent('/knowledge-agent#library')}`);
    await page.locator('#username').fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator('#password').fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent*');
    report.login = true;

    const title = `Issue 53 PDF ${crypto.randomUUID()}`;
    await page.locator('[data-open-import]:visible').first().click();
    await page.locator('#import-file').setInputFiles(file);
    await page.locator('#import-title').fill(title);
    await page.locator('#import-submit').click();
    await page.waitForFunction(() => !document.querySelector('#import-dialog')?.open, { timeout: 60_000 });
    const receipt = await page.evaluate(() => window.__pdfReceipt);
    assert(receipt, 'import processing receipt must be observable');
    assert.equal(receipt.http, 202);
    report.receipt = receipt;
    const taskId = receipt.body.task_id;
    assert(taskId, 'receipt must identify task');

    const deadline = Date.now() + 300_000;
    let task;
    while (Date.now() < deadline) {
      const response = await page.evaluate(async (id) => {
        const r = await fetch(`/v1/knowledge/pdf-imports/${encodeURIComponent(id)}`);
        return { http: r.status, body: await r.json() };
      }, taskId);
      task = response.body;
      report.states.push({ at: new Date().toISOString(), ...response });
      save();
      assert.equal(response.http, 200);
      if (['failed', 'partial', 'unsupported', 'dead'].includes(task.state)) break;
      if (task.state === 'parsed') {
        const result = await page.evaluate(async (query) => {
          const r = await fetch(`/v1/knowledge/search?q=${encodeURIComponent(query)}&limit=20`);
          return { http: r.status, body: await r.json() };
        }, 'MannequinVideos');
        report.search.push({ at: new Date().toISOString(), ...result });
        const hit = result.body.items?.find((item) => item.source_sha256 === sourceSha);
        if (hit) {
          report.hit = hit;
          const detail = await page.evaluate(async (id) => {
            const r = await fetch(`/v1/knowledge/${encodeURIComponent(id)}`);
            return { http: r.status, body: await r.json() };
          }, hit.knowledge_object_id);
          report.detail = detail;
          assert.equal(detail.http, 200);
          assert.equal(detail.body.knowledge_object_id, hit.knowledge_object_id);
          assert.equal(detail.body.searchable, true);
          assert.equal(detail.body.media_type, 'application/pdf');
          assert.equal(detail.body.citations.length > 0, true);
          assert.equal(detail.body.citations.some((citation) => Number(citation.page_no) > 0), true);
          report.status = 'passed';
          break;
        }
      }
      await page.waitForTimeout(1000);
    }
    assert.equal(report.status, 'passed', 'PDF must become searchable with a source-linked citation');
    report.finished = new Date().toISOString();
  } catch (error) {
    report.status = 'failed';
    report.failure = String(error.stack || error);
    process.exitCode = 1;
  } finally {
    report.finished = report.finished || new Date().toISOString();
    if (page) {
      try { await page.screenshot({ path: path.join(output, 'final.png'), fullPage: true }); } catch {}
    }
    save();
    if (browser) await browser.close();
  }
})();
