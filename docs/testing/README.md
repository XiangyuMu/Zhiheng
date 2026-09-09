# Testing and Evaluation Contracts

This directory defines the Step 0 verification contract for Zhiheng. It turns the PRD, the continuous-evolution architecture, and ADR-0001 into executable checks before product code exists.

The current contract tests validate schemas, synthetic fixtures, documentation bindings, and safety gates only. They must not claim that ingestion, memory, RAG, privacy erase, or evolution behavior is implemented.

## Sources of Truth

- `docs/product/PRD.md`
- `docs/architecture/continuous-evolution.md`
- `docs/architecture/adr/0001-mvp-technology-stack.md`

## Contract Artifacts

- `evaluation-spec.md` defines fixed and dynamic evaluation suites, hard gates, release binding, RAG metrics, canary rules, and failure injection requirements.
- `security-and-privacy-cases.md` defines the mandatory privacy, confirmation, erase, rollback, prompt-injection, and external-call cases.
- `tests/fixtures/evals/evaluation_sets.sample.json` is the synthetic Step 0 fixture for boundary, migration, retention, and safety cases.
- `tests/fixtures/evals/release_binding.sample.json` is the immutable eight-field release binding fixture.
- `tests/fixtures/evals/security_and_privacy_cases.yaml` mirrors the safety case catalog in a YAML format that can be validated independently.
- `tests/contracts/test_evaluation_contracts.py` checks these artifacts using only Python standard library modules.

## Running Checks

When the Python project skeleton exists, run:

```bash
python -m pytest tests/contracts
```

Before pytest is installed, the contract file can still be executed directly:

```bash
python tests/contracts/test_evaluation_contracts.py
```

JSON fixtures should also parse with:

```bash
python -m json.tool tests/fixtures/evals/evaluation_sets.sample.json
python -m json.tool tests/fixtures/evals/release_binding.sample.json
```

YAML fixtures can be parsed with the platform YAML parser when available:

```bash
ruby -e 'require "yaml"; YAML.load_file("tests/fixtures/evals/security_and_privacy_cases.yaml"); puts "yaml ok"'
```

## Privacy Rule

All fixtures in this directory must be synthetic or sanitized. They must not contain real personal details, API keys, model payloads, private routes, or evidence from the user's actual knowledge base.
