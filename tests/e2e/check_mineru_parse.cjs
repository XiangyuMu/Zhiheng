/* Real-browser parser acceptance. No routes or model responses are mocked.
 * ZHIHENG_TEST_USERNAME / ZHIHENG_TEST_PASSWORD supplied through private environment.
 * node tests/e2e/check_mineru_parse.cjs LOCAL_URL NEW_OUTPUT_DIR PDF
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const [base, output, file] = process.argv.slice(2);
assert(base && output && file, 'Expected URL, new evidence directory, PDF');
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname), 'Local service only');
assert(process.env.ZHIHENG_TEST_USERNAME && process.env.ZHIHENG_TEST_PASSWORD, 'Missing credentials');
fs.mkdirSync(output, { mode: 0o700 });
const report = {
  started: new Date().toISOString(), scope: 'issue52-real-parser',
  sha: execFileSync('git', ['rev-parse', 'HEAD'], {encoding:'utf8'}).trim(),
  dirty: Boolean(execFileSync('git', ['status', '--porcelain'], {encoding:'utf8'}).trim()),
  input: {name:path.basename(file), sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')},
  states: [], errors: [], status:'running',
};
const save = () => fs.writeFileSync(path.join(output,'report.json'), JSON.stringify(report,null,2), {mode:0o600});
(async () => {
  let browser;
  let page;
  try {
    browser = await chromium.launch({headless:true});
    report.browser = browser.version();
    page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror', e => report.errors.push(e.message));
    // Observe only the genuine import response; never rewrite requests or responses.
    // Reading a clone here avoids Chromium evicting small no-store response bodies.
    await page.addInitScript(() => {
      const original = window.fetch;
      window.__pdfImportReceipt = null;
      window.fetch = async function(...args) {
        const response = await original.apply(this,args);
        const input = args[0];
        const url = new URL(input instanceof Request ? input.url : String(input), location.href);
        const method = args[1]?.method || (input instanceof Request ? input.method : 'GET');
        if (url.pathname === '/v1/knowledge/pdf-imports' && method.toUpperCase() === 'POST') {
          try { window.__pdfImportReceipt = {http:response.status, body:await response.clone().json()}; }
          catch { window.__pdfImportReceipt = {http:response.status, error:'invalid import JSON'}; }
        }
        return response;
      };
    });
    await page.goto(base+'/login?next='+encodeURIComponent('/knowledge-agent#library'));
    await page.locator('#username').fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator('#password').fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent*');
    report.login = true;
    const title = `MinerU acceptance ${crypto.randomUUID()}`;
    report.title=title;
    await page.locator('[data-open-import]:visible').first().click();
    await page.locator('#import-file').setInputFiles(file);
    await page.locator('#import-title').fill(title);
    const started = Date.now();
    await page.locator('#import-submit').click();
    await page.waitForFunction(() => window.__pdfImportReceipt !== null, {timeout:60000});
    report.receipt=await page.evaluate(() => window.__pdfImportReceipt);
    save();
    assert.equal(report.receipt.http,202, 'PDF upload must be accepted');
    const id=report.receipt.body.task_id;
    assert(id, 'Receipt must identify this task');
    assert.equal(report.receipt.body.source_sha256,report.input.sha256, 'Receipt source digest');
    let terminal;
    while (Date.now()-started < 300000) {
      const status=await page.evaluate(async id => {
        const r=await fetch('/v1/knowledge/pdf-imports/'+encodeURIComponent(id));
        return {http:r.status,body:await r.json()};
      },id);
      report.states.push({at:new Date().toISOString(),...status}); save();
      assert.equal(status.http,200,'Current task must remain observable');
      assert.equal(status.body.task_id,id);
      if (['parsed','partial','failed','dead','unsupported'].includes(status.body.state)) {terminal=status.body;break;}
      await page.waitForTimeout(1000);
    }
    report.seconds=(Date.now()-started)/1000;
    assert(terminal,'PDF parse did not finish within the 300-second budget');
    assert.equal(terminal.state,'parsed','Only a complete real parse passes');
    assert.equal(terminal.backend,'mineru');
    assert.equal(terminal.source_sha256,report.input.sha256);
    assert(terminal.attempt_id && terminal.evidence_object_id);
    assert(terminal.page_count > 0);
    assert.equal(terminal.parsed_page_count,terminal.page_count);
    assert(terminal.block_count>0);
    report.parsed=terminal;
    report.status='passed';
  } catch(error) {report.status='failed';report.failure=String(error.stack || error);process.exitCode=1;}
  finally {
    report.finished=new Date().toISOString();
    if(page && report.login) {
      try { await page.screenshot({path:path.join(output,'final.png'),fullPage:true}); }
      catch(e) {report.screenshot_error=e.message;}
    }
    save();
    if(browser) await browser.close();
  }
})();
