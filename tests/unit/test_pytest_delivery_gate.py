"""Public report parser and executable delivery-gate contracts."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/pytest_delivery_gate.py"


def test_report_preserves_failure_and_skip_evidence(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("delivery_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = tmp_path / "report.xml"
    report.write_text("""<testsuites><testsuite tests="3" failures="1" skipped="1">
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
