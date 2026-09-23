from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from zhiheng.core.ids import sha256_text
from zhiheng.evaluation.g006_evolution import evaluate_g006_evolution
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "evolution" / "g006_lifecycle_cases.json"
)
APPROVED_EVAL_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "evals" / "evaluation_sets.sample.json"
)


def _load_fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "axis",
    [
        {"score": float("nan")},
        {"score": float("inf")},
        {"score": 2.0},
        {"score": -0.1},
        {"score": True},
        {"passed": "false"},
        {"clean": 1},
        {"violation": "false"},
    ],
)
def test_malformed_scores_cannot_pass(axis: dict[str, Any]) -> None:
    cases = _cases_for_set(_load_fixture(), "boundary")
    cases[0]["result"] = axis
    with pytest.raises(ValueError):
        evaluate_g006_evolution(release_id="synthetic-release", set_name="boundary", cases=cases)


def test_failure_tags_and_contradictory_success_are_preserved() -> None:
    cases = _cases_for_set(_load_fixture(), "boundary")
    cases[0]["failure_tags"] = ["safety.external_action"]
    cases[0]["result"] = {"score": 1.0, "passed": False}
    report = evaluate_g006_evolution(
        release_id="synthetic-release", set_name="boundary", cases=cases
    )
    assert report.failure_count == 1
    assert "safety.external_action" in report.case_assessments[0].failure_tags
    assert "result.failure" in report.case_assessments[0].failure_tags
    assert not report.promotion_eligible


def _case_by_id(report_cases: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    for case in report_cases:
        if case["case_id"] == case_id:
            return case
    raise AssertionError(case_id)


def _cases_for_set(fixture: dict[str, Any], set_name: str) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], json.loads(json.dumps(case)))
        for case in cast(list[dict[str, Any]], fixture["cases"])
        if case["set_name"] == set_name
    ]


def test_g006_mixed_set_fixture_is_rejected() -> None:
    fixture = _load_fixture()
    with pytest.raises(ValueError, match="case set does not match report set"):
        evaluate_g006_evolution(
            release_id=str(fixture["release_id"]),
            set_name=str(fixture["set_name"]),
            cases=cast(list[dict[str, Any]], fixture["cases"]),
        )


def test_g006_boundary_set_is_canonical() -> None:
    fixture = _load_fixture()
    cases = _cases_for_set(fixture, "boundary")
    report = evaluate_g006_evolution(
        release_id=str(fixture["release_id"]),
        set_name="boundary",
        cases=cases,
    )

    assert report.failure_count == 0
    assert report.learning_eligible is False
    assert report.candidate_only is False
    assert report.promotion_eligible is False
    assert report.fixed_set_coverage == {
        "boundary": True,
        "migration": False,
        "retention": False,
        "safety": False,
    }
    assert report.scores == {
        "overall": 1.0,
        "process": 1.0,
        "quality": 1.0,
        "result": 1.0,
    }
    assert report.canonical_digest() == f"sha256:{sha256_text(report.canonical_json())}"


def test_g006_process_violation_does_not_inherit_result_success() -> None:
    fixture = _load_fixture()
    cases = _cases_for_set(fixture, "boundary")
    for case in cases:
        if case["case_id"] == "boundary-001":
            case["process"]["status"] = "violation"
            break

    report = evaluate_g006_evolution(
        release_id=str(fixture["release_id"]),
        set_name="boundary",
        cases=cases,
    )
    case = _case_by_id(
        [dict(item.canonical_payload()) for item in report.case_assessments],
        "boundary-001",
    )

    assert case["result_score"] == 1.0
    assert case["process_score"] == 0.0
    assert report.failure_count == 1
    assert report.promotion_eligible is False
    assert report.learning_eligible is False


def test_g006_safety_failure_blocks_promotion() -> None:
    fixture = _load_fixture()
    cases = _cases_for_set(fixture, "safety")
    for case in cases:
        if case["case_id"] == "safety-001":
            case["result"]["status"] = "fail"
            break

    report = evaluate_g006_evolution(
        release_id=str(fixture["release_id"]),
        set_name="safety",
        cases=cases,
    )

    assert report.failure_count == 1
    assert report.fixed_set_coverage["safety"] is True
    assert report.promotion_eligible is False


def test_g006_missing_fixed_set_blocks_promotion() -> None:
    fixture = _load_fixture()
    cases = _cases_for_set(fixture, "boundary")

    report = evaluate_g006_evolution(
        release_id=str(fixture["release_id"]),
        set_name="boundary",
        cases=cases,
    )

    assert report.failure_count == 0
    assert report.fixed_set_coverage == {
        "boundary": True,
        "migration": False,
        "retention": False,
        "safety": False,
    }
    assert report.promotion_eligible is False
    assert report.learning_eligible is False


def test_g006_dynamic_set_is_always_candidate_only() -> None:
    fixture = _load_fixture()
    dynamic_case = next(
        case for case in json.loads(json.dumps(fixture["cases"])) if case["set_name"] == "dynamic"
    )

    report = evaluate_g006_evolution(
        release_id=str(fixture["release_id"]),
        set_name="dynamic",
        cases=[dynamic_case],
    )

    assert report.candidate_only is True
    assert report.promotion_eligible is False
    assert report.learning_eligible is False
    assert report.fixed_set_coverage == {
        "boundary": False,
        "migration": False,
        "retention": False,
        "safety": False,
    }


def test_g006_rejects_unredacted_payload_and_incomplete_evidence() -> None:
    fixture = _load_fixture()
    redacted_case = json.loads(json.dumps(fixture["cases"][0]))
    redacted_case["raw_payload"] = {"email": "alice@example.com"}

    with pytest.raises(ValueError, match="unredacted raw payload field"):
        evaluate_g006_evolution(
            release_id=str(fixture["release_id"]),
            set_name=str(fixture["set_name"]),
            cases=[redacted_case],
        )

    incomplete_case = json.loads(json.dumps(fixture["cases"][0]))
    del incomplete_case["evidence"]["quality"]

    with pytest.raises(ValueError, match="missing required quality evidence"):
        evaluate_g006_evolution(
            release_id=str(fixture["release_id"]),
            set_name=str(fixture["set_name"]),
            cases=[incomplete_case],
        )


def test_g006_registered_fixed_cases_cover_approved_fixture() -> None:
    fixture = json.loads(APPROVED_EVAL_FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_case_ids = {
        case["case_id"] for cases in fixture["fixed_sets"].values() for case in cases
    }
    registry_case_ids = {case.case_id for case in REGISTERED_FIXED_CASES}

    assert registry_case_ids == fixture_case_ids
