from dataclasses import replace

from zhiheng.evaluation.g006_evolution import EvaluationReport, G006CaseAssessment
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES
from zhiheng.evolution.releases import (
    _canonical_policy_snapshot,
    _fixed_set_snapshot_from_report,
)


def _report() -> EvaluationReport:
    return EvaluationReport(
        release_id="synthetic-release",
        set_name="promotion",
        scores={"result": 1, "process": 1, "quality": 1, "overall": 1},
        failure_count=0,
        learning_eligible=True,
        candidate_only=False,
        promotion_eligible=True,
        fixed_set_coverage={case.set_name: True for case in REGISTERED_FIXED_CASES},
        case_assessments=tuple(
            G006CaseAssessment(case.case_id, case.set_name, 1, 1, 1, (), "synthetic-digest")
            for case in REGISTERED_FIXED_CASES
        ),
    )


def test_fixed_snapshot_preserves_all_cases_without_self_attested_review() -> None:
    snapshot = _fixed_set_snapshot_from_report(_report())
    assert {name: len(cases) for name, cases in snapshot.items()} == {
        "boundary": 1,
        "migration": 1,
        "retention": 1,
        "safety": 4,
    }
    assert all("reviewed" not in case for cases in snapshot.values() for case in cases)


def test_earlier_safety_failure_is_not_overwritten_by_later_success() -> None:
    report = _report()
    assessments = list(report.case_assessments)
    index = next(i for i, case in enumerate(assessments) if case.set_name == "safety")
    assessments[index] = replace(assessments[index], failure_tags=("synthetic-failure",))
    report = replace(report, case_assessments=tuple(assessments))
    policy = _canonical_policy_snapshot(
        validation_report_ref="synthetic-validation",
        evaluation_report=report,
        promotion_report_digest="synthetic-promotion",
        canary_samples=5,
        source_trajectory_ids=("synthetic-trajectory",),
    )
    assert policy["safety_passed"] is False
