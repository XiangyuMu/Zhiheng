"""Public report parser and executable delivery-gate contracts."""

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/pytest_delivery_gate.py"


def test_report_preserves_failure_and_skip_evidence(tmp_path: Path) -> None:
    module = load_gate_module()
    report = tmp_path / "report.xml"
    report.write_text("""<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1">
    <testcase classname="tests.integration.test_answer_replay_authority" name="test_erase[x]">
    <failure message="private fixture mismatch">trace</failure></testcase>
    <testcase classname="tests.unit.test_other" name="test_ok"/>
    <testcase classname="tests.unit.test_other" name="test_skip"><skipped message="optional tool"/>
    </testcase></testsuite></testsuites>""")
    result = module.parse_report(report)
    assert result["first_failure"] == (
        "tests/integration/test_answer_replay_authority.py::test_erase[x]"
    )
    assert result["failed_nodes"] == [result["first_failure"]]
    assert result["skips"][0]["reason"] == "optional tool"
    assert result["required_evidence"]["privacy"]["status"] == "failed"
    assert result["required_evidence"]["worker_recovery"]["status"] == "missing"
    assert result["failure_groups"]["privacy"]["root_cause"] == "unconfirmed"
    assert result["failure_details"][0]["message"] == "private fixture mismatch"
    assert result["report_complete"] is True


def test_report_rejects_incomplete_junit_counts(tmp_path: Path) -> None:
    module = load_gate_module()
    report = tmp_path / "report.xml"
    report.write_text("""<testsuites tests="2" failures="0" errors="0" skipped="0">
    <testsuite tests="2" failures="0" errors="0" skipped="0">
    <testcase classname="tests.unit.test_other" name="test_ok"/>
    </testsuite></testsuites>""")
    result = module.parse_report(report)
    assert result["report_complete"] is False
    assert result["report_completeness_errors"] == [
        "report tests=2 but testcase elements=1",
    ]


def test_report_preserves_collection_errors(tmp_path: Path) -> None:
    module = load_gate_module()
    report = tmp_path / "report.xml"
    report.write_text("""<testsuites tests="1" failures="0" errors="1" skipped="0">
    <testsuite tests="1" failures="0" errors="1" skipped="0">
    <testcase name="tests/integration/test_import_contract.py">
    <error message="ImportError while importing test module">trace</error>
    </testcase></testsuite></testsuites>""")
    result = module.parse_report(report)
    assert result["first_failure"] == "tests/integration/test_import_contract.py"
    assert result["failure_details"][0]["kind"] == "error"
    assert result["report_complete"] is True


def test_collection_error_counts_for_required_module(tmp_path: Path) -> None:
    module = load_gate_module()
    report = tmp_path / "report.xml"
    report.write_text(
        """<testsuites tests="1" failures="0" errors="1" skipped="0">
        <testsuite tests="1" failures="0" errors="1" skipped="0">
        <testcase name="tests/integration/test_answer_replay_authority.py">
        <error message="import failed">trace</error>
        </testcase></testsuite></testsuites>"""
    )
    result = module.parse_report(report)
    assert result["required_evidence"]["privacy"]["status"] == "failed"


def test_malformed_incremental_event_is_reported(tmp_path: Path) -> None:
    module = load_gate_module()
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text('{"event":"test_report"}\n{"truncated"\n')
    result = module.load_incremental_outcomes(outcomes)
    assert result["event_count"] == 1
    assert result["malformed_count"] == 1


def test_gate_rejects_nonfinite_timeout(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(tmp_path / "evidence"),
            "--timeout",
            "nan",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--timeout must be a finite positive number" in result.stderr


def test_gate_passes_tiny_clean_checkout_with_all_required_groups(tmp_path: Path) -> None:
    repo = tiny_repo(tmp_path)
    for filename in (
        "test_answer_replay_authority.py",
        "test_g006_runner.py",
        "test_release_stage_gates.py",
        "test_g006_release_lifecycle.py",
        "test_g006_promotion_rollback.py",
        "test_g006_worker_recovery.py",
        "test_restic_restore_install.py",
    ):
        (repo / "tests/integration" / filename).write_text(
            "def test_required_group_passes():\n    assert True\n"
        )
    commit_all(repo)
    output = tmp_path / "evidence"

    result = run_gate(repo, output)

    assert result.returncode == 0, result.stderr + result.stdout
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["status"] == "passed"
    assert evidence["report_complete"] is True
    assert evidence["collected_tests"] == evidence["test_count"] == 7
    assert evidence["incremental_outcomes"]["collection_count"] == 7


def test_gate_retains_setup_failure_from_incremental_outcomes(tmp_path: Path) -> None:
    repo = tiny_repo(tmp_path)
    (repo / "tests/integration/test_answer_replay_authority.py").write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.fixture
            def broken_fixture():
                raise RuntimeError("setup evidence survives")

            def test_setup_failure_is_recorded(broken_fixture):
                assert broken_fixture
            """
        )
    )
    commit_all(repo)
    output = tmp_path / "evidence"

    result = run_gate(repo, output)

    assert result.returncode == 1
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["status"] == "failed"
    assert evidence["incremental_outcomes"]["first_failure"]["when"] == "setup"
    assert (
        "setup evidence survives"
        in evidence["incremental_outcomes"]["first_failure"]["longrepr"]
    )
    assert evidence["failure_details"][0]["kind"] == "error"


def test_gate_retains_collection_failure_from_incremental_outcomes(tmp_path: Path) -> None:
    repo = tiny_repo(tmp_path)
    (repo / "tests/integration/test_answer_replay_authority.py").write_text(
        "raise RuntimeError('collection evidence survives')\n"
    )
    commit_all(repo)
    output = tmp_path / "evidence"

    result = run_gate(repo, output)

    assert result.returncode == 1
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["status"] == "failed"
    assert evidence["incremental_outcomes"]["first_failure"]["event"] == "collection_error"
    assert (
        "collection evidence survives"
        in evidence["incremental_outcomes"]["first_failure"]["longrepr"]
    )
    assert evidence["first_failure"] == "tests/integration/test_answer_replay_authority.py"


def load_gate_module() -> Any:
    spec = importlib.util.spec_from_file_location("delivery_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests/integration").mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    return repo


def commit_all(repo: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    )


def run_gate(repo: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--timeout",
            "30",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
