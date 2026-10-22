from __future__ import annotations

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


def _module() -> Any:
    path = Path(__file__).parents[2] / "scripts" / "issue54_runtime_control.py"
    spec = importlib.util.spec_from_file_location("issue54_runtime_control", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rendered_scenarios_are_explicit_external_faults() -> None:
    module = _module()
    scenarios = module.render_scenarios("python scripts/issue54_runtime_control.py command")

    assert {item["category"] for item in scenarios} == {
        "lost_parser_attempt",
        "parser_failure",
        "partial_or_unsupported",
        "auto_retry_exhausted",
        "manual_ui_retry",
    }
    assert all(item["injected"] is True for item in scenarios)
    assert all(item["boundary"] == "external_parser" for item in scenarios)
    assert all(item["command"] for item in scenarios)


def test_control_state_is_atomic_and_proof_uses_observed_upstream_identity(
    tmp_path: Path,
) -> None:
    module = _module()
    control = module.ControlState(tmp_path / "control.json")
    control.arm("parser_failure", "failed", "mineru_failed")
    control.observe("POST", "/tasks", 202, "upstream-123")
    proof = module._proof(control, "parser_failure", "browser-task", "parser-attempt")

    assert proof["injected"] is True
    assert proof["boundary"] == "external_parser"
    assert proof["task_id"] == "browser-task"
    assert proof["observed_identity"] == "upstream-123"
    assert proof["observed_requests"] == 1


def test_fault_response_shapes_match_gateway_contract() -> None:
    module = _module()

    assert module._fault_response("lost_parser_attempt", "/tasks/upstream")[0] == 404
    assert module._fault_response("parser_failure", "/tasks/upstream")[1]["status"] == "failed"
    assert module._fault_response("partial_or_unsupported", "/tasks/upstream/result") == (
        200,
        {"results": {}},
    )
    assert module._fault_response("auto_retry_exhausted", "/tasks/upstream")[0] == 503
    assert module._fault_response("manual_ui_retry", "/tasks/upstream")[0] == 503
    assert module._fault_response("parser_failure", "/health") is None


def test_proxy_forwards_real_submission_then_injects_status_fault(tmp_path: Path) -> None:
    module = _module()

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:
            # Unread body bytes make close() send a TCP reset, which the
            # proxy reports as a 503 instead of forwarding the 202.
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"task_id":"upstream-1","status":"pending"}')

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"processing","task_id":"upstream-1"}')

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    control = module.ControlState(tmp_path / "control.json")
    control.arm("parser_failure", "failed", "mineru_failed")
    proxy = module._FaultProxyServer(
        ("127.0.0.1", 0),
        f"http://127.0.0.1:{upstream.server_address[1]}",
        control,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    base = f"http://127.0.0.1:{proxy.server_address[1]}"
    try:
        submission = urlopen(Request(f"{base}/tasks", data=b"pdf", method="POST"), timeout=5)
        assert submission.status == 202
        assert json.loads(submission.read())["task_id"] == "upstream-1"
        deadline = time.monotonic() + 5
        while True:
            status = urlopen(f"{base}/tasks/upstream-1", timeout=5)
            assert status.status == 200
            payload = json.loads(status.read())
            if payload["status"] == "failed":
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f"proxy did not inject fault: {payload}")
            time.sleep(0.05)
        observations = control.read()["observations"]
        assert observations[0]["status"] == 202
        assert all(item["status"] == 200 for item in observations[1:])
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proof_rejects_missing_upstream_identity(tmp_path: Path) -> None:
    module = _module()
    control = module.ControlState(tmp_path / "control.json")
    control.arm("parser_failure", "failed", "parser_unavailable")
    control.observe("GET", "/tasks/unknown", 404)
    try:
        module._proof(control, "parser_failure", "browser-task", "parser-attempt")
    except RuntimeError as exc:
        assert "upstream parser task identity" in str(exc)
    else:
        raise AssertionError("proof unexpectedly accepted missing upstream identity")


def test_concurrent_observations_are_not_lost(tmp_path: Path) -> None:
    module = _module()
    control = module.ControlState(tmp_path / "control.json")
    control.arm("parser_failure", "failed", "parser_unavailable")

    def record(index: int) -> None:
        control.observe("GET", f"/tasks/{index}", 200, f"upstream-{index}")

    threads = [threading.Thread(target=record, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    observations = control.read()["observations"]
    assert len(observations) == 20
    assert {item["path"] for item in observations} == {f"/tasks/{index}" for index in range(20)}
