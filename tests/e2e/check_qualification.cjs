/* Browser acceptance for owner-scoped conclusion qualification across sessions. */
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
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  const contextA = await browser.newContext();
  const contextB = await browser.newContext();
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  const checks = [];

  async function login(page) {
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-qualification", password: "issue17 workspace passphrase" }),
      });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-qualification");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent");
  }

  async function call(page, url, body, method = "POST") {
    return page.evaluate(async ({ url, body, method }) => {
      const cookie = document.cookie.split("; ").find((value) => value.startsWith("zhiheng_csrf="));
      const csrf = cookie ? decodeURIComponent(cookie.slice("zhiheng_csrf=".length)) : "";
      const response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": "*",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(`${response.status}: ${JSON.stringify(payload)}`);
      return payload;
    }, { url, body, method });
  }

  async function contextItems(page) {
    return page.evaluate(async () => {
      const response = await fetch(
        "/v1/conclusions/context?query=" + encodeURIComponent("cross conversation boundaries"),
        { credentials: "same-origin" },
      );
      if (!response.ok) throw new Error(`context failed: ${response.status}`);
      return (await response.json()).items;
    });
  }

  try {
    await login(pageA);
    await login(pageB);
    const source = await call(pageA, "/v1/conclusions/sources", { text: "Issue 17 qualification source" });
    const draft = await call(pageA, "/v1/conclusions", {
      source_id: source.id,
      title: "Issue 17 qualification draft",
      claim: "only approved conclusions cross conversation boundaries",
      domain_id: "education_learning",
      premises: [{ text: "user approval", confirmed: false }],
      excerpt: source.text,
    });

    assert.equal((await contextItems(pageA)).length, 0);
    assert.equal((await contextItems(pageB)).length, 0);
    checks.push("unapproved conclusions stay out of both browser sessions");

    const approved = await call(pageA, `/v1/conclusions/${draft.id}/approve`, {});
    assert.equal(approved.status, "formal");
    assert.equal((await contextItems(pageB)).length, 1);
    checks.push("approved conclusions become visible in a separate browser session");

    await pageB.screenshot({ path: path.join(output, "qualification-second-session.png"), fullPage: true });
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ checks }, null, 2));
    console.log("PASS", checks.join("; "));
  } finally {
    await contextA.close();
    await contextB.close();
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
