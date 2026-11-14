/* Issue #115: the weekly knowledge health report and change summary in the evolution center.
 *
 * Everything runs against the real API and Worker. The #113 stage already ran consolidation with
 * a clock 25 and 49 hours ahead, and its first run produced this user's first report. This stage
 * runs the Worker's consolidation step eight days ahead, so the next weekly report is due. The
 * week it covers lies ahead of the real clock, so tests/e2e/approve_during.py stands in for the
 * user approving this stage's own conclusions during that week. Later stages share this database,
 * so our formal knowledge is classified at the end.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback URL and output directory");
}
fs.mkdirSync(output, { recursive: true });

const ROOT = path.join(__dirname, "..", "..");
const PYTHON = fs.existsSync(path.join(ROOT, ".venv", "bin", "python"))
  ? path.join(ROOT, ".venv", "bin", "python") : "python3";
const HOUR = 3600 * 1000;
const RUN_AT = 8 * 24 * HOUR + 2 * HOUR;
const METRICS = [
  "duplicate_suggestions", "explicit_premise_share", "unresolved_conflicts",
  "unconfirmed_assumption_conclusions", "isolated_entries", "stale_volatile_memories",
  "handled_within_7_days_share",
];

function python(script, args) {
  return execFileSync(PYTHON, [path.join(__dirname, script), ...args], {
    cwd: ROOT, encoding: "utf8", env: process.env,
  });
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chromium" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const browserErrors = [];
  const evidence = {};
  const titles = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));

  async function api(url, options = {}) {
    return page.evaluate(async ({ url, options }) => {
      const csrf = document.cookie.split(";").map((item) => item.trim())
        .find((item) => item.startsWith("zhiheng_csrf="))?.slice("zhiheng_csrf=".length) || "";
      const response = await fetch(url, {
        credentials: "same-origin", ...options,
        headers: { Accept: "application/json", "Content-Type": "application/json",
          "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID(), "If-Match": "*",
          ...(options.headers || {}) },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      const text = await response.text();
      const body = text ? JSON.parse(text) : {};
      if (!response.ok) throw new Error(`${url}: ${response.status} ${body.detail || text}`);
      return body;
    }, { url, options });
  }

  async function formal(title, claim, premises) {
    const source = await api("/v1/conclusions/sources", { method: "POST", body: { text: `原文：${claim}。` } });
    const entry = await api("/v1/conclusions", { method: "POST", body: {
      source_id: source.id, title, claim, domain_id: "natural_sciences", premises, excerpt: claim,
    } });
    const approved = await api(`/v1/conclusions/${entry.id}/approve`, { method: "POST", body: {},
      headers: { "If-Match": entry.etag } });
    titles.push(title);
    return approved;
  }

  const pendingState = async () => ({
    counts: await api("/v1/review/counts"),
    items: (await api("/v1/review/items?deferred=include&limit=500")).items.map((item) => [item.type, item.id, item.etag]),
  });

  async function openHealth() {
    await page.goto(`${base}/evolution-center`);
    await page.locator("#health-body .health-metric").first().waitFor();
  }

  try {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }) });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");

    const before = (await api("/v1/knowledge-health/reports")).items;
    assert(before.length >= 1, "the #113 stage's run produced a first report");
    const previous = await api(`/v1/knowledge-health/reports/${before[0].id}`);

    const stated = await formal("issue115-stated", "苔原冻土在升温时释放甲烷",
      [{ text: "冻土持续升温", confirmed: true }]);
    const bare = await formal("issue115-bare", "火山灰可以短暂降低地表气温", []);
    const runAt = new Date(Date.now() + RUN_AT);
    python("approve_during.py", [new Date(runAt.getTime() - 24 * HOUR).toISOString(), stated.id, bare.id]);

    const run = JSON.parse(python("run_consolidation_at.py", [runAt.toISOString()]));
    assert.equal(run.ran, true, "a day after the #113 stage's last run, an idle run is due");
    assert.equal(run.health_reports_created, 1, "a week after the last report, the next one is due");
    const report = await api("/v1/knowledge-health/reports/latest");
    assert.equal(report.previous_report_id, previous.id);
    evidence.run = { ...run, report: report.id, previous: previous.id, period_end: report.period_end };

    // The change summary lists the knowledge approved in the week, with links to it.
    const pendingBefore = await pendingState();
    await openHealth();
    const added = page.locator(".change-list dd").first();
    for (const [title, entry] of [["issue115-stated", stated], ["issue115-bare", bare]]) {
      const link = added.getByRole("link", { name: title, exact: true });
      assert.equal(await link.count(), 1, `${title} is listed as added`);
      assert.equal(await link.getAttribute("href"), `/knowledge-agent?knowledge=${entry.knowledge_id}#library`);
    }
    const summary = await page.locator("#health-body").innerText();
    assert(summary.includes("新增正式知识"), "the change summary groups the new knowledge");
    evidence.summary = summary.slice(0, 600);

    // Every indicator shows its value and the change since the previous report.
    const cards = page.locator(".health-metric");
    assert.equal(await cards.count(), METRICS.length);
    for (const [index, name] of METRICS.entries()) {
      const metric = report.metrics[name];
      const text = await cards.nth(index).innerText();
      if (metric.change === null) {
        assert(text.includes("没有上期可比") || text.includes("不可比"), `${name} says it has nothing to compare`);
      } else if (metric.change === 0) {
        assert(text.includes("与上期持平"), `${name} says it is unchanged`);
      } else {
        assert(text.includes("较上期"), `${name} shows the change since the last report`);
      }
      assert.equal(metric.previous, previous.metrics[name].value, `${name} compares with the previous snapshot`);
    }
    const share = report.metrics.explicit_premise_share;
    assert(share.denominator >= 2 && share.numerator < share.denominator, "the bare conclusion lowers the share");
    await page.screenshot({ path: path.join(output, "health-report.png"), fullPage: true });
    evidence.metrics = Object.fromEntries(METRICS.map((name) => [name, report.metrics[name]]));

    // Suggestions link to 待处理 filtered to their type.
    const conflictLink = cards.nth(METRICS.indexOf("unresolved_conflicts")).getByRole("link", { name: "去待处理：结论冲突" });
    assert.equal(await conflictLink.getAttribute("href"), "/review-center?type=relation_suggestion");
    await conflictLink.click();
    await page.waitForURL("**/review-center?type=relation_suggestion");
    await page.locator('.type-filter[data-type="relation_suggestion"][aria-pressed="true"]').waitFor();

    // A finding links to its entry, and the link opens it.
    await openHealth();
    const finding = page.locator(".finding-list li").filter({ hasText: "issue115-bare" }).filter({ hasText: "缺少前提" });
    assert.equal(await finding.count(), 1, "the missing premise is a finding in the report");
    await finding.getByRole("link", { name: "issue115-bare" }).click();
    await page.waitForURL(`**/knowledge-agent?knowledge=${bare.knowledge_id}#library`);
    await page.locator("#knowledge-detail[open]").waitFor();
    await page.locator("#detail-title").filter({ hasText: "issue115-bare" }).waitFor();
    evidence.finding = { href: `/knowledge-agent?knowledge=${bare.knowledge_id}#library` };

    // Opening the reports changes nothing, and the earlier snapshot is still as it was.
    await openHealth();
    await page.locator("#health-report").selectOption(previous.id);
    await page.locator("#health-period").filter({ hasText: "覆盖" }).waitFor();
    await page.waitForFunction((id) => document.querySelector("#health-report").value === id, previous.id);
    assert.deepEqual(await pendingState(), pendingBefore, "opening a report changes no pending item");
    assert.deepEqual(await api(`/v1/knowledge-health/reports/${previous.id}`), previous,
      "the earlier snapshot reads as it did");
    assert.deepEqual(await api(`/v1/knowledge-health/reports/${report.id}`), report);

    await page.setViewportSize({ width: 390, height: 844 });
    await openHealth();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      "the report fits a narrow viewport");
    await page.screenshot({ path: path.join(output, "health-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });

    // Later stages share this database: classify our knowledge.
    for (const item of (await api("/v1/review/items?type=classification&limit=500")).items
      .filter((entry) => titles.includes(entry.title))) {
      await api(`/v1/knowledge/${item.id}/classifications`, { method: "PUT",
        body: { primary_domain_id: "natural_sciences", node_ids: [] } });
    }
    const left = (await api("/v1/review/items?deferred=include&limit=500")).items
      .filter((item) => titles.includes(item.title));
    assert.deepEqual(left, [], "the stage leaves nothing of its own in 待处理");

    assert.deepEqual(browserErrors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks: [
      "a weekly report covering the last seven days is generated in the background",
      "the change summary lists the knowledge added in the period with links to it",
      "every health indicator shows its value and the change since the previous snapshot",
      "suggestions link to the pending list filtered to their type",
      "findings in the report link to their entries",
      "opening reports changes no pending item and earlier snapshots stay unchanged",
      "the report fits a narrow viewport",
    ], evidence, browserErrors }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "failed", evidence,
      browserErrors, error: error.stack || String(error) }, null, 2));
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
