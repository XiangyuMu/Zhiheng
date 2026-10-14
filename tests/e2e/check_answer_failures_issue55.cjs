/* Issue #55: real browser proof that answer model failures fail closed.
 *
 * The Provider fixture below is an injected failure boundary. It never returns
 * a successful answer. The browser still submits through the rendered page and
 * the API still dispatches through the configured Provider transport.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const https = require("node:https");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const base = process.argv[2];
const output = process.argv[3];
if (!base || !output || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) {
  throw new Error("Pass a disposable loopback server URL and output directory");
}
fs.mkdirSync(output, { recursive: true, mode: 0o700 });

const injectedAnswers = {
  invalid_output: "ISSUE55_INVALID_OUTPUT_SHOULD_NOT_RENDER",
  transport_failure: "ISSUE55_503_SHOULD_NOT_RENDER",
};

function gitSha() {
  try {
    return execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  } catch (_) {
    return "unknown";
  }
}

function auditDiagnostic(auditId) {
  const databaseUrl = process.env.ZHIHENG_DATABASE_URL || "";
  const databasePath = databaseUrl.startsWith("sqlite:///")
    ? databaseUrl.slice("sqlite:///".length)
    : "";
  if (!auditId || !databasePath || !fs.existsSync(databasePath)) return null;
  const script = `
import json, sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT id, provider_id, model_id, status, diagnostic_code FROM model_call_audits WHERE id=?",
        (sys.argv[2],),
    ).fetchone()
print(json.dumps(dict(row) if row else None))
`;
  return JSON.parse(execFileSync("python3", ["-c", script, databasePath, auditId], {
    encoding: "utf8",
  }));
}

(async () => {
  let browser;
  let context;
  let providerServer;
  const calls = [];
  const browserErrors = [];
  const report = {
    status: "failed",
    sha: gitSha(),
    injected: true,
    boundary: "model_transport",
    cases: [],
    browserErrors,
  };

  function writeReport(extra = {}) {
    const reportPath = path.join(output, "report.json");
    fs.writeFileSync(reportPath, JSON.stringify({ ...report, ...extra }, null, 2), { mode: 0o600 });
    fs.chmodSync(reportPath, 0o600);
  }

  try {
    providerServer = https.createServer({
      key: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_KEY),
      cert: fs.readFileSync(process.env.ZHIHENG_ACCEPTANCE_PROVIDER_CERT),
    }, (request, response) => {
      if (request.method === "GET" && request.url === "/models") {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ data: [{ id: "issue55-failure-model" }] }));
        return;
      }
      if (request.method === "POST" && request.url === "/chat/completions") {
        let body = "";
        request.setEncoding("utf8");
        request.on("data", (chunk) => { body += chunk; });
        request.on("end", () => {
          const kind = body.includes("issue55-invalid-output")
            ? "invalid_output"
            : body.includes("issue55-transport-failure")
              ? "transport_failure"
              : "unknown";
          calls.push({
            kind,
            authorization: request.headers.authorization || "",
            body_sha256: require("node:crypto").createHash("sha256").update(body).digest("hex"),
          });
          if (kind === "invalid_output") {
            response.writeHead(200, { "content-type": "application/json" });
            response.end(JSON.stringify({
              choices: [{ message: { content: JSON.stringify({
                answer: injectedAnswers.invalid_output,
                claims: [],
              }) } }],
            }));
            return;
          }
          response.writeHead(503, { "content-type": "application/json" });
          response.end(JSON.stringify({
            error: "issue55 injected provider unavailable",
            marker: injectedAnswers.transport_failure,
          }));
        });
        return;
      }
      response.writeHead(404, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: "not found" }));
    });
    await new Promise((resolve) => providerServer.listen(0, "127.0.0.1", resolve));
    const providerUrl = `https://127.0.0.1:${providerServer.address().port}`;

    browser = await chromium.launch({
      headless: true,
      channel: process.env.BROWSER_CHANNEL || "chromium",
    });
    context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
    const page = await context.newPage();
    page.on("pageerror", (error) => browserErrors.push(error.message));

    await page.goto(`${base}/login`);
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
    await page.waitForURL("**/knowledge-agent**");

    async function api(url, options = {}) {
      return page.evaluate(async ({ url, options }) => {
        const csrf = document.cookie.split(";").map((value) => value.trim())
          .find((value) => value.startsWith("zhiheng_csrf="))?.slice(13) || "";
        const response = await fetch(url, {
          credentials: "same-origin",
          ...options,
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "If-Match": "*",
            "Idempotency-Key": crypto.randomUUID(),
            ...(options.headers || {}),
          },
          body: options.body === undefined ? undefined : JSON.stringify(options.body),
        });
        const raw = await response.text();
        let body = {};
        try { body = raw ? JSON.parse(raw) : {}; } catch (_) { body = { raw }; }
        return { status: response.status, ok: response.ok, body };
      }, { url, options });
    }
    async function ok(url, options = {}) {
      const result = await api(url, options);
      if (!result.ok) throw new Error(`${result.status}: ${JSON.stringify(result.body)}`);
      return result.body;
    }

    const created = await ok("/v1/model-config/providers", {
      method: "POST",
      body: {
        provider_kind: "openai-compatible",
        display_name: `Issue 55 Failure Provider ${Date.now()}`,
        base_url: providerUrl,
        api_key: "sk-issue55-injected-failure",
        enabled: true,
      },
    });
    const providerId = created.provider_id;
    const modelId = "issue55-failure-model";
    const modelCreated = await api(`/v1/model-config/providers/${providerId}/models`, {
      method: "POST",
      body: { model_id: modelId, protocol: "chat_completions" },
    });
    assert.equal(modelCreated.status, 200, JSON.stringify(modelCreated.body));
    const confirmed = await api(`/v1/model-config/providers/${providerId}/models/${modelId}`, {
      method: "PATCH",
      headers: { "If-Match": created.etag },
      body: { confirmed_capabilities: ["text"], protocol: "chat_completions" },
    });
    assert.equal(confirmed.status, 200, JSON.stringify(confirmed.body));
    const status = await ok("/v1/model-config/status");
    const defaults = await api("/v1/model-config/defaults", {
      method: "PUT",
      headers: { "If-Match": status.defaults.etag },
      body: { text: { provider_id: providerId, model_id: modelId } },
    });
    assert.equal(defaults.status, 200, JSON.stringify(defaults.body));

    async function ask(kind, expectedStopReason, expectedDiagnostic, expectedAuditStatus, expectedUiLabel) {
      const before = calls.length;
      const marker = kind === "invalid_output"
        ? "issue55-invalid-output"
        : "issue55-transport-failure";
      const query = `中文全文检索 正式视图 原文核验 ${marker}`;
      await page.goto(`${base}/knowledge-agent#research`);
      await page.locator("#question").fill(query);
      const responsePromise = page.waitForResponse((response) => (
        response.url().endsWith("/v1/answers")
        && response.request().method() === "POST"
      ));
      await page.locator("#answer-form button[type=submit]").click();
      const response = await responsePromise;
      assert.equal(response.status(), 200, `${kind} answer HTTP status`);
      const body = await response.json();
      await page.waitForFunction(({ query, stopLabel, answer }) => (
        document.querySelector("#answer-result")?.hidden === false
        && document.querySelector("#asked-question")?.textContent === query
        && document.querySelector("#stop-reason")?.textContent === stopLabel
        && document.querySelector("#answer")?.textContent === answer
      ), { query, stopLabel: expectedUiLabel, answer: body.answer }, { timeout: 20_000 });
      const uiStopReason = (await page.locator("#stop-reason").innerText()).trim();
      const answerText = await page.locator("#answer").innerText();
      const limitText = await page.locator("#answer-limits").innerText().catch(() => "");
      const screenshot = `${kind}.png`;
      await page.screenshot({ path: path.join(output, screenshot), fullPage: true });
      const providerDispatches = calls.slice(before);
      const dispatchCount = providerDispatches.length;
      assert(providerDispatches.every((item) => item.kind === kind), `${kind} dispatch kind mismatch`);
      const audit = auditDiagnostic(body.answer_audit_id);
      const diagnosticCode = body.answer_safety_reason_code || audit?.diagnostic_code || null;
      assert.equal(body.stop_reason, expectedStopReason, JSON.stringify(body));
      assert.equal(body.answer_provider_id, providerId, JSON.stringify(body));
      assert.equal(body.answer_model_id, modelId, JSON.stringify(body));
      assert(body.answer_audit_id, `${kind} missing answer_audit_id`);
      assert.equal(dispatchCount, 1, `${kind} dispatch count`);
      assert(audit, `${kind} missing model_call_audits row`);
      assert.equal(audit.id, body.answer_audit_id, `${kind} audit id`);
      assert.equal(audit.provider_id, providerId, `${kind} audit provider`);
      assert.equal(audit.model_id, modelId, `${kind} audit model`);
      assert.equal(audit.status, expectedAuditStatus, `${kind} audit status`);
      assert.equal(diagnosticCode, expectedDiagnostic, `${kind} diagnostic code`);
      assert.equal(uiStopReason, expectedUiLabel, `${kind} UI stop reason`);
      assert.equal(answerText, body.answer, `${kind} UI answer text`);
      assert(!answerText.includes(injectedAnswers[kind]), `${kind} leaked injected answer`);
      assert(!limitText.includes(injectedAnswers[kind]), `${kind} leaked injected marker`);
      assert(uiStopReason.length > 0, `${kind} missing UI stop reason`);
      assert.equal(await page.locator("#answer-result").isVisible(), true);
      const caseReport = {
        kind,
        scope_type: kind === "invalid_output"
          ? "model_output_validation"
          : "provider_transport_http",
        status: "passed",
        stop_reason: body.stop_reason,
        diagnostic_code: diagnosticCode,
        audit_id: body.answer_audit_id,
        provider_id: body.answer_provider_id,
        model_id: body.answer_model_id,
        dispatch_count: dispatchCount,
        ui_visible: true,
        ui_stop_reason_label: uiStopReason,
        screenshot,
        answer_text: answerText,
        audit_status: audit?.status || null,
        fabricated_answer_absent: true,
      };
      report.cases.push(caseReport);
      return caseReport;
    }

    await ask(
      "invalid_output",
      "invalid_model_output",
      "answer_model_invalid_output",
      "succeeded",
      "回答未通过校验",
    );
    await ask(
      "transport_failure",
      "model_failed",
      "provider_error",
      "failed",
      "回答暂不可用",
    );
    assert.equal(browserErrors.length, 0, browserErrors.join("; "));
    report.status = "passed";
    report.provider_url = providerUrl;
    report.calls = calls;
    writeReport();
  } catch (error) {
    writeReport({ failure: error.stack || String(error), calls });
    throw error;
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
    if (providerServer) await new Promise((resolve) => providerServer.close(resolve));
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
