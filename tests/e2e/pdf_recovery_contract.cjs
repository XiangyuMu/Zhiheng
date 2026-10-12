const assert = require("node:assert/strict");

const REQUIRED_FAULT_CATEGORIES = Object.freeze([
  "lost_parser_attempt",
  "parser_failure",
  "partial_or_unsupported",
  "auto_retry_exhausted",
  "manual_ui_retry",
]);

const TERMINAL_STATES = new Set(["failed", "partial", "unsupported", "dead"]);
const ACTIVE_PARSER_STATES = new Set(["accepted", "running", "processing"]);

function parseLastJsonLine(output, label) {
  const lines = String(output || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const parsed = JSON.parse(lines[index]);
      assert(parsed && typeof parsed === "object" && !Array.isArray(parsed), `${label} JSON proof must be an object`);
      return parsed;
    } catch (_) {
      // Continue scanning: deployment commands may print human-readable setup lines first.
    }
  }
  throw new Error(`${label} must print a JSON object on its last non-empty line`);
}

function validateFaultScenarios(scenarios) {
  assert(Array.isArray(scenarios), "ZHIHENG_ISSUE54_FAULTS_JSON must be an array");
  const names = new Set();
  const categories = new Set();
  const validated = scenarios.map((scenario, index) => {
    assert(scenario && typeof scenario === "object" && !Array.isArray(scenario), `fault scenario ${index + 1} must be an object`);
    const name = String(scenario.name || "").trim();
    assert(name, `fault scenario ${index + 1} requires name`);
    assert(!names.has(name), `fault scenario name must be unique: ${name}`);
    names.add(name);
    const category = String(scenario.category || "").trim();
    assert(REQUIRED_FAULT_CATEGORIES.includes(category), `fault scenario ${name} has unsupported category: ${category}`);
    categories.add(category);
    assert.equal(scenario.injected, true, `fault scenario ${name} must be explicitly marked injected=true`);
    assert.equal(scenario.boundary, "external_parser", `fault scenario ${name} must inject at external_parser boundary`);
    assert.equal(scenario.assert_polling_stopped, true, `fault scenario ${name} must prove polling stops after terminal state`);
    assert(typeof scenario.command === "string" && scenario.command.trim(), `fault scenario ${name} requires command`);
    assert(!scenario.task_id, `fault scenario ${name} must not pre-bind task_id; the harness creates a unique browser task`);
    const expectedState = String(scenario.expected_state || "").trim();
    assert(TERMINAL_STATES.has(expectedState), `fault scenario ${name} expected_state must be terminal`);
    const expectedErrorCode = String(scenario.expected_error_code || "").trim();
    assert(expectedErrorCode, `fault scenario ${name} requires expected_error_code`);
    if (category === "manual_ui_retry") {
      assert.equal(scenario.expect_ui_retry, true, `manual_ui_retry scenario ${name} must require UI retry`);
    } else {
      assert.notEqual(scenario.expect_ui_retry, true, `only manual_ui_retry may set expect_ui_retry=true`);
    }
    if (category === "auto_retry_exhausted") {
      assert(Number(scenario.expected_retry_count) >= 2, `auto_retry_exhausted scenario ${name} requires expected_retry_count >= 2`);
    }
    return { ...scenario, name, category, expected_state: expectedState, expected_error_code: expectedErrorCode };
  });
  for (const category of REQUIRED_FAULT_CATEGORIES) {
    assert(categories.has(category), `missing Issue 54 fault category: ${category}`);
  }
  return validated;
}

function validateFaultCommandProof(output, scenario, identity) {
  const proof = parseLastJsonLine(output, `fault scenario ${scenario.name}`);
  assert.equal(proof.injected, true, `fault scenario ${scenario.name} command proof must include injected=true`);
  assert.equal(proof.boundary, "external_parser", `fault scenario ${scenario.name} command proof must include boundary=external_parser`);
  assert.equal(proof.category, scenario.category, `fault scenario ${scenario.name} command proof category mismatch`);
  assert.equal(proof.task_id, identity.task_id, `fault scenario ${scenario.name} command proof task_id mismatch`);
  if (identity.attempt_id) {
    assert.equal(proof.attempt_id, identity.attempt_id, `fault scenario ${scenario.name} command proof attempt_id mismatch`);
  }
  assert(
    typeof proof.observed_identity === "string" && proof.observed_identity.trim(),
    `fault scenario ${scenario.name} command proof requires observed_identity`,
  );
  if (identity.upstream_task_id) {
    assert.equal(
      proof.observed_identity,
      identity.upstream_task_id,
      `fault scenario ${scenario.name} upstream identity mismatch`,
    );
  }
  assert.equal(
    proof.observed_ready,
    true,
    `fault scenario ${scenario.name} command proof must report observed_ready=true`,
  );
  return proof;
}

function validateParserActiveProof(output, identity) {
  const proof = parseLastJsonLine(output, "parser status probe");
  assert.equal(proof.task_id, identity.task_id, "parser status proof task_id mismatch");
  assert.equal(proof.attempt_id, identity.attempt_id, "parser status proof attempt_id mismatch");
  assert(ACTIVE_PARSER_STATES.has(String(proof.state || "")), "parser status proof must show an active upstream attempt");
  assert(typeof proof.upstream_task_id === "string" && proof.upstream_task_id.trim(), "parser status proof requires upstream_task_id");
  assert(proof.ready === true || proof.observed_ready === true, "parser status proof must report gateway/upstream ready");
  return proof;
}

function validateRestartCommandProof(output, kind, identity) {
  const proof = parseLastJsonLine(output, `${kind} restart`);
  assert.equal(proof.kind, kind, `${kind} restart proof kind mismatch`);
  assert.equal(proof.task_id, identity.task_id, `${kind} restart proof task_id mismatch`);
  assert(proof.observed_identity || proof.pid || proof.container_id, `${kind} restart proof must include observed identity`);
  assert(proof.ready === true || proof.observed_ready === true, `${kind} restart proof must report ready=true`);
  return proof;
}

function snapshotHasActiveLease(snapshot) {
  if (!snapshot || !Array.isArray(snapshot.jobs)) return false;
  return snapshot.jobs.some((job) => (
    job.status === "processing"
    && job.lease_owner_sha256
    && Number(job.attempts || 0) > 0
  ));
}

function nonEmptyString(value, label) {
  assert(typeof value === "string" && value.trim(), `${label} must be a non-empty string`);
  return value;
}

function validateRestartScenario(scenario, expectedSourceSha) {
  assert(scenario && typeof scenario === "object" && !Array.isArray(scenario), "restart scenario must be an object");
  assert(["worker", "gateway"].includes(scenario.kind), "restart scenario kind must be worker or gateway");
  assert.equal(scenario.status, "passed", `${scenario.kind} restart scenario must pass`);
  const receipt = scenario.receipt;
  assert(receipt && typeof receipt === "object" && !Array.isArray(receipt), `${scenario.kind} restart receipt must be an object`);
  const taskId = nonEmptyString(receipt.task_id, `${scenario.kind} restart receipt.task_id`);
  const attemptId = nonEmptyString(receipt.parser_attempt_id, `${scenario.kind} restart receipt.parser_attempt_id`);
  const evidenceObjectId = nonEmptyString(receipt.evidence_object_id, `${scenario.kind} restart receipt.evidence_object_id`);
  assert.equal(receipt.source_sha256, expectedSourceSha, `${scenario.kind} restart receipt source digest mismatch`);
  assert(snapshotHasActiveLease(scenario.active_lease_before_restart), `${scenario.kind} restart requires active lease proof`);

  if (scenario.kind === "gateway") {
    assert(scenario.active_parser_before_restart, "gateway restart requires active parser proof");
    validateParserActiveProof(
      JSON.stringify(scenario.active_parser_before_restart),
      { task_id: taskId, attempt_id: attemptId },
    );
  }

  const restart = scenario.restart;
  assert(restart && typeof restart === "object" && !Array.isArray(restart), `${scenario.kind} restart proof record must be an object`);
  assert.equal(restart.kind, scenario.kind, `${scenario.kind} restart record kind mismatch`);
  assert.equal(restart.status, "succeeded", `${scenario.kind} restart command must succeed`);
  assert.equal(restart.exit_code, 0, `${scenario.kind} restart command exit code must be zero`);
  validateRestartCommandProof(JSON.stringify(restart.proof), scenario.kind, { task_id: taskId });

  const terminal = scenario.terminal;
  assert(terminal && typeof terminal === "object" && !Array.isArray(terminal), `${scenario.kind} restart terminal state must be an object`);
  assert.equal(terminal.task_id, taskId, `${scenario.kind} restart terminal task mismatch`);
  assert.equal(terminal.evidence_object_id, evidenceObjectId, `${scenario.kind} restart terminal evidence mismatch`);
  assert.equal(terminal.source_sha256, expectedSourceSha, `${scenario.kind} restart terminal source digest mismatch`);
  assert.equal(terminal.state, "parsed", `${scenario.kind} restart terminal state must be parsed`);
  assert.equal(terminal.attempt_id, attemptId, `${scenario.kind} restart changed parser attempt identity`);
  const manifestSha = nonEmptyString(terminal.manifest_sha256, `${scenario.kind} restart terminal.manifest_sha256`);
  nonEmptyString(terminal.manifest_uri, `${scenario.kind} restart terminal.manifest_uri`);
  const attempts = scenario.final_snapshot?.attempts;
  assert(Array.isArray(attempts), `${scenario.kind} restart final snapshot attempts must be a list`);
  assert.equal(attempts.length, 1, `${scenario.kind} restart must not duplicate parse attempts`);
  assert.equal(attempts[0]?.id, attemptId, `${scenario.kind} restart final attempt id mismatch`);
  assert.equal(attempts[0]?.manifest_sha256, manifestSha, `${scenario.kind} restart final manifest mismatch`);
  assert(Number(scenario.elapsed_ms) > 0 && Number(scenario.elapsed_ms) <= 300_000, `${scenario.kind} restart elapsed_ms exceeds budget`);
  return scenario;
}

function validateRestartScenarios(scenarios, expectedSourceSha) {
  assert(Array.isArray(scenarios), "restart_scenarios must be a list");
  assert.equal(scenarios.length, 2, "restart_scenarios must contain worker and gateway scenarios");
  const validated = scenarios.map((scenario) => validateRestartScenario(scenario, expectedSourceSha));
  assert.deepEqual(new Set(validated.map((scenario) => scenario.kind)), new Set(["worker", "gateway"]));
  assert.equal(new Set(validated.map((scenario) => scenario.receipt.task_id)).size, 2, "worker and gateway restarts must use independent tasks");
  assert.equal(new Set(validated.map((scenario) => scenario.receipt.parser_attempt_id)).size, 2, "worker and gateway restarts must use independent attempts");
  return validated;
}

module.exports = {
  REQUIRED_FAULT_CATEGORIES,
  parseLastJsonLine,
  snapshotHasActiveLease,
  validateFaultCommandProof,
  validateFaultScenarios,
  validateParserActiveProof,
  validateRestartScenarios,
  validateRestartCommandProof,
};
