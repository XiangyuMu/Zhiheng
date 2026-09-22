/* Browser acceptance for owner-scoped conclusion qualification. */
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
  const page = await browser.newPage();
  const checks = [];
  await page.goto(`${base}/login`);
  await page.evaluate(async () => {
    const response = await fetch("/auth/bootstrap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }),
    });
    if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
  });
  await page.locator("#username").fill("issue17-workspace");
  await page.locator("#password").fill("issue17 workspace passphrase");
  await page.locator("#form button").click();
  await page.waitForURL("**/knowledge-agent");

  const result = await page.evaluate(async () => {
    const cookie = document.cookie.split("; ").find((value) => value.startsWith("zhiheng_csrf="));
    const csrf = cookie ? decodeURIComponent(cookie.slice("zhiheng_csrf=".length)) : "";
    const call = async (url, body, method = "POST") => {
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
    };
    const source = await call("/v1/conclusions/sources", { text: "Issue 17 qualification source" });
    const draft = await call("/v1/conclusions", {
      source_id: source.id,
      title: "Issue 17 qualification draft",
      claim: "only approved conclusions cross conversation boundaries",
      domain_id: "education_learning",
      premises: [{ text: "user approval", confirmed: false }],
      excerpt: source.text,
    });
    const before = await (await fetch(
      "/v1/conclusions/context?query=" + encodeURIComponent("cross conversation boundaries"),
      { credentials: "same-origin" },
    )).json();
    const approved = await call(`/v1/conclusions/${draft.id}/approve`, {});
    const after = await (await fetch(
      "/v1/conclusions/context?query=" + encodeURIComponent("cross conversation boundaries"),
      { credentials: "same-origin" },
    )).json();
    return { before: before.items, approved: approved.status, after: after.items };
  });

  assert.equal(result.before.length, 0);
  assert.equal(result.approved, "formal");
  assert.equal(result.after.length, 1);
  checks.push("unapproved conclusions stay out of cross-conversation context");
  await page.screenshot({ path: path.join(output, "qualification.png"), fullPage: true });
  fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ checks }, null, 2));
  console.log("PASS", checks[0]);
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
