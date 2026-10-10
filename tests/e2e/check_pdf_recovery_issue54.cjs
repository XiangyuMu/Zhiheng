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

function jsonEnv(name, fallback) {
  const raw = process.env[name];
  if (!raw || !raw.trim()) return fallback;
  try { return JSON.parse(raw); }
  catch (error) { throw Error(`${name} must be valid JSON: ${error.message}`); }
}

function redact(value) {
  return String(value || '').replace(/(token|key|secret|password)=\S+/gi, '$1=[REDACTED]');
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
    SELECT id, job_type, status, attempts, max_attempts, lease_owner, payload_json
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

async function runFaultInjections(page, taskId) {
  const scenarios = jsonEnv('ZHIHENG_ISSUE54_FAULTS_JSON', []);
  assert(Array.isArray(scenarios), 'ZHIHENG_ISSUE54_FAULTS_JSON must be an array');
  for (const scenario of scenarios) {
    const name = String(scenario.name || '').trim();
    assert(name, 'fault injection scenario requires name');
    const started = Date.now();
    const record = {
      name,
      type: String(scenario.type || 'external-command'),
      label: 'fault-injection',
      status: 'running',
      expected_state: scenario.expected_state || null,
      task_id: scenario.task_id || taskId,
    };
    report.fault_injections.push(record);
    save();
    try {
      if (scenario.command) {
        const stdoutPath = path.join(output, `fault-${name}.stdout.log`);
        const stderrPath = path.join(output, `fault-${name}.stderr.log`);
        try {
          const stdout = execFileSync('bash', ['-lc', scenario.command], {
            encoding: 'utf8', stdio: 'pipe', timeout: Number(scenario.timeout_ms || 120_000),
          });
          fs.writeFileSync(stdoutPath, redact(stdout), { mode: 0o600 });
          fs.writeFileSync(stderrPath, '', { mode: 0o600 });
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
        record.observed = {
          state: terminal.state,
          error_code: terminal.error_code || null,
          retryable: Boolean(terminal.retryable),
          redacted_summary_present: Boolean(terminal.redacted_summary),
        };
        if (scenario.expect_retry_endpoint) {
          const retry = await api(page, `/v1/knowledge/pdf-imports/${encodeURIComponent(record.task_id)}/retry`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': await csrfToken(page),
              'If-Match': terminal.etag,
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
  if (scenarios.length === 0) {
    report.fault_injections.push({
      status: 'not_run',
      label: 'fault-injection',
      reason: 'Set ZHIHENG_ISSUE54_FAULTS_JSON to run lost-task, failure, partial, unsupported, and retry scenarios.',
    });
    save();
  }
}

function recordRegressionReferences() {
  report.regression_references = [
    {
      claim: 'stale lease writeback is rejected and duplicate completion does not publish twice',
      command: 'uv run pytest -q tests/integration/test_pdf_parser_lease.py tests/integration/test_knowledge_job_claim_authority.py',
    },
    {
      claim: 'terminal failed/partial/unsupported browser states stop polling and expose recovery actions',
      command: 'node --test tests/e2e/test_import_polling.cjs && node tests/e2e/check_delivery_contracts.cjs ...',
    },
  ];
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
    assert.equal(report.dirty, false, 'Recovery evidence requires a clean checkout');
    if (process.env.ZHIHENG_EXPECTED_SHA) assert.equal(report.sha, process.env.ZHIHENG_EXPECTED_SHA);
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
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
      state => ['queued', 'processing'].includes(state.state),
      'before-worker-restart',
      30_000,
    );
    assert.equal(preWorker.task_id, taskId);
    assert.equal(preWorker.evidence_object_id, receiptBody.evidence_object_id);
    assert.equal(preWorker.source_sha256, report.input.sha256);
    restart('worker', process.env.ZHIHENG_RESTART_WORKER_COMMAND);
    dbSnapshot(taskId, 'after-worker-restart');
    await page.screenshot({path: path.join(output, 'after-worker-restart.png'), fullPage: true});

    const preGateway = await waitForTask(
      page,
      taskId,
      state => state.state === 'processing' || state.attempt_id || state.manifest_sha256,
      'before-gateway-restart',
      Math.max(1_000, deadline - Date.now()),
    );
    if (preGateway.attempt_id) firstAttempt = preGateway.attempt_id;
    if (firstAttempt) assert.equal(firstAttempt, persistedParserAttempt);
    restart('gateway', process.env.ZHIHENG_RESTART_GATEWAY_COMMAND);
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
    await runFaultInjections(page, taskId);
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
