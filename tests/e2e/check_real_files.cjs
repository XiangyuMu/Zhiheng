/* Opt-in real API acceptance: imports personal files into the running local service.
 * ZHIHENG_TEST_USERNAME / ZHIHENG_TEST_PASSWORD must be supplied privately.
 * node tests/e2e/check_real_files.cjs URL OUTPUT_DIR FILE_A FILE_B
 * No mocked services, internal Worker calls or account reset.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const [base, output, ...files] = process.argv.slice(2);
if (!base || !output || files.length !== 2 || !['localhost', '127.0.0.1'].includes(new URL(base).hostname)) throw Error('Expected local URL, private output directory and two files');
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const report = {
  started: new Date().toISOString(),
  sha: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
  dirty: Boolean(execFileSync('git', ['status', '--porcelain'], { encoding: 'utf8' }).trim()),
  imports: [], answers: [], errors: [],
  status: 'running',
};
let expectedPdf = null;
const saveReport = () => fs.writeFileSync(
  path.join(output, 'browser-report.json'),
  JSON.stringify(report, null, 2),
  { mode: 0o600 },
);
(async () => {
  let browser;
  let page;
  try {
    if (!process.env.ZHIHENG_TEST_USERNAME || !process.env.ZHIHENG_TEST_PASSWORD) throw Error('Missing test credentials');
    if (!process.env.ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID || !process.env.ZHIHENG_EXPECTED_ANSWER_MODEL_ID) throw Error('Missing expected answer provider/model identity');
    expectedPdf = process.env.ZHIHENG_PDF_EXPECTATION_FILE
      ? JSON.parse(fs.readFileSync(process.env.ZHIHENG_PDF_EXPECTATION_FILE, 'utf8'))
      : null;
    const declaredExpectations = expectedPdf?.answers || expectedPdf?.questions;
    if (!expectedPdf || !Array.isArray(declaredExpectations)) {
      throw Error('expectation file must declare keyed answers/questions for positive and negative checks');
    }
    const requiredKinds = new Set(['text', 'pdf', 'negative']);
    const declaredKinds = new Set(declaredExpectations.map(item => item?.kind));
    for (const kind of requiredKinds) {
      if (!declaredKinds.has(kind)) throw Error(`expectation file is missing ${kind} answer contract`);
    }
    if (declaredExpectations.filter(item => item?.kind === 'negative').some(item => item.unanswerable !== true)) {
      throw Error('negative expectation must explicitly declare unanswerable=true');
    }
    if (process.env.ZHIHENG_EXPECTED_SHA && report.sha !== process.env.ZHIHENG_EXPECTED_SHA) {
      throw Error('Acceptance SHA mismatch');
    }
    if (report.dirty) throw Error('Real-file acceptance requires a clean checkout');
    browser = await chromium.launch({headless:true});
    page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror', error => report.errors.push(error.message));
    await page.goto(base + '/login?next=' + encodeURIComponent('/knowledge-agent#library'));
    await page.locator('#username').fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator('#password').fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent*');
    report.login = true;
    for (const file of files) {
      const pdf = file.toLowerCase().endsWith('.pdf');
      const item = {
        file: path.basename(file),
        sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),
        states: [],
      };
      report.imports.push(item);
      await page.locator('[data-open-import]:visible').first().click();
      await page.locator('#import-file').setInputFiles(file);
      const title = '真实验收-' + path.basename(file) + '-' + Date.now();
      await page.locator('#import-title').fill(title);
      const responsePromise = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === (pdf ? '/v1/knowledge/pdf-imports' : '/v1/knowledge/imports'), {timeout:60000});
      await page.locator('#import-submit').click();
      const response = await responsePromise;
      item.http_status = response.status();
      if (!response.ok()) throw new Error(`import ${path.basename(file)} failed with HTTP ${response.status()}`);
      const receiptBody = await response.json();
      item.task_id = receiptBody.task_id || receiptBody.knowledge_object_id || null;
      const statusPath = pdf
        ? `/v1/knowledge/pdf-imports/${encodeURIComponent(receiptBody.task_id)}`
        : `/v1/knowledge/${encodeURIComponent(receiptBody.result?.knowledge_object_id)}/processing`;
      let finalState;
      await page.waitForFunction(async ({statusPath, pdf}) => {
        const result = await fetch(statusPath);
        if (!result.ok) return false;
        const state = await result.json();
        const terminal = pdf
          ? ['parsed', 'succeeded', 'failed', 'partial', 'unsupported', 'dead'].includes(state.state)
          : ['succeeded', 'failed', 'unsupported', 'partial', 'dead_letter'].includes(state.public_status);
        return terminal;
      }, {statusPath, pdf}, {timeout: pdf ? 300000 : 180000});
      finalState = await page.evaluate(async path => (await fetch(path)).json(), statusPath);
      item.final_state = finalState.state || finalState.public_status;
      item.processing_text = JSON.stringify(finalState);
      item.searchable = pdf ? Boolean(finalState.index_searchable) : finalState.searchable === true;
      item.terminal = true;
      if (!item.terminal) throw Error(`${path.basename(file)} did not reach a terminal UI state`);
      if (pdf && !/(parsed|succeeded|已可检索)/i.test(item.processing_text)) {
        throw Error(`${path.basename(file)} did not complete successfully`);
      }
      await page.screenshot({path:path.join(output,pdf?'pdf-import.png':'text-import.png'),fullPage:true});
    }
    await page.goto(base+'/knowledge-agent#research');
    const questions = [
      { kind: 'text', value: '聊天案例开场白一中，对方问“引起我注意干嘛”之后，建议如何回答？请引用原文。' },
      { kind: 'pdf', value: 'Comments 文档的主要审稿意见是什么？请引用原文。' },
      { kind: 'negative', value: '这些材料是否给出了作者的银行账户号码？没有证据请明确说明。' },
    ];
    for (const question of questions) {
      await page.locator('#question').fill(question.value);
      const previousAnswer = await page.locator('#answer').innerText().catch(() => '');
      const answerResponse = page.waitForResponse(
        r => new URL(r.url()).pathname === '/v1/answers' && r.request().method() === 'POST',
        { timeout: 240000 },
      );
      await page.locator('#answer-form button[type=submit]').click();
      const answerHttp = await answerResponse;
      const requestPayload = answerHttp.request().postDataJSON();
      if (!requestPayload || requestPayload.query !== question.value) {
        throw Error(`answer response was not bound to the current question: ${question.kind}`);
      }
      const answerBody = await answerHttp.json();
      await page.waitForFunction(
        previous => {
          const value = document.querySelector('#answer')?.textContent?.trim() || '';
          return value.length > 0 && value !== previous;
        },
        previousAnswer,
        { timeout: 240000 },
      );
      const answer={question: question.value, kind: question.kind, status:answerHttp.status(),answer:await page.locator('#answer').innerText(),route:await page.locator('#route').innerText(),citations:await page.locator('#citations .source-card').count(), citation_payload_count: Array.isArray(answerBody.citations) ? answerBody.citations.length : 0};
      answer.answer_provider_id = answerBody.answer_provider_id || null;
      answer.answer_model_id = answerBody.answer_model_id || null;
      answer.answer_audit_id = answerBody.answer_audit_id || null;
      report.answers.push(answer);
      saveReport();
      if (answer.status !== 200) throw Error(`answer request failed with HTTP ${answer.status}`);
      if (!answer.answer.trim()) throw Error(`empty answer for ${question.kind}`);
      if (question.kind !== 'negative' && answer.citations < 1) {
        throw Error(`answer for ${question.kind} has no supporting citation`);
      }
      const expectedAnswer = expectedPdf?.answers?.find(item => item.kind === question.kind)
        || expectedPdf?.questions?.find(item => item.kind === question.kind);
      if (expectedAnswer) {
        const facts = expectedAnswer.contains || expectedAnswer.answer_contains || expectedAnswer.facts || [];
        for (const fact of (Array.isArray(facts) ? facts : [facts])) {
          if (fact && !answer.answer.includes(String(fact))) {
            throw Error(`answer for ${question.kind} is missing expected fact`);
          }
        }
        if (expectedAnswer.unanswerable === true && answer.citations !== 0) {
          throw Error(`unanswerable ${question.kind} question returned citations`);
        }
      }
      if (process.env.ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID && answer.answer_provider_id !== process.env.ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID) {
        throw Error(`answer used provider ${answer.answer_provider_id || 'unset'}, expected ${process.env.ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID}`);
      }
      if (process.env.ZHIHENG_EXPECTED_ANSWER_MODEL_ID && answer.answer_model_id !== process.env.ZHIHENG_EXPECTED_ANSWER_MODEL_ID) {
        throw Error(`answer used model ${answer.answer_model_id || 'unset'}, expected ${process.env.ZHIHENG_EXPECTED_ANSWER_MODEL_ID}`);
      }
      if (process.env.ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID && !answer.answer_audit_id) {
        throw Error('answer did not expose a successful model-call audit id');
      }
      if (question.kind === 'negative') {
        if (!/(没有|不足|无法|未提供|未找到|证据)/.test(answer.answer)) {
          throw Error('negative answer did not state evidence insufficiency');
        }
        if (/(\d{10,}|银行卡|银行账户).*(\d{6,})/.test(answer.answer)) {
          throw Error('negative answer appears to fabricate an account number');
        }
      }
      await page.screenshot({path:path.join(output,`answer-${report.answers.length}.png`),fullPage:true});
      if(answer.citations) {
        await page.locator('#citations .source-card').first().click();
        const context = page.locator('#citation-context-text');
        await page.waitForFunction(() => {
          const value = document.querySelector('#citation-context-text')?.textContent?.trim() || '';
          const title = document.querySelector('#citation-title')?.textContent?.trim() || '';
          return value && !/^(正在|暂无可显示|暂时无法)/.test(value) && title && !/^(正在|暂时无法)/.test(title);
        }, { timeout: 20000 });
        report.answers.at(-1).source_text=await context.innerText();
        report.answers.at(-1).source_title=await page.locator('#citation-title').innerText();
        if (!report.answers.at(-1).source_text.trim()) throw Error('citation source text is empty');
        if (question.kind === 'pdf') {
          const link = page.locator('#citation-page-link');
          await link.waitFor({state:'visible', timeout:10000});
          const href = await link.getAttribute('href');
          if (!href) throw Error('PDF citation did not expose an original source link');
          const original = await page.request.get(new URL(href, base).href);
          if (original.status() !== 200) throw Error(`PDF citation source returned HTTP ${original.status()}`);
          const returnedSha = crypto.createHash('sha256').update(await original.body()).digest('hex');
          if (returnedSha !== itemForPdfSha(report.imports, files)) throw Error('PDF citation source digest does not match imported PDF');
          if (expectedPdf) {
            const expectedSha = expectedPdf.sha256 || expectedPdf.source_sha256 || expectedPdf.pdf_sha256;
            const expectedPage = Number(expectedPdf.page_no ?? expectedPdf.page ?? expectedPdf.expected_page_no);
            const expectedQuote = String(expectedPdf.quote || expectedPdf.expected_quote || '').replace(/\s+/g, ' ').trim();
            if (expectedSha !== returnedSha) throw Error('PDF expectation is bound to a different source digest');
            if (!Number.isInteger(expectedPage) || !href.endsWith(`#page=${expectedPage}`)) throw Error('PDF citation page does not match the bound expectation');
            if (!expectedQuote || !report.answers.at(-1).source_text.replace(/\s+/g, ' ').includes(expectedQuote)) throw Error('PDF citation quote does not match the bound expectation');
          }
          report.answers.at(-1).source_pdf_sha256 = returnedSha;
          report.answers.at(-1).source_page_link = href;
        }
        await page.screenshot({path:path.join(output,`source-${report.answers.length}.png`),fullPage:true});
        await page.locator('#close-citation').click();
      }
    }
    report.status = 'passed';
  } catch(error) {
    report.status = 'failed'; report.failure=error.stack;
    if (page) await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(() => {});
    process.exitCode=1;
  }
  finally {
    report.finished=new Date().toISOString();
    report.exit_code = process.exitCode || 0;
    saveReport();
    if (browser) await browser.close();
  }
})();

function itemForPdfSha(imports, inputFiles) {
  const pdf = inputFiles.find(file => file.toLowerCase().endsWith('.pdf'));
  const item = imports.find(entry => entry.file === path.basename(pdf));
  if (!item) throw Error('PDF import record is missing');
  return item.sha256;
}
