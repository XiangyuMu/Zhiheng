const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('src/zhiheng/api/static/knowledge-agent.js', 'utf8');
const polling = source.slice(source.indexOf('async function pollProcessing(id)'), source.indexOf('function renderProcessing()'));
async function run(responses) {
  const entry = {};
  let calls = 0, reloads = 0;
  const context = { state: { processing: new Map([['fixture', entry]]) }, processingNames: {},
    fetchJson: async () => { const value = responses[Math.min(calls++, responses.length - 1)]; if (value instanceof Error) throw value; return value; },
    loadKnowledge: async () => { reloads++; }, renderProcessing: () => {},
    setTimeout: (callback) => callback(),
  };
  vm.createContext(context);
  await vm.runInContext(polling + '\npollProcessing("fixture")', context);
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
