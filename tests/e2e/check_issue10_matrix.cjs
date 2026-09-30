/* Issue #10: prove the distributed browser scenarios form one complete loop. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const output = process.argv[3];
if (!output) throw new Error("Pass the browser acceptance output directory");
fs.mkdirSync(output, { recursive: true });

const requirements = {
  review: {
    path: "review-center-issue9/checks.json",
    labels: [
      "review queue shows the draft count and full draft detail",
      "unapproved draft is restored after closing and re-login",
      "version conflict is shown in the browser and retry after refresh succeeds",
      "closing review without an action does not approve the draft",
    ],
  },
  actions: {
    path: "workspace-full/checks.json",
    labels: ["central review restores drafts and records real decisions"],
    relationPath: "relations/checks.json",
    relationLabels: [
      "deferred relation remains reviewable",
      "reject relation records the real API decision",
      "approved relation enters formal searchable knowledge",
    ],
  },
  context: {
    path: "context-prompts-issue8/checks.json",
    labels: [
      "conflict confirmation is completed through the real browser dialog",
      "missing information is supplemented through the real browser dialog",
      "partial answers retain unrelated evidence while excluding unresolved conflict values",
    ],
  },
  qualification: {
    path: "qualification/checks.json",
    labels: [
      "unapproved conclusions stay out of both browser sessions",
      "approved conclusions become visible in a separate browser session",
    ],
  },
  failures: {
    path: "import-failures/checks.json",
    labels: [
      "failed status is rendered with bounded polling and recovery semantics",
      "not_found status is rendered with bounded polling and recovery semantics",
      "server_error status is rendered with bounded polling and recovery semantics",
      "network_error status is rendered with bounded polling and recovery semantics",
    ],
  },
};

function read(relative) {
  const file = path.join(output, relative);
  const payload = JSON.parse(fs.readFileSync(file, "utf8"));
  assert.equal(payload.status || "passed", "passed", `${relative} did not pass`);
  assert.deepEqual(payload.browserErrors || [], [], `${relative} has browser errors`);
  return payload;
}

try {
  const evidence = {};
  for (const [name, requirement] of Object.entries(requirements)) {
    const payload = read(requirement.path);
    const checks = payload.checks || [];
    for (const label of requirement.labels) assert(checks.includes(label), `${name}: ${label}`);
    evidence[name] = { path: requirement.path, checks: requirement.labels };
    if (requirement.relationPath) {
      const relation = read(requirement.relationPath);
      for (const label of requirement.relationLabels) {
        assert((relation.checks || []).includes(label), `actions: ${label}`);
      }
      evidence.actions.relations = { path: requirement.relationPath, checks: requirement.relationLabels };
    }
  }
  const checks = [
    "browser review queue covers counts, recovery, stale-version retry, and close-without-approval",
    "browser review actions cover conclusion and relation decisions",
    "browser answers cover conflict conditions, missing information, and unrelated continuation",
    "browser qualification isolates unapproved and serves approved conclusions across sessions",
    "browser failures distinguish terminal API states from transport and UI retry failures",
  ];
  fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ status: "passed", checks, evidence }, null, 2));
  console.log("PASS issue10 browser evidence matrix");
} catch (error) {
  fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
    status: "failed", checks: [], error: error.stack || String(error),
  }, null, 2));
  console.error(error);
  process.exitCode = 1;
}
