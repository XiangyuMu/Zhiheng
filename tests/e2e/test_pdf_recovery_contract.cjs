const assert = require("node:assert/strict");
const test = require("node:test");

const {
  REQUIRED_FAULT_CATEGORIES,
  snapshotHasActiveLease,
  validateFaultCommandProof,
  validateFaultScenarios,
  validateParserActiveProof,
  validateRestartScenarios,
  validateRestartCommandProof,
} = require("./pdf_recovery_contract.cjs");

function completeScenarios(overrides = {}) {
  return REQUIRED_FAULT_CATEGORIES.map((category) => ({
    name: category,
    category,
    injected: true,
    boundary: "external_parser",
    command: `issue54-fault-${category}`,
    assert_polling_stopped: true,
    expected_state: category === "partial_or_unsupported" ? "unsupported" : "failed",
    expected_error_code: `${category}_code`,
    expected_retry_count: category === "auto_retry_exhausted" ? 3 : undefined,
    expect_ui_retry: category === "manual_ui_retry",
    ...overrides[category],
  }));
}

test("Issue 54 fault scenarios require every external parser fault category", () => {
  assert.throws(() => validateFaultScenarios([]), /missing Issue 54 fault category/);
  const scenarios = completeScenarios();
  assert.equal(validateFaultScenarios(scenarios).length, REQUIRED_FAULT_CATEGORIES.length);

  assert.throws(
    () => validateFaultScenarios(scenarios.filter((item) => item.category !== "lost_parser_attempt")),
    /missing Issue 54 fault category: lost_parser_attempt/,
  );
});

test("Issue 54 fault scenarios cannot reuse pre-bound tasks or skip injected boundary proof", () => {
  assert.throws(
    () => validateFaultScenarios(completeScenarios({ parser_failure: { task_id: "old-task" } })),
    /must not pre-bind task_id/,
  );
  assert.throws(
    () => validateFaultScenarios(completeScenarios({ parser_failure: { boundary: "database" } })),
    /external_parser/,
  );
  assert.throws(
    () => validateFaultScenarios(completeScenarios({ parser_failure: { injected: false } })),
    /injected=true/,
  );
});

test("Issue 54 manual retry must be a UI retry scenario", () => {
  assert.throws(
    () => validateFaultScenarios(completeScenarios({ manual_ui_retry: { expect_ui_retry: false } })),
    /must require UI retry/,
  );
  assert.throws(
    () => validateFaultScenarios(completeScenarios({ parser_failure: { expect_ui_retry: true } })),
    /only manual_ui_retry/,
  );
});

test("fault command proof binds external injection to the current browser task", () => {
  const scenario = validateFaultScenarios(completeScenarios())[0];
  const proof = validateFaultCommandProof(
    `log line\n${JSON.stringify({
      injected: true,
      boundary: "external_parser",
      category: scenario.category,
      task_id: "task-1",
      attempt_id: "attempt-1",
      observed_identity: "upstream-1",
      observed_ready: true,
    })}`,
    scenario,
    { task_id: "task-1", attempt_id: "attempt-1", upstream_task_id: "upstream-1" },
  );
  assert.equal(proof.task_id, "task-1");

  assert.throws(
    () => validateFaultCommandProof("not json", scenario, { task_id: "task-1" }),
    /must print a JSON object/,
  );
  assert.throws(
    () => validateFaultCommandProof(
      JSON.stringify({
        injected: true,
        boundary: "external_parser",
        category: scenario.category,
        task_id: "task-1",
        attempt_id: "attempt-1",
        observed_ready: true,
      }),
      scenario,
      { task_id: "task-1", attempt_id: "attempt-1", upstream_task_id: "upstream-1" },
    ),
    /requires observed_identity/,
  );
  assert.throws(
    () => validateFaultCommandProof(
      JSON.stringify({
        injected: true,
        boundary: "external_parser",
        category: scenario.category,
        task_id: "task-1",
        attempt_id: "attempt-1",
        observed_identity: "upstream-2",
        observed_ready: true,
      }),
      scenario,
      { task_id: "task-1", attempt_id: "attempt-1", upstream_task_id: "upstream-1" },
    ),
    /upstream identity mismatch/,
  );
  assert.throws(
    () => validateFaultCommandProof(
      JSON.stringify({
        injected: true,
        boundary: "external_parser",
        category: scenario.category,
        task_id: "task-1",
        attempt_id: "attempt-1",
        observed_identity: "upstream-1",
        observed_ready: false,
      }),
      scenario,
      { task_id: "task-1", attempt_id: "attempt-1", upstream_task_id: "upstream-1" },
    ),
    /observed_ready=true/,
  );
});

test("parser active proof requires upstream identity before gateway restart", () => {
  const proof = validateParserActiveProof(
    JSON.stringify({
      task_id: "task-1",
      attempt_id: "attempt-1",
      state: "running",
      upstream_task_id: "upstream-1",
      ready: true,
    }),
    { task_id: "task-1", attempt_id: "attempt-1" },
  );
  assert.equal(proof.upstream_task_id, "upstream-1");

  assert.throws(
    () => validateParserActiveProof(
      JSON.stringify({ task_id: "task-1", attempt_id: "attempt-1", state: "queued", ready: true }),
      { task_id: "task-1", attempt_id: "attempt-1" },
    ),
    /active upstream/,
  );
});

test("restart command proof reports changed process identity and readiness", () => {
  const proof = validateRestartCommandProof(
    JSON.stringify({
      kind: "worker",
      task_id: "task-1",
      observed_identity: "pid-1234",
      ready: true,
    }),
    "worker",
    { task_id: "task-1" },
  );
  assert.equal(proof.kind, "worker");

  assert.throws(
    () => validateRestartCommandProof(
      JSON.stringify({ kind: "worker", task_id: "task-1", observed_identity: "pid-1234" }),
      "worker",
      { task_id: "task-1" },
    ),
    /ready=true/,
  );
});

test("active lease detection requires a processing job with an owner and attempt count", () => {
  assert.equal(snapshotHasActiveLease({ jobs: [{ status: "queued", attempts: 0 }] }), false);
  assert.equal(
    snapshotHasActiveLease({
      jobs: [{ status: "processing", attempts: 1, lease_owner_sha256: "abc" }],
    }),
    true,
  );
});

function restartScenario(kind, taskId, attemptId, manifestSha) {
  return {
    kind,
    status: "passed",
    receipt: {
      task_id: taskId,
      evidence_object_id: `${taskId}-evidence`,
      source_sha256: "source-sha",
      parser_attempt_id: attemptId,
    },
    active_lease_before_restart: {
      task: { id: taskId, state: "processing", evidence_object_id: `${taskId}-evidence` },
      jobs: [{ status: "processing", attempts: 1, lease_owner_sha256: "lease", parser_attempt_id: attemptId }],
    },
    active_parser_before_restart: kind === "gateway" ? {
      task_id: taskId,
      attempt_id: attemptId,
      state: "running",
      upstream_task_id: `${taskId}-upstream`,
      ready: true,
    } : null,
    restart: {
      kind,
      status: "succeeded",
      exit_code: 0,
      proof: { kind, task_id: taskId, observed_identity: `${kind}-pid`, ready: true },
    },
    terminal: {
      task_id: taskId,
      evidence_object_id: `${taskId}-evidence`,
      source_sha256: "source-sha",
      state: "parsed",
      attempt_id: attemptId,
      manifest_sha256: manifestSha,
      manifest_uri: `file:///objects/${manifestSha}.json`,
    },
    final_snapshot: {
      attempts: [{ id: attemptId, manifest_sha256: manifestSha }],
    },
    elapsed_ms: 1000,
  };
}

test("restart scenarios require independent active tasks and final manifests", () => {
  const scenarios = [
    restartScenario("worker", "task-worker", "attempt-worker", "manifest-worker"),
    restartScenario("gateway", "task-gateway", "attempt-gateway", "manifest-gateway"),
  ];
  assert.equal(validateRestartScenarios(scenarios, "source-sha").length, 2);

  assert.throws(
    () => validateRestartScenarios([
      restartScenario("worker", "task-1", "attempt-worker", "manifest-worker"),
      restartScenario("gateway", "task-1", "attempt-gateway", "manifest-gateway"),
    ], "source-sha"),
    /independent tasks/,
  );
  assert.throws(
    () => validateRestartScenarios([
      restartScenario("worker", "task-worker", "attempt-worker", "manifest-worker"),
      {
        ...restartScenario("gateway", "task-gateway", "attempt-gateway", "manifest-gateway"),
        active_parser_before_restart: null,
      },
    ], "source-sha"),
    /gateway restart requires active parser proof/,
  );
});
