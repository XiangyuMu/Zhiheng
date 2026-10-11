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
  assert(proof.observed_identity || proof.observed_ready !== undefined, `fault scenario ${scenario.name} command proof must include observed_identity or observed_ready`);
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

module.exports = {
  REQUIRED_FAULT_CATEGORIES,
  parseLastJsonLine,
  snapshotHasActiveLease,
  validateFaultCommandProof,
  validateFaultScenarios,
  validateParserActiveProof,
  validateRestartCommandProof,
};
