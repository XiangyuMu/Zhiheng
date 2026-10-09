const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  initializeEvidence,
  loadExpectation,
} = require("./pdf_evidence_helpers.cjs");

test("initializeEvidence creates a new output directory and report without overwrite", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "zhiheng-evidence-"));
  const output = path.join(root, "new-output");
  const report = { status: "running" };
  const evidence = initializeEvidence("helper-test", output, report);
  assert.equal(evidence.setupError, null);
  assert.equal(evidence.outputReady, true);
  assert.equal(evidence.reportPath, path.join(output, "report.json"));
  evidence.save();
  assert.equal(JSON.parse(fs.readFileSync(evidence.reportPath, "utf8")).status, "running");
});

test("initializeEvidence reports existing output beside the directory", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "zhiheng-evidence-"));
  const output = path.join(root, "existing-output");
  fs.mkdirSync(output);
  const report = { status: "running" };
  const evidence = initializeEvidence("helper-test", output, report);
  assert.match(evidence.setupError, /already exists/);
  assert.equal(evidence.outputReady, false);
  assert.equal(path.dirname(evidence.reportPath), root);
  assert.notEqual(evidence.reportPath, path.join(output, "report.json"));
  evidence.save();
  assert.equal(fs.existsSync(path.join(output, "report.json")), false);
  assert.equal(JSON.parse(fs.readFileSync(evidence.reportPath, "utf8")).status, "running");
});

test("loadExpectation requires an expectation bound to the input PDF digest", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "zhiheng-evidence-"));
  const expectationPath = path.join(root, "expectation.json");
  fs.writeFileSync(
    expectationPath,
    JSON.stringify({ sha256: "a".repeat(64), page_no: 2, quote: "expected quote" }),
  );
  assert.equal(loadExpectation(expectationPath, "a".repeat(64)).page_no, 2);
  assert.throws(
    () => loadExpectation(expectationPath, "b".repeat(64)),
    /bound to the input PDF SHA/,
  );
});
