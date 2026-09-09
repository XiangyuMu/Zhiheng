from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "evals"

EIGHT_RELEASE_FIELDS = [
    "candidate_id",
    "target_component",
    "source_evaluation_ids",
    "source_evidence_refs",
    "validation_report_ref",
    "reviewer_decision_ref",
    "approved_artifact_digest",
    "rollback_target_id",
]

REQUIRED_BUDGET_FIELDS = [
    "max_input_tokens",
    "max_output_tokens",
    "max_model_calls",
    "max_wall_clock_ms",
    "max_external_calls",
    "embedding_generation",
    "model_route",
    "budget_status",
]

REQUIRED_DYNAMIC_CASE_FIELDS = [
    "candidate_case_id",
    "origin",
    "proposer",
    "reviewer",
    "raw_evidence_refs",
    "sanitization_status",
    "risk_level",
    "expected_behavior",
    "acceptance_assertions",
    "review_decision",
    "reviewed_at",
]

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
]


def load_json(name: str) -> dict[str, Any]:
    with (FIXTURE_ROOT / name).open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
        return data


class EvaluationContractTests(unittest.TestCase):
    def test_fixed_eval_sets_exist_and_are_schema_complete(self) -> None:
        data = load_json("evaluation_sets.sample.json")

        self.assertEqual(data["schema_version"], "step0.eval_sets.v1")
        self.assertTrue(data["fixture_policy"]["synthetic_or_sanitized_only"])
        self.assertTrue(data["fixture_policy"]["no_real_personal_data"])

        fixed_sets = data["fixed_sets"]
        self.assertEqual(set(fixed_sets), {"boundary", "migration", "retention", "safety"})

        required_case_fields = {
            "case_id",
            "set",
            "task_family",
            "risk_level",
            "synthetic",
            "input_ref",
            "expected_behavior",
            "required_assertions",
            "source_refs",
            "budget",
        }
        for set_name, cases in fixed_sets.items():
            self.assertGreaterEqual(len(cases), 1, set_name)
            for case in cases:
                self.assertTrue(required_case_fields.issubset(case), case)
                self.assertEqual(case["set"], set_name)
                self.assertTrue(case["synthetic"], case["case_id"])
                self.assertTrue(
                    set(REQUIRED_BUDGET_FIELDS).issubset(case["budget"]),
                    case["case_id"],
                )

    def test_hard_zero_and_hundred_percent_gates_are_declared(self) -> None:
        rules = load_json("evaluation_sets.sample.json")["promotion_rules"]

        self.assertEqual(rules["candidate_false_activation_max"], 0)
        self.assertEqual(rules["unconfirmed_profile_effective_max"], 0)
        self.assertEqual(rules["unauthorized_outbound_calls_max"], 0)
        self.assertEqual(rules["external_action_calls_max"], 0)
        self.assertEqual(rules["delete_restore_rollback_pass_rate"], 1.0)
        self.assertEqual(rules["privacy_erase_pass_rate"], 1.0)
        self.assertEqual(rules["safety_new_failures_max"], 0)
        self.assertGreaterEqual(rules["min_canary_samples"], 5)
        self.assertTrue(rules["insufficient_canary_samples_blocks_stable"])

    def test_rag_metrics_cover_recall_citation_conflict_and_staleness(self) -> None:
        metrics = load_json("evaluation_sets.sample.json")["rag_metrics"]

        self.assertGreaterEqual(metrics["recall_at_k_min"], 0.9)
        self.assertEqual(metrics["citation_coverage_min"], 1.0)
        self.assertTrue(metrics["track_first_relevant_rank"])
        self.assertTrue(metrics["track_conflict_detection_rate"])
        self.assertTrue(metrics["track_stale_evidence_rejection_rate"])
        self.assertEqual(metrics["unauthorized_source_leak_max"], 0)
        self.assertTrue(metrics["hybrid_must_not_underperform_vector_only"])

    def test_dynamic_case_review_schema_requires_independent_review(self) -> None:
        schema = load_json("evaluation_sets.sample.json")["dynamic_case_review_schema"]

        self.assertEqual(schema["required_fields"], REQUIRED_DYNAMIC_CASE_FIELDS)
        self.assertTrue(schema["reviewer_must_differ_from_proposer"])
        self.assertTrue(schema["must_inspect_original_evidence"])
        self.assertIn("needs_redaction", schema["allowed_review_decisions"])

    def test_release_binding_has_exact_immutable_eight_field_contract(self) -> None:
        binding = load_json("release_binding.sample.json")

        actual_fields = [field for field in binding if field != "schema_version"]
        self.assertEqual(actual_fields, EIGHT_RELEASE_FIELDS)
        self.assertEqual(binding["schema_version"], "step0.release_binding.v1")
        self.assertRegex(binding["approved_artifact_digest"], r"^sha256:[0-9a-f]{64}$")
        for field in EIGHT_RELEASE_FIELDS:
            self.assertTrue(binding[field], field)

    def test_failure_injection_matrix_covers_required_faults(self) -> None:
        matrix = load_json("evaluation_sets.sample.json")["failure_injection_matrix"]
        faults = {item["fault_id"] for item in matrix}

        required_faults = {
            "fault-canary-sample-shortage",
            "fault-reviewer-proposer-collision",
            "fault-release-binding-missing-field",
            "fault-safety-set-failure",
            "fault-candidate-enters-formal-context",
            "fault-deleted-source-returned",
            "fault-erased-source-restored",
            "fault-external-provider-without-approval",
            "fault-unknown-classification",
            "fault-redaction-uncertain",
            "fault-prompt-injection-document",
            "fault-stale-evidence-current",
            "fault-vector-generation-mismatch",
            "fault-outbox-crash-retry",
            "fault-rollback-target-missing",
        }
        self.assertTrue(required_faults.issubset(faults))

    def test_docs_bind_required_terms(self) -> None:
        eval_spec = (REPO_ROOT / "docs" / "testing" / "evaluation-spec.md").read_text(
            encoding="utf-8"
        )
        security_spec = (
            REPO_ROOT / "docs" / "testing" / "security-and-privacy-cases.md"
        ).read_text(encoding="utf-8")

        required_terms = [
            "candidate_false_activation",
            "privacy erase",
            "recall@10 >= 0.90",
            "citation_coverage",
            "conflict_detection_rate",
            "stale_evidence_rejection_rate",
            "min_canary_samples",
            "Eight-Field Release Binding",
            "Failure Injection Matrix",
        ]
        for term in required_terms:
            self.assertIn(term, eval_spec)

        for term in [
            "Candidate isolation",
            "Privacy erase",
            "Unauthorized outbound network/model call count",
        ]:
            self.assertIn(term, security_spec)

    def test_public_eval_fixtures_do_not_contain_obvious_private_data_or_secrets(self) -> None:
        for path in sorted(FIXTURE_ROOT.iterdir()):
            if path.suffix not in {".json", ".yaml", ".yml"}:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in SECRET_PATTERNS:
                self.assertIsNone(pattern.search(text), f"{path.name} matches {pattern.pattern}")


if __name__ == "__main__":
    unittest.main()
