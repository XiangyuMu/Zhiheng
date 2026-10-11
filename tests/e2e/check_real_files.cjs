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
    for (const expectation of declaredExpectations) {
      if (expectation?.kind === 'negative') continue;
      const facts = expectation?.contains || expectation?.answer_contains || expectation?.facts || [];
      const declaredFacts = Array.isArray(facts) ? facts : [facts];
      if (!declaredFacts.some((fact) => String(fact || '').trim())) {
        throw Error(`positive expectation ${expectation?.kind || 'unknown'} must declare supporting facts`);
      }
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
    await page.addInitScript(() => {
      const originalFetch = window.fetch;
      window.__zhihengObserved = { imports: [], answers: [] };
      const parseJson = (value) => {
        if (typeof value !== "string") return null;
        try { return JSON.parse(value); } catch { return null; }
      };
      const requestBody = async (input, init) => {
        if (init && typeof init.body === "string") return parseJson(init.body);
        if (input instanceof Request) {
          try { return await input.clone().json(); } catch { return null; }
        }
        return null;
      };
      window.fetch = async (...args) => {
        const input = args[0];
        const init = args[1] || {};
        const url = new URL(input instanceof Request ? input.url : String(input), location.href);
        const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
        const answerRequest = url.pathname === "/v1/answers" && method === "POST"
          ? await requestBody(input, init)
          : null;
        const response = await originalFetch.apply(window, args);
        if (
          (url.pathname === "/v1/knowledge/imports" || url.pathname === "/v1/knowledge/pdf-imports")
          && method === "POST"
        ) {
          try {
            window.__zhihengObserved.imports.push({
              path: url.pathname,
              http: response.status,
              body: await response.clone().json(),
            });
          } catch {
            window.__zhihengObserved.imports.push({ path: url.pathname, http: response.status, error: "invalid import JSON" });
          }
        }
        if (url.pathname === "/v1/answers" && method === "POST") {
          try {
            window.__zhihengObserved.answers.push({
              path: url.pathname,
              http: response.status,
              request: answerRequest,
              body: await response.clone().json(),
            });
          } catch {
            window.__zhihengObserved.answers.push({ path: url.pathname, http: response.status, request: answerRequest, error: "invalid answer JSON" });
          }
        }
        return response;
      };
    });
    await page.goto(base + '/login?next=' + encodeURIComponent('/knowledge-agent#library'));
    await page.locator('#username').fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator('#password').fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent*');
    report.login = true;
    const imported = {};
    for (const file of files) {
      const pdf = file.toLowerCase().endsWith('.pdf');
      const kind = pdf ? 'pdf' : 'text';
      const item = {
        file: path.basename(file),
        sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),
        states: [],
      };
      report.imports.push(item);
      await page.locator('[data-open-import]:visible').first().click();
      await page.locator('#import-file').setInputFiles(file);
      const title = '真实验收-' + path.basename(file) + '-' + crypto.randomUUID().replaceAll('-', '').slice(0, 12);
      item.title = title;
      await page.locator('#import-title').fill(title);
      const importPath = pdf ? '/v1/knowledge/pdf-imports' : '/v1/knowledge/imports';
      const observedIndex = await observedCount(page, 'imports');
      await page.locator('#import-submit').click();
      const observedImport = await nextObserved(page, 'imports', observedIndex, { path: importPath }, 60000);
      await page.waitForFunction(() => !document.querySelector('#import-dialog')?.open, { timeout: 60000 });
      item.http_status = observedImport.http;
      item.import_receipt = observedImport.body || null;
      if (observedImport.http < 200 || observedImport.http >= 300) {
        throw new Error(`import ${path.basename(file)} failed with HTTP ${observedImport.http}`);
      }
      const receiptBody = observedImport.body;
      item.task_id = receiptBody.task_id || receiptBody.knowledge_object_id || receiptBody.result?.knowledge_object_id || null;
      const statusPath = pdf
        ? `/v1/knowledge/pdf-imports/${encodeURIComponent(receiptBody.task_id)}`
        : `/v1/knowledge/${encodeURIComponent(receiptBody.result?.knowledge_object_id)}/processing`;
      if (!item.task_id) throw Error(`${path.basename(file)} receipt did not identify the current import`);
      const finalSnapshot = await pollTerminalStatus(page, statusPath, { pdf, timeoutMs: pdf ? 300000 : 180000 });
      const finalState = finalSnapshot.body;
      item.states = finalSnapshot.states;
      item.final_state = finalState.state || finalState.public_status;
      item.processing_text = JSON.stringify(finalState);
      item.searchable = pdf ? Boolean(finalState.index_searchable) : finalState.searchable === true;
      item.terminal = true;
      if (pdf) {
        if (finalState.task_id !== receiptBody.task_id) throw Error('PDF terminal snapshot is not bound to the current task');
        if (finalState.evidence_object_id !== receiptBody.evidence_object_id) throw Error('PDF terminal snapshot is not bound to the current evidence object');
        if (finalState.source_sha256 !== item.sha256) throw Error('PDF terminal snapshot source digest mismatch');
        if (finalState.state !== 'parsed') {
          throw Error(`${path.basename(file)} did not complete parsing`);
        }
      } else {
        if (finalState.public_status !== 'succeeded' || finalState.searchable !== true) {
          throw Error(`${path.basename(file)} did not become succeeded and searchable`);
        }
      }
      item.knowledge = await findCurrentKnowledge(page, {
        title,
        query: pdf ? (expectedPdf?.quote || 'MannequinVideos') : '引起我注意',
        expected: {
          knowledge_object_id: receiptBody.result?.knowledge_object_id || null,
          evidence_object_id: receiptBody.evidence_object_id || null,
          source_sha256: pdf ? item.sha256 : null,
          media_type: pdf ? 'application/pdf' : 'text/plain',
        },
      });
      imported[kind] = item;
      await page.screenshot({path:path.join(output,pdf?'pdf-import.png':'text-import.png'),fullPage:true});
    }
    await page.goto(base+'/knowledge-agent#research');
    const questions = [
      { kind: 'text', value: `资料《${imported.text.title}》：引起我注意干嘛？请原样保留这句话并引用原文回答。` },
      { kind: 'pdf', value: `资料《${imported.pdf.title}》：Comments 文档的主要审稿意见是什么？请引用 MannequinVideos 原文。` },
      { kind: 'negative', value: `只根据刚导入的资料《${imported.text.title}》和《${imported.pdf.title}》回答：这些材料是否给出了作者的银行账户号码？没有证据请明确说明。` },
    ];
    for (const question of questions) {
      await page.locator('#question').fill(question.value);
      const previousAnswer = await page.locator('#answer').innerText().catch(() => '');
      const answerIndex = await observedCount(page, 'answers');
      await page.locator('#answer-form button[type=submit]').click();
      const observedAnswer = await nextObserved(page, 'answers', answerIndex, {}, 240000);
      const requestPayload = observedAnswer.request;
      if (!requestPayload || requestPayload.query !== question.value) {
        throw Error(`answer response was not bound to the current question: ${question.kind}`);
      }
      const answerBody = observedAnswer.body;
      await page.waitForFunction(
        previous => {
          const value = document.querySelector('#answer')?.textContent?.trim() || '';
          return value.length > 0 && value !== previous;
        },
        previousAnswer,
        { timeout: 240000 },
      );
      const targetImport = imported[question.kind] || null;
      const expectedAnswer = expectedPdf?.answers?.find(item => item.kind === question.kind)
        || expectedPdf?.questions?.find(item => item.kind === question.kind);
      const expectedFacts = expectedAnswer?.contains || expectedAnswer?.answer_contains || expectedAnswer?.facts || [];
      const targetCitation = targetImport
        ? await findCitationForImportWithFacts(
          page,
          answerBody.citations || [],
          targetImport.knowledge,
          Array.isArray(expectedFacts) ? expectedFacts : [expectedFacts],
        )
        : null;
      const answer={
        question: question.value,
        kind: question.kind,
        status:observedAnswer.http,
        request_query: requestPayload.query,
        answer:await page.locator('#answer').innerText(),
        route:await page.locator('#route').innerText(),
        stop_reason: answerBody.stop_reason || null,
        claims_count: Array.isArray(answerBody.claims) ? answerBody.claims.length : 0,
        insufficiency_count: Array.isArray(answerBody.insufficiencies) ? answerBody.insufficiencies.length : 0,
        citations:await page.locator('#citations .source-card').count(),
        citation_payload_count: Array.isArray(answerBody.citations) ? answerBody.citations.length : 0,
      };
      answer.answer_provider_id = answerBody.answer_provider_id || null;
      answer.answer_model_id = answerBody.answer_model_id || null;
      answer.answer_audit_id = answerBody.answer_audit_id || null;
      answer.bound_source = targetImport ? {
        knowledge_object_id: targetImport.knowledge.knowledge_object_id,
        knowledge_version_id: targetImport.knowledge.knowledge_version_id,
        evidence_object_id: targetImport.knowledge.evidence_object_id,
        source_sha256: targetImport.knowledge.source_sha256,
      } : null;
      answer.bound_citation = targetCitation ? {
        citation_id: targetCitation.citation_id,
        source_id: targetCitation.source_id,
        source_version_id: targetCitation.source_version_id,
        evidence_object_id: targetCitation.evidence_object_id,
        content_version_id: targetCitation.content_version_id,
        content_span_id: targetCitation.content_span_id,
        page_no: targetCitation.page_no,
      } : null;
      report.answers.push(answer);
      saveReport();
      if (answer.status !== 200) throw Error(`answer request failed with HTTP ${answer.status}`);
      if (!answer.answer.trim()) throw Error(`empty answer for ${question.kind}`);
      if (answer.stop_reason !== 'completed') {
        throw Error(`answer for ${question.kind} did not complete: ${answer.stop_reason || 'unset'}`);
      }
      if (question.kind !== 'negative') {
        if (!targetCitation) {
          throw Error(`answer for ${question.kind} did not cite the current imported object/version`);
        }
        if (answer.citations < 1 || answer.citation_payload_count < 1) {
          throw Error(`answer for ${question.kind} has no supporting citation`);
        }
        if (answer.claims_count < 1) {
          throw Error(`answer for ${question.kind} has no cited claim in the answer payload`);
        }
        assertClaimsBoundToCitations(answerBody.claims || [], answerBody.citations || [], targetCitation, question.kind);
      }
      if (expectedAnswer) {
        const facts = expectedFacts;
        for (const fact of (Array.isArray(facts) ? facts : [facts])) {
          if (fact && !answer.answer.includes(String(fact))) {
            throw Error(`answer for ${question.kind} is missing expected fact`);
          }
        }
        if (expectedAnswer.unanswerable === true) {
          if (answer.citations !== 0 || answer.citation_payload_count !== 0) {
            throw Error(`unanswerable ${question.kind} question returned citations`);
          }
          if (answer.claims_count !== 0) {
            throw Error(`unanswerable ${question.kind} question returned cited claims`);
          }
          if (answer.insufficiency_count < 1) {
            throw Error(`unanswerable ${question.kind} question did not expose insufficiency metadata`);
          }
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
        if (answer.claims_count !== 0 || answer.citation_payload_count !== 0) {
          throw Error('negative answer returned cited claims or citation payloads');
        }
        if (answer.insufficiency_count < 1) {
          throw Error('negative answer did not expose insufficiency metadata');
        }
        if (!/(没有|不足|无法|未提供|未找到|证据)/.test(answer.answer)) {
          throw Error('negative answer did not state evidence insufficiency');
        }
        if (/(\d{10,}|银行卡|银行账户).*(\d{6,})/.test(answer.answer)) {
          throw Error('negative answer appears to fabricate an account number');
        }
      }
      await page.screenshot({path:path.join(output,`answer-${report.answers.length}.png`),fullPage:true});
      if(answer.citations && question.kind !== 'negative') {
        const sourceContext = await citationContext(page, targetCitation);
        report.answers.at(-1).source_text = sourceContext.quote || sourceContext.context || '';
        report.answers.at(-1).source_title = sourceContext.title || '';
        report.answers.at(-1).source_page_no = sourceContext.page_no || targetCitation.page_no || null;
        if (!report.answers.at(-1).source_text.trim()) throw Error('citation source text is empty');
        for (const fact of (Array.isArray(expectedFacts) ? expectedFacts : [expectedFacts])) {
          if (fact && !report.answers.at(-1).source_text.includes(String(fact))) {
            throw Error(`citation source text for ${question.kind} is missing expected supporting fact`);
          }
        }
        const sourceCard = page.locator('#citations .source-card').filter({ hasText: targetImport.title }).first();
        await sourceCard.waitFor({ state: 'visible', timeout: 20000 });
        await sourceCard.click();
        const context = page.locator('#citation-context-text');
        await page.waitForFunction(() => {
          const value = document.querySelector('#citation-context-text')?.textContent?.trim() || '';
          const title = document.querySelector('#citation-title')?.textContent?.trim() || '';
          return value && !/^(正在|暂无可显示|暂时无法)/.test(value) && title && !/^(正在|暂时无法)/.test(title);
        }, { timeout: 20000 });
        const uiSourceText = await context.innerText();
        const uiSourceTitle = await page.locator('#citation-title').innerText();
        if (uiSourceTitle !== targetImport.title) throw Error(`UI citation title is not the current ${question.kind} import`);
        if (!uiSourceText.trim()) throw Error('citation source text is empty');
        if (question.kind === 'pdf') {
          const link = page.locator('#citation-page-link');
          await link.waitFor({state:'visible', timeout:10000});
          const href = await link.getAttribute('href');
          if (!href) throw Error('PDF citation did not expose an original source link');
          const original = await page.request.get(new URL(href, base).href);
          if (original.status() !== 200) throw Error(`PDF citation source returned HTTP ${original.status()}`);
          const returnedSha = crypto.createHash('sha256').update(await original.body()).digest('hex');
          if (returnedSha !== imported.pdf.sha256) throw Error('PDF citation source digest does not match imported PDF');
          if (expectedPdf) {
            const expectedSha = expectedPdf.sha256 || expectedPdf.source_sha256 || expectedPdf.pdf_sha256;
            const expectedPage = Number(expectedPdf.page_no ?? expectedPdf.page ?? expectedPdf.expected_page_no);
            const expectedQuote = String(expectedPdf.quote || expectedPdf.expected_quote || '').replace(/\s+/g, ' ').trim();
            if (expectedSha !== returnedSha) throw Error('PDF expectation is bound to a different source digest');
            if (!Number.isInteger(expectedPage) || targetCitation.page_no !== expectedPage || !href.endsWith(`#page=${expectedPage}`)) throw Error('PDF citation page does not match the bound expectation');
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

async function observedCount(page, bucket) {
  return page.evaluate(name => window.__zhihengObserved?.[name]?.length || 0, bucket);
}

async function nextObserved(page, bucket, startIndex, match, timeoutMs) {
  const handle = await page.waitForFunction(
    ({ bucket, startIndex, match }) => {
      const entries = window.__zhihengObserved?.[bucket] || [];
      return entries.slice(startIndex).find(entry => {
        if (match.path && entry.path !== match.path) return false;
        return true;
      }) || null;
    },
    { bucket, startIndex, match },
    { timeout: timeoutMs },
  );
  const value = await handle.jsonValue();
  if (!value) throw Error(`no observed ${bucket} response`);
  return value;
}

async function pollTerminalStatus(page, statusPath, { pdf, timeoutMs }) {
  const states = [];
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const snapshot = await page.evaluate(async target => {
      const response = await fetch(target, { signal: AbortSignal.timeout(10000) });
      let body = null;
      try { body = await response.json(); } catch { body = null; }
      return { http: response.status, body };
    }, statusPath);
    states.push({ at: new Date().toISOString(), ...snapshot });
    if (snapshot.http !== 200) throw Error(`status ${statusPath} returned HTTP ${snapshot.http}`);
    const state = snapshot.body || {};
    const terminal = pdf
      ? ['parsed', 'succeeded', 'failed', 'partial', 'unsupported', 'dead'].includes(state.state)
      : ['succeeded', 'failed', 'unsupported', 'partial', 'dead_letter'].includes(state.public_status);
    if (terminal) return { ...snapshot, states };
    await page.waitForTimeout(1000);
  }
  throw Error(`${statusPath} did not reach terminal state within ${timeoutMs}ms`);
}

async function findCurrentKnowledge(page, { title, query, expected }) {
  const result = await page.evaluate(async ({ query }) => {
    const response = await fetch(`/v1/knowledge/search?limit=100&sort=updated_desc`, { signal: AbortSignal.timeout(10000) });
    return { http: response.status, body: await response.json() };
  }, { query });
  if (result.http !== 200) throw Error(`knowledge search failed with HTTP ${result.http}`);
  const item = (result.body.items || []).find(candidate => {
    if (candidate.title !== title) return false;
    if (expected.knowledge_object_id && candidate.knowledge_object_id !== expected.knowledge_object_id) return false;
    if (expected.evidence_object_id && candidate.evidence_object_id !== expected.evidence_object_id) return false;
    return candidate.source_sha256 === expected.source_sha256 && candidate.media_type === expected.media_type;
  });
  let current = item;
  if (!current && expected.knowledge_object_id) {
    const detail = await page.evaluate(async id => {
      const response = await fetch(`/v1/knowledge/${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(10000) });
      return { http: response.status, body: await response.json() };
    }, expected.knowledge_object_id);
    if (detail.http === 200) current = detail.body;
  }
  if (!current) throw Error(`current import ${title} is not searchable as the current object`);
  if (current.searchable !== true) throw Error(`current import ${title} is not marked searchable`);
  if (current.title !== title
      || (expected.knowledge_object_id && current.knowledge_object_id !== expected.knowledge_object_id)
      || current.media_type !== expected.media_type) {
    throw Error(`current import ${title} identity does not match the current receipt`);
  }
  if (!current.knowledge_object_id || !current.knowledge_version_id || !current.evidence_object_id) {
    throw Error(`current import ${title} lacks object/version/evidence identifiers`);
  }
  return {
    ...current,
    knowledge_version_id: current.knowledge_version_id || current.current_version_id,
    evidence_object_id: current.evidence_object_id || expected.evidence_object_id,
    source_sha256: current.source_sha256 || expected.source_sha256,
  };
}

async function findCitationForImportWithFacts(page, citations, knowledge, facts) {
  const candidates = citations.filter(citation => (
    citation.source_id === knowledge.knowledge_object_id
    && citation.source_version_id === knowledge.knowledge_version_id
    && citation.evidence_object_id === knowledge.evidence_object_id
  ));
  if (!candidates.length) return null;
  const required = facts.map(fact => String(fact || '').trim()).filter(Boolean);
  if (!required.length) return candidates[0];
  for (const candidate of candidates) {
    const context = await citationContext(page, candidate);
    const source = String(context.quote || context.context || '').replace(/\s+/g, ' ');
    if (required.every(fact => source.includes(fact))) return candidate;
  }
  return candidates[0];
}

function findCitationForImport(citations, knowledge) {
  return citations.find(citation => (
    citation.source_id === knowledge.knowledge_object_id
    && citation.source_version_id === knowledge.knowledge_version_id
    && citation.evidence_object_id === knowledge.evidence_object_id
  )) || null;
}

function assertClaimsBoundToCitations(claims, citations, targetCitation, kind) {
  const known = new Set(citations.map(citation => citation.citation_id));
  let targetClaim = false;
  for (const claim of claims) {
    for (const id of claim.citation_ids || []) {
      if (!known.has(id)) throw Error(`answer for ${kind} includes claim citation outside payload citations`);
      if (id === targetCitation.citation_id) targetClaim = true;
    }
  }
  if (!targetClaim) throw Error(`answer for ${kind} has no claim bound to the current imported citation`);
}

async function citationContext(page, citation) {
  const response = await page.evaluate(async payload => {
    const r = await fetch('/v1/citations/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10000),
    });
    return { http: r.status, body: await r.json() };
  }, citation);
  if (response.http !== 200) throw Error(`citation context returned HTTP ${response.http}`);
  return response.body;
}
