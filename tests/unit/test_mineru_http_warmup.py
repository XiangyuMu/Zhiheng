import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import mineru_http_warmup as module
from scripts.mineru_http_warmup import warmup


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        assert self.headers.get("Authorization") == "Bearer secret"
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b'{"attempt_id":"warmup-1"}')

    def do_GET(self) -> None:
        assert self.headers.get("Authorization") == "Bearer secret"
        type(self).calls += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "status": "ready",
                    "gateway": "healthy",
                    "upstream": "healthy",
                    "model": "ready",
                    "probe": {"attempt_id": "warmup-1", "state": "succeeded"},
                }
            ).encode()
        )


def test_warmup_requires_real_ready_and_never_writes_token(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = tmp_path / "token"
        token.write_text("secret\n", encoding="utf-8")
        report = tmp_path / "warmup.json"
        assert warmup(f"http://127.0.0.1:{server.server_port}", token, report, 1) == 0
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["status"] == "passed"
        assert payload["attempt_id"] == "warmup-1"
        assert "secret" not in report.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_missing_token_preserves_failure_report(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    assert warmup("http://127.0.0.1:9392", tmp_path / "missing", report, 1) == 1
    assert json.loads(report.read_text())["failure_code"] == "FileNotFoundError"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://user:secret@localhost",
        "http://127.0.0.1?key=secret",
        "http://localhost/#secret",
        "http://localhost/elsewhere",
    ],
)
def test_untrusted_url_rejected_without_reading_token_or_recording_url(
    tmp_path: Path,
    url: str,
) -> None:
    report = tmp_path / "report.json"
    assert warmup(url, tmp_path / "missing", report, 1) == 1
    payload = json.loads(report.read_text())
    assert payload["failure_code"] == "ValueError"
    assert "gateway_url" not in payload
    assert "secret" not in report.read_text()


def test_authenticated_requests_do_not_follow_redirects(tmp_path: Path) -> None:
    reached_sink: list[bool] = []

    class Redirect(Handler):
        def do_GET(self) -> None:
            if self.path == "/sink":
                reached_sink.append(True)
            self.send_response(302)
            self.send_header("Location", "/sink")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = tmp_path / "token"
        token.write_text("secret")
        report = tmp_path / "report.json"
        assert warmup(f"http://127.0.0.1:{server.server_port}", token, report, 0.1) == 1
        assert not reached_sink
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize("mode", ["missing_attempt", "wrong_attempt", "not_ready"])
def test_unproven_readiness_fails_with_bounded_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    token = tmp_path / "token"
    token.write_text("private-token")
    report = tmp_path / "report.json"

    def respond(url: str, token: str, timeout: float, method: str = "GET") -> dict[str, object]:
        assert 0 < timeout <= 0.05
        if method == "POST":
            return {} if mode == "missing_attempt" else {"attempt_id": "current"}
        return {
            "status": "not_ready" if mode == "not_ready" else "ready",
            "model": "ready",
            "probe": {"attempt_id": "wrong", "state": "succeeded"},
        }

    monkeypatch.setattr(module, "request", respond)
    started = time.monotonic()
    assert warmup("http://127.0.0.1:9392", token, report, 0.05) == 1
    assert time.monotonic() - started < 1
    payload = json.loads(report.read_text())
    assert payload["status"] == "failed"
    assert payload["failure_code"]
    assert "private-token" not in report.read_text()
    assert "token_file_sha256" not in payload
