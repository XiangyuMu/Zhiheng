#!/usr/bin/env python3
"""Collect bounded, same-commit delivery evidence from a clean checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def file_digest(path: Path) -> str:
    """Return the SHA-256 digest of a file without loading it all at once."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parser_backend(profile: str) -> str:
    backends = {"pdf": "mineru", "deepdoc": "deepdoc"}
    if profile not in backends:
        raise ValueError("parser profile must be pdf or deepdoc")
    return backends[profile]


def load_pdf_evidence_matrix(repo: Path) -> list[dict[str, Any]]:
    matrix_path = repo / "docs/testing/issue-52-56-evidence-matrix.json"
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load PDF evidence matrix: {matrix_path}") from error
    issues = matrix.get("issues") if isinstance(matrix, dict) else None
    if not isinstance(issues, list):
        raise ValueError("PDF evidence matrix must contain an issues list")
    by_issue = {item.get("issue"): item for item in issues if isinstance(item, dict)}
    expected = set(range(52, 57))
    if set(by_issue) != expected:
        raise ValueError("PDF evidence matrix must contain exactly issues 52, 53, 54, 55 and 56")
    for issue in sorted(expected):
        item = by_issue[issue]
        if not item.get("stage") or not item.get("artifact"):
            raise ValueError(f"PDF evidence matrix issue {issue} lacks stage or artifact")
        requirements = item.get("requirements")
        if (
            not isinstance(requirements, list)
            or not requirements
            or not all(isinstance(value, str) and value.strip() for value in requirements)
        ):
            raise ValueError(f"PDF evidence matrix issue {issue} lacks requirements")
    return [by_issue[issue] for issue in sorted(expected)]


def clean_environment() -> dict[str, str]:
    # Never inherit production storage, credentials, pytest selectors or local import paths.
    cleaned = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ZHIHENG_", "RESTIC_", "PYTEST_", "UV_"))
        and key not in {"PYTHONPATH", "VIRTUAL_ENV", "BROWSER_CHANNEL", "PLAYWRIGHT_MODULE_PATH"}
    }
    # These names are the deliberately narrow, private inputs for the real PDF
    # stages.  They are copied without ever being serialized into the report.
    for key in {
        "ZHIHENG_TEST_USERNAME",
        "ZHIHENG_TEST_PASSWORD",
        "ZHIHENG_PDF_ACCEPTANCE_URL",
        "ZHIHENG_PDF_FILE",
        "ZHIHENG_PDF_EXPECTATION_FILE",
        "ZHIHENG_REAL_FILE_A",
        "ZHIHENG_REAL_FILE_B",
        "ZHIHENG_RUNTIME_CHECKOUT",
        "ZHIHENG_RESTART_WORKER_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_COMMAND",
        "ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND",
        "ZHIHENG_PARSER_ACTIVE_PROBE_COMMAND",
        "ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID",
        "ZHIHENG_EXPECTED_ANSWER_MODEL_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_PROVIDER_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_MODEL_ID",
        "MINERU_PARSER_PROFILE",
        "MINERU_WARMUP_TIMEOUT_SECONDS",
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH",
        "ZHIHENG_ISSUE54_FAULTS_JSON",
        "ZHIHENG_MINERU_RUNTIME",
        "ZHIHENG_MINERU_GATEWAY_URL",
        "ZHIHENG_MINERU_TOKEN_FILE",
    }:
        if key in os.environ:
            cleaned[key] = os.environ[key]
    return cleaned


def pytest_environment(env: dict[str, str]) -> dict[str, str]:
    """Keep real acceptance routes and storage out of synthetic test fixtures."""
    result = dict(env)
    for key in (
        "ZHIHENG_PDF_PARSER_BACKEND",
        "ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID",
        "ZHIHENG_EXPECTED_ANSWER_MODEL_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_PROVIDER_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_MODEL_ID",
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH",
    ):
        result.pop(key, None)
    return result


def real_pdf_preflight() -> list[str]:
    required = [
        "ZHIHENG_TEST_USERNAME",
        "ZHIHENG_TEST_PASSWORD",
        "ZHIHENG_PDF_ACCEPTANCE_URL",
        "ZHIHENG_PDF_FILE",
        "ZHIHENG_PDF_EXPECTATION_FILE",
        "ZHIHENG_REAL_FILE_A",
        "ZHIHENG_REAL_FILE_B",
        "ZHIHENG_RESTART_WORKER_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_COMMAND",
        "ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND",
        "ZHIHENG_PARSER_ACTIVE_PROBE_COMMAND",
        "ZHIHENG_ISSUE54_FAULTS_JSON",
        "ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID",
        "ZHIHENG_EXPECTED_ANSWER_MODEL_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_PROVIDER_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_MODEL_ID",
    ]
    code = (
        "import os, pathlib, sys; "
        f"missing=[k for k in {required!r} if not os.environ.get(k)]; "
        "missing += [k for k in ('ZHIHENG_PDF_FILE','ZHIHENG_REAL_FILE_A','ZHIHENG_REAL_FILE_B') "
        "if os.environ.get(k) and not pathlib.Path(os.environ[k]).is_file()]; "
        "print('missing real PDF acceptance inputs: ' + ','.join(missing)) if missing else None; "
        "raise SystemExit(2 if missing else 0)"
    )
    return [sys.executable, "-c", code]


def create_evidence_directory(output: Path) -> None:
    # Refuse every existing run, including public directories and symlinks.
    output.mkdir(parents=True, mode=0o700, exist_ok=False)


def write_report(output: Path, report: dict[str, Any]) -> None:
    descriptor = os.open(output / "report.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def execute(
    repo: Path,
    output: Path,
    name: str,
    command: list[str],
    timeout: float | None,
    env: dict[str, str],
) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"name": name, "command": command, "status": "failed"}
    descriptor = os.open(output / f"{name}.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as log:
        try:
            process = subprocess.Popen(
                command,
                cwd=repo,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            result["error"] = str(error)
            result["exit_code"] = None
            result["seconds"] = round(time.monotonic() - started, 3)
            return result
        try:
            process.wait(timeout=timeout)
            result["status"] = "passed" if process.returncode == 0 else "failed"
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            result["status"] = (
                "timeout" if isinstance(error, subprocess.TimeoutExpired) else "interrupted"
            )
        finally:
            # A child can outlive its leader. Terminate the complete session,
            # while preserving cleanup failures in the acceptance evidence.
            cleanup_errors: list[str] = []
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as error:
                cleanup_errors.append(f"SIGTERM: {error}")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    cleanup_errors.append(f"SIGKILL: {error}")
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cleanup_errors.append("process group remained alive after SIGKILL")
            if cleanup_errors:
                result["status"] = "failed"
                result["cleanup_error"] = "; ".join(cleanup_errors)
        result["exit_code"] = process.returncode
        result["seconds"] = round(time.monotonic() - started, 3)
    return result


def mineru_command(name: str, output: Path) -> list[str]:
    """Select an explicit real runtime; never turn a skipped runtime into success."""
    runtime = os.environ.get("ZHIHENG_MINERU_RUNTIME", "compose")
    if runtime not in {"host", "compose"}:
        raise ValueError("ZHIHENG_MINERU_RUNTIME must be host or compose")
    if runtime == "host":
        url = validated_loopback_url(
            os.environ.get("ZHIHENG_MINERU_GATEWAY_URL", "http://127.0.0.1:9392")
        )
        token_file = os.environ.get("ZHIHENG_MINERU_TOKEN_FILE")
        if not token_file:
            raise ValueError("host MinerU requires ZHIHENG_MINERU_TOKEN_FILE")
        if name == "mineru-warmup":
            return [
                sys.executable, "scripts/mineru_http_warmup.py", "--url", url,
                "--token-file", token_file, "--diagnostics", str(output / "mineru-warmup.json"),
                "--timeout", str(mineru_warmup_timeout()),
            ]
        return [
            sys.executable, "scripts/mineru_http_health.py", "--url", url,
            "--token-file", token_file,
        ]
    if name == "mineru-warmup":
        return ["bash", "scripts/mineru_local.sh", "start"]
    return ["bash", "scripts/mineru_local.sh", "health"]


def validated_loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("MinerU gateway must be a loopback origin without credentials")
    _ = parsed.port
    return f"{parsed.scheme}://{parsed.netloc}"


def mineru_warmup_timeout() -> float:
    raw = os.environ.get("MINERU_WARMUP_TIMEOUT_SECONDS", "1800")
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError("MINERU_WARMUP_TIMEOUT_SECONDS must be numeric") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError("MINERU_WARMUP_TIMEOUT_SECONDS must be finite and positive")
    return value


def validate_stage_evidence(
    output: Path,
    matrix: list[dict[str, Any]],
    sha: str,
    expected_pdf_sha256: str | None = None,
) -> None:
    """Require each real PDF stage to leave a passed report bound to this SHA."""
    def require_dict(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        return value

    def require_nonempty(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} is missing")
        return value

    def require_list(value: Any, label: str) -> list[Any]:
        if not isinstance(value, list):
            raise ValueError(f"{label} must be a list")
        return value

    def require_object_list(value: Any, label: str) -> list[dict[str, Any]]:
        rows = require_list(value, label)
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{label} must be a non-empty list of objects")
        return rows

    def require_positive_number(value: Any, label: str) -> None:
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{label} must be positive")

    for item in matrix:
        issue = int(item["issue"])
        report_path = output / str(item["artifact"])
        if not report_path.is_file():
            raise ValueError(f"Issue {issue} evidence report is missing: {report_path}")
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Issue {issue} evidence report is not valid JSON") from error
        if payload.get("status") != "passed":
            raise ValueError(f"Issue {issue} evidence report did not pass")
        if payload.get("sha") != sha:
            raise ValueError(f"Issue {issue} evidence report is not bound to {sha}")
        if issue in {52, 53, 54} and expected_pdf_sha256 is not None:
            source = require_dict(payload.get("input"), f"Issue {issue} input")
            if source.get("sha256") != expected_pdf_sha256:
                raise ValueError(f"Issue {issue} input is not bound to the accepted PDF")
        if issue == 52:
            source = require_dict(payload.get("input"), "Issue 52 input")
            source_sha = require_nonempty(source.get("sha256"), "Issue 52 input.sha256")
            receipt = require_dict(payload.get("receipt"), "Issue 52 receipt")
            receipt_body = require_dict(receipt.get("body"), "Issue 52 receipt.body")
            require_nonempty(receipt_body.get("task_id"), "Issue 52 receipt task_id")
            require_nonempty(
                receipt_body.get("evidence_object_id"), "Issue 52 receipt evidence_object_id"
            )
            if receipt_body.get("source_sha256") != source_sha:
                raise ValueError("Issue 52 receipt source digest is not bound to input")
            parsed = require_dict(payload.get("parsed"), "Issue 52 parsed")
            if parsed.get("state") not in {"parsed", "succeeded"}:
                raise ValueError("Issue 52 evidence does not prove a parsed terminal state")
            if parsed.get("backend") != "mineru":
                raise ValueError("Issue 52 evidence does not prove MinerU parsing")
            require_nonempty(parsed.get("attempt_id"), "Issue 52 parsed.attempt_id")
            if parsed.get("evidence_object_id") != receipt_body.get("evidence_object_id"):
                raise ValueError("Issue 52 parsed evidence object is not bound to receipt")
            if parsed.get("source_sha256") != source_sha:
                raise ValueError("Issue 52 parsed source digest is not bound to input")
            require_nonempty(parsed.get("manifest_uri"), "Issue 52 parsed.manifest_uri")
            require_nonempty(parsed.get("manifest_sha256"), "Issue 52 parsed.manifest_sha256")
            manifest = require_dict(payload.get("manifest"), "Issue 52 manifest")
            require_nonempty(manifest.get("uri"), "Issue 52 manifest.uri")
            require_nonempty(manifest.get("sha256"), "Issue 52 manifest.sha256")
            if manifest.get("sha256") != parsed.get("manifest_sha256"):
                raise ValueError("Issue 52 manifest digest is not bound to parsed output")
            for field in ("page_count", "block_count"):
                if not isinstance(manifest.get(field), int) or manifest[field] < 1:
                    raise ValueError(f"Issue 52 manifest.{field} is invalid")
            artifacts = require_dict(payload.get("artifacts"), "Issue 52 artifacts")
            for key in ("manifest", "pages", "images"):
                if key not in artifacts:
                    raise ValueError(f"Issue 52 artifacts.{key} is missing")
        elif issue == 53:
            source = require_dict(payload.get("input"), "Issue 53 input")
            source_sha = require_nonempty(source.get("sha256"), "Issue 53 input.sha256")
            receipt = require_dict(payload.get("receipt"), "Issue 53 receipt")
            receipt_body = require_dict(receipt.get("body"), "Issue 53 receipt.body")
            require_nonempty(receipt_body.get("task_id"), "Issue 53 receipt task_id")
            evidence_object_id = require_nonempty(
                receipt_body.get("evidence_object_id"), "Issue 53 receipt evidence_object_id"
            )
            if receipt_body.get("source_sha256") != source_sha:
                raise ValueError("Issue 53 receipt source digest is not bound to input")
            search = require_object_list(payload.get("search"), "Issue 53 search")
            if not any(row.get("hit_found") is True for row in search):
                raise ValueError("Issue 53 evidence does not prove a library hit")
            hit = require_dict(payload.get("hit"), "Issue 53 hit")
            if hit.get("evidence_object_id") != evidence_object_id:
                raise ValueError("Issue 53 hit evidence object is not bound to receipt")
            if hit.get("source_sha256") != source_sha:
                raise ValueError("Issue 53 hit source digest is not bound to input")
            if hit.get("searchable") is not True or hit.get("media_type") != "application/pdf":
                raise ValueError("Issue 53 hit does not prove searchable PDF qualification")
            detail = require_dict(payload.get("detail"), "Issue 53 detail")
            if (
                detail.get("searchable") is not True
                or detail.get("knowledge_object_id") != hit.get("knowledge_object_id")
                or detail.get("media_type") != "application/pdf"
                or not isinstance(detail.get("citation_count"), int)
                or detail["citation_count"] < 1
            ):
                raise ValueError("Issue 53 detail is not searchable")
            generation = require_dict(
                detail.get("retrieval_generation"), "Issue 53 retrieval_generation"
            )
            if generation.get("index_status") != "active":
                raise ValueError("Issue 53 retrieval generation is not active")
            provider_id = require_nonempty(
                generation.get("provider_id"), "Issue 53 retrieval provider_id"
            )
            expected_provider = os.environ.get("ZHIHENG_EXPECTED_EMBEDDING_PROVIDER_ID")
            if expected_provider and provider_id != expected_provider:
                raise ValueError("Issue 53 embedding provider differs from the configured route")
            model_id = require_nonempty(
                generation.get("model_id"), "Issue 53 retrieval model_id"
            )
            expected_model = os.environ.get("ZHIHENG_EXPECTED_EMBEDDING_MODEL_ID")
            if expected_model and model_id != expected_model:
                raise ValueError("Issue 53 embedding model differs from the configured route")
            require_nonempty(
                generation.get("model_revision"), "Issue 53 retrieval model_revision"
            )
            require_positive_number(generation.get("dimension"), "Issue 53 retrieval dimension")
            if generation.get("purpose") != "retrieval":
                raise ValueError("Issue 53 retrieval generation purpose is not retrieval")
            require_nonempty(generation.get("physical_index_ref"), "Issue 53 physical_index_ref")
        elif issue == 54:
            source = require_dict(payload.get("input"), "Issue 54 input")
            source_sha = require_nonempty(source.get("sha256"), "Issue 54 input.sha256")
            scenarios = require_object_list(
                payload.get("restart_scenarios"), "Issue 54 restart scenarios"
            )
            if {item.get("kind") for item in scenarios} != {"worker", "gateway"}:
                raise ValueError(
                    "Issue 54 evidence must contain independent worker and gateway scenarios"
                )
            if (
                len(scenarios) != 2
                or len({item.get("receipt", {}).get("task_id") for item in scenarios}) != 2
            ):
                raise ValueError("Issue 54 restart scenarios must use independent tasks")
            restarts = require_object_list(payload.get("restarts"), "Issue 54 restarts")
            if {item.get("kind") for item in restarts if item.get("status") == "succeeded"} != {
                "worker",
                "gateway",
            }:
                raise ValueError("Issue 54 evidence does not prove both process restarts")
            scenario_by_kind = {item["kind"]: item for item in scenarios}
            for scenario in scenarios:
                receipt = require_dict(scenario.get("receipt"), "Issue 54 scenario receipt")
                task_id = require_nonempty(receipt.get("task_id"), "Issue 54 scenario task_id")
                if receipt.get("source_sha256") != source_sha:
                    raise ValueError("Issue 54 scenario source digest is not bound to input")
                require_nonempty(receipt.get("parser_attempt_id"), "Issue 54 parser attempt")
                if scenario.get("status") != "passed":
                    raise ValueError("Issue 54 restart scenario did not pass")
                terminal = require_dict(scenario.get("terminal"), "Issue 54 terminal recovery")
                if (
                    terminal.get("state") != "parsed"
                    or terminal.get("task_id") != task_id
                    or terminal.get("source_sha256") != source_sha
                ):
                    raise ValueError("Issue 54 terminal recovery is not bound to scenario")
                require_nonempty(terminal.get("manifest_sha256"), "Issue 54 terminal manifest")
                searchable = require_dict(
                    scenario.get("searchable"), "Issue 54 searchable qualification"
                )
                if (
                    searchable.get("evidence_object_id") != receipt.get("evidence_object_id")
                    or searchable.get("index_status") != "active"
                ):
                    raise ValueError("Issue 54 restart lacks active searchable qualification")
                require_nonempty(
                    searchable.get("knowledge_object_id"),
                    "Issue 54 searchable knowledge object",
                )
                require_nonempty(
                    searchable.get("physical_index_ref"),
                    "Issue 54 searchable physical index",
                )
                elapsed_ms = scenario.get("elapsed_ms")
                if not isinstance(elapsed_ms, (int, float)) or elapsed_ms > 300_000:
                    raise ValueError("Issue 54 restart exceeded parse plus index budget")
                lease = require_dict(
                    scenario.get("active_lease_before_restart"),
                    "Issue 54 active lease proof",
                )
                if not any(
                    isinstance(job, dict)
                    and job.get("status") == "processing"
                    and int(job.get("attempts", 0)) > 0
                    and job.get("lease_owner_sha256")
                    for job in require_list(lease.get("jobs"), "Issue 54 active lease jobs")
                ):
                    raise ValueError("Issue 54 restart lacks an active leased job")
            for restart in restarts:
                if restart.get("status") != "succeeded" or restart.get("exit_code") != 0:
                    raise ValueError("Issue 54 restart did not succeed")
                require_nonempty(restart.get("command_sha256"), "Issue 54 restart command_sha256")
                before = require_nonempty(
                    restart.get("before_identity_sha256"), "Issue 54 restart before identity"
                )
                after = require_nonempty(
                    restart.get("after_identity_sha256"), "Issue 54 restart after identity"
                )
                if before == after:
                    raise ValueError("Issue 54 restart process identity did not change")
                proof = require_dict(restart.get("proof"), "Issue 54 restart proof")
                restart_scenario = scenario_by_kind.get(restart.get("kind"))
                scenario_receipt = (
                    require_dict(restart_scenario.get("receipt"), "Issue 54 scenario receipt")
                    if restart_scenario
                    else {}
                )
                if (
                    proof.get("kind") != restart.get("kind")
                    or proof.get("task_id") != scenario_receipt.get("task_id")
                ):
                    raise ValueError("Issue 54 restart proof is not bound to current task")
                if proof.get("ready") is not True and proof.get("observed_ready") is not True:
                    raise ValueError("Issue 54 restart proof is missing readiness")
                if not (
                    proof.get("observed_identity") or proof.get("pid") or proof.get("container_id")
                ):
                    raise ValueError("Issue 54 restart proof is missing process identity")
            parser_scenario = scenario_by_kind["gateway"]
            parser_proof = require_dict(
                parser_scenario.get("active_parser_before_restart"),
                "Issue 54 active parser proof",
            )
            parser_receipt = require_dict(
                parser_scenario.get("receipt"), "Issue 54 gateway receipt"
            )
            if (
                parser_proof.get("task_id") != parser_receipt.get("task_id")
                or not parser_proof.get("upstream_task_id")
            ):
                raise ValueError("Issue 54 parser proof is not bound to current task")
            if (
                parser_proof.get("ready") is not True
                and parser_proof.get("observed_ready") is not True
            ):
                raise ValueError("Issue 54 parser proof is missing readiness")
            faults = require_object_list(payload.get("fault_injections"), "Issue 54 fault evidence")
            required = {
                "lost_parser_attempt",
                "parser_failure",
                "partial_or_unsupported",
                "auto_retry_exhausted",
                "manual_ui_retry",
            }
            observed = {
                item.get("category") for item in faults if item.get("status") == "passed"
            }
            if observed != required:
                raise ValueError("Issue 54 evidence does not prove all five fault categories")
            for fault in faults:
                if fault.get("task_id") in {item["receipt"]["task_id"] for item in scenarios}:
                    raise ValueError("Issue 54 fault task reused the restart task")
                if fault.get("source_sha256") != source_sha:
                    raise ValueError("Issue 54 fault source digest is not bound to input")
                observed_state = require_dict(fault.get("observed"), "Issue 54 fault observed")
                if (
                    observed_state.get("state") != fault.get("expected_state")
                    or observed_state.get("error_code") != fault.get("expected_error_code")
                ):
                    raise ValueError("Issue 54 fault terminal state is not bound to expectation")
                polling = require_dict(observed_state.get("polling"), "Issue 54 fault polling")
                if polling.get("polling_stopped") is not True:
                    raise ValueError("Issue 54 evidence is missing polling-stop proof")
                command_proof = require_dict(
                    fault.get("command_proof"), "Issue 54 fault command proof"
                )
                if (
                    command_proof.get("injected") is not True
                    or command_proof.get("boundary") != "external_parser"
                    or command_proof.get("category") != fault.get("category")
                    or command_proof.get("task_id") != fault.get("task_id")
                ):
                    raise ValueError("Issue 54 fault command proof is not bound to scenario")
                if (
                    not isinstance(command_proof.get("observed_identity"), str)
                    or not command_proof["observed_identity"]
                ):
                    raise ValueError("Issue 54 fault command proof is missing observation")
                if fault.get("category") == "auto_retry_exhausted" and (
                    not isinstance(observed_state.get("retry_count"), int)
                    or observed_state["retry_count"] != 3
                ):
                    raise ValueError("Issue 54 auto retry count is not exactly three attempts")
                if fault.get("category") == "manual_ui_retry":
                    retry_result = require_dict(
                        fault.get("retry_result"), "Issue 54 manual retry result"
                    )
                    if fault.get("ui_retry_clicked") is not True:
                        raise ValueError("Issue 54 manual retry did not use the UI")
                    if (
                        retry_result.get("task_id") != fault.get("task_id")
                        or retry_result.get("source_sha256") != fault.get("source_sha256")
                    ):
                        raise ValueError("Issue 54 manual retry result is not bound to fault")
                    require_nonempty(
                        retry_result.get("manifest_sha256"),
                        "Issue 54 manual retry manifest_sha256",
                    )
                    searchable = require_dict(
                        retry_result.get("searchable"),
                        "Issue 54 manual retry searchable qualification",
                    )
                    if searchable.get("index_status") != "active":
                        raise ValueError("Issue 54 manual retry index is not active")
                    require_nonempty(
                        searchable.get("physical_index_ref"),
                        "Issue 54 manual retry physical index",
                    )
        elif issue == 55:
            failure_path = output / "browser/answer-failures-issue55/report.json"
            try:
                failure = require_dict(
                    json.loads(failure_path.read_text(encoding="utf-8")),
                    "Issue 55 failure evidence",
                )
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError("Issue 55 failure evidence is missing or invalid") from error
            if (
                failure.get("status") != "passed" or failure.get("sha") != sha
                or failure.get("injected") is not True
                or failure.get("boundary") != "model_transport"
            ):
                raise ValueError("Issue 55 failure proof must be passed, injected and same-SHA")
            command = failure.get("command")
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(item, str) and item for item in command)
                or not isinstance(failure.get("input_digest"), str)
                or len(failure["input_digest"]) != 64
                or not isinstance(failure.get("elapsed_ms"), (int, float))
                or failure["elapsed_ms"] <= 0
                or failure.get("exit_code") != 0
            ):
                raise ValueError("Issue 55 failure evidence metadata is incomplete")
            cases = require_object_list(failure.get("cases"), "Issue 55 failure cases")
            expected_failures = {
                "invalid_output": (
                    "invalid_model_output", "answer_model_invalid_output", "succeeded",
                ),
                "transport_failure": ("model_failed", "provider_error", "failed"),
            }
            if len(cases) != 2 or {row.get("kind") for row in cases} != set(expected_failures):
                raise ValueError("Issue 55 requires both invalid output and transport failure")
            for case in cases:
                if (
                    case.get("status") != "passed" or case.get("ui_visible") is not True
                    or case.get("stop_reason") != expected_failures[case["kind"]][0]
                    or type(case.get("dispatch_count")) is not int or case["dispatch_count"] != 1
                ):
                    raise ValueError("Issue 55 failure lacks bounded dispatch or visible UI")
                for field in ("audit_id", "provider_id", "model_id", "diagnostic_code"):
                    require_nonempty(case.get(field), f"Issue 55 failure {field}")
                require_nonempty(
                    case.get("response_diagnostic_code"),
                    "Issue 55 failure response_diagnostic_code",
                )
                if (
                    case["diagnostic_code"] != expected_failures[case["kind"]][1]
                    or case["response_diagnostic_code"] != expected_failures[case["kind"]][1]
                    or case.get("audit_status") != expected_failures[case["kind"]][2]
                ):
                    raise ValueError("Issue 55 failure diagnosis or audit status does not match")
            answers = require_object_list(payload.get("answers"), "Issue 55 answers")
            by_kind = {item.get("kind"): item for item in answers}
            if set(by_kind) != {"text", "pdf", "negative"}:
                raise ValueError("Issue 55 evidence must include text, PDF and negative answers")
            if any(
                item.get("status") != 200
                or item.get("stop_reason") != "completed"
                or not item.get("answer_audit_id")
                or not item.get("answer_provider_id")
                or not item.get("answer_model_id")
                for item in answers
            ):
                raise ValueError("Issue 55 evidence is missing answer audit identity")
            negative = by_kind["negative"]
            if (
                negative.get("citations") != 0
                or negative.get("citation_payload_count") != 0
                or negative.get("claims_count") != 0
                or not isinstance(negative.get("insufficiency_count"), int)
                or negative["insufficiency_count"] < 1
            ):
                raise ValueError("Issue 55 evidence does not prove a citation-free negative answer")
            for kind in ("text", "pdf"):
                answer = by_kind[kind]
                if (
                    not isinstance(answer.get("citations"), int)
                    or answer["citations"] < 1
                    or not isinstance(answer.get("citation_payload_count"), int)
                    or answer["citation_payload_count"] < 1
                    or not isinstance(answer.get("claims_count"), int)
                    or answer["claims_count"] < 1
                    or not isinstance(answer.get("bound_source"), dict)
                    or not isinstance(answer.get("bound_citation"), dict)
                ):
                    raise ValueError("Issue 55 evidence does not prove cited positive answers")
        elif issue == 56:
            steps = payload.get("steps")
            if (
                not payload.get("clean_before")
                or not payload.get("clean_after")
                or payload.get("same_sha") is not True
            ):
                raise ValueError("Issue 56 evidence is not clean and same-SHA bound")
            steps = require_object_list(steps, "Issue 56 steps")
            if any(item.get("status") != "passed" or item.get("exit_code") != 0 for item in steps):
                raise ValueError("Issue 56 evidence contains incomplete quality-gate steps")
            required_steps = {
                "dependencies", "versions", "compile", "ruff", "mypy", "pytest", "browser",
                "real-pdf-preflight", "mineru-warmup", "parser-health", "pdf-parse", "pdf-search",
                "pdf-recovery", "real-files", "extraction",
            }
            if len(steps) != len(required_steps) or {
                item.get("name") for item in steps
            } != required_steps:
                raise ValueError("Issue 56 evidence is missing required quality-gate steps")
            if any(
                not isinstance(item.get("seconds"), (int, float))
                or item["seconds"] < 0
                or not isinstance(item.get("command"), list)
                or not item["command"]
                or any(not isinstance(part, str) or not part for part in item["command"])
                for item in steps
            ):
                raise ValueError("Issue 56 evidence steps lack timing or command details")
            pytest_path = output / "pytest" / "evidence.json"
            if not pytest_path.is_file():
                raise ValueError("Issue 56 pytest evidence report is missing")
            try:
                pytest_evidence = json.loads(pytest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError("Issue 56 pytest evidence report is not valid JSON") from error
            if not isinstance(pytest_evidence, dict):
                raise ValueError("Issue 56 pytest evidence report must be an object")
            if (
                pytest_evidence.get("status") != "passed"
                or pytest_evidence.get("sha") != sha
                or pytest_evidence.get("termination") != "completed"
                or pytest_evidence.get("exit_code") != 0
                or pytest_evidence.get("report_complete") is not True
                or pytest_evidence.get("clean_after") is not True
                or pytest_evidence.get("skips")
                or pytest_evidence.get("failed_nodes")
            ):
                raise ValueError("Issue 56 pytest evidence did not pass cleanly")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel")).resolve()
    output = args.output.resolve()
    if output == repo or repo in output.parents:
        parser.error("evidence must be outside the checkout")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        parser.error("clean tracked and untracked working tree required")
    # Ignored developer configuration must not contaminate a supposedly clean run.
    if (repo / ".env").exists():
        parser.error("use an isolated checkout without .env")
    create_evidence_directory(output)
    sha = git(repo, "rev-parse", "HEAD")
    evidence_matrix = load_pdf_evidence_matrix(repo)
    pdf_path = Path(os.environ.get("ZHIHENG_PDF_FILE", ""))
    expected_pdf_sha256 = file_digest(pdf_path) if pdf_path.is_file() else None
    env = clean_environment()
    env["ZHIHENG_ACCEPTANCE_OUTPUT"] = str(output / "browser")
    env["ZHIHENG_DELIVERY_EVIDENCE"] = str(output / "recovery")
    env["ZHIHENG_REQUIRE_DELIVERY_EVIDENCE"] = "1"
    env["ZHIHENG_EXPECTED_SHA"] = sha
    env["ZHIHENG_EVIDENCE_DIR"] = str(output)
    env["MINERU_DIAGNOSTIC_DIR"] = str(output / "mineru-warmup")
    env["ZHIHENG_PDF_PARSER_BACKEND"] = parser_backend(env.get("MINERU_PARSER_PROFILE", "pdf"))
    report: dict[str, Any] = {
        "sha": sha,
        "status": "running",
        "clean_before": True,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [],
        "requirements": "docs/testing/issue-52-56-evidence-matrix.json",
        "evidence_matrix": evidence_matrix,
    }
    steps = [
        ("dependencies", ["uv", "sync", "--frozen", "--extra", "dev"]),
        ("versions", ["uv", "pip", "freeze"]),
        (
            "compile",
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-m",
                "compileall",
                "-q",
                "src",
                "scripts",
                "migrations",
            ],
        ),
        ("ruff", ["uv", "run", "--frozen", "ruff", "check", "."]),
        ("mypy", ["uv", "run", "--frozen", "mypy", "src", "tests"]),
        (
            "pytest",
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "scripts/pytest_delivery_gate.py",
                "--output",
                str(output / "pytest"),
                "--timeout",
                str(args.timeout),
                "--natural-completion",
            ],
        ),
        ("browser", ["bash", "scripts/browser_acceptance.sh"]),
        ("real-pdf-preflight", real_pdf_preflight()),
        ("mineru-warmup", mineru_command("mineru-warmup", output)),
        ("parser-health", mineru_command("parser-health", output)),
        (
            "pdf-parse",
            [
                "node",
                "tests/e2e/check_mineru_parse.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "pdf-parse"),
                os.environ.get("ZHIHENG_PDF_FILE", ""),
            ],
        ),
        (
            "pdf-search",
            [
                "node",
                "tests/e2e/check_pdf_search_issue53.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "pdf-search"),
                os.environ.get("ZHIHENG_PDF_FILE", ""),
                os.environ.get("ZHIHENG_PDF_EXPECTATION_FILE", ""),
            ],
        ),
        (
            "pdf-recovery",
            [
                "node",
                "tests/e2e/check_pdf_recovery_issue54.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "pdf-recovery"),
                os.environ.get("ZHIHENG_PDF_FILE", ""),
            ],
        ),
        (
            "real-files",
            [
                "node",
                "tests/e2e/check_real_files.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "real-files"),
                os.environ.get("ZHIHENG_REAL_FILE_A", ""),
                os.environ.get("ZHIHENG_REAL_FILE_B", ""),
            ],
        ),
        (
            "extraction",
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-c",
                "import json,dataclasses; "
                "from zhiheng.evaluation.issue29_conclusion_extraction "
                "import evaluate_issue29_conclusion_extraction as run; r=run(); "
                "print(json.dumps(dataclasses.asdict(r),ensure_ascii=False,indent=2)); "
                "raise SystemExit(0 if r.passed else 1)",
            ],
        ),
    ]
    for name, command in steps:
        print(f"Running {name} for {sha}", flush=True)
        timeout = (
            None
            if name == "pytest"
            else mineru_warmup_timeout() + 30
            if name == "mineru-warmup"
            else args.timeout + 30
        )
        step_env = env
        if name == "pytest":
            step_env = pytest_environment(env)
        runtime_restart: dict[str, Any] | None = None
        if name == "pdf-parse":
            restart_command = os.environ.get("ZHIHENG_RESTART_WORKER_COMMAND")
            if restart_command:
                runtime_restart = execute(
                    repo, output, "pdf-worker-restart",
                    ["bash", "-lc", restart_command], 60, env,
                )
                runtime_restart.pop("command", None)
                runtime_restart["command_sha256"] = hashlib.sha256(
                    restart_command.encode("utf-8")
                ).hexdigest()
                if runtime_restart["status"] != "passed":
                    result = {
                        "name": name,
                        "command": command,
                        "status": "failed",
                        "exit_code": runtime_restart.get("exit_code"),
                        "seconds": runtime_restart["seconds"],
                        "runtime_restart": runtime_restart,
                    }
                    report["steps"].append(result)
                    write_report(output, report)
                    break
        result = execute(repo, output, name, command, timeout, step_env)
        if runtime_restart is not None:
            result["runtime_restart"] = runtime_restart
        report["steps"].append(result)
        write_report(output, report)
        if (
            name == "dependencies"
            or name == "real-pdf-preflight"
            or name == "mineru-warmup"
            or name == "parser-health"
            or name in {"pdf-parse", "pdf-search", "pdf-recovery"}
        ) and result["status"] != "passed":
            break
    try:
        validate_stage_evidence(
            output,
            [item for item in evidence_matrix if int(item["issue"]) != 56],
            sha,
            expected_pdf_sha256,
        )
        report["stage_evidence"] = "passed"
    except ValueError as error:
        report["stage_evidence"] = "failed"
        report["stage_evidence_error"] = str(error)
    report["clean_after"] = not bool(git(repo, "status", "--porcelain", "--untracked-files=all"))
    report["same_sha"] = sha == git(repo, "rev-parse", "HEAD")
    report["status"] = (
        "passed"
        if (
            len(report["steps"]) == len(steps)
            and all(step["status"] == "passed" for step in report["steps"])
            and report["clean_after"]
            and report["same_sha"]
            and report.get("stage_evidence") == "passed"
        )
        else "failed"
    )
    # Write a complete preliminary delivery report before validating its own
    # row in the evidence matrix. The final write below replaces this copy.
    write_report(output, report)
    try:
        validate_stage_evidence(output, evidence_matrix, sha, expected_pdf_sha256)
    except ValueError as error:
        report["stage_evidence"] = "failed"
        report["stage_evidence_error"] = str(error)
        report["status"] = "failed"
    pytest_evidence = output / "pytest" / "evidence.json"
    if pytest_evidence.exists():
        evidence = json.loads(pytest_evidence.read_text())
        report["skips"] = evidence.get("skips", [])
        report["pytest_budget_status"] = evidence.get("budget_status", "unknown")
        if report["skips"] or evidence.get("sha") != sha or evidence.get("status") != "passed":
            report["status"] = "failed"
    else:
        report["status"] = "failed"
    report["artifacts"] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path != output / "report.json"
    }
    write_report(output, report)
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
