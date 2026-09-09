from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from zhiheng.evaluation import evaluate_g005_retrieval_runtime

pytest.importorskip("sqlite_vec")


def test_g005_runtime_retrieval_metrics_meet_acceptance_gates(tmp_path: Path) -> None:
    result = evaluate_g005_retrieval_runtime(tmp_path)

    failure_report = json.dumps(
        {
            "metrics": asdict(result.metrics),
            "thresholds": result.thresholds,
            "per_query_details": result.per_query_details,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    assert result.metrics.exact_lookup_accuracy >= 0.95, failure_report
    assert result.metrics.recall_at_10 >= 0.90, failure_report
    assert (
        result.metrics.hybrid_recall_at_10 >= result.metrics.vector_only_recall_at_10
    ), failure_report
    assert result.metrics.fact_critical_citation_coverage == 1.0, failure_report
    assert result.metrics.unauthorized_source_leak_count == 0, failure_report
    assert result.metrics.candidate_false_activation_count == 0, failure_report
    assert result.metrics.external_action_calls == 0, failure_report
    assert result.metrics.conflict_surfaced, failure_report
    assert result.metrics.empty_db_no_hallucination, failure_report
    assert result.passed, failure_report
