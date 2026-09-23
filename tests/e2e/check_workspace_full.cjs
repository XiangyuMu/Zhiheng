/* Browser acceptance against a disposable local Zhiheng database.
 * node tests/e2e/check_workspace_full.cjs URL OUTPUT_DIR
 * The script imports synthetic materials and creates a test account; never target personal data.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) {
  throw new Error('Pass a disposable loopback server URL and screenshot output directory.');
}
fs.mkdirSync(output, { recursive: true });
(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || 'chromium' });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const checks = [];
  async function check(label, work) { await work(); checks.push(label); console.log('PASS', label); }
  async function shot(name) { await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true }); }
  async function noOverflow() { assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'body must not scroll horizontally'); }
  async function apiJson(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(';').map((x) => x.trim()).find((x) => x.startsWith('zhiheng_csrf='))?.slice('zhiheng_csrf='.length) || '';
      const response = await fetch(url, { credentials: 'same-origin', ...options, headers: {
        Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrf,
        'Idempotency-Key': crypto.randomUUID(), 'If-Match': '*', ...(options.headers || {}),
      }, body: options.body ? JSON.stringify(options.body) : undefined });
      const body = await response.json();
      if (!response.ok) throw new Error(`${response.status}: ${body.detail || 'request failed'}`);
      return body;
    }, { url, options });
  }
  try {
    await page.goto(`${base}/login?next=${encodeURIComponent('/knowledge-agent#research')}`);
    await page.waitForFunction(() => !document.querySelector('#form button').disabled);
    await shot('login-desktop');
    await page.evaluate(async () => {
      const response = await fetch('/auth/bootstrap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'issue17-workspace', password: 'issue17 workspace passphrase' }),
      });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator('#username').fill('issue17-workspace');
    await page.locator('#password').fill('issue17 workspace passphrase');
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent#research');
    await page.locator('#library-nav-count').waitFor({ state: 'attached' });
    await check('research is the only visible primary workspace', async () => {
      assert(await page.locator('#screen-research').isVisible());
      assert(!await page.locator('#screen-library').isVisible());
      assert(!await page.locator('#screen-decisions').isVisible());
      assert(!await page.locator('#import-dialog').isVisible());
      await noOverflow();
    });
    await shot('research-desktop');
    await check('import is explicit and completes through the real API', async () => {
      await page.locator('.topbar [data-open-import]').click();
      await page.locator('#import-title').fill('中文检索研究记录');
      await page.locator('#import-text').fill('中文全文检索必须回查正式视图后，才能进入回答上下文。研究笔记📖：所有结论都需要回到原文核验。<script>window.untrustedRan=true</script>');
      await page.locator('#import-submit').click();
      await page.waitForFunction(() => location.hash === '#library');
      await page.getByRole('button', { name: '中文检索研究记录', exact: true }).waitFor({ timeout: 25000 });
      assert.equal(await page.evaluate(() => window.untrustedRan), undefined);
      assert(!await page.locator('#import-dialog').isVisible());
    });
    await shot('library-desktop');
    await check('library filtering and reader preserve context', async () => {
      await page.locator('#library-search').fill('不存在的资料标题');
      await page.waitForFunction(() => document.querySelectorAll('.material-row').length === 0);
      await page.getByRole('button', { name: '清除筛选', exact: true }).click();
      await page.getByRole('button', { name: '中文检索研究记录', exact: true }).click();
      await page.locator('#detail-text').filter({ hasText: '中文全文检索' }).waitFor();
      assert(await page.locator('#knowledge-detail').isVisible());
      assert.equal(await page.evaluate(() => window.untrustedRan), undefined);
      await page.keyboard.press('Escape');
      assert(!await page.locator('#knowledge-detail').isVisible());
      assert(await page.getByRole('button', { name: '中文检索研究记录', exact: true }).isVisible());
    });
    await page.locator('[data-nav="research"]').click();
    await page.locator('#question').fill('中文 全文 检索 正式 视图');
    await page.locator('#answer-form button[type="submit"]').click();
    await page.locator('#citations .source-card strong').filter({ hasText: '中文检索研究记录' }).first().waitFor({ timeout: 20000 });
    await check('citations open a separate reader with safe highlighted text', async () => {
      const answer = await page.locator('#answer').innerText();
      await page.locator('#citations .source-card').first().click();
      await page.locator('#citation-context-text mark').waitFor();
      assert((await page.locator('#citation-context-text mark').innerText()).length > 0);
      assert.equal(await page.locator('#answer').innerText(), answer);
      assert.equal(await page.evaluate(() => window.untrustedRan), undefined);
      await shot('citation-desktop');
      await page.keyboard.press('Escape');
      assert(!await page.locator('#citation-context').isVisible());
      assert.equal(await page.locator('#answer').innerText(), answer);
    });
    await check('hash navigation preserves question draft and answer', async () => {
      await page.locator('#question').fill('未提交的问题草稿');
      const answer = await page.locator('#answer').innerText();
      await page.locator('[data-nav="library"]').click();
      await page.locator('[data-nav="research"]').click();
      assert.equal(await page.locator('#question').inputValue(), '未提交的问题草稿');
      assert.equal(await page.locator('#answer').innerText(), answer);
    });
    await check('request failure preserves draft and prevents duplicate submissions', async () => {
      let count = 0;
      let release;
      const gate = new Promise((resolve) => { release = resolve; });
      await page.route('**/v1/answers', async (route) => { count += 1; await gate; await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"synthetic failure"}' }); });
      const submitted = page.waitForRequest('**/v1/answers');
      await page.locator('#answer-form button[type="submit"]').click();
      await submitted;
      assert(await page.locator('#answer-form button[type="submit"]').isDisabled());
      release();
      await page.locator('#question-error').waitFor({ state: 'visible' });
      assert.equal(count, 1);
      assert.equal(await page.locator('#question').inputValue(), '未提交的问题草稿');
      await page.unroute('**/v1/answers');
    });
    await check('missing evidence stays explicit without pretending to search online', async () => {
      await page.locator('#question').fill('qzxv742919 未记录的天文学结论');
      await page.locator('#answer-form button[type="submit"]').click();
      await page.locator('#answer-limits').waitFor({ state: 'visible' });
      assert((await page.locator('#answer-limits').innerText()).includes('联网搜索尚未接入'));
    });
    await check('comparison is a separate usable screen', async () => {
      await page.locator('[data-nav="decisions"]').click();
      await page.locator('#decision-problem').fill('中文全文检索使用哪种正式视图核验方案？');
      await page.locator('#decision-options').fill('方法 A：中文全文检索后回查正式视图\n方法 B：中文全文检索后直接使用结果');
      await page.locator('#decision-constraints').fill('两周内完成验证');
      await page.locator('#decision-form button[type="submit"]').click();
      await page.locator('#decision-result').waitFor({ state: 'visible', timeout: 20000 });
      assert((await page.locator('#decision-result').innerText()).length > 30);
      await noOverflow();
    });
    await shot('comparison-desktop');
    await page.locator('[data-nav="settings"]').click();
    await page.locator('#model-status').filter({ hasText: /配置|服务/ }).waitFor();
    await shot('settings-desktop');
    await check('secondary pages load through authenticated APIs', async () => {
      await page.goto(`${base}/memory-center`);
      await page.locator('#view-title').waitFor();
      await page.locator('#tab-formal').click();
      assert.equal(await page.locator('#tab-formal').getAttribute('aria-selected'), 'true');
      await noOverflow(); await shot('context-desktop');
      await page.goto(`${base}/evolution-center`);
      await page.locator('#refresh').waitFor();
      await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
      await noOverflow(); await shot('system-desktop');
    });
    await check('central review restores drafts and records real decisions', async () => {
      const source = await apiJson('/v1/conclusions/sources', { method: 'POST', body: { text: '浏览器验收原文：固定条件下复习有效。' } });
      const makeDraft = (claim) => apiJson('/v1/conclusions', { method: 'POST', body: {
        source_id: source.id, title: '浏览器审核草稿', claim, domain_id: 'education_learning',
        premises: [{ text: '固定条件', confirmed: false }], excerpt: source.text,
        evidence: [{ text: source.text }],
      } });
      const first = await makeDraft('固定条件下复习有效');
      const second = await makeDraft('固定条件下复习需要复核');
      const third = await makeDraft('固定条件下复习需要更多样本');
      const fourth = await makeDraft('固定条件下复习暂不确定');
      await page.goto(`${base}/review-center`);
      await page.locator('#total').filter({ hasText: /[2-9]/ }).waitFor();
      assert(await page.getByText('结论草稿').first().isVisible());
      await page.getByRole('button', { name: /浏览器审核草稿/ }).first().click();
      assert((await page.locator('#detail').innerText()).includes('固定条件'));
      await page.getByRole('button', { name: '稍后处理' }).click();
      await page.locator('#message').filter({ hasText: '操作已保存' }).waitFor();
      await page.locator('#queue').filter({ hasText: '浏览器审核草稿' }).waitFor();
      await page.reload();
      await page.locator('#total').waitFor();
      assert((await page.locator('#queue').innerText()).includes('浏览器审核草稿'));
      await page.getByRole('button', { name: /浏览器审核草稿/ }).last().click();
      await page.getByRole('button', { name: '批准' }).click();
      await page.locator('#message').filter({ hasText: '操作已保存' }).waitFor();
      assert.equal((await apiJson(`/v1/conclusions/${first.id}`)).status, 'formal');
      assert.equal((await apiJson(`/v1/conclusions/${second.id}`)).status, 'deferred');
      page.once('dialog', async (dialog) => { await dialog.accept('固定条件下复习需要更多样本（已修订）'); });
      await page.locator('.queue-item').filter({ hasText: '固定条件下复习需要更多样本' }).click();
      await page.getByRole('button', { name: '修订' }).click();
      await page.locator('#message').filter({ hasText: '操作已保存' }).waitFor();
      assert.equal((await apiJson(`/v1/conclusions/${third.id}`)).claim, '固定条件下复习需要更多样本（已修订）');
      await page.locator('.queue-item').filter({ hasText: '固定条件下复习暂不确定' }).click();
      await page.getByRole('button', { name: '拒绝' }).click();
      await page.locator('#message').filter({ hasText: '操作已保存' }).waitFor();
      assert.equal((await apiJson(`/v1/conclusions/${fourth.id}`)).status, 'rejected');
      await noOverflow(); await shot('review-desktop');
    });
    await check('conflict prompt remains actionable and unrelated answers continue', async () => {
      const update = (value) => apiJson('/v1/personal-updates', { method: 'POST', body: {
        memory_type: 'fact', state_key: 'profile.browser_city', value: { text: value }, source_kind: 'user_explicit',
      } });
      await update('北京'); await update('上海');
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator('#question').fill('我现在居住在哪个城市？');
      await page.locator('#answer-form button[type="submit"]').click();
      await page.locator('#context-prompt-dialog').waitFor({ state: 'visible', timeout: 20000 });
      assert((await page.locator('#context-prompt-values').innerText()).includes('冲突'));
      await page.getByRole('button', { name: '稍后处理' }).last().click();
      await page.locator('#toast').filter({ hasText: '待办' }).waitFor();
      await update('杭州'); await update('深圳');
      await page.locator('#question').fill('我现在居住在哪个城市？');
      await page.locator('#answer-form button[type="submit"]').click();
      await page.locator('#context-prompt-dialog').waitFor({ state: 'visible', timeout: 20000 });
      await page.getByRole('button', { name: '跳过' }).last().click();
      await page.locator('#toast').filter({ hasText: '提示已处理' }).waitFor();
      await page.locator('#question').fill('qzxv742919 未记录的天文学结论');
      await page.locator('#answer-form button[type="submit"]').click();
      await page.locator('#answer-limits').waitFor({ state: 'visible' });
      assert((await page.locator('#answer').innerText()).length > 0);
    });
    await check('every screen fits narrow viewports', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      for (const [name, route] of [['research','/knowledge-agent#research'],['library','/knowledge-agent#library'],['comparison','/knowledge-agent#decisions'],['settings','/knowledge-agent#settings'],['context','/memory-center'],['review','/review-center'],['system','/evolution-center'],['login','/login']]) {
        await page.goto(base + route);
        await page.locator('main').waitFor();
        await noOverflow(); await shot(`${name}-mobile`);
      }
    });
    assert.deepEqual(errors, [], 'no uncaught browser errors');
    fs.writeFileSync(path.join(output, 'checks.json'), JSON.stringify({ checks, browserErrors: errors }, null, 2));
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
