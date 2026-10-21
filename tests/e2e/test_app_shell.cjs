// Shared workspace sidebar and pending badge in a real browser (#81, ADR 0011).
// Serves the static pages without the API server: /v1/review/counts is stubbed so the
// badge rules (hidden at 0, capped at 99+, refreshed after a write) are deterministic.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const STATIC_DIR = path.join(__dirname, '..', '..', 'src', 'zhiheng', 'api', 'static');
const BASE = 'http://shell.test';
const PAGES = [
  ['/knowledge-agent#research', 'knowledge-agent.html', '研究问答'],
  ['/knowledge-agent#library', 'knowledge-agent.html', '资料库'],
  ['/memory-center', 'memory-center.html', '个人上下文'],
  ['/taxonomy-center', 'taxonomy-center.html', '分类中心'],
  ['/evolution-center', 'evolution-center.html', '系统改进'],
  ['/review-center', 'review-center.html', '待处理'],
];
const TYPES = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript' };

let browser;
let pendingTotal = 0;
let countRequests = 0;

before(async () => { browser = await chromium.launch(); });
after(async () => { await browser?.close(); });

async function open(route, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport });
  await context.route(`${BASE}/**`, async (request) => {
    const url = new URL(request.request().url());
    if (url.pathname === '/v1/review/counts') {
      countRequests += 1;
      return request.fulfill({ json: { total: pendingTotal, by_type: {} } });
    }
    if (url.pathname.startsWith('/v1/')) {
      if (request.request().method() !== 'GET') return request.fulfill({ json: { status: 'ok' } });
      return request.fulfill({ status: 404, json: { detail: 'not stubbed' } });
    }
    const page = PAGES.find(([pageRoute]) => pageRoute.split('#')[0] === url.pathname);
    const file = page ? page[1] : url.pathname.slice(1);
    const full = path.join(STATIC_DIR, file);
    if (!full.startsWith(STATIC_DIR) || !fs.existsSync(full)) return request.fulfill({ status: 404, body: '' });
    return request.fulfill({ body: fs.readFileSync(full), contentType: TYPES[path.extname(file)] });
  });
  const page = await context.newPage();
  await page.goto(BASE + route);
  await page.locator('aside.sidebar .nav-link').first().waitFor();
  return page;
}

async function sidebarLinks(page) {
  return page.locator('aside.sidebar .nav-link').evaluateAll((links) => links.map((link) => ({
    href: link.getAttribute('href'),
    label: link.querySelector('.nav-label-text')?.textContent || link.textContent.trim(),
    current: link.getAttribute('aria-current'),
  })));
}

test('every workspace page shows the same sidebar and highlights only itself', async () => {
  pendingTotal = 0;
  let reference;
  for (const [route, , label] of PAGES) {
    const page = await open(route);
    const links = await sidebarLinks(page);
    const shape = links.map(({ href, label: text }) => `${href} ${text}`);
    reference ??= shape;
    assert.deepEqual(shape, reference, `${route} uses the shared sidebar`);
    assert.deepEqual(links.filter((link) => link.current === 'page').map((link) => link.label), [label], route);
    assert.equal(await page.getByText('集中审核').count(), 0, route);
    const layout = await page.locator('aside.sidebar').evaluate((aside) => ({
      position: getComputedStyle(aside).position, width: Math.round(aside.getBoundingClientRect().width),
    }));
    assert.deepEqual(layout, { position: 'sticky', width: 224 }, `${route} lays the sidebar out as a column`);
    await page.context().close();
  }
  for (const label of ['研究问答', '资料库', '待处理', '方案比较', '个人上下文', '分类中心', '设置', '系统改进']) {
    assert(reference.some((entry) => entry.endsWith(` ${label}`)), `sidebar lists ${label}`);
  }
});

test('the pending badge is hidden at zero and caps at 99+', async () => {
  pendingTotal = 0;
  let page = await open('/memory-center');
  const badge = page.locator('#pending-nav-count');
  await page.waitForFunction(() => document.querySelector('#pending-nav-count')?.dataset.loaded === 'true');
  assert.equal(await badge.isHidden(), true);
  await page.context().close();

  pendingTotal = 7;
  page = await open('/taxonomy-center');
  await page.locator('#pending-nav-count').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#pending-nav-count').innerText(), '7');
  assert.match(await page.locator('a[href="/review-center"]').getAttribute('aria-label'), /待处理.*7/);
  await page.context().close();

  pendingTotal = 150;
  page = await open('/review-center');
  await page.locator('#pending-nav-count').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#pending-nav-count').innerText(), '99+');
  await page.context().close();
});

test('the badge follows a write without reloading the page', async () => {
  pendingTotal = 2;
  const page = await open('/knowledge-agent#research');
  const badge = page.locator('#pending-nav-count');
  await badge.waitFor({ state: 'visible' });
  assert.equal(await badge.innerText(), '2');
  const navigations = [];
  page.on('framenavigated', (frame) => navigations.push(frame.url()));
  pendingTotal = 1;
  await page.evaluate(() => fetch('/v1/review/items/memory_candidate/abc/defer', { method: 'POST' }));
  await page.waitForFunction(() => document.querySelector('#pending-nav-count')?.textContent === '1');
  pendingTotal = 0;
  const before = countRequests;
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('zhiheng:pending-changed')));
  await badge.waitFor({ state: 'hidden' });
  assert(countRequests > before);
  // Reads never trigger a recount.
  const afterWrites = countRequests;
  await page.evaluate(() => fetch('/v1/taxonomy'));
  await page.waitForTimeout(400);
  assert.equal(countRequests, afterWrites);
  assert.deepEqual(navigations, []);
  await page.context().close();
});

test('at 390px every page keeps the sidebar without horizontal overflow', async () => {
  pendingTotal = 150;
  for (const [route] of PAGES) {
    const page = await open(route, { width: 390, height: 844 });
    await page.locator('#pending-nav-count').waitFor({ state: 'visible' });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    assert(overflow <= 1, `${route} overflows by ${overflow}px`);
    const outside = await page.locator('aside.sidebar .nav-link').evaluateAll((links) => links
      .filter((link) => { const box = link.getBoundingClientRect(); return box.left < 0 || box.right > innerWidth + 1; })
      .map((link) => link.textContent));
    assert.deepEqual(outside, [], `${route} sidebar links stay on screen`);
    await page.context().close();
  }
});
