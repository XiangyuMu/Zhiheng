from __future__ import annotations

import ast
from pathlib import Path


def test_memory_api_does_not_write_domain_state_tables() -> None:
    source_path = Path("src/zhiheng/api/memory.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_tables = {
        "memory_candidates",
        "memory_candidate_versions",
        "formal_memories",
        "formal_memory_versions",
        "memory_confirmation_requests",
        "memory_confirmation_decisions",
        "memory_generation_events",
        "memory_current_state",
        "memory_evidence_refs",
    }
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        sql = " ".join(node.value.upper().split())
        for table in forbidden_tables:
            upper_table = table.upper()
            if any(
                marker in sql
                for marker in (
                    f"INSERT INTO {upper_table}",
                    f"UPDATE {upper_table}",
                    f"DELETE FROM {upper_table}",
                )
            ):
                violations.append(f"line {node.lineno}: writes {table}")

    assert violations == []
