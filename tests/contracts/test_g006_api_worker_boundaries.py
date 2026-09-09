from __future__ import annotations

import ast
from pathlib import Path

API_PATH = Path("src/zhiheng/api/evolution.py")
JS_PATH = Path("src/zhiheng/api/static/evolution-center.js")


def test_evolution_api_does_not_import_release_controller_or_worker_executor() -> None:
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "ReleaseController" not in imported
    assert "EvolutionJobExecutor" not in imported
    assert "process_jobs_once" not in imported


def test_evolution_api_writes_only_user_command_or_receipt_tables() -> None:
    source = API_PATH.read_text(encoding="utf-8").lower()

    forbidden_writes = [
        "insert into evolution_proposals",
        "insert into validation_reports",
        "insert into review_reports",
        "insert into release_transition_events",
        "insert into strategy_release_heads",
        "update strategy_releases",
        "update strategy_release_heads",
        "delete from strategy_releases",
    ]
    for statement in forbidden_writes:
        assert statement not in source

    assert "insert or ignore into outbox_events" in source
    assert "memory_operation_receipts" not in source


def test_evolution_api_redaction_policy_covers_raw_model_tool_and_secret_fields() -> None:
    source = API_PATH.read_text(encoding="utf-8").lower()

    for token in ["raw_text", "query", "model_payload", "tool_args", "secret", "token"]:
        assert token in source


def test_evolution_center_does_not_contain_mock_or_placeholder_paths() -> None:
    script = JS_PATH.read_text(encoding="utf-8").lower()

    assert "501" not in script
    assert "mock" not in script
    assert "demo" not in script
    assert "/v1/evolution/" in script
