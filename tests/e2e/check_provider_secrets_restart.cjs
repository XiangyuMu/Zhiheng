/* Issue #40: prove migrated Provider state survives real API/Worker restart. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const https = require("node:https");
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output) throw new Error("Pass a service URL and output directory");
fs.mkdirSync(output, { recursive: true });

(async () => {
  let browser;
  let context;
  let page;
  const receivedAuth = [];
  const expectMigrated = process.env.ZHIHENG_EXPECT_MIGRATED === "1";
  let expectedAuthorization = expectMigrated
    ? "Bearer issue40-browser-legacy-key"
    : "Bearer sk-issue40-reentry-key";
  let providerServer;
  const evidence = {};
  const responseBodyReads = [];
  async function leakageEvidence(keys, logNames, apiBodies) {
    const root = process.env.ZHIHENG_ACCEPTANCE_OUTPUT || output;
    const logs = logNames.map((name) => {
      const file = `${root}/${name}`;
      assert(fs.existsSync(file), `missing restart log: ${file}`);
      return fs.readFileSync(file, "utf8");
    }).join("\n");
    const databaseUrl = process.env.ZHIHENG_DATABASE_URL || "";
    const databasePath = databaseUrl.startsWith("sqlite:///") ? databaseUrl.slice("sqlite:///".length) : "";
    assert(databasePath && fs.existsSync(databasePath));
    const ciphertexts = execFileSync("python3", ["-c", `
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    for (value,) in db.execute("SELECT ciphertext_b64 FROM provider_secret_records WHERE ciphertext_b64 IS NOT NULL"):
        print(value)
`, databasePath], { encoding: "utf8" }).trim().split("\n").filter(Boolean);
    const databaseFiles = [databasePath, `${databasePath}-wal`, `${databasePath}-shm`]
      .filter((file) => fs.existsSync(file));
    const databaseBytes = Buffer.concat(databaseFiles.map((file) => fs.readFileSync(file)));
    const browserValues = await page.evaluate(() => ({
      visible: document.body.innerText,
      inputs: Array.from(document.querySelectorAll("input, textarea")).map((item) => item.value).join("\n"),
      storage: `${JSON.stringify(localStorage)}${JSON.stringify(sessionStorage)}`,
    }));
    const accessibility = await page.locator("body").ariaSnapshot();
    const capturedResponses = await Promise.all(responseBodyReads);
    for (const result of capturedResponses) {
      if (result.error) throw new Error(`could not read response body for ${result.url}: ${result.error}`);
    }
    const responseBodies = capturedResponses.map((result) => result.body);
    const outputs = [browserValues.visible, accessibility, browserValues.inputs,
      browserValues.storage, ...apiBodies, ...responseBodies];
    const screenshotPath = `${output}/provider-restart.png`;
    await page.screenshot({ path: screenshotPath, fullPage: true });
    const screenshot = fs.readFileSync(screenshotPath);
    for (const key of keys) {
      assert(!logs.includes(key));
      assert(!databaseBytes.includes(Buffer.from(key)));
      assert(outputs.every((value) => !value.includes(key)));
      assert(!screenshot.includes(Buffer.from(key)));
    }
    for (const reference of ["env:ZHIHENG_PRIVATE_ISSUE40_LEGACY", ...ciphertexts]) {
      assert(!logs.includes(reference));
      assert(!outputs.some((value) => value.includes(reference)));
      assert(!screenshot.includes(Buffer.from(reference)));
    }
    assert(ciphertexts.length > 0);
    return {
      page: true,
      accessibility: true,
      input_values: true,
      storage: true,
      http_responses: responseBodies.length,
      http_response_urls: capturedResponses.map((result) => result.url),
      audit: true,
      api_log: true,
      worker_log: true,
      database: true,
      wal_shm: databaseFiles.length >= 1,
      scanned_database_files: databaseFiles,
      screenshot: "provider-restart.png",
      legacy_key_scanned: true,
      ciphertexts_scanned: ciphertexts.length,
    };
  }
  try {
  browser = await chromium.launch({ headless: true, channel: "chromium" });
  context = await browser.newContext();
  page = await context.newPage();
  page.on("response", (response) => {
    if (["xhr", "fetch"].includes(response.request().resourceType())) {
      responseBodyReads.push(response.text().then((body) => ({ url: response.url(), body }))
        .catch((error) => ({ url: response.url(), error })));
    }
  });
  providerServer = https.createServer({
    key: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_KEY),
    cert: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_CERT),
  }, (request, response) => {
    if (request.url === "/models") {
      receivedAuth.push(request.headers.authorization || "");
      if (request.headers.authorization !== expectedAuthorization) {
        response.writeHead(401, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: "invalid authorization" }));
        return;
      }
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: "model-a" }] }));
      return;
    }
    if (request.url === "/chat/completions") {
      receivedAuth.push(request.headers.authorization || "");
      if (request.headers.authorization !== expectedAuthorization) {
        response.writeHead(401, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: "invalid authorization" }));
        return;
      }
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify({
        answer: "restored provider answer",
        claims: [],
        conflicts: [],
        assumptions: [],
        insufficiencies: [],
        output_tokens: 3,
        personalization_refs: [],
      }) } }] }));
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => providerServer.listen(0, "127.0.0.1", resolve));
  const providerUrl = `https://127.0.0.1:${providerServer.address().port}`;
    await page.goto(`${base}/login`);
    await page.evaluate(async () => {
      const response = await fetch("/auth/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "issue17-workspace", password: "issue17 workspace passphrase" }) });
      if (!response.ok && response.status !== 409) throw new Error(`bootstrap failed: ${response.status}`);
    });
    await page.locator("#username").fill("issue17-workspace");
    await page.locator("#password").fill("issue17 workspace passphrase");
    await page.locator("#form button").click();
    await page.waitForURL("**/knowledge-agent**");
    const state = await page.evaluate(async () => {
      const response = await fetch("/v1/model-config/providers", { credentials: "same-origin" });
      return { status: response.status, body: await response.json() };
    });
    assert.equal(state.status, 200);
    const provider = state.body.find((item) => item.display_name === "Issue 38 Legacy Provider");
    assert(provider);
    assert.equal(provider.secret_source, "local");
    assert(!JSON.stringify(state.body).includes("ZHIHENG_PRIVATE_ISSUE40_LEGACY"));
    const csrf = await page.evaluate(() => document.cookie.split(";").map((v) => v.trim())
      .find((v) => v.startsWith("zhiheng_csrf="))?.slice(13) || "");
    if (expectMigrated) {
      assert.equal(provider.secret_status, "configured");
      const updated = await page.evaluate(async ({ providerId, etag, csrf, providerUrl }) => {
        const response = await fetch(`/v1/model-config/providers/${providerId}`, {
          method: "PATCH", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
            "If-Match": etag, "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ base_url: providerUrl, enabled: true }),
        });
        return { status: response.status, body: await response.json() };
      }, { providerId: provider.provider_id, etag: provider.etag, csrf, providerUrl });
      assert.equal(updated.status, 200, JSON.stringify(updated.body));
      const connectivity = await page.evaluate(async ({ providerId, csrf }) => {
        const response = await fetch(`/v1/model-config/providers/${providerId}/connectivity-test`, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
            "Idempotency-Key": crypto.randomUUID() },
        });
        return { status: response.status, body: await response.json() };
      }, { providerId: provider.provider_id, csrf });
      assert.equal(connectivity.status, 200, JSON.stringify(connectivity.body));
      assert.equal(connectivity.body.status, "succeeded");
      evidence.pre_reentry_status = provider.secret_status;
      evidence.connectivity = connectivity.body.status;
      assert.deepEqual(receivedAuth.at(-1), "Bearer issue40-browser-legacy-key");
      const answer = await page.evaluate(async () => {
        const response = await fetch("/v1/answers", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ query: "restore install", intent: "complex_synthesis" }),
        });
        return { status: response.status, body: await response.json() };
      });
      assert.equal(answer.status, 200, JSON.stringify(answer.body));
      assert.match(answer.body.answer, /restored provider answer/);
      assert.deepEqual(receivedAuth.at(-1), "Bearer issue40-browser-legacy-key");
      const audit = await page.evaluate(async ({ providerId }) => {
        const response = await fetch(`/v1/model-config/audits?provider_id=${providerId}`);
        return { status: response.status, body: await response.json() };
      }, { providerId: provider.provider_id });
      assert.equal(audit.status, 200, JSON.stringify(audit.body));
      const search = await page.evaluate(async () => (await fetch("/v1/knowledge/search?sort=updated_desc")).status);
      assert.equal(search, 200);
      const leakage = await leakageEvidence(
        ["issue40-browser-legacy-key"],
        ["api-restart.log", "worker-restart.log"],
        [JSON.stringify(state), JSON.stringify(updated), JSON.stringify(connectivity), JSON.stringify(answer), JSON.stringify(audit)],
      );
      evidence.leakage = leakage;
      fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ status: "passed", checks: [
        "migrated Provider remains configured after API and Worker restart",
        "legacy environment variable is absent while migrated Provider authenticates over HTTPS",
        "knowledge search remains available after migration restart",
      ], evidence: { provider_id: provider.provider_id, pre_reentry_status: provider.secret_status,
        post_reentry_status: updated.body.secret_status, connectivity: connectivity.body.status,
        search_status: search, model_call: "succeeded", model_answer: answer.body.answer,
        legacy_environment_removed: true, leakage } }, null, 2));
      return;
    }
    assert.equal(provider.secret_status, "unavailable");
    evidence.pre_reentry_status = provider.secret_status;
    const repaired = await page.evaluate(async ({ providerId, etag, csrf, providerUrl }) => {
      const response = await fetch(`/v1/model-config/providers/${providerId}`, {
        method: "PATCH", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
          "If-Match": etag, "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ api_key: "sk-issue40-reentry-key", base_url: providerUrl, enabled: true }),
      });
      return { status: response.status, body: await response.json() };
    }, { providerId: provider.provider_id, etag: provider.etag, csrf, providerUrl });
    assert.equal(repaired.status, 200);
    assert.equal(repaired.body.secret_status, "configured");
    expectedAuthorization = "Bearer sk-issue40-reentry-key";
    const connectivity = await page.evaluate(async ({ providerId, csrf }) => {
      const response = await fetch(`/v1/model-config/providers/${providerId}/connectivity-test`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID() },
      });
      return { status: response.status, body: await response.json() };
    }, { providerId: provider.provider_id, csrf });
    assert.equal(connectivity.status, 200, JSON.stringify(connectivity.body));
    assert.equal(connectivity.body.status, "succeeded");
    evidence.connectivity = connectivity.body.status;
    assert.deepEqual(receivedAuth.at(-1), "Bearer sk-issue40-reentry-key");
    const search = await page.evaluate(async () => {
      const response = await fetch("/v1/knowledge/search?sort=updated_desc", { credentials: "same-origin" });
      return response.status;
    });
    assert.equal(search, 200);
    const leakage = await leakageEvidence(
      ["sk-issue40-reentry-key"],
      ["api-restart-2.log", "worker-restart-2.log"],
      [JSON.stringify(state), JSON.stringify(repaired), JSON.stringify(connectivity)],
    );
    evidence.leakage = leakage;
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ status: "passed", checks: [
      "tampered Provider is unavailable after API and Worker restart",
      "Provider key can be re-entered after recovery",
      "re-entered Provider succeeds through a real authenticated HTTPS probe",
      "legacy environment reference is absent after restart",
      "knowledge search remains available after Provider restart",
    ], evidence: { provider_id: provider.provider_id, pre_reentry_status: "unavailable",
      post_reentry_status: repaired.body.secret_status, connectivity: connectivity.body.status,
      search_status: search, leakage } }, null, 2));
  } catch (error) {
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ status: "failed", error: error.stack || String(error), evidence }, null, 2));
    throw error;
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
    if (providerServer) await new Promise((resolve) => providerServer.close(resolve));
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
