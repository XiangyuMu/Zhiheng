/* Browser acceptance for the authenticated workspace shell and answer surface. */
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
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
  const errors = [];
  const checks = [];
  page.on("pageerror", (error) => errors.push(error.message));
  async function check(label, work) { await work(); checks.push(label); console.log("PASS", label); }
  async function noOverflow() { assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)); }
  try {
    await page.goto(`${base}/login?next=${encodeURIComponent('/knowledge-agent#research')}`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }) });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent#research");
    await check("authenticated research workspace loads", async () => {
      assert(await page.locator("#screen-research").isVisible());
      await noOverflow();
      await page.screenshot({ path: path.join(output, "research.png"), fullPage: true });
    });
    await check("taxonomy APIs are reachable from the authenticated browser", async () => {
      const result = await page.evaluate(async () => {
        const [domains, taxonomy] = await Promise.all([fetch("/v1/domains"), fetch("/v1/taxonomy")]);
        return { domains: domains.status, taxonomy: taxonomy.status };
      });
      assert.deepEqual(result, { domains: 200, taxonomy: 200 });
    });
    await check("classification migration workspace loads", async () => {
      await page.goto(`${base}/taxonomy-center`);
      await page.locator("main").waitFor();
      await noOverflow();
      await page.screenshot({ path: path.join(output, "taxonomy.png"), fullPage: true });
    });
    await page.goto(`${base}/knowledge-agent#research`);
    await page.locator("#question").fill("qzxv742919 未记录的天文学结论");
    await page.locator("#answer-form button[type='submit']").click();
    await page.locator("#answer-limits").waitFor({ state: "visible", timeout: 20000 });
    await check("missing evidence remains explicit", async () => {
      assert((await page.locator("#answer-limits").innerText()).includes("联网搜索尚未接入"));
    });
    await check("mobile workspace has no horizontal overflow", async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator("main").waitFor();
      await noOverflow();
      await page.screenshot({ path: path.join(output, "research-mobile.png"), fullPage: true });
    });
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ checks, browserErrors: errors }, null, 2));
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
