const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('src/zhiheng/api/static/knowledge-agent.js', 'utf8');
const start = source.indexOf('const PDF_UNSUPPORTED_MESSAGE');
const gate = source.slice(start, source.indexOf('function fileKind(file)', start));

function load(fetchJson) {
  const requested = [];
  const context = { fetchJson: async (url) => { requested.push(url); return fetchJson(url); } };
  vm.createContext(context);
  vm.runInContext(gate + '\nthis.gate = { PDF_UNSUPPORTED_MESSAGE, pdfUnsupportedNotice, loadPdfCapability, pdfUploadError };', context);
  return { ...context.gate, requested };
}

test('no live parser worker blocks PDF with a readable reason before upload', async () => {
  const gate = load(() => ({ pdf_parsing: { available: false, backends: [], reason: 'no live worker has reported' }, workers: [] }));
  const notice = await gate.loadPdfCapability();
  assert.deepEqual(gate.requested, ['/v1/deployment/capabilities']);
  assert.equal(notice, gate.PDF_UNSUPPORTED_MESSAGE);
  assert.match(notice, /PDF 解析/);
});
test('a live parser worker allows PDF', async () => {
  const gate = load(() => ({ pdf_parsing: { available: true, backends: ['mineru'], reason: null }, workers: [{}] }));
  assert.equal(await gate.loadPdfCapability(), null);
});
test('an older server without the endpoint leaves the decision to the upload', async () => {
  const gate = load(() => { throw Object.assign(new Error('not found'), { status: 404 }); });
  assert.equal(await gate.loadPdfCapability(), null);
  assert.equal(gate.pdfUnsupportedNotice({}), null);
});
test('a server-side refusal is shown as the same readable reason', () => {
  const gate = load(() => ({}));
  const refused = Object.assign(new Error('no live worker declares PDF parsing; PDF uploads are unsupported'), { status: 422 });
  assert.equal(gate.pdfUploadError(refused).message, gate.PDF_UNSUPPORTED_MESSAGE);
  const other = Object.assign(new Error('malformed PDF'), { status: 400 });
  assert.equal(gate.pdfUploadError(other), other);
});
