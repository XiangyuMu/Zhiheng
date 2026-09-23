from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zhiheng.core.ids import sha256_text

_FIXED_SET_NAMES = ("boundary", "migration", "retention", "safety")
_PROMOTION_SET_NAME = "promotion"
_ALLOWED_SET_NAMES = set(_FIXED_SET_NAMES) | {"dynamic", _PROMOTION_SET_NAME}
_REDACTED_KEYS = {
    "cookie",
    "email",
    "key",
    "phone",
    "raw_model_payload",
    "raw_payload",
    "raw_query",
    "raw_tool_payload",
    "tool_payload",
}
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b|\b\d{3}-\d{2}-\d{4}\b")


@dataclass(frozen=True, slots=True)
class G006CaseAssessment:
    case_id: str
    set_name: str
    result_score: float
    process_score: float
    quality_score: float
    failure_tags: tuple[str, ...]
    evidence_digest: str

    @property
    def passed(self) -> bool:
        return not self.failure_tags

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "evidence_digest": self.evidence_digest,
            "failure_tags": list(self.failure_tags),
            "process_score": self.process_score,
            "quality_score": self.quality_score,
            "result_score": self.result_score,
            "set_name": self.set_name,
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    release_id: str
    set_name: str
    scores: Mapping[str, float]
    failure_count: int
    learning_eligible: bool
    candidate_only: bool
    promotion_eligible: bool
    fixed_set_coverage: Mapping[str, bool]
    case_assessments: tuple[G006CaseAssessment, ...]

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "candidate_only": self.candidate_only,
            "case_assessments": [
                case.canonical_payload()
                for case in sorted(
                    self.case_assessments,
                    key=lambda item: (item.set_name, item.case_id),
                )
            ],
            "failure_count": self.failure_count,
            "fixed_set_coverage": dict(sorted(self.fixed_set_coverage.items())),
            "learning_eligible": self.learning_eligible,
            "promotion_eligible": self.promotion_eligible,
            "release_id": self.release_id,
            "scores": dict(sorted(self.scores.items())),
            "set_name": self.set_name,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def canonical_digest(self) -> str:
        return f"sha256:{sha256_text(self.canonical_json())}"


def evaluate_g006_evolution(
    *,
    release_id: str,
    set_name: str,
    cases: Sequence[Mapping[str, Any]],
) -> EvaluationReport:
    if not release_id:
        raise ValueError("release_id is required")
    _validate_set_name(set_name, field_name="set_name")

    parsed_cases = tuple(_parse_case(case, index=index) for index, case in enumerate(cases))
    for case in parsed_cases:
        case_set = str(case["set_name"])
        if set_name == "dynamic":
            if case_set != "dynamic":
                raise ValueError("dynamic report cannot include fixed-set cases")
        elif set_name == _PROMOTION_SET_NAME:
            if case_set == "dynamic":
                raise ValueError("promotion report cannot include dynamic cases")
        elif case_set == "dynamic":
            raise ValueError("promotion report cannot include dynamic cases")
        elif case_set != set_name and set_name in _FIXED_SET_NAMES:
            raise ValueError("case set does not match report set")

    assessments = tuple(_assess_case(case) for case in parsed_cases)
    ordered_assessments = tuple(sorted(assessments, key=lambda item: (item.set_name, item.case_id)))
    scores = _aggregate_scores(ordered_assessments)
    failure_count = sum(1 for case in ordered_assessments if not case.passed)
    fixed_set_coverage = {
        fixed_set: any(case.set_name == fixed_set for case in ordered_assessments)
        for fixed_set in _FIXED_SET_NAMES
    }
    candidate_only = set_name == "dynamic"
    all_fixed_sets_present = all(fixed_set_coverage.values())
    learning_eligible = not candidate_only and failure_count == 0 and all_fixed_sets_present
    promotion_eligible = learning_eligible
    return EvaluationReport(
        release_id=release_id,
        set_name=set_name,
        scores=scores,
        failure_count=failure_count,
        learning_eligible=learning_eligible,
        candidate_only=candidate_only,
        promotion_eligible=promotion_eligible,
        fixed_set_coverage=fixed_set_coverage,
        case_assessments=ordered_assessments,
    )


def _parse_case(case: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    _reject_unredacted_payload(case, path=f"cases[{index}]")
    case_id = _require_str(case, "case_id", path=f"cases[{index}]")
    set_name = _require_str(case, "set_name", path=f"cases[{index}]")
    _validate_set_name(set_name, field_name=f"cases[{index}].set_name")
    result = _require_mapping(case, "result", path=f"cases[{index}]")
    process = _require_mapping(case, "process", path=f"cases[{index}]")
    quality = _require_mapping(case, "quality", path=f"cases[{index}]")
    evidence = _require_mapping(case, "evidence", path=f"cases[{index}]")
    result_evidence = _require_evidence_layer(evidence, "result", path=f"cases[{index}].evidence")
    process_evidence = _require_evidence_layer(evidence, "process", path=f"cases[{index}].evidence")
    quality_evidence = _require_evidence_layer(evidence, "quality", path=f"cases[{index}].evidence")
    failure_tags = case.get("failure_tags", ())
    if not isinstance(failure_tags, (list, tuple)) or any(
        not isinstance(tag, str) or not tag.strip() for tag in failure_tags
    ):
        raise ValueError("failure_tags must be a sequence of nonempty strings")
    return {
        "case_id": case_id,
        "set_name": set_name,
        "result": result,
        "process": process,
        "quality": quality,
        "failure_tags": tuple(failure_tags),
        "evidence": {
            "process": process_evidence,
            "quality": quality_evidence,
            "result": result_evidence,
        },
    }


def _assess_case(case: Mapping[str, Any]) -> G006CaseAssessment:
    result_score, result_passed = _score_axis(
        case["result"], accepted_statuses={"ok", "pass", "passed", "success"}
    )
    process_score, process_passed = _score_axis(
        case["process"], accepted_statuses={"clean", "ok", "pass", "passed"}
    )
    quality_score, quality_passed = _score_axis(
        case["quality"],
        accepted_statuses={"acceptable", "good", "high", "ok", "pass", "passed"},
    )
    failure_tags = list(case["failure_tags"])
    if not result_passed:
        failure_tags.append("result.failure")
    if not process_passed:
        failure_tags.append("process.violation")
    if not quality_passed:
        failure_tags.append("quality.failure")
    evidence_digest = _evidence_digest(case["evidence"])
    return G006CaseAssessment(
        case_id=str(case["case_id"]),
        set_name=str(case["set_name"]),
        result_score=result_score,
        process_score=process_score,
        quality_score=quality_score,
        failure_tags=tuple(failure_tags),
        evidence_digest=evidence_digest,
    )


def _aggregate_scores(cases: Sequence[G006CaseAssessment]) -> dict[str, float]:
    if not cases:
        return {"overall": 0.0, "process": 0.0, "quality": 0.0, "result": 0.0}
    result_score = sum(case.result_score for case in cases) / len(cases)
    process_score = sum(case.process_score for case in cases) / len(cases)
    quality_score = sum(case.quality_score for case in cases) / len(cases)
    overall_score = (result_score + process_score + quality_score) / 3.0
    return {
        "overall": overall_score,
        "process": process_score,
        "quality": quality_score,
        "result": result_score,
    }


def _score_axis(axis: Mapping[str, Any], *, accepted_statuses: set[str]) -> tuple[float, bool]:
    for field in ("passed", "clean", "violation"):
        if field in axis and type(axis[field]) is not bool:
            raise ValueError(f"axis {field} must be a boolean")
    declared_failure = (
        axis.get("passed") is False or axis.get("clean") is False or axis.get("violation") is True
    )
    if "score" in axis:
        if type(axis["score"]) not in (int, float):
            raise ValueError("axis score must be a number")
        score = float(axis["score"])
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("axis score must be finite and between zero and one")
        return score, score == 1.0 and not declared_failure
    if declared_failure:
        return 0.0, False
    if "passed" in axis:
        passed = bool(axis["passed"])
        return (1.0 if passed else 0.0), passed
    if "clean" in axis:
        passed = bool(axis["clean"])
        return (1.0 if passed else 0.0), passed
    if "violation" in axis:
        passed = not bool(axis["violation"])
        return (1.0 if passed else 0.0), passed
    status = str(axis.get("status", "")).strip().lower()
    if not status:
        raise ValueError("axis status is required")
    passed = status in accepted_statuses
    return (1.0 if passed else 0.0), passed


def _evidence_digest(evidence: Mapping[str, Sequence[str]]) -> str:
    return f"sha256:{sha256_text(_canonical_json(_normalize_evidence(evidence)))}"


def _normalize_evidence(evidence: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    return {
        "process": list(evidence["process"]),
        "quality": list(evidence["quality"]),
        "result": list(evidence["result"]),
    }


def _require_evidence_layer(
    evidence: Mapping[str, Any],
    layer: str,
    *,
    path: str,
) -> tuple[str, ...]:
    if layer not in evidence:
        raise ValueError(f"{path} missing required {layer} evidence")
    refs = evidence[layer]
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise ValueError(f"{path}.{layer} evidence must be a sequence of refs")
    normalized = tuple(item for item in refs if isinstance(item, str) and item)
    if len(normalized) != len(tuple(refs)) or not normalized:
        raise ValueError(f"{path}.{layer} evidence must contain sanitized refs")
    return normalized


def _reject_unredacted_payload(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key).lower()
            if key_name in _REDACTED_KEYS:
                raise ValueError(f"{path} contains unredacted raw payload field: {key_name}")
            _reject_unredacted_payload(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_unredacted_payload(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if _EMAIL_RE.search(value):
            raise ValueError(f"{path} contains unredacted email data")
        if _PHONE_RE.search(value):
            raise ValueError(f"{path} contains unredacted phone data")


def _require_str(case: Mapping[str, Any], field: str, *, path: str) -> str:
    value = case.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}.{field} is required")
    return value


def _require_mapping(case: Mapping[str, Any], field: str, *, path: str) -> Mapping[str, Any]:
    value = case.get(field)
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}.{field} is required")
    return value


def _validate_set_name(set_name: str, *, field_name: str) -> None:
    if set_name not in _ALLOWED_SET_NAMES:
        raise ValueError(f"{field_name} must be one of {sorted(_ALLOWED_SET_NAMES)!r}")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
