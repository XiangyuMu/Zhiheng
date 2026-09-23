from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"
G005_FIXTURE_DIRS = ("retrieval", "decisions", "gaps")

REQUIRED_CITATION_FIELDS = {
    "source_type",
    "source_id",
    "source_version_id",
    "span_start",
    "span_end",
    "quote_hash",
}
REQUIRED_AGENTIC_BUDGET_FIELDS = {
    "max_rounds",
    "max_subqueries",
    "max_retrieval_calls",
    "max_model_calls",
    "max_context_candidates",
    "max_wall_clock_ms",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
)
FORBIDDEN_API_TABLES = {
    "chunks",
    "fts_chunks",
    "chunk_embeddings",
    "embedding_generations",
    "knowledge_objects",
    "knowledge_versions",
    "formal_memories",
    "formal_memory_versions",
    "memory_candidates",
    "memory_candidate_versions",
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
        return data


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item: dict[str, Any] = json.loads(line)
        rows.append(item)
    return rows


def _g005_fixture_paths() -> Iterable[Path]:
    for dirname in G005_FIXTURE_DIRS:
        yield from sorted((FIXTURE_ROOT / dirname).glob("*"))


def test_g005_retrieval_corpus_declares_authorization_and_citation_expectations() -> None:
    rows = _load_jsonl(FIXTURE_ROOT / "retrieval" / "g005_corpus.jsonl")

    serving = [row for row in rows if row["expected_serving"]]
    forbidden = [row for row in rows if not row["expected_serving"]]
    forbidden_reasons = {str(row["forbidden_reason"]) for row in forbidden}

    assert len(serving) >= 2
    assert {
        "candidate namespace",
        "soft deleted",
        "privacy erased",
        "stale source_version_id",
        "wrong confirmation generation",
    }.issubset(forbidden_reasons)
    for row in serving:
        assert REQUIRED_CITATION_FIELDS.issubset(row["expected_citation"])
        assert row["expected_citation"]["source_version_id"] == row["source_version_id"]
        assert row["expected_citation"]["quote_hash"] == row["quote_hash"]


def test_g005_query_fixtures_lock_routes_budgets_and_forbidden_sources() -> None:
    cases = _load_json(FIXTURE_ROOT / "retrieval" / "g005_queries.json")["cases"]
    by_id = {str(case["case_id"]): case for case in cases}

    structured = by_id["structured-goal-direct-lookup"]
    hybrid = by_id["hybrid-current-version-citation"]
    agentic = by_id["agentic-no-new-evidence-stops"]

    assert structured["expected_route"] == "structured_lookup"
    assert structured["max_model_calls"] == 0
    assert "agentic_rag" in structured["forbidden_routes"]
    assert set(hybrid["required_citation_fields"]) == REQUIRED_CITATION_FIELDS
    assert {"ko-candidate", "ko-deleted", "ko-erased"}.issubset(set(hybrid["forbidden_source_ids"]))
    assert REQUIRED_AGENTIC_BUDGET_FIELDS.issubset(agentic)
    assert {"no_new_evidence", "repeated_query"}.issubset(set(agentic["must_stop_on"]))


def test_g005_decision_fixtures_forbid_external_action_capabilities() -> None:
    cases = _load_json(FIXTURE_ROOT / "decisions" / "g005_decision_cases.json")["cases"]

    case = cases[0]

    assert case["external_action_allowed"] is False
    assert {"trade", "message_send", "purchase", "publish", "email_send"}.issubset(
        set(case["forbidden_capabilities"])
    )


def test_g005_gap_fixtures_require_formal_goal_and_non_deficit_language() -> None:
    cases = _load_json(FIXTURE_ROOT / "gaps" / "g005_gap_cases.json")["cases"]
    by_id = {str(case["case_id"]): case for case in cases}

    formal = by_id["formal-goal-produces-gap"]
    candidate = by_id["candidate-goal-produces-no-gap"]

    assert formal["goal_status"] == "formal_current"
    assert {"why", "benefit", "evidence", "missing_coverage"}.issubset(
        set(formal["expected_recommendation_fields"])
    )
    assert formal["must_not_claim_user_lacks_ability"] is True
    assert formal["auto_ingest_allowed"] is False
    assert candidate["goal_status"] == "pending_confirmation"
    assert candidate["expected_recommendations"] == 0


def test_g005_public_fixtures_contain_only_synthetic_sanitized_data() -> None:
    for path in _g005_fixture_paths():
        if path.suffix not in {".json", ".jsonl"}:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            assert pattern.search(text) is None, f"{path} matches {pattern.pattern}"


def test_g005_api_modules_do_not_directly_write_domain_tables() -> None:
    from zhiheng.api import decisions, gaps, retrieval

    api_paths = [
        REPO_ROOT / "src" / "zhiheng" / "api" / "retrieval.py",
        REPO_ROOT / "src" / "zhiheng" / "api" / "decisions.py",
        REPO_ROOT / "src" / "zhiheng" / "api" / "gaps.py",
    ]

    route_paths = {
        route.path for module in (retrieval, decisions, gaps) for route in module.router.routes
    }
    assert {
        "/v1/answers",
        "/v1/lookups/memory/{state_key:path}",
        "/v1/lookups/knowledge/{knowledge_id}",
        "/v1/decisions/analyze",
        "/v1/decisions/{run_id}/save",
        "/v1/knowledge-gaps/refresh",
        "/v1/knowledge-gaps",
        "/v1/knowledge-gaps/{gap_id}/dismiss",
    }.issubset(route_paths)

    for path in api_paths:
        assert path.exists(), f"{path} is required"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        source = path.read_text(encoding="utf-8")
        has_raw_sql_execute = any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "execute" for call in calls
        )
        forbidden_table_mentions = [
            table for table in FORBIDDEN_API_TABLES if re.search(rf"\b{table}\b", source)
        ]
        assert not has_raw_sql_execute, f"{path} executes SQL directly"
        assert forbidden_table_mentions == [], f"{path} mentions {forbidden_table_mentions}"
