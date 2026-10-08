/* Opt-in real API acceptance: imports personal files into the running local service.
 * ZHIHENG_TEST_USERNAME / ZHIHENG_TEST_PASSWORD must be supplied privately.
 * node tests/e2e/check_real_files.cjs URL OUTPUT_DIR FILE_A FILE_B
 * No mocked services, internal Worker calls or account reset.
 */
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const [base, output, ...files] = process.argv.slice(2);
if (!base || !output || files.length !== 2 || !['localhost', '127.0.0.1'].includes(new URL(base).hostname)) throw Error('Expected local URL, private output directory and two files');
if (!process.env.ZHIHENG_TEST_USERNAME || !process.env.ZHIHENG_TEST_PASSWORD) throw Error('Missing test credentials');
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const report = { started: new Date().toISOString(), imports: [], answers: [], errors: [] };
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror', error => report.errors.push(error.message));
  try {
    await page.goto(base + '/login?next=' + encodeURIComponent('/knowledge-agent#library'));
    await page.locator('#username').fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator('#password').fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent*');
    report.login = true;
    for (const file of files) {
      const pdf = file.toLowerCase().endsWith('.pdf');
      const item = {file:path.basename(file), states:[]}; report.imports.push(item);
      await page.locator('[data-open-import]:visible').first().click();
      await page.locator('#import-file').setInputFiles(file);
      const title = '真实验收-' + path.basename(file) + '-' + Date.now();
      await page.locator('#import-title').fill(title);
      const responsePromise = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === (pdf ? '/v1/knowledge/pdf-imports' : '/v1/knowledge/imports'), {timeout:60000});
      await page.locator('#import-submit').click();
      const response = await responsePromise;
      item.http_status = response.status();
      if (!response.ok()) continue;
      await page.waitForFunction(() => !document.querySelector('#import-dialog').open);
      if (!pdf) {
        for (let n=0;n<120;n++) {
          const state = await page.evaluate(async title => {const r=await fetch('/v1/knowledge/search?q='+encodeURIComponent(title)+'&limit=5'); const b=await r.json(); const x=b.items?.find(i=>i.title===title); if(!x?.knowledge_object_id)return {http:r.status,body:{public_status:'queued'}}; const p=await fetch('/v1/knowledge/'+x.knowledge_object_id+'/processing'); return {http:p.status,body:await p.json()};},title);
          item.states.push({at:new Date().toISOString(),...state});
          const s=state.body.public_status || state.body.state || state.body.status;
          if (['succeeded','parsed','dead','failed','unsupported','partial'].includes(s)) break;
          await page.waitForTimeout(1000);
        }
      } else {
        await page.waitForTimeout(5000);
        item.processing_text=await page.locator('#processing-section').innerText();
      }
      await page.screenshot({path:path.join(output,pdf?'pdf-import.png':'text-import.png'),fullPage:true});
    }
    await page.goto(base+'/knowledge-agent#research');
    for (const question of ['聊天案例开场白一中，对方问“引起我注意干嘛”之后，建议如何回答？请引用原文。','Comments 文档的主要审稿意见是什么？请引用原文。','这些材料是否给出了作者的银行账户号码？没有证据请明确说明。']) {
      await page.locator('#question').fill(question);
      const answerResponses=[];
      const onResponse=r=>{if(new URL(r.url()).pathname==='/v1/answers' && r.request().method()==='POST') answerResponses.push(r.status());};
      page.on('response',onResponse);
      await page.locator('#answer-form button[type=submit]').click();
      await page.locator('#answer-result').waitFor({state:'visible',timeout:240000});
      const answer={question,status:answerResponses.at(-1)||null,answer:await page.locator('#answer').innerText(),route:await page.locator('#route').innerText(),citations:await page.locator('#citations .source-card').count()};
      report.answers.push(answer);
      page.off('response',onResponse);
      await page.screenshot({path:path.join(output,`answer-${report.answers.length}.png`),fullPage:true});
      if(answer.citations) {
        await page.locator('#citations .source-card').first().click();
        await page.waitForTimeout(1000);
        report.answers.at(-1).source_text=await page.locator('#citation-context-text').innerText();
        await page.screenshot({path:path.join(output,`source-${report.answers.length}.png`),fullPage:true});
        await page.locator('#close-citation').click();
      }
    }
  } catch(error) {report.failure=error.stack; process.exitCode=1;}
  finally {report.finished=new Date().toISOString();fs.writeFileSync(path.join(output,'browser-report.json'),JSON.stringify(report,null,2),{mode:0o600}); await browser.close();}
})();
