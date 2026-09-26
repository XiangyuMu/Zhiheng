"""Fixed synthetic evaluation for conversation conclusion extraction.

The score is intentionally computed from fresh extractor output for every
case. The fixture records expected claims and premises, while offsets are
validated against the source text at evaluation time.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zhiheng.conclusions.extraction import (
    ExtractedConclusion,
    HeuristicConversationConclusionExtractor,
)

MIN_DIALOGUES = 50
RECALL_THRESHOLD = 0.90
PREMISE_RECALL_THRESHOLD = 0.95
OFFSET_VALIDITY_THRESHOLD = 1.0
NEGATIVE_FALSE_EXTRACTIONS_MAX = 0


@dataclass(frozen=True, slots=True)
class Issue29Metrics:
    dialogue_count: int
    expected_claim_count: int
    matched_claim_count: int
    expected_premise_count: int
    matched_premise_count: int
    valid_offset_count: int
    offset_count: int
    negative_dialogue_count: int
    negative_false_extractions: int
    unexpected_extraction_count: int
    conclusion_recall: float
    premise_recall: float
    offset_validity: float


@dataclass(frozen=True, slots=True)
class Issue29EvaluationResult:
    metrics: Issue29Metrics
    passed: bool
    thresholds: Mapping[str, float | int]
    per_case: tuple[dict[str, Any], ...]


Extractor = Callable[..., list[ExtractedConclusion]]


def evaluate_issue29_conclusion_extraction(
    fixture_path: Path | None = None,
    *,
    extractor: Extractor | None = None,
) -> Issue29EvaluationResult:
    """Run the fixed Issue29 set through the supplied or production extractor."""

    fixture = _load_fixture(fixture_path)
    cases = fixture["cases"]
    if len(cases) < MIN_DIALOGUES:
        raise ValueError(f"Issue29 requires at least {MIN_DIALOGUES} fixed dialogues")

    extract = extractor or HeuristicConversationConclusionExtractor().extract
    per_case: list[dict[str, Any]] = []
    expected_claim_count = matched_claim_count = 0
    expected_premise_count = matched_premise_count = 0
    valid_offset_count = offset_count = 0
    negative_dialogue_count = negative_false_extractions = 0
    unexpected_extraction_count = 0

    for case in cases:
        source = _source_text(case)
        extracted = extract(query=str(case["query"]), answer=str(case.get("answer", "")))
        expected = case["expected"]
        remaining = list(enumerate(extracted))
        matched = 0
        case_premises = case_matched_premises = 0
        case_offsets = case_valid_offsets = 0
        for expected_item in expected:
            expected_claim_count += 1
            expected_premise_count += len(expected_item["premises"])
            case_premises += len(expected_item["premises"])
            match_index = next(
                (
                    index
                    for index, (_, candidate) in enumerate(remaining)
                    if candidate.claim == expected_item["claim"]
                ),
                None,
            )
            if match_index is None:
                continue
            _, item = remaining.pop(match_index)
            matched += 1
            matched_claim_count += 1
            premise_texts = {str(premise.get("text", "")) for premise in item.premises}
            case_matched_premises += sum(
                premise in premise_texts for premise in expected_item["premises"]
            )
            expected_start = int(expected_item["start_offset"])
            expected_end = int(expected_item["end_offset"])
            offset_count += 1
            case_offsets += 1
            if (
                expected_start >= 0
                and item.start_offset == expected_start
                and item.end_offset == expected_end
                and source[item.start_offset : item.end_offset] == expected_item["claim"]
            ):
                valid_offset_count += 1
                case_valid_offsets += 1

        matched_premise_count += case_matched_premises
        # Every actual output must be consumed by one gold item; duplicates
        # and extra claims therefore reduce the quality score.
        unexpected = [item.claim for _, item in remaining]
        unexpected_extraction_count += len(unexpected)
        negative = bool(case.get("negative", False))
        if negative:
            negative_dialogue_count += 1
            negative_false_extractions += len(extracted)
        per_case.append(
            {
                "case_id": str(case["case_id"]),
                "expected_claims": len(expected),
                "extracted_claims": len(extracted),
                "matched_claims": matched,
                "matched_premises": case_matched_premises,
                "expected_premises": case_premises,
                "valid_offsets": case_valid_offsets,
                "offsets": case_offsets,
                "negative": negative,
                "negative_false_extractions": len(extracted) if negative else 0,
                "unexpected_claims": unexpected,
            }
        )

    metrics = Issue29Metrics(
        dialogue_count=len(cases),
        expected_claim_count=expected_claim_count,
        matched_claim_count=matched_claim_count,
        expected_premise_count=expected_premise_count,
        matched_premise_count=matched_premise_count,
        valid_offset_count=valid_offset_count,
        offset_count=offset_count,
        negative_dialogue_count=negative_dialogue_count,
        negative_false_extractions=negative_false_extractions,
        unexpected_extraction_count=unexpected_extraction_count,
        conclusion_recall=_ratio(matched_claim_count, expected_claim_count),
        premise_recall=_ratio(matched_premise_count, expected_premise_count),
        offset_validity=_ratio(valid_offset_count, offset_count),
    )
    thresholds: dict[str, float | int] = {
        "minimum_dialogues": MIN_DIALOGUES,
        "conclusion_recall": RECALL_THRESHOLD,
        "premise_recall": PREMISE_RECALL_THRESHOLD,
        "offset_validity": OFFSET_VALIDITY_THRESHOLD,
        "negative_false_extractions_max": NEGATIVE_FALSE_EXTRACTIONS_MAX,
    }
    passed = (
        metrics.dialogue_count >= MIN_DIALOGUES
        and metrics.conclusion_recall >= RECALL_THRESHOLD
        and metrics.premise_recall >= PREMISE_RECALL_THRESHOLD
        and metrics.offset_validity >= OFFSET_VALIDITY_THRESHOLD
        and metrics.negative_false_extractions <= NEGATIVE_FALSE_EXTRACTIONS_MAX
        and metrics.unexpected_extraction_count == 0
    )
    return Issue29EvaluationResult(metrics, passed, thresholds, tuple(per_case))


def _load_fixture(path: Path | None) -> dict[str, Any]:
    fixture_path = path or (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "fixtures"
        / "conclusions"
        / "issue29_synthetic_dialogues.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("Issue29 fixture must contain a cases list")
    return data


def _source_text(case: Mapping[str, Any]) -> str:
    query = str(case["query"]).strip()
    answer = str(case.get("answer", "")).strip()
    return f"{query}\n助手：{answer}" if answer else query


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0
