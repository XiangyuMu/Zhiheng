const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('src/zhiheng/api/static/knowledge-agent.js', 'utf8');
const polling = source.slice(source.indexOf('async function pollProcessing(id)'), source.indexOf('function renderProcessing()'));
const pdfPolling = source.slice(source.indexOf('async function pollPdfTask(id)'), source.indexOf('async function pollProcessing(id)'));
async function run(responses, pdf = false) {
  const entry = {};
  let calls = 0, reloads = 0;
  const context = { state: { processing: new Map([['fixture', entry]]) }, processingNames: {},
    fetchJson: async () => { const value = responses[Math.min(calls++, responses.length - 1)]; if (value instanceof Error) throw value; return value; },
    loadKnowledge: async () => { reloads++; }, renderProcessing: () => {},
    setTimeout: (callback) => callback(),
  };
  vm.createContext(context);
  await vm.runInContext(pdf ? pdfPolling + '\npollPdfTask("fixture")' : polling + '\npollProcessing("fixture")', context);
  return { entry, calls, reloads, retained: context.state.processing.has('fixture') };
}
test('404 stops after one request without granting readiness', async () => {
  const result = await run([Object.assign(new Error(), { status: 404 })]);
  assert.equal(result.calls, 1); assert.equal(result.reloads, 0);
  assert.equal(result.entry.label, '当前服务版本不支持该任务状态');
});
test('server failures retry only three times', async () => {
  const result = await run([Object.assign(new Error(), { status: 503 })]);
  assert.equal(result.calls, 3); assert.equal(result.reloads, 0);
  assert.equal(result.entry.label, '暂时无法确认状态');
});
for (const status of ['unsupported', 'failed', 'partial']) test(status + ' remains visible and is not searchable', async () => {
  const result = await run([{ public_status: status, searchable: true, error_code: 'fixture_failure' }]);
  assert.equal(result.reloads, 0); assert.equal(result.calls, 1); assert.equal(result.retained, true);
  assert.equal(result.entry.terminal, true); assert.match(result.entry.label, /fixture_failure/);
});
test('only succeeded with searchable removes task and refreshes library', async () => {
  const result = await run([{ public_status: 'succeeded', searchable: true }]);
  assert.equal(result.reloads, 1); assert.equal(result.retained, false);
});

test('a deleted item leaves the panel without refreshing the library', async () => {
  const result = await run([{ public_status: 'deleted', searchable: false }]);
  assert.equal(result.calls, 1); assert.equal(result.reloads, 0); assert.equal(result.retained, false);
});
for (const state of ['unsupported', 'failed', 'partial']) test('PDF ' + state + ' stops polling without refreshing searchable library', async () => {
  const result = await run([{ state, searchable: true, error_code: 'fixture_pdf_failure' }], true);
  assert.equal(result.calls, 1);
  assert.equal(result.reloads, 0);
  assert.equal(result.entry.terminal, true);
  assert.equal(result.entry.polling, false);
});
test('PDF continues observing automatic retries until qualified completion', async () => {
  const result = await run([
    { state: 'processing' },
    { state: 'queued', error_code: 'parser_unavailable' },
    { state: 'processing' },
    { state: 'parsed', searchable: true },
  ], true);
  assert.equal(result.calls, 4);
  assert.equal(result.reloads, 1);
  assert.equal(result.entry.polling, false);
});

const dismissing = source.slice(source.indexOf('async function dismissPdfTask(id, button)'), source.indexOf('async function retryImportTask(id, entry, button)'));
async function dismiss(response) {
  const calls = [], toasts = [];
  const context = { state: { processing: new Map([['pdf-task', { kind: 'pdf', terminal: true, state: 'failed' }]]) },
    busy: async (_button, _text, work) => work(), readCookie: () => 'csrf', crypto: { randomUUID: () => 'key' },
    fetchJson: async (url, options) => { calls.push({ url, method: options.method }); if (response instanceof Error) throw response; return response; },
    showToast: (text) => toasts.push(text), renderProcessing: () => {},
  };
  vm.createContext(context);
  await vm.runInContext(dismissing + '\ndismissPdfTask("pdf-task", {})', context);
  return { calls, toasts, retained: context.state.processing.has('pdf-task') };
}
test('移除 dismisses a failed PDF that never became knowledge', async () => {
  const result = await dismiss({ task_id: 'pdf-task', state: 'dismissed' });
  assert.deepEqual(result.calls, [{ url: '/v1/knowledge/pdf-imports/pdf-task/dismiss', method: 'POST' }]);
  assert.equal(result.retained, false);
});
test('移除 of a PDF already in the library keeps it and points to the library', async () => {
  const result = await dismiss(Object.assign(new Error(), { status: 409 }));
  assert.equal(result.retained, true);
  assert.match(result.toasts[0], /资料库中删除/);
});
