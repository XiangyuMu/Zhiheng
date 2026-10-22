"""The delivery collector must preserve failures and isolate local configuration."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def collector() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts/delivery_acceptance.py"
    spec = importlib.util.spec_from_file_location("delivery_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collector_drops_production_and_test_selection_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "ZHIHENG_DATABASE_URL",
        "RESTIC_PASSWORD",
        "PYTHONPATH",
        "PYTEST_ADDOPTS",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
        "BROWSER_CHANNEL",
        "PLAYWRIGHT_MODULE_PATH",
    ):
        monkeypatch.setenv(key, "must-not-inherit")
    env = collector().clean_environment()
    assert all(value != "must-not-inherit" for value in env.values())
    assert "PATH" in env


def test_collector_preserves_required_recovery_and_artifact_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_ISSUE54_FAULTS_JSON", "[]")
    monkeypatch.setenv("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH", "/private/object-store")
    env = collector().clean_environment()
    assert env["ZHIHENG_ISSUE54_FAULTS_JSON"] == "[]"
    assert env["ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH"] == "/private/object-store"


@pytest.mark.parametrize("code,status", [(0, "passed"), (3, "failed")])
def test_collector_retains_exit_status_and_output(tmp_path: Path, code: int, status: str) -> None:
    module = collector()
    command = [sys.executable, "-c", f"print('synthetic diagnostic'); raise SystemExit({code})"]
    result = module.execute(tmp_path, tmp_path, "probe", command, 5, module.clean_environment())
    assert result["status"] == status
    assert result["exit_code"] == code
    assert "synthetic diagnostic" in (tmp_path / "probe.log").read_text()


def test_collector_bounds_hung_step(tmp_path: Path) -> None:
    module = collector()
    result = module.execute(
        tmp_path,
        tmp_path,
        "timeout",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        0.1,
        module.clean_environment(),
    )
    assert result["status"] == "timeout"
    assert result["seconds"] < 10


def test_collector_preserves_process_group_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = collector()
    original_killpg = module.os.killpg

    def deny_cleanup(pid: int, signal_number: int) -> None:
        if signal_number == module.signal.SIGTERM:
            raise PermissionError("cleanup denied")
        original_killpg(pid, signal_number)

    monkeypatch.setattr(module.os, "killpg", deny_cleanup)
    result = module.execute(
        tmp_path,
        tmp_path,
        "cleanup-failure",
        [sys.executable, "-c", "print('done')"],
        5,
        module.clean_environment(),
    )
    assert result["status"] == "failed"
    assert "SIGTERM: cleanup denied" in result["cleanup_error"]


@pytest.mark.parametrize("profile,backend", [("pdf", "mineru"), ("deepdoc", "deepdoc")])
def test_parser_profile_maps_to_supported_application_backend(profile: str, backend: str) -> None:
    assert collector().parser_backend(profile) == backend


def test_unknown_parser_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="parser profile"):
        collector().parser_backend("unknown")


def test_host_runtime_requires_explicit_token_and_real_warmup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = collector()
    monkeypatch.setenv("ZHIHENG_MINERU_RUNTIME", "host")
    monkeypatch.delenv("ZHIHENG_MINERU_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError, match="TOKEN_FILE"):
        module.mineru_command("mineru-warmup", tmp_path)
    monkeypatch.setenv("ZHIHENG_MINERU_TOKEN_FILE", "/private/token")
    command = module.mineru_command("mineru-warmup", tmp_path)
    assert command[1] == "scripts/mineru_http_warmup.py"
    assert "--token-file" in command
    assert "/private/token" in command
    assert module.clean_environment()["ZHIHENG_MINERU_TOKEN_FILE"] == "/private/token"
    monkeypatch.setenv("ZHIHENG_MINERU_GATEWAY_URL", "http://user:secret@example.com?token=secret")
    with pytest.raises(ValueError, match="loopback"):
        module.mineru_command("mineru-warmup", tmp_path)
    monkeypatch.setenv("ZHIHENG_MINERU_GATEWAY_URL", "http://127.0.0.1:9392")
    monkeypatch.setenv("MINERU_WARMUP_TIMEOUT_SECONDS", "12.5")
    assert "12.5" in module.mineru_command("mineru-warmup", tmp_path)
    monkeypatch.setenv("ZHIHENG_MINERU_RUNTIME", "skip")
    with pytest.raises(ValueError, match="host or compose"):
        module.mineru_command("mineru-warmup", tmp_path)


def test_pdf_evidence_matrix_requires_all_issue_rows() -> None:
    module = collector()
    matrix = module.load_pdf_evidence_matrix(Path(__file__).resolve().parents[2])
    assert [item["issue"] for item in matrix] == [52, 53, 54, 55, 56]
    assert all(item["requirements"] for item in matrix)


def evidence_matrix() -> list[dict[str, Any]]:
    return [
        {"issue": 52, "artifact": "pdf-parse/report.json"},
        {"issue": 53, "artifact": "pdf-search/report.json"},
        {"issue": 54, "artifact": "pdf-recovery/report.json"},
        {"issue": 55, "artifact": "real-files/browser-report.json"},
        {"issue": 56, "artifact": "report.json"},
    ]


def delivery_steps() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "status": "passed",
            "exit_code": 0,
            "seconds": 1.0,
            "command": ["run", name],
        }
        for name in (
            "dependencies",
            "versions",
            "compile",
            "ruff",
            "mypy",
            "pytest",
            "browser",
            "real-pdf-preflight",
            "mineru-warmup",
            "parser-health",
            "pdf-parse",
            "pdf-search",
            "pdf-recovery",
            "real-files",
            "extraction",
        )
    ]


def valid_payloads() -> dict[int, dict[str, Any]]:
    fault_rows: list[dict[str, Any]] = []
    for category in (
        "lost_parser_attempt",
        "parser_failure",
        "partial_or_unsupported",
        "auto_retry_exhausted",
        "manual_ui_retry",
    ):
        task_id = f"fault-{category}"
        row: dict[str, Any] = {
            "name": category,
            "category": category,
            "status": "passed",
            "expected_state": "failed",
            "expected_error_code": f"{category}_code",
            "task_id": task_id,
            "source_sha256": "source",
            "observed": {
                "state": "failed",
                "error_code": f"{category}_code",
                "polling": {"polling_stopped": True},
            },
            "command_proof": {
                "injected": True,
                "boundary": "external_parser",
                "category": category,
                "task_id": task_id,
                "attempt_id": f"attempt-{category}",
                "observed_identity": f"upstream-{category}",
            },
        }
        if category == "auto_retry_exhausted":
            observed = row["observed"]
            assert isinstance(observed, dict)
            observed["retry_count"] = 3
        if category == "manual_ui_retry":
            row["ui_retry_clicked"] = True
            row["retry_result"] = {
                "task_id": task_id,
                "attempt_id": "retry-attempt",
                "source_sha256": "source",
                "manifest_sha256": "retry-manifest",
                "searchable": {
                    "index_status": "active",
                    "physical_index_ref": "sqlite_vec:retry",
                },
            }
        fault_rows.append(row)
    answer_base = {
        "status": 200,
        "stop_reason": "completed",
        "answer_provider_id": "provider",
        "answer_model_id": "model",
        "answer_audit_id": "audit",
    }
    return {
        52: {
            "status": "passed",
            "sha": "same-sha",
            "input": {"sha256": "source"},
            "receipt": {
                "body": {
                    "task_id": "task",
                    "evidence_object_id": "evidence",
                    "source_sha256": "source",
                }
            },
            "parsed": {
                "state": "parsed",
                "backend": "mineru",
                "attempt_id": "attempt",
                "evidence_object_id": "evidence",
                "source_sha256": "source",
                "manifest_uri": "object://manifest",
                "manifest_sha256": "digest",
            },
            "manifest": {
                "uri": "object://manifest",
                "sha256": "digest",
                "page_count": 1,
                "block_count": 1,
            },
            "artifacts": {"manifest": {}, "pages": [], "images": []},
        },
        53: {
            "status": "passed",
            "sha": "same-sha",
            "input": {"sha256": "source"},
            "receipt": {
                "body": {
                    "task_id": "task-53",
                    "evidence_object_id": "evidence-53",
                    "source_sha256": "source",
                }
            },
            "search": [{"hit_found": True}],
            "hit": {
                "knowledge_object_id": "knowledge-53",
                "evidence_object_id": "evidence-53",
                "source_sha256": "source",
                "media_type": "application/pdf",
                "searchable": True,
            },
            "detail": {
                "knowledge_object_id": "knowledge-53",
                "searchable": True,
                "media_type": "application/pdf",
                "citation_count": 1,
                "retrieval_generation": {
                    "provider_id": "embedding-provider",
                    "model_id": "embed",
                    "model_revision": "rev",
                    "dimension": 1024,
                    "purpose": "retrieval",
                    "index_status": "active",
                    "physical_index_ref": "sqlite_vec:1",
                },
            },
        },
        54: {
            "status": "passed",
            "sha": "same-sha",
            "input": {"sha256": "source"},
            "restart_scenarios": [
                {
                    "kind": kind,
                    "status": "passed",
                    "receipt": {
                        "task_id": f"restart-{kind}",
                        "source_sha256": "source",
                        "evidence_object_id": f"evidence-{kind}",
                        "parser_attempt_id": f"attempt-{kind}",
                    },
                    "active_lease_before_restart": {
                        "jobs": [
                            {
                                "status": "processing",
                                "attempts": 1,
                                "lease_owner_sha256": "lease",
                            }
                        ]
                    },
                    **(
                        {
                            "active_parser_before_restart": {
                                "task_id": "restart-gateway",
                                "attempt_id": "attempt-gateway",
                                "state": "running",
                                "upstream_task_id": "upstream-task",
                                "ready": True,
                            }
                        }
                        if kind == "gateway"
                        else {}
                    ),
                    "terminal": {
                        "state": "parsed",
                        "task_id": f"restart-{kind}",
                        "evidence_object_id": f"evidence-{kind}",
                        "source_sha256": "source",
                        "manifest_sha256": f"manifest-{kind}",
                        "manifest_uri": f"file:///objects/{kind}/manifest.json",
                    },
                    "searchable": {
                        "knowledge_object_id": f"knowledge-{kind}",
                        "evidence_object_id": f"evidence-{kind}",
                        "index_status": "active",
                        "physical_index_ref": f"sqlite_vec:{kind}",
                    },
                    "elapsed_ms": 1000,
                }
                for kind in ("worker", "gateway")
            ],
            "restarts": [
                {
                    "kind": kind,
                    "status": "succeeded",
                    "exit_code": 0,
                    "command_sha256": f"{kind}-digest",
                    "before_identity_sha256": f"{kind}-before",
                    "after_identity_sha256": f"{kind}-after",
                    "proof": {
                        "kind": kind,
                        "task_id": f"restart-{kind}",
                        "observed_identity": f"{kind}-identity",
                        "ready": True,
                    },
                }
                for kind in ("worker", "gateway")
            ],
            "fault_injections": fault_rows,
        },
        55: {
            "status": "passed",
            "sha": "same-sha",
            "imports": [
                {
                    "file": "notes.txt",
                    "sha256": "b" * 64,
                    "knowledge": {
                        "knowledge_object_id": "text",
                        "knowledge_version_id": "text-version",
                        "evidence_object_id": "text-evidence",
                        "source_sha256": "c" * 64,
                        "source_file_sha256": "b" * 64,
                    },
                },
                {
                    "file": "Comments.pdf",
                    "sha256": "a" * 64,
                    "knowledge": {
                        "knowledge_object_id": "pdf",
                        "knowledge_version_id": "pdf-version",
                        "evidence_object_id": "pdf-evidence",
                        "source_sha256": "a" * 64,
                        "source_file_sha256": "a" * 64,
                    },
                },
            ],
            "answers": [
                {
                    **answer_base,
                    "kind": "negative",
                    "citations": 0,
                    "citation_payload_count": 0,
                    "claims_count": 0,
                    "insufficiency_count": 1,
                },
                {
                    **answer_base,
                    "kind": "text",
                    "citations": 1,
                    "citation_payload_count": 1,
                    "claims_count": 1,
                    "bound_source": {
                        "knowledge_object_id": "text",
                        "knowledge_version_id": "text-version",
                        "evidence_object_id": "text-evidence",
                        "source_sha256": "c" * 64,
                    },
                    "bound_citation": {
                        "citation_id": "text-citation",
                        "source_id": "text",
                        "source_version_id": "text-version",
                        "evidence_object_id": "text-evidence",
                        "content_version_id": "text-content",
                        "content_span_id": "text-span",
                        "page_no": None,
                    },
                },
                {
                    **answer_base,
                    "kind": "pdf",
                    "citations": 1,
                    "citation_payload_count": 1,
                    "claims_count": 1,
                    "bound_source": {
                        "knowledge_object_id": "pdf",
                        "knowledge_version_id": "pdf-version",
                        "evidence_object_id": "pdf-evidence",
                        "source_sha256": "a" * 64,
                    },
                    "bound_citation": {
                        "citation_id": "pdf-citation",
                        "source_id": "pdf",
                        "source_version_id": "pdf-version",
                        "evidence_object_id": "pdf-evidence",
                        "content_version_id": "pdf-content",
                        "content_span_id": "pdf-span",
                        "page_no": 1,
                    },
                },
            ],
        },
        56: {
            "status": "passed",
            "sha": "same-sha",
            "clean_before": True,
            "clean_after": True,
            "same_sha": True,
            "steps": delivery_steps(),
        },
    }


def write_payloads(root: Path, payloads: dict[int, dict[str, Any]]) -> None:
    failure_path = root / "browser/answer-failures-issue55/report.json"
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.write_text(json.dumps(failure_evidence()))
    for item in evidence_matrix():
        path = root / str(item["artifact"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payloads[int(str(item["issue"]))]))
    pytest_path = root / "pytest" / "evidence.json"
    pytest_path.parent.mkdir(parents=True, exist_ok=True)
    pytest_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "sha": "same-sha",
                "termination": "completed",
                "exit_code": 0,
                "report_complete": True,
                "clean_after": True,
                "budget_status": "met",
                "skips": [],
                "failed_nodes": [],
            }
        )
    )


def test_stage_evidence_requires_passed_same_sha_reports(tmp_path: Path) -> None:
    module = collector()
    payloads = valid_payloads()
    write_payloads(tmp_path, payloads)
    module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")

    (tmp_path / "pdf-search/report.json").write_text('{"status":"failed","sha":"same-sha"}')
    with pytest.raises(ValueError, match="Issue 53"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")

    payloads = valid_payloads()
    payloads[53]["sha"] = "wrong-sha"
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="not bound"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payloads: payloads[52].__setitem__("receipt", {"body": []}), "Issue 52"),
        (lambda payloads: payloads[53].__setitem__("hit", []), "Issue 53"),
        (
            lambda payloads: payloads[54]["fault_injections"].__setitem__(0, "bad"),
            "Issue 54",
        ),
        (lambda payloads: payloads[55].__setitem__("answers", ["bad"]), "Issue 55"),
        (lambda payloads: payloads[56].__setitem__("steps", [1]), "Issue 56"),
        (lambda payloads: payloads[56]["steps"].append(payloads[56]["steps"][0]), "Issue 56"),
        (
            lambda payloads: payloads[56]["steps"][0].__setitem__("command", "not-a-list"),
            "Issue 56",
        ),
    ],
)
def test_stage_evidence_rejects_malformed_nested_values(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    module = collector()
    payloads = copy.deepcopy(valid_payloads())
    mutate(payloads)
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match=message):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


def test_stage_evidence_requires_pytest_delivery_evidence(tmp_path: Path) -> None:
    module = collector()
    payloads = valid_payloads()
    write_payloads(tmp_path, payloads)
    (tmp_path / "pytest" / "evidence.json").write_text(
        '{"status":"failed","sha":"same-sha","termination":"completed","exit_code":1,'
        '"report_complete":true,"clean_after":true,"budget_status":"met",'
        '"skips":[],"failed_nodes":[]}'
    )
    with pytest.raises(ValueError, match="pytest evidence"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


def test_stage_evidence_requires_manual_retry_and_answer_audit(tmp_path: Path) -> None:
    module = collector()
    payloads = copy.deepcopy(valid_payloads())
    faults = payloads[54]["fault_injections"]
    assert isinstance(faults, list)
    manual = next(item for item in faults if item["category"] == "manual_ui_retry")
    manual.pop("ui_retry_clicked")
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="manual retry"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")

    payloads = copy.deepcopy(valid_payloads())
    answers = payloads[55]["answers"]
    assert isinstance(answers, list)
    answers[0].pop("answer_audit_id")
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="answer audit"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


@pytest.mark.parametrize(
    ("issue", "mutate", "message"),
    [
        (52, lambda payloads: payloads[52]["parsed"].__setitem__("backend", "deepdoc"), "MinerU"),
        (
            53,
            lambda payloads: payloads[53]["detail"]["retrieval_generation"].__setitem__(
                "index_status", "building"
            ),
            "active",
        ),
    ],
)
def test_stage_evidence_rejects_unqualified_pdf_proofs(
    tmp_path: Path,
    issue: int,
    mutate: Any,
    message: str,
) -> None:
    module = collector()
    payloads = copy.deepcopy(valid_payloads())
    mutate(payloads)
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match=message):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


def test_stage_evidence_requires_delivery_report(tmp_path: Path) -> None:
    module = collector()
    matrix = [{"issue": 56, "artifact": "report.json"}]
    with pytest.raises(ValueError, match="Issue 56 evidence report is missing"):
        module.validate_stage_evidence(tmp_path, matrix, "same-sha")
    (tmp_path / "report.json").write_text('{"status":"passed","sha":"wrong-sha"}')
    with pytest.raises(ValueError, match="not bound"):
        module.validate_stage_evidence(tmp_path, matrix, "same-sha")


def test_synthetic_environment_does_not_inherit_real_storage_or_routes() -> None:
    module = collector()
    private = {
        "ZHIHENG_PDF_PARSER_BACKEND": "mineru",
        "ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID": "real-answer-provider",
        "ZHIHENG_EXPECTED_ANSWER_MODEL_ID": "real-answer-model",
        "ZHIHENG_EXPECTED_EMBEDDING_PROVIDER_ID": "real-embedding-provider",
        "ZHIHENG_EXPECTED_EMBEDDING_MODEL_ID": "real-embedding-model",
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": "/real/objects",
    }
    original = dict(private, PATH="/bin", ZHIHENG_DELIVERY_EVIDENCE="/evidence")
    isolated = module.synthetic_environment(original)
    assert not private.keys() & isolated.keys()
    assert isolated["ZHIHENG_DELIVERY_EVIDENCE"] == "/evidence"
    assert all(original[key] == value for key, value in private.items())


def test_browser_stage_runs_synthetic_workers_without_the_real_pdf_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = collector()
    repo = tmp_path / "checkout"
    repo.mkdir()
    output = tmp_path / "evidence"
    monkeypatch.setenv("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH", "/real/objects")
    monkeypatch.setattr(sys, "argv", ["delivery", "--output", str(output)])
    monkeypatch.setattr(
        module,
        "git",
        lambda root, *args: (
            str(repo)
            if args == ("rev-parse", "--show-toplevel")
            else "same-sha"
            if args == ("rev-parse", "HEAD")
            else ""
        ),
    )
    monkeypatch.setattr(module, "load_pdf_evidence_matrix", lambda root: evidence_matrix())
    monkeypatch.setattr(module, "mineru_command", lambda *args: ["synthetic"])
    environments: dict[str, dict[str, str]] = {}

    def run(
        root: Path,
        dest: Path,
        name: str,
        argv: list[str],
        timeout: float | None,
        env: dict[str, str],
    ) -> dict[str, Any]:
        environments[name] = env
        return {"name": name, "command": argv, "status": "passed", "exit_code": 0, "seconds": 0.01}

    monkeypatch.setattr(module, "execute", run)
    module.main()
    # The browser stage starts its own synthetic Worker, which must not inherit
    # the real MinerU declaration (it has no parser URL and would refuse to start).
    assert "ZHIHENG_PDF_PARSER_BACKEND" not in environments["browser"]
    assert "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH" not in environments["browser"]
    assert environments["pdf-parse"]["ZHIHENG_PDF_PARSER_BACKEND"] == "mineru"


def test_runtime_checkout_is_preserved_for_real_restart_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_RUNTIME_CHECKOUT", "/isolated/checkout")
    assert collector().clean_environment()["ZHIHENG_RUNTIME_CHECKOUT"] == "/isolated/checkout"


def test_executor_timeout_reaps_shell_descendants(tmp_path: Path) -> None:
    import shlex
    import subprocess
    import time

    module = collector()
    pid_file = tmp_path / "child.pid"
    child = "import time; print('child ready', flush=True); time.sleep(60)"
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} & "
        f"echo $! > {shlex.quote(str(pid_file))}; wait"
    )
    result = module.execute(
        tmp_path,
        tmp_path,
        "descendant",
        ["bash", "-c", command],
        1,
        module.clean_environment(),
    )
    assert result["status"] == "timeout"
    pid = int(pid_file.read_text())
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
    )
    assert state.returncode != 0 or state.stdout.strip().startswith("Z")
    log = tmp_path / "descendant.log"
    original = log.read_bytes()
    assert b"child ready" in original
    time.sleep(0.05)
    assert log.read_bytes() == original


@pytest.mark.parametrize("outcome", ["success", "nonzero", "timeout"])
def test_restart_result_is_private_and_failure_stops_pdf_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    import hashlib
    import shlex

    module = collector()
    repo = tmp_path / "checkout"
    repo.mkdir()
    output = tmp_path / "evidence"
    marker = "SYNTHETIC_PRIVATE_RESTART_ARGUMENT"
    code = {
        "success": "raise SystemExit(0)",
        "nonzero": "raise SystemExit(7)",
        "timeout": "import time; time.sleep(60)",
    }[outcome]
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {marker}"
    monkeypatch.setenv("ZHIHENG_RESTART_WORKER_COMMAND", command)
    monkeypatch.setattr(sys, "argv", ["delivery", "--output", str(output)])
    monkeypatch.setattr(
        module,
        "git",
        lambda root, *args: (
            str(repo)
            if args == ("rev-parse", "--show-toplevel")
            else "same-sha"
            if args == ("rev-parse", "HEAD")
            else ""
        ),
    )
    monkeypatch.setattr(module, "load_pdf_evidence_matrix", lambda root: evidence_matrix())
    monkeypatch.setattr(module, "mineru_command", lambda *args: ["synthetic"])
    calls: list[str] = []
    execute = module.execute

    def run(
        root: Path,
        dest: Path,
        name: str,
        argv: list[str],
        timeout: float | None,
        env: dict[str, str],
    ) -> dict[str, Any]:
        calls.append(name)
        if name == "pdf-worker-restart":
            # Only the timeout case needs a short budget; the others must
            # survive a cold interpreter start on a loaded CI runner.
            budget = 0.3 if outcome == "timeout" else 30.0
            return cast(dict[str, Any], execute(root, dest, name, argv, budget, env))
        return {"name": name, "command": argv, "status": "passed", "exit_code": 0, "seconds": 0.01}

    monkeypatch.setattr(module, "execute", run)
    module.main()
    serialized = (output / "report.json").read_text()
    assert marker not in serialized
    report = json.loads(serialized)
    step = next(s for s in report["steps"] if s["name"] == "pdf-parse")
    restart = step["runtime_restart"]
    assert "command" not in restart
    assert restart["command_sha256"] == hashlib.sha256(command.encode()).hexdigest()
    assert (
        restart["status"]
        == {
            "success": "passed",
            "nonzero": "failed",
            "timeout": "timeout",
        }[outcome]
    )
    assert ("pdf-parse" in calls) == (outcome == "success")
    if outcome == "nonzero":
        assert restart["exit_code"] == 7
    if outcome != "success":
        assert report["status"] == "failed"
        assert "pdf-search" not in calls


def test_collector_creates_private_evidence_with_permissive_umask(tmp_path: Path) -> None:
    import os
    import stat

    module = collector()
    previous = os.umask(0)
    try:
        output = tmp_path / "private-run"
        module.create_evidence_directory(output)
        module.write_report(output, {"status": "failed"})
        result = module.execute(
            tmp_path,
            output,
            "failure",
            [sys.executable, "-c", "raise SystemExit(1)"],
            5,
            module.clean_environment(),
        )
        assert result["exit_code"] == 1
        assert stat.S_IMODE(output.stat().st_mode) == 0o700
        for path in (output / "report.json", output / "failure.log"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(FileExistsError):
            module.create_evidence_directory(output)
    finally:
        os.umask(previous)


def failure_evidence() -> dict[str, Any]:
    return {
        "status": "passed",
        "sha": "same-sha",
        "injected": True,
        "boundary": "model_transport",
        "command": ["node", "tests/e2e/check_answer_failures_issue55.cjs"],
        "input_digest": "a" * 64,
        "elapsed_ms": 123,
        "exit_code": 0,
        "cases": [
            {
                "kind": kind,
                "status": "passed",
                "stop_reason": stop,
                "diagnostic_code": code,
                "audit_id": kind,
                "provider_id": "provider",
                "response_diagnostic_code": code,
                "model_id": "model",
                "dispatch_count": 1,
                "ui_visible": True,
                "audit_status": "succeeded" if kind == "invalid_output" else "failed",
            }
            for kind, stop, code in (
                ("invalid_output", "invalid_model_output", "answer_model_invalid_output"),
                ("transport_failure", "model_failed", "provider_error"),
            )
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "sha",
        "unbounded",
        "ui",
        "audit",
        "wrong_diagnostic",
        "wrong_response_diagnostic",
        "audit_status",
        "command",
        "input_digest",
        "non_hex_digest",
        "elapsed_ms",
        "boolean_elapsed_ms",
        "nan_elapsed_ms",
        "huge_elapsed_ms",
        "exit_code",
        "boolean_exit_code",
    ],
)
def test_issue55_requires_same_sha_bounded_failure_proof(tmp_path: Path, change: str) -> None:
    module = collector()
    write_payloads(tmp_path, valid_payloads())
    failure_path = tmp_path / "browser/answer-failures-issue55/report.json"
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure = failure_evidence()
    if change == "missing":
        failure_path.unlink(missing_ok=True)
    else:
        if change == "sha":
            failure["sha"] = "old-sha"
        if change == "unbounded":
            failure["cases"][0]["dispatch_count"] = 10
        if change == "ui":
            failure["cases"][0]["ui_visible"] = False
        if change == "audit":
            failure["cases"][0]["audit_id"] = ""
        if change == "wrong_diagnostic":
            failure["cases"][1]["diagnostic_code"] = "wrong_but_nonempty"
        if change == "wrong_response_diagnostic":
            failure["cases"][1]["response_diagnostic_code"] = "wrong_but_nonempty"
        if change == "audit_status":
            failure["cases"][1].pop("audit_status")
        if change == "command":
            failure["command"] = []
        if change == "input_digest":
            failure["input_digest"] = "short"
        if change == "non_hex_digest":
            failure["input_digest"] = "z" * 64
        if change == "elapsed_ms":
            failure["elapsed_ms"] = 0
        if change == "boolean_elapsed_ms":
            failure["elapsed_ms"] = True
        if change == "nan_elapsed_ms":
            failure["elapsed_ms"] = float("nan")
        if change == "huge_elapsed_ms":
            failure["elapsed_ms"] = 10**400
        if change == "exit_code":
            failure["exit_code"] = 1
        if change == "boolean_exit_code":
            failure["exit_code"] = False
        failure_path.write_text(json.dumps(failure))
    with pytest.raises(ValueError, match="Issue 55"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


def test_pdf_stages_require_one_input_digest(tmp_path: Path) -> None:
    module = collector()
    payloads = valid_payloads()
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="accepted PDF"):
        module.validate_stage_evidence(
            tmp_path,
            evidence_matrix(),
            "same-sha",
            expected_pdf_sha256="b" * 64,
        )


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10**400])
def test_issue54_timing_rejects_non_finite_or_boolean_values(tmp_path: Path, value: object) -> None:
    module = collector()
    payloads = valid_payloads()
    payloads[54]["restart_scenarios"][0]["elapsed_ms"] = value
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="Issue 54"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10**400])
def test_issue56_timing_rejects_non_finite_or_boolean_values(tmp_path: Path, value: object) -> None:
    module = collector()
    payloads = valid_payloads()
    payloads[56]["steps"][0]["seconds"] = value
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="Issue 56"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


def test_issue55_pdf_answer_must_bind_to_accepted_pdf(tmp_path: Path) -> None:
    module = collector()
    payloads = valid_payloads()
    expected = "a" * 64

    def replace_source(value: object) -> object:
        if isinstance(value, dict):
            return {key: replace_source(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_source(item) for item in value]
        return expected if value == "source" else value

    payloads = cast(
        dict[int, dict[str, Any]],
        {issue: replace_source(payload) for issue, payload in payloads.items()},
    )
    payloads[55]["imports"][1]["sha256"] = "b" * 64
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="Issue 55 PDF import"):
        module.validate_stage_evidence(
            tmp_path,
            evidence_matrix(),
            "same-sha",
            expected_pdf_sha256=expected,
        )


def test_issue55_pdf_answer_rejects_unrelated_citation_identity(tmp_path: Path) -> None:
    module = collector()
    payloads = valid_payloads()

    def replace_source(value: object) -> object:
        if isinstance(value, dict):
            return {key: replace_source(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_source(item) for item in value]
        return "a" * 64 if value == "source" else value

    payloads = cast(
        dict[int, dict[str, Any]],
        {issue: replace_source(payload) for issue, payload in payloads.items()},
    )
    payloads[55]["answers"][2]["bound_citation"] = {"citation_id": "unrelated"}
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="Issue 55 PDF answer citation"):
        module.validate_stage_evidence(
            tmp_path, evidence_matrix(), "same-sha", expected_pdf_sha256="a" * 64
        )


def test_issue55_text_answer_rejects_unrelated_citation_identity(tmp_path: Path) -> None:
    module = collector()
    payloads = valid_payloads()
    payloads[55]["answers"][1]["bound_source"]["knowledge_object_id"] = "unrelated"
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="Issue 55 text answer citation"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")


def test_issue55_text_answer_rejects_invalid_normalized_source_digest(tmp_path: Path) -> None:
    module = collector()
    payloads = valid_payloads()
    payloads[55]["imports"][0]["knowledge"]["source_sha256"] = "not-a-sha256"
    payloads[55]["answers"][1]["bound_source"]["source_sha256"] = "not-a-sha256"
    write_payloads(tmp_path, payloads)
    with pytest.raises(ValueError, match="Issue 55 text answer citation"):
        module.validate_stage_evidence(tmp_path, evidence_matrix(), "same-sha")
