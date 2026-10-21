// Scattered handling surfaces only link to 待处理 (#85, ADR 0011).
// Serves the static pages without the API server: /v1/review/counts and the inbox are
// stubbed so the "N 待处理 → 去处理" links, the URL-driven type filter and the research
// page's context-prompt actions are deterministic.
const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const STATIC_DIR = path.join(__dirname, '..', '..', 'src', 'zhiheng', 'api', 'static');
// A .localhost origin is a secure context, so crypto.randomUUID() works for writes.
const BASE = 'http://entry.localhost';
const PAGES = {
  '/knowledge-agent': 'knowledge-agent.html',
  '/memory-center': 'memory-center.html',
  '/taxonomy-center': 'taxonomy-center.html',
  '/review-center': 'review-center.html',
};
const TYPES = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript' };
const COUNTS = {
  total: 9,
  by_type: {
    conclusion_draft: 4, relation_suggestion: 0, personal_conflict: 0, missing_info_prompt: 0,
    extraction_supplement: 0, memory_candidate: 3, classification: 2,
  },
};

let browser;
let requests;
let inbox;
let answerPrompts;

before(async () => { browser = await chromium.launch(); });
after(async () => { await browser?.close(); });
beforeEach(() => { requests = []; inbox = []; answerPrompts = []; });

async function open(route) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.route(`${BASE}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.startsWith('/v1/')) {
      requests.push({
        method: request.method(), path: url.pathname, search: url.search,
        ifMatch: request.headers()['if-match'], body: request.postData(),
      });
      if (url.pathname === '/v1/review/counts') return route.fulfill({ json: COUNTS });
      if (url.pathname === '/v1/review/items' && request.method() === 'GET') {
        const type = url.searchParams.get('type');
        return route.fulfill({ json: {
          items: inbox.filter((item) => !type || item.type === type), total: inbox.length,
        } });
      }
      if (url.pathname === '/v1/answers') {
        return route.fulfill({ json: {
          answer: '需要你的个人背景。', citations: [], stop_reason: 'completed',
          context_prompts: answerPrompts,
        } });
      }
      if (url.pathname === '/v1/conversations') return route.fulfill({ json: { id: 'conv-1' } });
      if (url.pathname === '/v1/taxonomy') {
        return route.fulfill({ json: { domains: [
          { id: 'education_learning', name: '教育学习', status: 'active', is_primary: true },
        ] } });
      }
      if (request.method() !== 'GET') return route.fulfill({ json: { status: 'completed', result: {} } });
      return route.fulfill({ status: 404, json: { detail: 'not stubbed' } });
    }
    const file = PAGES[url.pathname] || url.pathname.slice(1);
    const full = path.join(STATIC_DIR, file);
    if (!full.startsWith(STATIC_DIR) || !fs.existsSync(full)) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ body: fs.readFileSync(full), contentType: TYPES[path.extname(file)] });
  });
  const page = await context.newPage();
  await page.goto(BASE + route);
  return page;
}

async function pendingLink(page, type) {
  const link = page.locator(`a[data-pending-type="${type}"]:visible`);
  await link.filter({ hasText: /\d+ 待处理 → 去处理/ }).waitFor();
  return link;
}

test('个人上下文 shows the memory candidate count and links to the filtered 待处理', async () => {
  const page = await open('/memory-center');
  const link = await pendingLink(page, 'memory_candidate');
  assert.equal((await link.innerText()).trim(), '3 待处理 → 去处理');
  assert.equal(await link.getAttribute('href'), '/review-center?type=memory_candidate');
  assert.equal((await page.locator('#tab-candidates [data-count]').innerText()).trim(), '3');
  for (const name of ['确认', '拒绝', '批量确认', '批量拒绝']) {
    assert.equal(await page.getByRole('button', { name, exact: true }).count(), 0, `no ${name} button`);
  }
  assert(!requests.some((entry) => entry.path.startsWith('/v1/memory/candidates')));
  await page.context().close();
});

test('资料库 replaces the unclassified list with a link to classification items', async () => {
  const page = await open('/knowledge-agent#library');
  const link = await pendingLink(page, 'classification');
  assert.equal((await link.innerText()).trim(), '2 待处理 → 去处理');
  assert.equal(await link.getAttribute('href'), '/review-center?type=classification');
  assert.equal(await page.getByRole('button', { name: '确认归类' }).count(), 0);
  assert.equal(await page.locator('#unclassified-list').count(), 0);
  assert(!requests.some((entry) => entry.path === '/v1/knowledge/unclassified'));
  await page.context().close();
});

test('分类中心 replaces 待审核调整 with a link to classification items', async () => {
  const page = await open('/taxonomy-center');
  const link = await pendingLink(page, 'classification');
  assert.equal((await link.innerText()).trim(), '2 待处理 → 去处理');
  assert.equal(await link.getAttribute('href'), '/review-center?type=classification');
  await page.locator('#domains').filter({ hasText: '教育学习' }).waitFor();
  for (const name of ['保存', '批准已确认迁移', '回滚此迁移']) {
    assert.equal(await page.getByRole('button', { name }).count(), 0, `no ${name} button`);
  }
  assert(!requests.some((entry) => entry.path.startsWith('/v1/taxonomy/proposals')));
  await page.context().close();
});

test('待处理 opens with the type filter named in the URL', async () => {
  const page = await open('/review-center?type=memory_candidate');
  await page.locator('.type-filter[data-type="memory_candidate"][aria-pressed="true"]').waitFor();
  const listing = requests.find((entry) => entry.path === '/v1/review/items');
  assert.equal(new URLSearchParams(listing.search).get('type'), 'memory_candidate');
  await page.context().close();

  requests = [];
  const unknown = await open('/review-center?type=nonsense');
  await unknown.locator('.type-filter[data-type=""][aria-pressed="true"]').waitFor();
  await unknown.waitForFunction(() => document.querySelector('#queue')?.getAttribute('aria-busy') === 'false');
  const all = requests.find((entry) => entry.path === '/v1/review/items');
  assert.equal(new URLSearchParams(all.search).get('type'), null);
  await unknown.context().close();
});

function pendingItem(type, id, extra = {}) {
  return {
    type, id, kind: null, etag: `inbox-etag-${id}`, pending_since: '2026-10-01T00:00:00+00:00',
    title: id, summary: null, actions: ['defer'], detail_actions: [],
    requires_individual_approval: false, deferred: false, detail: {}, ...extra,
  };
}

async function ask(page) {
  await page.locator('#question').fill('我的职业是什么？');
  await page.locator('#answer-form button[type="submit"]').click();
  await page.locator('#context-prompt-dialog').waitFor({ state: 'visible' });
}

async function writeTo(page, pathname) {
  await page.waitForFunction(() => !document.querySelector('#context-prompt-dialog').open);
  return requests.filter((entry) => entry.method === 'POST' && entry.path === pathname);
}

test('the research context prompt decides through the same actions as 待处理', async () => {
  inbox = [
    pendingItem('missing_info_prompt', 'mp1', { actions: ['reject', 'defer'], detail_actions: ['supplement'] }),
    pendingItem('personal_conflict', 'pc1', {
      detail_actions: ['confirm', 'reject', 'supplement'], detail: { candidate_etag: 'cand-etag' },
    }),
  ];
  const missing = {
    id: 'mp1', kind: 'missing', status: 'pending', state_key: 'profile.job', reason: '回答需要你的职业。',
  };
  const conflict = {
    id: 'pc1', kind: 'conflict', status: 'pending', state_key: 'profile.city', reason: '居住城市冲突。',
    candidate: { text: '上海' }, existing: { text: '北京' }, candidate_etag: 'cand-etag',
  };
  const page = await open('/knowledge-agent#research');

  answerPrompts = [missing];
  await ask(page);
  await page.locator('#context-prompt-defer').click();
  let writes = await writeTo(page, '/v1/review/items/missing_info_prompt/mp1/defer');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].ifMatch, 'inbox-etag-mp1');

  await ask(page);
  await page.locator('#context-prompt-skip').click();
  writes = await writeTo(page, '/v1/review/items/missing_info_prompt/mp1/reject');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].ifMatch, 'inbox-etag-mp1');

  answerPrompts = [conflict];
  await ask(page);
  assert.equal((await page.locator('#context-prompt-skip').innerText()).trim(), '保留已有记录');
  assert.equal((await page.locator('#context-prompt-confirm').innerText()).trim(), '采用新记录');
  await page.locator('#context-prompt-defer').click();
  writes = await writeTo(page, '/v1/review/items/personal_conflict/pc1/defer');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].ifMatch, 'inbox-etag-pc1');

  await ask(page);
  await page.locator('#context-prompt-skip').click();
  writes = await writeTo(page, '/v1/personal-updates/conflicts/pc1/decision');
  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0].body), { decision: 'reject' });
  assert.equal(writes[0].ifMatch, 'cand-etag');

  await ask(page);
  await page.locator('#context-prompt-confirm').click();
  writes = await writeTo(page, '/v1/personal-updates/conflicts/pc1/decision');
  assert.equal(writes.length, 2);
  assert.deepEqual(JSON.parse(writes[1].body), { decision: 'confirm' });

  // No write goes to the retired research-only decision route except supplements.
  assert(!requests.some((entry) => entry.method === 'POST' && entry.path.endsWith('/decision')
    && entry.path.startsWith('/v1/personal-updates/context-prompts/')));
  await page.context().close();
});

test('a research conflict decision sends the inbox candidate etag, never *', async () => {
  inbox = [pendingItem('personal_conflict', 'pc2', {
    detail_actions: ['confirm', 'reject', 'supplement'], detail: { candidate_etag: 'inbox-cand-etag' },
  })];
  // The answer's prompt carries no candidate etag of its own.
  answerPrompts = [{
    id: 'pc2', kind: 'conflict', status: 'pending', state_key: 'profile.city', reason: '居住城市冲突。',
    candidate: { text: '上海' }, existing: { text: '北京' },
  }];
  const page = await open('/knowledge-agent#research');

  await ask(page);
  await page.locator('#context-prompt-confirm').click();
  const writes = await writeTo(page, '/v1/personal-updates/conflicts/pc2/decision');

  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0].body), { decision: 'confirm' });
  assert.equal(writes[0].ifMatch, 'inbox-cand-etag');
  await page.context().close();
});

test('deciding a deferred prompt from research restores it first', async () => {
  inbox = [pendingItem('missing_info_prompt', 'mp2', {
    deferred: true, actions: ['restore'], etag: 'deferred-etag',
  })];
  answerPrompts = [{
    id: 'mp2', kind: 'missing', status: 'deferred', state_key: 'profile.job', reason: '回答需要你的职业。',
  }];
  const page = await open('/knowledge-agent#research');
  await page.route(`${BASE}/v1/review/items/missing_info_prompt/mp2/restore`, (route) => {
    requests.push({
      method: 'POST', path: new URL(route.request().url()).pathname,
      ifMatch: route.request().headers()['if-match'],
    });
    inbox = [pendingItem('missing_info_prompt', 'mp2', { actions: ['reject', 'defer'], etag: 'restored-etag' })];
    return route.fulfill({ json: { status: 'completed', result: {} } });
  });

  await ask(page);
  await page.locator('#context-prompt-skip').click();
  const rejects = await writeTo(page, '/v1/review/items/missing_info_prompt/mp2/reject');

  const restored = requests.filter((entry) => entry.path === '/v1/review/items/missing_info_prompt/mp2/restore');
  assert.deepEqual(restored.map((entry) => entry.ifMatch), ['deferred-etag']);
  assert.deepEqual(rejects.map((entry) => entry.ifMatch), ['restored-etag']);
  await page.context().close();
});
