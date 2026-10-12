import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
                {"status": "ready", "gateway": "healthy", "upstream": "healthy", "model": "ready"}
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

