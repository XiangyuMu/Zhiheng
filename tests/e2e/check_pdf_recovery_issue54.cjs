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
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const {
  snapshotHasActiveLease,
  validateFaultCommandProof,
  validateFaultScenarios,
  validateParserActiveProof,
  validateRestartCommandProof,
} = require('./pdf_recovery_contract.cjs');

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
  db_snapshots: [],
  fault_injections: [],
  regression_references: [],
  states: [],
  timings: {},
  status: 'running',
  exit_code: null,
};
const save = () => fs.writeFileSync(
  path.join(output, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 },
);

function commandEnv(identity, extra = {}) {
  return {
    ...process.env,
    ZHIHENG_ISSUE54_TASK_ID: identity.task_id || '',
    ZHIHENG_ISSUE54_ATTEMPT_ID: identity.attempt_id || '',
    ZHIHENG_ISSUE54_EVIDENCE_OBJECT_ID: identity.evidence_object_id || '',
    ZHIHENG_ISSUE54_SOURCE_SHA256: identity.source_sha256 || report.input.sha256,
    ...extra,
  };
}

function restart(kind, command, identity) {
  const started = Date.now();
  const safeName = `${kind}-${report.restarts.length + 1}`;
  const commandDigest = crypto.createHash('sha256').update(command).digest('hex');
  const stdoutPath = path.join(output, `${safeName}.stdout.log`);
  const stderrPath = path.join(output, `${safeName}.stderr.log`);
  const identityCommand = kind === 'worker'
    ? process.env.ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND
    : process.env.ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND;
  const identityProbe = () => execFileSync('bash', ['-lc', identityCommand], {
    encoding: 'utf8', stdio: 'pipe', timeout: 30_000, env: commandEnv(identity),
  }).trim();
  const beforeIdentity = identityProbe();
  assert(beforeIdentity, `${kind} identity probe returned empty output`);
  const writeLog = (file, value) => fs.writeFileSync(file, redact(value), { mode: 0o600 });
  try {
    const stdout = execFileSync('bash', ['-lc', command], {
      encoding: 'utf8', stdio: 'pipe', timeout: 120_000, env: commandEnv(identity),
    });
    writeLog(stdoutPath, stdout);
    writeLog(stderrPath, '');
    const proof = validateRestartCommandProof(stdout, kind, identity);
    const afterIdentity = identityProbe();
    assert(afterIdentity && afterIdentity !== beforeIdentity, `${kind} identity did not change after restart`);
    report.restarts.push({ kind, status: 'succeeded', exit_code: 0, command_sha256: commandDigest, before_identity_sha256: crypto.createHash('sha256').update(beforeIdentity).digest('hex'), after_identity_sha256: crypto.createHash('sha256').update(afterIdentity).digest('hex'), proof, elapsed_ms: Date.now() - started, stdout_path: stdoutPath, stderr_path: stderrPath });
  } catch (error) {
    writeLog(stdoutPath, error.stdout);
    writeLog(stderrPath, error.stderr || error.message);
    report.restarts.push({ kind, status: 'failed', exit_code: typeof error.status === 'number' ? error.status : null, command_sha256: commandDigest, elapsed_ms: Date.now() - started, stdout_path: stdoutPath, stderr_path: stderrPath, timed_out: error.killed === true });
    save();
    throw error;
  }
  save();
}

function jsonEnv(name, fallback) {
  const raw = process.env[name];
  if (!raw || !raw.trim()) return fallback;
  try { return JSON.parse(raw); }
  catch (error) { throw Error(`${name} must be valid JSON: ${error.message}`); }
}

function redact(value) {
  return String(value || '')
    .replace(/(authorization\s*[:=]\s*bearer\s+)[^\s"',}]+/gi, '$1[REDACTED]')
    .replace(/((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*["']?)[^\s,"'}]+/gi, '$1[REDACTED]')
    .replace(/(Bearer\s+)[^\s"',}]+/gi, '$1[REDACTED]');
}

function dbSnapshot(taskId, label) {
  const database = process.env.ZHIHENG_SQLITE_PATH
    || process.env.ZHIHENG_DATABASE_PATH
    || path.join(os.homedir(), '.local/share/zhiheng/zhiheng.sqlite');
  if (!fs.existsSync(database)) {
    report.db_snapshots.push({ label, skipped: true, reason: `database does not exist: ${database}` });
    save();
    return null;
  }
  const script = String.raw`
import json, sqlite3, sys
db, task_id = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
def payload(value):
    try:
        parsed = json.loads(value or '{}')
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
task = conn.execute('SELECT id, state, backend, evidence_object_id FROM pdf_tasks WHERE id=?', (task_id,)).fetchone()
jobs = []
for row in conn.execute("""
    SELECT id, job_type, status, attempts, max_attempts, lease_owner, lease_expires_at, payload_json
    FROM jobs
    WHERE json_extract(payload_json, '$.task_id') = ?
    ORDER BY updated_at, id
""", (task_id,)):
    body = payload(row['payload_json'])
    jobs.append({
        'id': row['id'],
        'job_type': row['job_type'],
        'status': row['status'],
        'attempts': row['attempts'],
        'max_attempts': row['max_attempts'],
        'lease_owner_sha256': __import__('hashlib').sha256(str(row['lease_owner'] or '').encode()).hexdigest() if row['lease_owner'] else None,
        'lease_expires_at': row['lease_expires_at'],
        'parser_attempt_id': body.get('parser_attempt_id') or body.get('attempt_id'),
        'knowledge_object_id': body.get('knowledge_object_id'),
        'failure_code': body.get('failure_code'),
        'failure_stage': body.get('failure_stage'),
        'retryable': body.get('retryable'),
    })
attempts = [dict(row) for row in conn.execute(
    'SELECT id, attempt_no, status, backend, manifest_uri, manifest_sha256 FROM pdf_parse_attempts WHERE task_id=? ORDER BY attempt_no',
    (task_id,),
)]
outbox = []
for row in conn.execute("""
    SELECT id, event_type, status, payload_json
    FROM outbox_events
    WHERE aggregate_id=? OR json_extract(payload_json, '$.task_id')=?
    ORDER BY created_at, id
""", (task_id, task_id)):
    body = payload(row['payload_json'])
    outbox.append({
        'id': row['id'],
        'event_type': row['event_type'],
        'status': row['status'],
        'parser_attempt_id': body.get('parser_attempt_id') or body.get('attempt_id'),
        'knowledge_object_id': body.get('knowledge_object_id'),
    })
print(json.dumps({'task': dict(task) if task else None, 'jobs': jobs, 'attempts': attempts, 'outbox': outbox}, ensure_ascii=False))
`;
  const stdout = execFileSync('python3', ['-c', script, database, taskId], {
    encoding: 'utf8', stdio: 'pipe', timeout: 30_000,
  });
  const snapshot = { label, at: new Date().toISOString(), ...JSON.parse(stdout) };
  report.db_snapshots.push(snapshot);
  save();
  return snapshot;
}

function parserAttemptFrom(snapshot) {
  if (!snapshot) return null;
  for (const collection of [snapshot.jobs || [], snapshot.outbox || [], snapshot.attempts || []]) {
    for (const item of collection) {
      const value = item.parser_attempt_id || item.id;
      if (value) return value;
    }
  }
  return null;
}

async function api(page, path, options = {}) {
  return await page.evaluate(async ({ path, options }) => {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch { body = await response.text(); }
    return { http: response.status, body };
  }, { path, options });
}

async function csrfToken(page) {
  return await page.evaluate(() => {
    const match = document.cookie.match(/(?:^|; )zhiheng_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  });
}

async function waitForTask(page, taskId, predicate, label, timeoutMs) {
  const started = Date.now();
  let last = null;
  while (Date.now() - started < timeoutMs) {
    const response = await api(page, `/v1/knowledge/pdf-imports/${encodeURIComponent(taskId)}`);
    assert.equal(response.http, 200);
    last = response.body;
    report.states.push({ at: new Date().toISOString(), label, http: response.http, body: response.body });
    save();
    if (predicate(response.body)) return response.body;
    await page.waitForTimeout(1_000);
  }
  throw Error(`${label} timed out; last state was ${JSON.stringify(last)}`);
}

async function waitForActiveLease(page, taskId, label, timeoutMs) {
  const started = Date.now();
  let snapshot = null;
  while (Date.now() - started < timeoutMs) {
    snapshot = dbSnapshot(taskId, label);
    if (snapshotHasActiveLease(snapshot)) return snapshot;
    await page.waitForTimeout(500);
  }
  throw Error(`${label} did not observe an active processing lease: ${JSON.stringify(snapshot)}`);
}

async function parserStatusProof(identity) {
  const command = process.env.ZHIHENG_PARSER_ACTIVE_PROBE_COMMAND;
  assert(command, 'Missing parser active probe command');
  const output = execFileSync('bash', ['-lc', command], {
    encoding: 'utf8',
    stdio: 'pipe',
    timeout: 30_000,
    env: commandEnv(identity),
  });
  const proof = validateParserActiveProof(output, identity);
  report.parser_probes = report.parser_probes || [];
  report.parser_probes.push(proof);
  save();
  return proof;
}

async function assertPollingStopped(page, taskId, label) {
  const key = `/v1/knowledge/pdf-imports/${encodeURIComponent(taskId)}`;
  const before = await page.evaluate((path) => (
    (window.__issue54StatusRequests || []).filter((item) => item === path).length
  ), key);
  await page.waitForTimeout(2_500);
  const after = await page.evaluate((path) => (
    (window.__issue54StatusRequests || []).filter((item) => item === path).length
  ), key);
  assert.equal(after, before, `${label} continued polling after terminal state`);
  return { before, after, polling_stopped: true };
}

async function uploadPdfTask(page, file, title) {
  await page.locator('[data-open-import]:visible').first().click();
  await page.locator('#import-file').setInputFiles(file);
  await page.locator('#import-title').fill(title);
  await page.locator('#import-submit').click();
  const receipt = await page.waitForFunction(
    () => window.__issue54Receipt,
    { timeout: 60_000 },
  );
  const payload = await receipt.jsonValue();
  assert.equal(payload.http, 202);
  assert(payload.body.task_id && payload.body.evidence_object_id);
  assert.equal(payload.body.source_sha256, report.input.sha256);
  await page.evaluate(() => { window.__issue54Receipt = null; });
  return payload.body;
}

async function runFaultInjections(page, file) {
  const scenarios = validateFaultScenarios(jsonEnv('ZHIHENG_ISSUE54_FAULTS_JSON', null));
  for (const scenario of scenarios) {
    const name = String(scenario.name || '').trim();
    assert(name, 'fault injection scenario requires name');
    const taskReceipt = await uploadPdfTask(page, file, `Issue 54 fault ${name} ${crypto.randomUUID()}`);
    const initialScenarioSnapshot = dbSnapshot(taskReceipt.task_id, `fault-${name}-receipt`);
    const scenarioIdentity = {
      task_id: taskReceipt.task_id,
      attempt_id: parserAttemptFrom(initialScenarioSnapshot),
      evidence_object_id: taskReceipt.evidence_object_id,
      source_sha256: taskReceipt.source_sha256,
    };
    const started = Date.now();
    const record = {
      name,
      category: scenario.category,
      type: String(scenario.type || 'external-command'),
      label: 'fault-injection',
      status: 'running',
      expected_state: scenario.expected_state || null,
      expected_error_code: scenario.expected_error_code,
      task_id: taskReceipt.task_id,
      evidence_object_id: taskReceipt.evidence_object_id,
      source_sha256: taskReceipt.source_sha256,
    };
    report.fault_injections.push(record);
    save();
    try {
      if (scenario.command) {
        const stdoutPath = path.join(output, `fault-${name}.stdout.log`);
        const stderrPath = path.join(output, `fault-${name}.stderr.log`);
        try {
          const stdout = execFileSync('bash', ['-lc', scenario.command], {
            encoding: 'utf8',
            stdio: 'pipe',
            timeout: Number(scenario.timeout_ms || 120_000),
            env: commandEnv(scenarioIdentity, {
              ZHIHENG_ISSUE54_FAULT_CATEGORY: scenario.category,
              ZHIHENG_ISSUE54_EXPECTED_STATE: scenario.expected_state,
              ZHIHENG_ISSUE54_EXPECTED_ERROR_CODE: scenario.expected_error_code,
            }),
          });
          fs.writeFileSync(stdoutPath, redact(stdout), { mode: 0o600 });
          fs.writeFileSync(stderrPath, '', { mode: 0o600 });
          record.command_proof = validateFaultCommandProof(stdout, scenario, scenarioIdentity);
        } catch (error) {
          fs.writeFileSync(stdoutPath, redact(error.stdout), { mode: 0o600 });
          fs.writeFileSync(stderrPath, redact(error.stderr || error.message), { mode: 0o600 });
          throw error;
        }
        record.stdout_path = stdoutPath;
        record.stderr_path = stderrPath;
      }
      if (scenario.expected_state) {
        const terminal = await waitForTask(
          page,
          record.task_id,
          state => state.state === scenario.expected_state,
          `fault-${name}`,
          Number(scenario.wait_ms || 120_000),
        );
        assert.equal(terminal.error_code, scenario.expected_error_code);
        record.observed = {
          state: terminal.state,
          error_code: terminal.error_code || null,
          retryable: Boolean(terminal.retryable),
          redacted_summary_present: Boolean(terminal.redacted_summary),
        };
        if (scenario.expected_retry_count !== undefined) {
          const snapshot = dbSnapshot(record.task_id, `fault-${name}-terminal`);
          const attempts = (snapshot?.jobs || []).map((job) => Number(job.attempts || 0));
          assert(attempts.some((value) => value >= Number(scenario.expected_retry_count)), `${name} did not observe expected retry count`);
          record.observed.retry_count = Math.max(...attempts);
        }
        if (scenario.assert_polling_stopped) {
          record.observed.polling = await assertPollingStopped(
            page,
            record.task_id,
            `fault-${name}`,
          );
        }
        if (scenario.expect_ui_retry) {
          const item = page.locator('.processing-item').filter({ hasText: name }).first();
          await item.getByRole('button', { name: '重试', exact: true }).click();
          record.ui_retry_clicked = true;
          await page.waitForTimeout(1_000);
        } else if (scenario.expect_retry_endpoint) {
          const retry = await api(page, `/v1/knowledge/pdf-imports/${encodeURIComponent(record.task_id)}/retry`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': await csrfToken(page),
              'If-Match': terminal.etag || '',
              'Idempotency-Key': crypto.randomUUID(),
            },
            body: '{}',
          });
          assert.equal(retry.http, 200, `retry endpoint failed for ${name}`);
          record.retry_http = retry.http;
        }
      }
      record.elapsed_ms = Date.now() - started;
      record.status = 'passed';
    } catch (error) {
      record.elapsed_ms = Date.now() - started;
      record.status = 'failed';
      record.failure = String(error.stack || error);
      save();
      throw error;
    }
    save();
  }
}

function recordRegressionReferences() {
  report.regression_references = [
    {
      claim: 'stale lease writeback is rejected and duplicate completion does not publish twice',
      command: ['uv', 'run', 'pytest', '-q', 'tests/integration/test_pdf_parser_lease.py', 'tests/integration/test_knowledge_job_claim_authority.py'],
    },
    {
      claim: 'terminal failed/partial/unsupported browser states stop polling and expose recovery actions',
      command: ['node', '--test', 'tests/e2e/test_import_polling.cjs'],
    },
  ].map((item, index) => {
    const started = Date.now();
    const logPath = path.join(output, `regression-${index + 1}.log`);
    try {
      const stdout = execFileSync(item.command[0], item.command.slice(1), {
        encoding: 'utf8',
        stdio: 'pipe',
        timeout: 120_000,
        cwd: process.cwd(),
      });
      const safeOutput = redact(stdout);
      fs.writeFileSync(logPath, safeOutput, { mode: 0o600 });
      return {
        ...item,
        command: item.command.join(' '),
        status: 'passed',
        exit_code: 0,
        elapsed_ms: Date.now() - started,
        log_path: logPath,
        output_sha256: crypto.createHash('sha256').update(safeOutput).digest('hex'),
      };
    } catch (error) {
      const outputText = `${error.stdout || ''}\n${error.stderr || error.message || ''}`;
      fs.writeFileSync(logPath, redact(outputText), { mode: 0o600 });
      throw Error(`regression command failed: ${item.command.join(' ')}; log=${logPath}`);
    }
  });
  save();
}

(async () => {
  let browser;
  let page;
  try {
    assert(process.env.ZHIHENG_TEST_USERNAME && process.env.ZHIHENG_TEST_PASSWORD, 'Missing credentials');
    assert(process.env.ZHIHENG_RESTART_WORKER_COMMAND, 'Missing Worker restart command');
    assert(process.env.ZHIHENG_RESTART_GATEWAY_COMMAND, 'Missing gateway restart command');
    assert(process.env.ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND, 'Missing Worker identity command');
    assert(process.env.ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND, 'Missing gateway identity command');
    assert(process.env.ZHIHENG_PARSER_ACTIVE_PROBE_COMMAND, 'Missing parser active probe command');
    assert(process.env.ZHIHENG_ISSUE54_FAULTS_JSON, 'Missing Issue 54 fault scenarios');
    assert.equal(report.dirty, false, 'Recovery evidence requires a clean checkout');
    if (process.env.ZHIHENG_EXPECTED_SHA) assert.equal(report.sha, process.env.ZHIHENG_EXPECTED_SHA);
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(() => {
      const originalFetch = window.fetch;
      window.__issue54Receipt = null;
      window.__issue54StatusRequests = [];
      window.fetch = async (...args) => {
        const response = await originalFetch.apply(window, args);
        const request = args[0];
        const url = new URL(request instanceof Request ? request.url : String(request), location.href);
        const method = args[1]?.method || (request instanceof Request ? request.method : 'GET');
        if (
          method.toUpperCase() === 'GET'
          && url.pathname.startsWith('/v1/knowledge/pdf-imports/')
        ) {
          window.__issue54StatusRequests.push(url.pathname);
        }
        if (url.pathname === '/v1/knowledge/pdf-imports' && method.toUpperCase() === 'POST') {
          try {
            window.__issue54Receipt = { http: response.status, body: await response.clone().json() };
          } catch { window.__issue54Receipt = { http: response.status, body: null }; }
        }
        return response;
      };
    });
    await page.goto(`${base}/login?next=${encodeURIComponent('/knowledge-agent#library')}`);
    await page.locator('#username').fill(process.env.ZHIHENG_TEST_USERNAME);
    await page.locator('#password').fill(process.env.ZHIHENG_TEST_PASSWORD);
    await page.locator('#form button').click();
    await page.waitForURL('**/knowledge-agent*');
    report.login = true;
    await page.locator('[data-open-import]:visible').first().click();
    await page.locator('#import-file').setInputFiles(file);
    await page.locator('#import-title').fill(`Issue 54 recovery ${crypto.randomUUID()}`);
    await page.locator('#import-submit').click();
    const receipt = await page.waitForFunction(
      () => window.__issue54Receipt,
      { timeout: 60_000 },
    );
    const receiptPayload = await receipt.jsonValue();
    assert.equal(receiptPayload.http, 202);
    const receiptBody = receiptPayload.body;
    assert(receiptBody.task_id && receiptBody.evidence_object_id);
    assert.equal(receiptBody.source_sha256, report.input.sha256);
    report.receipt = {
      task_id: receiptBody.task_id,
      evidence_object_id: receiptBody.evidence_object_id,
      source_sha256: receiptBody.source_sha256,
    };
    const taskId = receiptBody.task_id;
    const receiptAt = Date.now();
    const initialSnapshot = dbSnapshot(taskId, 'after-receipt');
    const persistedParserAttempt = parserAttemptFrom(initialSnapshot);
    assert(persistedParserAttempt, 'database snapshot must expose the durable local parser attempt identity');
    report.receipt.parser_attempt_id = persistedParserAttempt;
    let firstAttempt = null;
    const deadline = Date.now() + 300_000;
    const preWorker = await waitForTask(
      page,
      taskId,
      state => state.state === 'processing',
      'before-worker-restart',
      30_000,
    );
    assert.equal(preWorker.task_id, taskId);
    assert.equal(preWorker.evidence_object_id, receiptBody.evidence_object_id);
    assert.equal(preWorker.source_sha256, report.input.sha256);
    const activeLease = await waitForActiveLease(page, taskId, 'before-worker-restart-active-lease', 30_000);
    report.active_lease_before_worker_restart = activeLease;
    restart('worker', process.env.ZHIHENG_RESTART_WORKER_COMMAND, {
      task_id: taskId,
      attempt_id: persistedParserAttempt,
      evidence_object_id: receiptBody.evidence_object_id,
      source_sha256: report.input.sha256,
    });
    dbSnapshot(taskId, 'after-worker-restart');
    await page.screenshot({path: path.join(output, 'after-worker-restart.png'), fullPage: true});

    const preGateway = await waitForTask(
      page,
      taskId,
      state => state.state === 'processing',
      'before-gateway-restart',
      Math.max(1_000, deadline - Date.now()),
    );
    assert.equal(preGateway.state, 'processing', 'gateway restart must occur while parsing is active');
    if (preGateway.attempt_id) firstAttempt = preGateway.attempt_id;
    if (firstAttempt) assert.equal(firstAttempt, persistedParserAttempt);
    const parserProof = await parserStatusProof({
      task_id: taskId,
      attempt_id: persistedParserAttempt,
      evidence_object_id: receiptBody.evidence_object_id,
      source_sha256: report.input.sha256,
    });
    report.active_parser_before_gateway_restart = parserProof;
    restart('gateway', process.env.ZHIHENG_RESTART_GATEWAY_COMMAND, {
      task_id: taskId,
      attempt_id: persistedParserAttempt,
      evidence_object_id: receiptBody.evidence_object_id,
      source_sha256: report.input.sha256,
    });
    dbSnapshot(taskId, 'after-gateway-restart');
    await page.screenshot({path: path.join(output, 'after-gateway-restart.png'), fullPage: true});

    const parsed = await waitForTask(
      page,
      taskId,
      state => ['parsed', 'failed', 'partial', 'unsupported', 'dead'].includes(state.state),
      'terminal-after-restarts',
      Math.max(1_000, deadline - Date.now()),
    );
    if (['failed', 'partial', 'unsupported', 'dead'].includes(parsed.state)) {
      throw Error(`recovery reached terminal failure: ${parsed.state} ${parsed.error_code || ''}`);
    }
    assert.equal(parsed.state, 'parsed');
    assert.equal(parsed.attempt_id, persistedParserAttempt, 'recovery changed parser attempt identity');
    assert(parsed.manifest_uri && parsed.manifest_sha256, 'recovered parse must publish a durable manifest reference');
    const finalSnapshot = dbSnapshot(taskId, 'parsed-after-restarts');
    const parseAttempts = finalSnapshot ? (finalSnapshot.attempts || []) : [];
    assert.equal(parseAttempts.length, 1, 'recovery must not duplicate PDF parse attempts');
    assert.equal(parseAttempts[0].id, persistedParserAttempt);
    assert.equal(parseAttempts[0].manifest_sha256, parsed.manifest_sha256);
    report.timings.parse_and_recovery_ms = Date.now() - receiptAt;
    assert(report.timings.parse_and_recovery_ms <= 300_000, 'parse plus recovery exceeded the 5 minute budget');
    await runFaultInjections(page, file);
    recordRegressionReferences();
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.failure = String(error.stack || error);
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    report.finished = new Date().toISOString();
    report.exit_code = process.exitCode || 0;
    save();
    if (browser) await browser.close();
  }
})();
