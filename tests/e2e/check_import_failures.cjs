/* Browser acceptance for import failure and polling semantics.
 *
 * The import task is created through the real rendered page and real API.
 * Only that task's status endpoint is intercepted to make transport and
 * terminal states deterministic. Evidence names the controlled injection so
 * it is not confused with a Worker-produced failure.
 *
 * node tests/e2e/check_import_failures.cjs LOOPBACK_URL OUTPUT_DIR
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback server URL and screenshot output directory.");
}
fs.mkdirSync(output, { recursive: true });

const scenarios = [
  {
    name: "failed",
    title: "浏览器失败状态",
    status: "failed",
    code: "parse_failed",
    reason: "原始资料无法解析，请重试或补充资料。",
    retryable: true,
  },
  {
    name: "unsupported",
    title: "浏览器不支持状态",
    status: "unsupported",
    code: "unsupported_media_type",
    reason: "当前服务版本不支持该资料类型。",
    retryable: false,
  },
  {
    name: "partial",
    title: "浏览器部分完成状态",
    status: "partial",
    code: "partial_import",
    reason: "部分页面未能处理，请补充资料。",
    retryable: false,
  },
  {
    name: "not_found",
    title: "浏览器版本不支持状态查询",
    transport: "404",
  },
  {
    name: "server_error",
    title: "浏览器服务错误重试",
    transport: "503",
  },
  {
    name: "network_error",
    title: "浏览器网络错误重试",
    transport: "network",
  },
];

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.BROWSER_CHANNEL || "chromium",
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const checks = [];
  const evidence = {
    mode: "real_page_and_real_import_api_with_controlled_status_endpoint_injection",
    scenarios: {},
  };
  const errors = [];
  const routeStates = new Map();
  let activeScenario = null;

  page.on("pageerror", (error) => errors.push(error.message));

  await page.route("**/v1/knowledge/*/processing", async (route) => {
    const requestUrl = new URL(route.request().url());
    const id = requestUrl.pathname.split("/").at(-2);
    if (!activeScenario && !routeStates.has(id)) {
      await route.continue();
      return;
    }
    if (!routeStates.has(id)) {
      routeStates.set(id, { scenario: activeScenario, calls: 0 });
    }
    const state = routeStates.get(id);
    state.calls += 1;
    const scenario = state.scenario;

    if (scenario.transport === "404") {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "processing status endpoint is unavailable" }),
      });
      return;
    }
    if (scenario.transport === "503" && state.calls <= 3) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "synthetic upstream failure" }),
      });
      return;
    }
    if (scenario.transport === "network" && state.calls <= 3) {
      await route.abort("failed");
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        public_status: scenario.status,
        status: scenario.status,
        searchable: false,
        retryable: Boolean(scenario.retryable),
        error_code: scenario.code,
        redacted_summary: scenario.reason,
        etag: `knowledge-job:controlled-${id}`,
      }),
    });
  });

  function writeChecks(status, error) {
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      status,
      checks,
      evidence,
      browserErrors: errors,
      error: error ? error.stack || String(error) : undefined,
    }, null, 2));
  }

  async function shot(name) {
    await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true });
  }

  async function login() {
    await page.goto(`${base}/login?next=${encodeURIComponent("/knowledge-agent#research")}`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: "issue17-workspace",
          password: "issue17 workspace passphrase",
        }),
      });
      if (!response.ok && response.status !== 409) {
        throw new Error(`bootstrap failed: ${response.status}`);
      }
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent#research");
  }

  async function createImport(scenario) {
    activeScenario = scenario;
    await page.locator(".topbar [data-open-import]").first().click();
    await page.locator("#import-title").fill(scenario.title);
    await page.locator("#import-text").fill(
      `${scenario.title} 的真实页面失败状态验收原文。`,
    );
    await page.locator("#import-submit").click();
    await page.waitForFunction(() => location.hash === "#library");
    const row = page.locator(".processing-item").filter({ hasText: scenario.title }).first();
    await row.waitFor({ timeout: 20000 });
    activeScenario = null;
    return row;
  }

  async function waitForCalls(scenario, expected, timeout = 10000) {
    const started = Date.now();
    let task;
    while (Date.now() - started < timeout) {
      task = [...routeStates.values()].find((item) => item.scenario === scenario);
      if (task && task.calls >= expected) break;
      await page.waitForTimeout(100);
    }
    assert(task, `no intercepted status request for ${scenario.name}`);
    assert.equal(
      task.calls,
      expected,
      `${scenario.name} must stop after ${expected} status requests`,
    );
    await page.waitForTimeout(2200);
    assert.equal(
      task.calls,
      expected,
      `${scenario.name} must remain quiet after reaching ${expected} status requests`,
    );
  }

  try {
    await login();
    for (const scenario of scenarios) {
      const row = await createImport(scenario);
      if (scenario.status) {
        await row.getByText(new RegExp(`${scenario.code}：${scenario.reason}`)).waitFor({
          timeout: 20000,
        });
        assert(await row.getByRole("button", { name: "补充资料" }).isVisible());
        if (scenario.retryable) {
          assert(await row.getByRole("button", { name: "重试" }).isVisible());
        }
        assert(!await row.getByText("可检索", { exact: true }).isVisible());
        const task = [...routeStates.values()].find((item) => item.scenario === scenario);
        assert.equal(task.calls, 1);
        evidence.scenarios[scenario.name] = {
          state: scenario.status,
          error_code: scenario.code,
          reason: scenario.reason,
          retryable: scenario.retryable,
          status_requests: task.calls,
          rendered_searchable: false,
          response_injection: "controlled_status_endpoint",
        };
      } else if (scenario.transport === "404") {
        await row.getByText("当前服务版本不支持该任务状态").waitFor({ timeout: 20000 });
        await page.waitForTimeout(800);
        await waitForCalls(scenario, 1);
        evidence.scenarios[scenario.name] = {
          status_requests: 1,
          polling_stopped: true,
          message: "当前服务版本不支持该任务状态",
        };
      } else {
        await row.getByText("暂时无法确认状态").waitFor({ timeout: 20000 });
        await waitForCalls(scenario, 3, 10000);
        evidence.scenarios[scenario.name] = {
          status_requests: 3,
          polling_stopped: true,
          message: "暂时无法确认状态",
          transport: scenario.transport,
        };
      }
      checks.push(`${scenario.name} status is rendered with bounded polling and recovery semantics`);
      await shot(scenario.name);
    }
    assert.deepEqual(errors, [], "no uncaught browser errors");
    writeChecks("passed");
  } catch (error) {
    await shot("failure").catch(() => {});
    writeChecks("failed", error);
    throw error;
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
