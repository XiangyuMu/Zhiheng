#!/usr/bin/env python3
"""Warm a real MinerU gateway over its authenticated HTTP contract."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def local_origin(url: str) -> str:
    parsed = urlsplit(url)
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def request(url: str, token: str, timeout: float, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("MinerU response must be an object")
    return payload


def warmup(url: str, token_file: Path, diagnostics: Path, timeout: float) -> int:
    started = time.monotonic()
    report: dict[str, Any] = {
        "status": "failed",
        "observations": [],
    }
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    try:
        url = local_origin(url)
        report["gateway_url"] = url
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        report["sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("MinerU token file is empty")
        accepted = request(f"{url.rstrip('/')}/warmup", token, min(10, timeout), "POST")
        report["attempt_id"] = accepted.get("attempt_id")
        if not isinstance(report["attempt_id"], str) or not report["attempt_id"]:
            raise ValueError("warmup did not return an attempt")
        while (remaining := timeout - (time.monotonic() - started)) > 0:
            try:
                ready = request(f"{url.rstrip('/')}/ready", token, min(10, remaining))
                report["observations"].append(
                    {
                        key: ready.get(key)
                        for key in ("status", "gateway", "upstream", "model", "upstream_version")
                    }
                )
                probe = ready.get("probe")
                if (
                    ready.get("status") == "ready"
                    and ready.get("model") == "ready"
                    and isinstance(probe, dict)
                    and probe.get("attempt_id") == report["attempt_id"]
                    and probe.get("state") == "succeeded"
                    and time.monotonic() - started <= timeout
                ):
                    report["probe"] = {"attempt_id": probe["attempt_id"], "state": probe["state"]}
                    report["status"] = "passed"
                    return 0
            except (OSError, ValueError, urllib.error.URLError) as error:
                report["observations"].append({"error_class": type(error).__name__})
            time.sleep(min(2, max(0, timeout - (time.monotonic() - started))))
        report["failure_code"] = "mineru_warmup_timeout"
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["status"] = "failed"
        report["failure_code"] = type(error).__name__
        return 1
    except KeyboardInterrupt:
        report["failure_code"] = "interrupted"
        return 130
    finally:
        report["seconds"] = round(time.monotonic() - started, 3)
        diagnostics.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:9392")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    return warmup(args.url, args.token_file, args.diagnostics, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
