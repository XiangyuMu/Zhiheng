const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('src/zhiheng/api/static/knowledge-agent.js', 'utf8');
const START = '// PDF capability gate: start';
const END = '// PDF capability gate: end';
const gate = source.slice(source.indexOf(START), source.indexOf(END));

function load(fetchJson) {
  const requested = [];
  const context = { fetchJson: async (url) => { requested.push(url); return fetchJson(url); } };
  vm.createContext(context);
  vm.runInContext(gate + '\nthis.gate = { PDF_UNSUPPORTED_MESSAGE, fileKind, pdfUnsupportedNotice, loadPdfCapability, isPdfRefusal, splitRefusedPdfs, refusedPdfMessage };', context);
  return { ...context.gate, requested };
}
const unavailable = () => ({ pdf_parsing: { available: false, backends: [], reason: 'no live worker has reported', error_code: 'unsupported_pdf_parser' }, live_workers: 0 });
const available = () => ({ pdf_parsing: { available: true, backends: ['mineru'], reason: null, error_code: null }, live_workers: 1 });
const file = (name, type = '') => ({ name, type });
const names = (files) => [...files].map((item) => item.name);

test('no live parser worker blocks PDF with a readable reason before upload', async () => {
  const gate = load(unavailable);
  const notice = await gate.loadPdfCapability();
  assert.deepEqual(gate.requested, ['/v1/deployment/capabilities']);
  assert.equal(notice, gate.PDF_UNSUPPORTED_MESSAGE);
  assert.match(notice, /PDF 解析的部署能力/);
});
test('a live parser worker allows PDF', async () => {
  const gate = load(available);
  assert.equal(await gate.loadPdfCapability(), null);
});
test('an older server without the endpoint leaves the decision to the upload', async () => {
  const gate = load(() => { throw Object.assign(new Error('not found'), { status: 404 }); });
  assert.equal(await gate.loadPdfCapability(), null);
  assert.equal(gate.pdfUnsupportedNotice({}), null);
});
test('a mixed batch keeps text, Markdown and images and refuses only the PDFs', async () => {
  const gate = load(unavailable);
  const batch = [file('notes.md'), file('paper.pdf', 'application/pdf'), file('scan.png', 'image/png'), file('plain.txt', 'text/plain'), file('SLIDES.PDF')];
  const { importable, refusedPdfs } = await gate.splitRefusedPdfs(batch);
  assert.deepEqual(names(importable), ['notes.md', 'scan.png', 'plain.txt']);
  assert.deepEqual(names(refusedPdfs), ['paper.pdf', 'SLIDES.PDF']);
  assert.match(gate.refusedPdfMessage(refusedPdfs), /《paper\.pdf》、《SLIDES\.PDF》/);
});
test('a batch without PDFs does not ask for the deployment capabilities', async () => {
  const gate = load(unavailable);
  const { importable, refusedPdfs } = await gate.splitRefusedPdfs([file('notes.md'), file('scan.jpg')]);
  assert.deepEqual(names(importable), ['notes.md', 'scan.jpg']);
  assert.deepEqual(names(refusedPdfs), []);
  assert.deepEqual(gate.requested, []);
});
test('with a live parser every file stays importable', async () => {
  const gate = load(available);
  const { importable, refusedPdfs } = await gate.splitRefusedPdfs([file('notes.md'), file('paper.pdf')]);
  assert.deepEqual(names(importable), ['notes.md', 'paper.pdf']);
  assert.deepEqual(names(refusedPdfs), []);
});
test('a server-side refusal is recognised by its stable code, not its wording', () => {
  const gate = load(() => ({}));
  const refused = Object.assign(new Error('worded differently'), { status: 422, code: 'unsupported_pdf_parser' });
  assert.equal(gate.isPdfRefusal(refused), true);
  const sameWordsNoCode = Object.assign(new Error('no live worker declares PDF parsing; PDF uploads are unsupported'), { status: 422 });
  assert.equal(gate.isPdfRefusal(sameWordsNoCode), false);
  const other = Object.assign(new Error('malformed PDF'), { status: 400 });
  assert.equal(gate.isPdfRefusal(other), false);
});
test('fetchJson exposes the stable error code from the response body', async () => {
  const start = source.indexOf('async function fetchJson(');
  const body = source.slice(start, source.indexOf('\nfunction mutate(', start));
  const context = {
    fetch: async () => ({ ok: false, status: 422, text: async () => JSON.stringify({ detail: 'refused', error_code: 'unsupported_pdf_parser' }) }),
    location: { pathname: '/', search: '', hash: '', assign() {} },
  };
  vm.createContext(context);
  vm.runInContext(body + '\nthis.fetchJson = fetchJson;', context);
  await assert.rejects(context.fetchJson('/x'), (error) => error.code === 'unsupported_pdf_parser' && error.message === 'refused' && error.status === 422);
});
