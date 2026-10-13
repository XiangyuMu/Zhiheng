#!/usr/bin/env python3
"""Control an external MinerU fault proxy for the Issue 54 browser harness.

The proxy is intentionally outside Zhiheng's database and parser gateway. It
forwards to a real MinerU HTTP endpoint and only changes responses for an
explicitly armed scenario. State is kept in a standalone JSON file so the
browser harness can prove what was armed and what the proxy observed.

Typical setup in the private runtime host::

    python scripts/issue54_runtime_control.py serve \
      --listen 127.0.0.1:19393 --upstream http://127.0.0.1:19392 \
      --state /tmp/issue54-fault-control.json

Point the private gateway process at ``http://127.0.0.1:19393`` before
starting it. The proxy does not restart services and never opens Zhiheng's
SQLite database.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "name": "lost-parser-attempt",
        "category": "lost_parser_attempt",
        "expected_state": "failed",
        "expected_error_code": "mineru_task_not_found",
        "assert_polling_stopped": True,
    },
    {
        "name": "parser-failure",
        "category": "parser_failure",
        "expected_state": "failed",
        "expected_error_code": "mineru_failed",
        "assert_polling_stopped": True,
    },
    {
        "name": "partial-or-unsupported",
        "category": "partial_or_unsupported",
        "expected_state": "failed",
        "expected_error_code": "mineru_result_invalid",
        "assert_polling_stopped": True,
    },
    {
        "name": "auto-retry-exhausted",
        "category": "auto_retry_exhausted",
        "expected_state": "failed",
        "expected_error_code": "parser_unavailable",
        "expected_retry_count": 3,
        "wait_ms": 300_000,
        "assert_polling_stopped": True,
    },
    {
        "name": "manual-ui-retry",
        "category": "manual_ui_retry",
        "expected_state": "failed",
        "expected_error_code": "parser_unavailable",
        "expect_ui_retry": True,
        "assert_polling_stopped": True,
        "wait_ms": 300_000,
    },
)

_SCENARIO_BY_CATEGORY = {item["category"]: item for item in SCENARIOS}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ControlState:
    """Atomic, standalone controller state shared by proxy and CLI commands."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._lock = threading.Lock()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "schema": "issue54.external-parser-control.v1",
                "armed": None,
                "observations": [],
            }
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read controller state: {self.path}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("controller state must be a JSON object")
        return value

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def write(self, value: dict[str, Any]) -> None:
        with self._lock:
            self._write_unlocked(value)

    def update(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Apply a read-modify-write transaction under one process lock."""
        with self._lock:
            state = self._read_unlocked()
            mutator(state)
            self._write_unlocked(state)
            return state

    def arm(self, category: str, expected_state: str, expected_error_code: str) -> None:
        if category not in _SCENARIO_BY_CATEGORY:
            raise ValueError(f"unsupported Issue 54 category: {category}")
        self.write({
            "schema": "issue54.external-parser-control.v1",
            "armed": {
                "category": category,
                "expected_state": expected_state,
                "expected_error_code": expected_error_code,
                "armed_at": _now(),
                "fault_task_id": None,
                "active": False,
            },
            "observations": [],
        })

    def observe(
        self,
        method: str,
        path: str,
        status: int,
        upstream_task_id: str | None = None,
    ) -> None:
        def mutate(state: dict[str, Any]) -> None:
            armed = state.get("armed")
            if isinstance(armed, dict) and upstream_task_id and not armed.get("fault_task_id"):
                armed["fault_task_id"] = upstream_task_id
            observations = state.setdefault("observations", [])
            if not isinstance(observations, list):
                raise RuntimeError("controller observations must be a list")
            observations.append({
                "at": _now(),
                "method": method,
                "path": path,
                "status": status,
                **({"upstream_task_id": upstream_task_id} if upstream_task_id else {}),
            })
        self.update(mutate)


def _fault_response(category: str, path: str) -> tuple[int, dict[str, Any]] | None:
    if category == "lost_parser_attempt" and path.count("/") == 2:
        return 404, {"detail": "injected lost upstream parser task"}
    if category == "parser_failure" and path.count("/") == 2:
        return 200, {"status": "failed", "task_id": path.rsplit("/", 1)[-1]}
    if category in {"auto_retry_exhausted", "manual_ui_retry"} and path.count("/") == 2:
        return 503, {"detail": "injected external parser outage"}
    if category == "partial_or_unsupported" and path.endswith("/result"):
        return 200, {"results": {}}
    return None


class _ProxyHandler(BaseHTTPRequestHandler):
    server: _FaultProxyServer

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def _proxy(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        state = self.server.control.read()
        armed = state.get("armed")
        path = urlsplit(self.path).path
        fault: tuple[int, dict[str, Any]] | None = None
        if isinstance(armed, dict):
            task_id = armed.get("fault_task_id")
            if task_id is None and self.command == "POST" and path == "/tasks":
                forwarded = self.server.forward(self.command, path, body, self.headers)
                upstream_task_id = _json_task_id(forwarded[1])
                def activate(value: dict[str, Any]) -> None:
                    armed_value = value.get("armed")
                    if isinstance(armed_value, dict) and upstream_task_id:
                        current_task_id = armed_value.get("fault_task_id")
                        if current_task_id and current_task_id != upstream_task_id:
                            return
                        armed_value["fault_task_id"] = current_task_id or upstream_task_id
                        armed_value["active"] = True
                self.server.control.observe(
                    self.command,
                    path,
                    forwarded[0],
                    upstream_task_id,
                )
                self.server.control.update(activate)
                self._send(*forwarded)
                return
            if task_id and armed.get("active") and path.startswith(f"/tasks/{task_id}"):
                fault = _fault_response(str(armed.get("category")), path)
        if fault is None:
            response = self.server.forward(self.command, path, body, self.headers)
        else:
            response = fault
        self.server.control.observe(
            self.command,
            path,
            response[0],
            str(armed.get("fault_task_id")) if isinstance(armed, dict) else None,
        )
        self._send(*response)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _FaultProxyServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], upstream: str, control: ControlState) -> None:
        super().__init__(address, _ProxyHandler)
        self.upstream = upstream.rstrip("/")
        self.control = control

    def forward(
        self, method: str, path: str, body: bytes, headers: Any
    ) -> tuple[int, dict[str, Any]]:
        request = Request(
            f"{self.upstream}{path}",
            data=body or None,
            method=method,
            headers={
                key: value
                for key, value in headers.items()
                if key.lower() in {"authorization", "content-type", "accept"}
            },
        )
        try:
            with urlopen(request, timeout=120) as response:
                raw = response.read()
                return int(response.status), _json_payload(raw)
        except HTTPError as error:
            return int(error.code), _json_payload(error.read())
        except (OSError, URLError):
            return 503, {"detail": "external parser upstream unavailable"}


def _json_payload(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"detail": "upstream returned a non-JSON response"}
    return value if isinstance(value, dict) else {"value": value}


def _json_task_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("task_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _proof(control: ControlState, category: str, task_id: str, attempt_id: str) -> dict[str, Any]:
    state = control.read()
    armed = state.get("armed")
    if not isinstance(armed, dict) or armed.get("category") != category:
        raise RuntimeError(f"controller is not armed for {category}")
    observations = state.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError("controller has no observed external parser request")
    upstream_task_id = armed.get("fault_task_id")
    if not upstream_task_id:
        for observation in reversed(observations):
            candidate = observation.get("upstream_task_id")
            if isinstance(candidate, str) and candidate.strip():
                upstream_task_id = candidate
                break
    if not isinstance(upstream_task_id, str) or not upstream_task_id.strip():
        raise RuntimeError("controller has no observed upstream parser task identity")
    def activate(value: dict[str, Any]) -> None:
        current = value.get("armed")
        if isinstance(current, dict) and current.get("category") == category:
            current["active"] = True
            current["fault_task_id"] = upstream_task_id
    control.update(activate)
    return {
        "injected": True,
        "boundary": "external_parser",
        "category": category,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "observed_identity": upstream_task_id,
        "observed_requests": len(observations),
        "observed_ready": True,
        "proof_source": "standalone-controller-state",
    }


def render_scenarios(command: str) -> list[dict[str, Any]]:
    return [
        {
            **item,
            "injected": True,
            "boundary": "external_parser",
            "command": command,
        }
        for item in SCENARIOS
    ]


def run_command(control: ControlState) -> int:
    category = os.environ.get("ZHIHENG_ISSUE54_FAULT_CATEGORY", "").strip()
    phase = os.environ.get("ZHIHENG_ISSUE54_PHASE", "").strip()
    if phase == "prepare":
        scenario = _SCENARIO_BY_CATEGORY.get(category)
        if scenario is None:
            raise SystemExit(f"unsupported fault category: {category}")
        control.arm(category, scenario["expected_state"], scenario["expected_error_code"])
        print(json.dumps({"armed": True, "category": category}))
        return 0
    if phase == "proof":
        print(
            json.dumps(
                _proof(
                    control,
                    category,
                    os.environ.get("ZHIHENG_ISSUE54_TASK_ID", ""),
                    os.environ.get("ZHIHENG_ISSUE54_ATTEMPT_ID", ""),
                )
            )
        )
        return 0
    raise SystemExit("ZHIHENG_ISSUE54_PHASE must be prepare or proof")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--listen", required=True)
    serve.add_argument("--upstream", required=True)
    serve.add_argument("--state", required=True, type=Path)
    render = subparsers.add_parser("render-scenarios")
    render.add_argument("--command", required=True)
    control = subparsers.add_parser("command")
    control.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "render-scenarios":
        print(json.dumps(render_scenarios(args.command), ensure_ascii=False))
        return 0
    if args.mode == "command":
        return run_command(ControlState(args.state))
    host, separator, port_text = args.listen.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        parser.error("--listen must be HOST:PORT")
    server = _FaultProxyServer(
        (host, int(port_text)), args.upstream, ControlState(args.state)
    )
    def stop(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        json.dumps({"ready": True, "boundary": "external_parser", "listen": args.listen}),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
