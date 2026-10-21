// 待处理 page in a real browser (#82, ADR 0011, ADR 0008).
// Serves the static page without the API server: the inbox API and each type's own
// endpoints are stubbed so list/detail sync, filters, keyboard triage, undo and the
// loading/empty/error states are deterministic. Timers are driven with page.clock.
const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const STATIC_DIR = path.join(__dirname, '..', '..', 'src', 'zhiheng', 'api', 'static');
const BASE = 'http://pending.test';
const TYPES = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript' };
const PENDING_TYPES = [
  'conclusion_draft', 'relation_suggestion', 'personal_conflict', 'missing_info_prompt',
  'extraction_supplement', 'memory_candidate', 'classification',
];

let browser;
let items;
let writes;
let failNext;
let itemsGate;
let itemsError;
let batchFailures;

function item(type, id, title, since, extra = {}) {
  return {
    type, id, kind: null, etag: `etag-${id}`, pending_since: since, title, summary: null,
    actions: ['approve', 'reject', 'defer'], detail_actions: [],
    requires_individual_approval: false, deferred: false, detail: {}, ...extra,
  };
}

function seed() {
  return [
    item('conclusion_draft', 'c1', '复习结论', '2026-10-01T00:00:00+00:00',
      { requires_individual_approval: true }),
    item('relation_suggestion', 'r1', '左侧结论', '2026-10-02T00:00:00+00:00', {
      requires_individual_approval: true, summary: '右侧结论',
      detail: { kind: 'supports', left_id: 'c1', right_id: 'c9', stale: false },
    }),
    item('personal_conflict', 'pc1', 'profile.city', '2026-10-03T00:00:00+00:00', {
      actions: ['defer'], detail_actions: ['confirm', 'reject', 'supplement'],
      detail: {
        state_key: 'profile.city', candidate_etag: 'cand-etag',
        candidate_value: { text: '上海' }, existing_value: { text: '北京' },
      },
    }),
    item('missing_info_prompt', 'mp1', '你的职业是？', '2026-10-04T00:00:00+00:00', {
      actions: ['reject', 'defer'], detail_actions: ['supplement'], summary: '回答需要职业',
      detail: { state_key: 'profile.job', payload: { query: '你的职业是？' } },
    }),
    item('memory_candidate', 'm1', 'preference.tea', '2026-10-05T00:00:00+00:00', {
      kind: 'explicit_extracted', summary: '喜欢绿茶', detail: { value: { text: '喜欢绿茶' } },
    }),
    item('classification', 'k1', '未分类资料', '2026-10-06T00:00:00+00:00', {
      kind: 'unclassified', actions: ['defer'], detail_actions: ['choose_domain'],
      detail: { primary_domain_id: 'unclassified', suggested_primary_domain_id: null },
    }),
    item('memory_candidate', 'm2', 'preference.coffee', '2026-09-01T00:00:00+00:00', {
      kind: 'explicit_extracted', deferred: true, actions: ['restore'],
    }),
  ];
}

const CONCLUSIONS = {
  c1: {
    id: 'c1', title: '复习结论', claim: '固定条件下复习有效', status: 'draft', etag: 'etag-c1',
    premises: [{ text: '固定条件', confirmed: false }], source: { text: '研究原文' },
    evidence: [{ text: '证据片段' }], classification: { primary_domain_id: 'education_learning' },
    relations: [],
  },
  c9: {
    id: 'c9', title: '已有结论', claim: '右侧结论', status: 'formal', etag: 'etag-c9',
    premises: [], source: { text: '另一原文' }, relations: [],
  },
};

before(async () => { browser = await chromium.launch(); });
after(async () => { await browser?.close(); });
beforeEach(() => {
  items = seed();
  writes = [];
  failNext = null;
  itemsGate = null;
  itemsError = null;
  batchFailures = {};
});

function listing(url) {
  const types = (url.searchParams.get('type') || '').split(',').filter(Boolean);
  const deferred = url.searchParams.get('deferred') || 'exclude';
  const selected = items.filter((entry) => (!types.length || types.includes(entry.type))
    && (deferred === 'include' || entry.deferred === (deferred === 'only')));
  selected.sort((a, b) => a.pending_since.localeCompare(b.pending_since));
  if (url.searchParams.get('order') === 'newest') selected.reverse();
  return { items: selected, total: selected.length, limit: 500, offset: 0, next_offset: null };
}

function counts() {
  const byType = Object.fromEntries(PENDING_TYPES.map((type) => [type, 0]));
  items.filter((entry) => !entry.deferred).forEach((entry) => { byType[entry.type] += 1; });
  return { total: Object.values(byType).reduce((a, b) => a + b, 0), by_type: byType };
}

async function open(viewport = { width: 1440, height: 900 }, { clock = true } = {}) {
  const context = await browser.newContext({ viewport });
  await context.route(`${BASE}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.startsWith('/v1/')) {
      if (request.method() !== 'GET') {
        writes.push({
          method: request.method(), path: url.pathname, ifMatch: request.headers()['if-match'],
          body: request.postData() ? JSON.parse(request.postData()) : null,
        });
        if (failNext) {
          const failure = failNext;
          failNext = null;
          return route.fulfill({ status: failure.status, json: { detail: failure.detail } });
        }
        if (url.pathname === '/v1/review/batch') {
          const { action, items: targets } = JSON.parse(request.postData());
          const results = targets.map((target) => {
            const failure = batchFailures[target.id];
            if (failure) return { ...target, status: 'failed', reason: failure.reason, message: failure.message, result: null };
            items = items.filter((entry) => entry.id !== target.id);
            return { type: target.type, id: target.id, status: 'succeeded', reason: null, message: null, result: {} };
          });
          return route.fulfill({ json: { status: 'completed', result: { action, items: results } } });
        }
        const id = url.pathname.match(/^\/v1\/review\/items\/[^/]+\/([^/]+)\/[^/]+$/)?.[1];
        items = items.filter((entry) => entry.id !== id);
        return route.fulfill({ json: { status: 'completed', result: {} } });
      }
      if (url.pathname === '/v1/review/items') {
        if (itemsGate) await itemsGate;
        if (itemsError) return route.fulfill({ status: 500, json: { detail: itemsError } });
        return route.fulfill({ json: listing(url) });
      }
      if (url.pathname === '/v1/review/counts') return route.fulfill({ json: counts() });
      if (url.pathname === '/v1/taxonomy') {
        return route.fulfill({ json: { domains: [
          { id: 'education_learning', name: '教育学习', status: 'active' },
          { id: 'health', name: '健康', status: 'active' },
        ], record_types: [] } });
      }
      const conclusion = url.pathname.match(/^\/v1\/conclusions\/([^/]+)$/);
      if (conclusion && CONCLUSIONS[conclusion[1]]) return route.fulfill({ json: CONCLUSIONS[conclusion[1]] });
      return route.fulfill({ status: 404, json: { detail: 'not stubbed' } });
    }
    const file = url.pathname === '/review-center' ? 'review-center.html' : url.pathname.slice(1);
    const full = path.join(STATIC_DIR, file);
    if (!full.startsWith(STATIC_DIR) || !fs.existsSync(full)) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ body: fs.readFileSync(full), contentType: TYPES[path.extname(file)] });
  });
  const page = await context.newPage();
  page.errors = [];
  page.on('pageerror', (error) => page.errors.push(error.message));

  // The fake clock keeps flowing (so Playwright's own polling still runs); runFor jumps
  // across the undo window without waiting five real seconds.
  if (clock) await page.clock.install();
  await page.goto(`${BASE}/review-center`);
  if (clock) await page.clock.resume();
  return page;
}

const selectedId = (page) => page.locator('#queue .queue-item[aria-current="true"]').getAttribute('data-entry-id');
const ids = (page) => page.locator('#queue .queue-item').evaluateAll((nodes) => nodes.map((node) => node.dataset.entryId));

async function ready(page) {
  await page.locator('#queue .queue-item').first().waitFor();
}

test('j/k move through the oldest-first list and the detail follows', async () => {
  const page = await open();
  await ready(page);
  assert.deepEqual(await ids(page), ['c1', 'r1', 'pc1', 'mp1', 'm1', 'k1']);
  assert.equal(await selectedId(page), 'c1');
  await page.locator('#detail').filter({ hasText: '固定条件下复习有效' }).waitFor();
  const detail = await page.locator('#detail').innerText();
  for (const part of ['假设：固定条件', '研究原文', '证据片段', 'education_learning', '关系']) {
    assert(detail.includes(part), `conclusion detail shows ${part}`);
  }
  await page.keyboard.press('j');
  assert.equal(await selectedId(page), 'r1');
  await page.locator('#detail').filter({ hasText: '右侧结论' }).waitFor();
  await page.keyboard.press('j');
  await page.locator('#detail').filter({ hasText: '上海' }).waitFor();
  assert((await page.locator('#detail').innerText()).includes('北京'));
  await page.keyboard.press('k');
  assert.equal(await selectedId(page), 'r1');
  await page.keyboard.press('k');
  await page.keyboard.press('k');
  assert.equal(await selectedId(page), 'c1', 'k stops at the first item');
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

test('type keys, deferred view and newest-first order change the list', async () => {
  const page = await open();
  await ready(page);
  assert.equal(await page.locator('#total').innerText(), '6');
  assert.equal(await page.locator('[data-count="memory_candidate"]').innerText(), '1');
  await page.keyboard.press('6');
  await page.waitForFunction(() => document.querySelectorAll('#queue .queue-item').length === 1);
  assert.deepEqual(await ids(page), ['m1']);
  assert.equal(await page.locator('.type-filter[data-type="memory_candidate"]').getAttribute('aria-pressed'), 'true');
  await page.locator('#show-deferred').check();
  await page.locator('#queue .queue-item[data-entry-id="m2"]').waitFor();
  assert.deepEqual(await ids(page), ['m2']);
  await page.locator('#detail [data-action="restore"]').waitFor();
  await page.locator('#show-deferred').uncheck();
  await page.keyboard.press('0');
  await page.waitForFunction(() => document.querySelectorAll('#queue .queue-item').length === 6);
  await page.locator('#sort-order').selectOption('newest');
  await page.waitForFunction(() => document.querySelector('#queue .queue-item')?.dataset.entryId === 'k1');
  assert.deepEqual(await ids(page), ['k1', 'm1', 'mp1', 'pc1', 'r1', 'c1']);
  await page.context().close();
});

test('a deferred item shows only 恢复 in its detail, whatever its type', async () => {
  items.push(
    item('personal_conflict', 'pc2', 'profile.home', '2026-09-02T00:00:00+00:00', {
      deferred: true, actions: ['restore'],
      detail: { state_key: 'profile.home', candidate_value: { text: '上海' }, existing_value: { text: '北京' } },
    }),
    item('missing_info_prompt', 'mp2', '你的城市是？', '2026-09-03T00:00:00+00:00', {
      deferred: true, actions: ['restore'], detail: { state_key: 'profile.home', payload: { query: '你的城市是？' } },
    }),
  );
  const page = await open();
  await ready(page);
  await page.locator('#show-deferred').check();
  await page.locator('#queue .queue-item[data-entry-id="pc2"]').waitFor();
  for (const id of ['m2', 'pc2', 'mp2']) {
    await page.locator(`#queue .queue-item[data-entry-id="${id}"]`).click();
    await page.locator(`#detail[data-ready$=":${id}"]`).waitFor();
    const controls = await page.locator('#detail').evaluate((detail) => [...detail.querySelectorAll('button, textarea, select, input')]
      .filter((control) => control.offsetParent !== null).map((control) => control.textContent.trim() || control.tagName));
    assert.deepEqual(controls, ['恢复'], `${id} offers only 恢复`);
  }
  await page.keyboard.press('a');
  await page.locator('#message').filter({ hasText: '先在详情中恢复' }).waitFor();
  assert.deepEqual(writes, []);
  await page.context().close();
});

test('shortcuts are ignored while typing and ? shows the key map', async () => {
  const page = await open();
  await ready(page);
  await page.keyboard.press('j');
  await page.keyboard.press('j');
  await page.keyboard.press('j');
  assert.equal(await selectedId(page), 'mp1');
  const input = page.locator('#detail textarea');
  await input.click();
  await page.keyboard.type('jkardxRAD?1');
  assert.equal(await input.inputValue(), 'jkardxRAD?1');
  assert.equal(await selectedId(page), 'mp1');
  assert.equal(await page.locator('#undo-toast').isHidden(), true);
  assert.equal(await page.locator('#shortcut-help').isVisible(), false);
  assert.deepEqual(writes, []);
  await page.locator('#detail h2').click();
  await page.keyboard.press('?');
  await page.locator('#shortcut-help').waitFor({ state: 'visible' });
  const help = await page.locator('#shortcut-help').innerText();
  for (const key of ['j', 'k', 'Enter', 'a', 'r', 'd', 'u', '0', '?', 'x', 'Shift+A', 'Shift+R', 'Shift+D']) assert(help.includes(key), `help lists ${key}`);
  await page.keyboard.press('j');
  assert.equal(await selectedId(page), 'mp1', 'keys do not act behind the open help');
  await page.keyboard.press('Escape');
  await page.locator('#shortcut-help').waitFor({ state: 'hidden' });
  await page.context().close();
});

test('unavailable action keys do nothing but show why', async () => {
  const page = await open();
  await ready(page);
  await page.keyboard.press('j');
  await page.keyboard.press('j');
  assert.equal(await selectedId(page), 'pc1');
  await page.locator('#detail').filter({ hasText: '上海' }).waitFor();
  await page.keyboard.press('a');
  await page.locator('#message').filter({ hasText: '详情中选择或补充' }).waitFor();
  await page.keyboard.press('r');
  assert.equal(await page.locator('#undo-toast').isHidden(), true);
  assert.equal(await selectedId(page), 'pc1');
  await page.keyboard.press('7');
  await page.waitForFunction(() => document.querySelector('#queue .queue-item')?.dataset.entryId === 'k1');
  await page.locator('#detail select[data-domain-picker]').waitFor();
  await page.keyboard.press('a');
  await page.locator('#message').filter({ hasText: 'Enter 选择领域' }).waitFor();
  await page.keyboard.press('Enter');
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.domainPicker), 'true');
  assert.deepEqual(writes, []);
  await page.context().close();
});

test('a on a conclusion is sent only after the undo window', async () => {
  const page = await open();
  await ready(page);
  await page.locator('#detail').filter({ hasText: '固定条件下复习有效' }).waitFor();
  await page.keyboard.press('a');
  await page.locator('#undo-toast').filter({ hasText: '已批准' }).waitFor();
  assert.deepEqual(await ids(page), ['r1', 'pc1', 'mp1', 'm1', 'k1']);
  assert.equal(await selectedId(page), 'r1');
  await page.clock.runFor(4900);
  assert.deepEqual(writes, [], 'nothing is sent inside the undo window');
  await page.clock.runFor(200);
  await page.locator('#message').filter({ hasText: '操作已保存' }).waitFor();
  assert.deepEqual(writes.map((write) => [write.method, write.path, write.ifMatch]),
    [['POST', '/v1/review/items/conclusion_draft/c1/approve', 'etag-c1']]);
  await page.context().close();
});

test('a on an individually approved item is refused until its detail is shown', async () => {
  const page = await open();
  await ready(page);
  // The relation's detail never finishes loading, so it is never fully shown.
  await page.route('**/v1/conclusions/c9', () => new Promise(() => {}));
  await page.keyboard.press('j');
  await page.keyboard.press('a');
  await page.locator('#message').filter({ hasText: '先在详情中看完' }).waitFor();
  assert.equal(await page.locator('#undo-toast').isHidden(), true);
  assert.equal(await selectedId(page), 'r1');
  await page.context().close();
});

test('u undoes within five seconds and nothing is ever sent', async () => {
  const page = await open();
  await ready(page);
  await page.keyboard.press('j');
  await page.keyboard.press('j');
  await page.keyboard.press('d');
  await page.locator('#undo-toast').filter({ hasText: '已延后' }).waitFor();
  assert.equal((await ids(page)).includes('pc1'), false);
  await page.clock.runFor(3000);
  await page.keyboard.press('u');
  await page.locator('#undo-toast').waitFor({ state: 'hidden' });
  assert.equal(await selectedId(page), 'pc1');
  await page.clock.runFor(10000);
  assert.deepEqual(writes, []);
  await page.context().close();
});

test('a failed action returns the item to the list with the reason', async () => {
  const page = await open();
  await ready(page);
  await page.locator('#queue .queue-item[data-entry-id="m1"]').click();
  failNext = { status: 412, detail: 'version changed' };
  await page.keyboard.press('r');
  await page.clock.runFor(5100);
  await page.locator('#queue .queue-item[data-entry-id="m1"] .queue-error').filter({ hasText: 'version changed' }).waitFor();
  assert.match(await page.locator('#message').innerText(), /拒绝失败：version changed/);
  assert.equal(await selectedId(page), 'm1', 'the failed item is selected again');
  assert.deepEqual(writes.map((write) => [write.path, write.ifMatch, write.body]),
    [['/v1/review/items/memory_candidate/m1/reject', 'etag-m1', {}]]);
  await page.context().close();
});

test('defer of a draft goes through the inbox action route', async () => {
  const page = await open();
  await ready(page);
  await page.keyboard.press('d');
  await page.clock.runFor(5100);
  await page.locator('#message').filter({ hasText: '操作已保存' }).waitFor();
  assert.deepEqual(writes.map((write) => [write.path, write.ifMatch]),
    [['/v1/review/items/conclusion_draft/c1/defer', 'etag-c1']]);
  await page.context().close();
});

test('loading, empty and error states are visible', async () => {
  let release;
  itemsGate = new Promise((resolve) => { release = resolve; });
  let page = await open();
  await page.locator('#queue .kb-loading').waitFor();
  assert.equal(await page.locator('#queue').getAttribute('aria-busy'), 'true');
  release();
  await ready(page);
  assert.equal(await page.locator('#queue').getAttribute('aria-busy'), 'false');
  await page.context().close();

  itemsGate = null;
  items = [];
  page = await open();
  await page.locator('#queue .kb-empty').filter({ hasText: '没有待处理事项' }).waitFor();
  await page.context().close();

  itemsError = 'database unavailable';
  page = await open();
  await page.locator('#queue .kb-error').filter({ hasText: 'database unavailable' }).waitFor();
  itemsError = null;
  items = seed();
  await page.locator('#queue .kb-error button', { hasText: '重试' }).click();
  await ready(page);
  await page.context().close();
});

test('at 390px the list and the detail are separate screens and a works only in the detail', async () => {
  const page = await open({ width: 390, height: 844 });
  await ready(page);
  assert.equal(await page.locator('#detail').isVisible(), false);
  for (let step = 0; step < 4; step += 1) await page.keyboard.press('j');
  assert.equal(await selectedId(page), 'm1');
  await page.keyboard.press('a');
  await page.locator('#message').filter({ hasText: '打开详情' }).waitFor();
  assert.equal(await page.locator('#undo-toast').isHidden(), true);
  await page.keyboard.press('Enter');
  await page.locator('#detail').filter({ hasText: '喜欢绿茶' }).waitFor({ state: 'visible' });
  assert.equal(await page.locator('#queue').isVisible(), false);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  assert(overflow <= 1, `page overflows by ${overflow}px`);
  await page.keyboard.press('a');
  await page.locator('#undo-toast').filter({ hasText: '已批准' }).waitFor();
  await page.keyboard.press('Escape');
  await page.locator('#queue').waitFor({ state: 'visible' });
  await page.locator('#queue .queue-item').first().click();
  await page.locator('#detail').waitFor({ state: 'visible' });
  await page.locator('#detail .back-to-list').click();
  await page.locator('#queue').waitFor({ state: 'visible' });
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

const selectedIds = (page) => page.locator('#queue .queue-row[data-selected="true"] .queue-item')
  .evaluateAll((nodes) => nodes.map((node) => node.dataset.entryId));

async function selectByX(page, entryIds) {
  for (const entryId of entryIds) {
    await page.locator(`#queue .queue-item[data-entry-id="${entryId}"]`).click();
    await page.locator('#detail h2').click();
    await page.keyboard.press('x');
  }
}

test('x toggles the current item into the selection and the bar counts it', async () => {
  const page = await open();
  await ready(page);
  assert.equal(await page.locator('#batch-bar').isHidden(), true);
  await page.keyboard.press('x');
  await page.keyboard.press('j');
  await page.keyboard.press('x');
  assert.deepEqual(await selectedIds(page), ['c1', 'r1']);
  await page.locator('#batch-bar').filter({ hasText: '已选 2 项' }).waitFor();
  assert.equal(await page.locator('#queue .queue-check').first().isChecked(), true);
  await page.keyboard.press('x');
  assert.deepEqual(await selectedIds(page), ['c1']);
  await page.locator('#queue .queue-row').nth(4).locator('.queue-check').check();
  assert.deepEqual(await selectedIds(page), ['c1', 'm1']);
  await page.locator('#batch-clear').click();
  assert.equal(await page.locator('#batch-bar').isHidden(), true);
  assert.deepEqual(await selectedIds(page), []);
  assert.deepEqual(writes, []);
  await page.context().close();
});

test('Shift+R rejects the selection as one batch after the undo window and the badge follows', async () => {
  const page = await open();
  await ready(page);
  await page.locator('#pending-nav-count').filter({ hasText: '6' }).waitFor();
  await selectByX(page, ['c1', 'mp1', 'm1']);
  await page.keyboard.press('Shift+R');
  await page.locator('#undo-toast').filter({ hasText: '已拒绝 3 项' }).waitFor();
  assert.deepEqual(await ids(page), ['r1', 'pc1', 'k1']);
  assert.equal(await page.locator('#batch-bar').isHidden(), true);
  await page.clock.runFor(4900);
  assert.deepEqual(writes, [], 'nothing is sent inside the undo window');
  await page.clock.runFor(200);
  await page.locator('#message').filter({ hasText: '已拒绝 3 项' }).waitFor();
  assert.deepEqual(writes.map((write) => [write.method, write.path, write.body]), [['POST', '/v1/review/batch', {
    action: 'reject',
    items: [
      { type: 'conclusion_draft', id: 'c1', etag: 'etag-c1' },
      { type: 'missing_info_prompt', id: 'mp1', etag: 'etag-mp1' },
      { type: 'memory_candidate', id: 'm1', etag: 'etag-m1' },
    ],
  }]]);
  await page.clock.runFor(500);
  await page.locator('#pending-nav-count').filter({ hasText: '3' }).waitFor();
  assert.deepEqual(await ids(page), ['r1', 'pc1', 'k1']);
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

test('u undoes a batch: the items come back in place, still selected, and nothing is sent', async () => {
  const page = await open();
  await ready(page);
  await selectByX(page, ['r1', 'm1']);
  await page.keyboard.press('Shift+D');
  await page.locator('#undo-toast').filter({ hasText: '已延后 2 项' }).waitFor();
  assert.deepEqual(await ids(page), ['c1', 'pc1', 'mp1', 'k1']);
  await page.clock.runFor(3000);
  await page.keyboard.press('u');
  await page.locator('#undo-toast').waitFor({ state: 'hidden' });
  assert.deepEqual(await ids(page), ['c1', 'r1', 'pc1', 'mp1', 'm1', 'k1']);
  assert.deepEqual(await selectedIds(page), ['r1', 'm1']);
  await page.locator('#batch-bar').filter({ hasText: '已选 2 项' }).waitFor();
  await page.clock.runFor(10000);
  assert.deepEqual(writes, []);
  await page.context().close();
});

// Wherever the detail pane rests (stuck to the top of the viewport while the page scrolls,
// or at the end of the page), with the detail scrolled to its end, which of its action
// buttons are hidden behind the undo toast?
async function coveredDetailButtons(page) {
  return page.evaluate(() => {
    const detail = document.getElementById('detail');
    const toast = document.getElementById('undo-toast').getBoundingClientRect();
    const max = document.documentElement.scrollHeight - innerHeight;
    const covered = [];
    for (let y = 0; y <= max + 10; y += 10) {
      window.scrollTo(0, Math.min(y, max));
      const stuck = Math.abs(detail.getBoundingClientRect().top - 16) < 1;
      if (!stuck && y < max) continue;
      detail.scrollTop = detail.scrollHeight;
      detail.querySelectorAll('.actions button').forEach((button) => {
        const box = button.getBoundingClientRect();
        if (box.bottom > innerHeight || (box.bottom > toast.top && box.top < toast.bottom
          && box.right > toast.left && box.left < toast.right)) {
          covered.push({ y: Math.min(y, max), stuck, top: box.top, bottom: box.bottom, toastTop: toast.top });
        }
      });
    }
    return covered;
  });
}

test('on desktop the undo toast never covers the detail action buttons', async () => {
  // A long source makes the detail scroll inside its sticky pane, so its actions sit at
  // the bottom of the viewport where the toast is.
  const original = CONCLUSIONS.c9.source;
  CONCLUSIONS.c9.source = { text: '很长的原文。'.repeat(400) };
  try {
    for (const extra of [0, 30]) {
      items = seed();
      for (let n = 0; n < extra; n += 1) {
        items.push(item('memory_candidate', `filler${n}`, `filler ${n}`, `2026-10-${String(10 + (n % 18)).padStart(2, '0')}T00:00:00+00:00`));
      }
      const page = await open({ width: 1280, height: 720 });
      await ready(page);
      await page.keyboard.press('r');
      await page.locator('#undo-toast').filter({ hasText: '已拒绝' }).waitFor();
      await page.locator('#detail').filter({ hasText: '右侧结论' }).waitFor();
      await page.locator('#detail .actions').waitFor();
      assert.deepEqual(await coveredDetailButtons(page), [], `toast covers detail buttons with ${6 + extra} items`);
      await page.context().close();
    }
  } finally {
    CONCLUSIONS.c9.source = original;
  }
});

test('at 390px x and Shift+D work from the list and the batch bar fits', async () => {
  const page = await open({ width: 390, height: 844 });
  await ready(page);
  await page.keyboard.press('x');
  await page.keyboard.press('j');
  await page.keyboard.press('j');
  await page.keyboard.press('x');
  assert.deepEqual(await selectedIds(page), ['c1', 'pc1']);
  await page.locator('#batch-bar').filter({ hasText: '已选 2 项' }).waitFor({ state: 'visible' });
  assert.equal(await page.locator('#detail').isVisible(), false, 'x does not open the detail');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  assert(overflow <= 1, `page overflows by ${overflow}px`);
  for (const action of ['approve', 'reject', 'defer']) {
    const box = await page.locator(`#batch-bar [data-batch-action="${action}"]`).boundingBox();
    assert(box.x >= 0 && box.x + box.width <= 390, `${action} button fits on screen`);
  }
  await page.keyboard.press('Shift+D');
  await page.locator('#undo-toast').filter({ hasText: '已延后 2 项' }).waitFor();
  await page.clock.runFor(5100);
  await page.locator('#message').filter({ hasText: '已延后 2 项' }).waitFor();
  assert.deepEqual(writes.map((write) => [write.path, write.body.items.map((i) => i.id)]),
    [['/v1/review/batch', ['c1', 'pc1']]]);
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

test('a partly failed batch removes what succeeded and keeps the failures selected with reasons', async () => {
  const page = await open();
  await ready(page);
  batchFailures = {
    mp1: { reason: 'stale', message: 'version changed' },
    m1: { reason: 'not_found', message: 'pending item not found' },
  };
  await selectByX(page, ['c1', 'mp1', 'm1']);
  await page.keyboard.press('Shift+R');
  await page.clock.runFor(5100);
  await page.locator('#message').filter({ hasText: '已拒绝 1 项，2 项拒绝失败' }).waitFor();
  assert.deepEqual(await ids(page), ['r1', 'pc1', 'mp1', 'm1', 'k1']);
  assert.deepEqual(await selectedIds(page), ['mp1', 'm1']);
  await page.locator('#batch-bar').filter({ hasText: '已选 2 项' }).waitFor();
  assert.match(await page.locator('#queue .queue-item[data-entry-id="mp1"] .queue-error').innerText(), /拒绝失败：内容已变化/);
  assert.match(await page.locator('#queue .queue-item[data-entry-id="m1"] .queue-error').innerText(), /拒绝失败：事项已不存在/);
  assert.equal(await selectedId(page), 'mp1', 'the first failure is the current item');
  await page.clock.runFor(500);
  await page.locator('#pending-nav-count').filter({ hasText: '5' }).waitFor();
  await page.context().close();
});

test('a refused batch request brings every item back selected with the reason', async () => {
  const page = await open();
  await ready(page);
  await selectByX(page, ['mp1', 'm1']);
  failNext = { status: 400, detail: 'duplicate items in batch' };
  await page.keyboard.press('Shift+D');
  await page.clock.runFor(5100);
  await page.locator('#message').filter({ hasText: '2 项延后失败' }).waitFor();
  assert.deepEqual(await selectedIds(page), ['mp1', 'm1']);
  assert.match(await page.locator('#queue .queue-item[data-entry-id="m1"] .queue-error').innerText(), /duplicate items in batch/);
  await page.context().close();
});

test('Shift+A is greyed out with the reason while the selection needs individual approval', async () => {
  items.push(item('memory_candidate', 'm3', 'profile.style', '2026-10-07T00:00:00+00:00', {
    kind: 'inferred', requires_individual_approval: true,
  }));
  const page = await open();
  await ready(page);
  await selectByX(page, ['m1', 'm3']);
  const approve = page.locator('#batch-bar [data-batch-action="approve"]');
  assert.equal(await approve.isDisabled(), true);
  await page.locator('#batch-reason').filter({ hasText: '推断画像' }).waitFor();
  assert.equal(await page.locator('#batch-bar [data-batch-action="reject"]').isEnabled(), true);
  await page.keyboard.press('Shift+A');
  await page.locator('#message').filter({ hasText: '逐条批准' }).waitFor();
  assert.equal(await page.locator('#undo-toast').isHidden(), true);
  assert.deepEqual(await selectedIds(page), ['m1', 'm3']);
  // Dropping the inferred candidate makes the low-stakes rest approvable together.
  await selectByX(page, ['m3']);
  assert.equal(await approve.isEnabled(), true);
  assert.equal(await page.locator('#batch-reason').isHidden(), true);
  await approve.click();
  await page.locator('#undo-toast').filter({ hasText: '已批准 1 项' }).waitFor();
  await page.clock.runFor(5100);
  await page.locator('#message').filter({ hasText: '已批准 1 项' }).waitFor();
  assert.deepEqual(writes.map((write) => [write.path, write.body.action, write.body.items.map((i) => i.id)]),
    [['/v1/review/batch', 'approve', ['m1']]]);
  await page.context().close();
});
