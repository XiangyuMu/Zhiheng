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
if (!process.env.ZHIHENG_TEST_USERNAME || !process.env.ZHIHENG_TEST_PASSWORD) throw Error('Missing test credentials');
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const report = {
  started: new Date().toISOString(),
  sha: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
  dirty: Boolean(execFileSync('git', ['status', '--porcelain'], { encoding: 'utf8' }).trim()),
  imports: [], answers: [], errors: [],
};
const saveReport = () => fs.writeFileSync(
  path.join(output, 'browser-report.json'),
  JSON.stringify(report, null, 2),
  { mode: 0o600 },
);
(async () => {
  let browser;
  let page;
  try {
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
      await page.waitForFunction(() => !document.querySelector('#import-dialog').open);
      await page.locator('#processing-section').waitFor({state:'visible',timeout:30000});
      await page.waitForFunction(({title, pdf}) => {
        const text = document.querySelector('#processing-section')?.innerText || '';
        if (!text.includes(title)) return false;
        return pdf ? /(succeeded|parsed|unsupported|failed|partial|dead|不支持|失败)/i.test(text)
                   : /(已可检索|处理失败|unsupported|failed|部分)/i.test(text);
      }, {title, pdf}, {timeout: pdf ? 180000 : 120000});
      item.processing_text = await page.locator('#processing-section').innerText();
      item.searchable = !pdf && item.processing_text.includes('已可检索');
      item.terminal = /(已可检索|unsupported|failed|partial|dead|不支持|失败)/i.test(item.processing_text);
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
      const answerResponses=[];
      const onResponse=r=>{if(new URL(r.url()).pathname==='/v1/answers' && r.request().method()==='POST') answerResponses.push(r.status());};
      page.on('response',onResponse);
      await page.locator('#answer-form button[type=submit]').click();
      await page.locator('#answer-result').waitFor({state:'visible',timeout:240000});
      const answer={question: question.value, kind: question.kind, status:answerResponses.at(-1)||null,answer:await page.locator('#answer').innerText(),route:await page.locator('#route').innerText(),citations:await page.locator('#citations .source-card').count()};
      if (answer.status !== 200) throw Error(`answer request failed with HTTP ${answer.status}`);
      if (!answer.answer.trim()) throw Error(`empty answer for ${question.kind}`);
      if (question.kind !== 'negative' && answer.citations < 1) {
        throw Error(`answer for ${question.kind} has no supporting citation`);
      }
      if (question.kind === 'negative') {
        if (!/(没有|不足|无法|未提供|未找到|证据)/.test(answer.answer)) {
          throw Error('negative answer did not state evidence insufficiency');
        }
        if (/(\d{10,}|银行卡|银行账户).*(\d{6,})/.test(answer.answer)) {
          throw Error('negative answer appears to fabricate an account number');
        }
      }
      report.answers.push(answer);
      page.off('response',onResponse);
      await page.screenshot({path:path.join(output,`answer-${report.answers.length}.png`),fullPage:true});
      if(answer.citations) {
        await page.locator('#citations .source-card').first().click();
        await page.waitForTimeout(1000);
        report.answers.at(-1).source_text=await page.locator('#citation-context-text').innerText();
        if (!report.answers.at(-1).source_text.trim()) throw Error('citation source text is empty');
        await page.screenshot({path:path.join(output,`source-${report.answers.length}.png`),fullPage:true});
        await page.locator('#close-citation').click();
      }
    }
  } catch(error) {report.failure=error.stack; process.exitCode=1;}
  finally {
    report.finished=new Date().toISOString();
    saveReport();
    if (browser) await browser.close();
  }
})();
