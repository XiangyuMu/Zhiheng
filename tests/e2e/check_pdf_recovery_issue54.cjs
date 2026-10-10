/* Real-process PDF recovery acceptance for Issue #54.
 *
 * Usage:
 *   ZHIHENG_TEST_USERNAME=... ZHIHENG_TEST_PASSWORD=... \
 *   ZHIHENG_RESTART_WORKER_COMMAND='...' \
 *   ZHIHENG_RESTART_GATEWAY_COMMAND='...' \
 *   node tests/e2e/check_pdf_recovery_issue54.cjs URL OUTPUT_DIR PDF
 *
 * The restart commands are deliberately supplied by the deployment harness;
 * this script never replaces them with an in-process fake Worker or gateway.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const [base, output, file] = process.argv.slice(2);
assert(base && output && file, 'Expected URL, output directory and PDF');
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname), 'Local service only');
fs.mkdirSync(output, { recursive: true, mode: 0o700 });

const report = {
  started: new Date().toISOString(),
  scope: 'issue54-real-process-recovery',
  sha: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
  dirty: Boolean(execFileSync('git', ['status', '--porcelain'], { encoding: 'utf8' }).trim()),
  input: {
    name: path.basename(file),
    sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),
  },
  restarts: [],
  states: [],
  status: 'running',
  exit_code: null,
};
const save = () => fs.writeFileSync(
  path.join(output, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 },
);

function restart(kind, command) {
  const started = Date.now();
  const safeName = `${kind}-${report.restarts.length + 1}`;
  const commandDigest = crypto.createHash('sha256').update(command).digest('hex');
  const stdoutPath = path.join(output, `${safeName}.stdout.log`);
  const stderrPath = path.join(output, `${safeName}.stderr.log`);
  const identityCommand = kind === 'worker'
    ? process.env.ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND
    : process.env.ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND;
  const identity = () => execFileSync('bash', ['-lc', identityCommand], {
    encoding: 'utf8', stdio: 'pipe', timeout: 30_000,
  }).trim();
  const beforeIdentity = identity();
  assert(beforeIdentity, `${kind} identity probe returned empty output`);
  const writeLog = (file, value) => fs.writeFileSync(file, String(value || '').replace(/(token|key|secret|password)=\S+/gi, '$1=[REDACTED]'), { mode: 0o600 });
  try {
    const stdout = execFileSync('bash', ['-lc', command], { encoding: 'utf8', stdio: 'pipe', timeout: 120_000 });
    writeLog(stdoutPath, stdout);
    writeLog(stderrPath, '');
    const afterIdentity = identity();
    assert(afterIdentity && afterIdentity !== beforeIdentity, `${kind} identity did not change after restart`);
    report.restarts.push({ kind, status: 'succeeded', exit_code: 0, command_sha256: commandDigest, before_identity_sha256: crypto.createHash('sha256').update(beforeIdentity).digest('hex'), after_identity_sha256: crypto.createHash('sha256').update(afterIdentity).digest('hex'), elapsed_ms: Date.now() - started, stdout_path: stdoutPath, stderr_path: stderrPath });
  } catch (error) {
    writeLog(stdoutPath, error.stdout);
    writeLog(stderrPath, error.stderr || error.message);
    report.restarts.push({ kind, status: 'failed', exit_code: typeof error.status === 'number' ? error.status : null, command_sha256: commandDigest, elapsed_ms: Date.now() - started, stdout_path: stdoutPath, stderr_path: stderrPath, timed_out: error.killed === true });
    save();
    throw error;
  }
  save();
}

(async () => {
  let browser;
  try {
    assert(process.env.ZHIHENG_TEST_USERNAME && process.env.ZHIHENG_TEST_PASSWORD, 'Missing credentials');
    assert(process.env.ZHIHENG_RESTART_WORKER_COMMAND, 'Missing Worker restart command');
    assert(process.env.ZHIHENG_RESTART_GATEWAY_COMMAND, 'Missing gateway restart command');
    assert(process.env.ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND, 'Missing Worker identity command');
    assert(process.env.ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND, 'Missing gateway identity command');
    assert.equal(report.dirty, false, 'Recovery evidence requires a clean checkout');
    if (process.env.ZHIHENG_EXPECTED_SHA) assert.equal(report.sha, process.env.ZHIHENG_EXPECTED_SHA);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.goto(`${base}/login?next=${encodeURIComponent('/knowledge-agent#library')}`);
    await page.locator('#username').fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator('#password').fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent*');
    report.login = true;
    await page.locator('[data-open-import]:visible').first().click();
    await page.locator('#import-file').setInputFiles(file);
    await page.locator('#import-title').fill(`Issue 54 recovery ${crypto.randomUUID()}`);
    const receiptPromise = page.waitForResponse(
      response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/v1/knowledge/pdf-imports',
      { timeout: 60_000 },
    );
    await page.locator('#import-submit').click();
    const receipt = await receiptPromise;
    assert.equal(receipt.status(), 202);
    const receiptBody = await receipt.json();
    assert(receiptBody.task_id && receiptBody.evidence_object_id);
    assert.equal(receiptBody.source_sha256, report.input.sha256);
    report.receipt = {
      task_id: receiptBody.task_id,
      evidence_object_id: receiptBody.evidence_object_id,
      source_sha256: receiptBody.source_sha256,
    };
    const taskId = receiptBody.task_id;
    let firstAttempt;
    let workerRestarted = false;
    let gatewayRestarted = false;
    const deadline = Date.now() + 300_000;
    while (Date.now() < deadline) {
      const response = await page.evaluate(async id => {
        const result = await fetch(`/v1/knowledge/pdf-imports/${encodeURIComponent(id)}`);
        return { http: result.status, body: await result.json() };
      }, taskId);
      assert.equal(response.http, 200);
      const state = response.body;
      assert.equal(state.task_id, taskId);
      assert.equal(state.evidence_object_id, receiptBody.evidence_object_id);
      assert.equal(state.source_sha256, report.input.sha256);
      if (state.attempt_id) firstAttempt ||= state.attempt_id;
      if (firstAttempt && state.attempt_id) assert.equal(state.attempt_id, firstAttempt);
      report.states.push({ at: new Date().toISOString(), ...response });
      save();
      if (!workerRestarted && state.state === 'processing' && state.attempt_id) {
        restart('worker', process.env.ZHIHENG_RESTART_WORKER_COMMAND);
        workerRestarted = true;
        await page.screenshot({path: path.join(output, 'after-worker-restart.png'), fullPage: true});
      }
      if (workerRestarted && !gatewayRestarted && state.state === 'processing' && state.attempt_id) {
        restart('gateway', process.env.ZHIHENG_RESTART_GATEWAY_COMMAND);
        gatewayRestarted = true;
        await page.screenshot({path: path.join(output, 'after-gateway-restart.png'), fullPage: true});
      }
      if (['failed', 'partial', 'unsupported', 'dead'].includes(state.state)) {
        throw Error(`recovery reached terminal failure: ${state.state} ${state.error_code || ''}`);
      }
      if (state.state === 'parsed') break;
      await page.waitForTimeout(1_000);
    }
    assert(workerRestarted && gatewayRestarted, 'both real process restart commands must run');
    assert(firstAttempt, 'the original parser attempt must be persisted before restart');
    assert(report.states.some(item => item.body.state === 'parsed'), 'task did not recover to parsed');
    assert(report.states.filter(item => item.body.state === 'parsed').every(item => item.body.attempt_id === firstAttempt), 'recovery changed parser attempt identity');
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.failure = String(error.stack || error);
    if (browser) await browser.pages()[0].screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    report.finished = new Date().toISOString();
    report.exit_code = process.exitCode || 0;
    save();
    if (browser) await browser.close();
  }
})();
