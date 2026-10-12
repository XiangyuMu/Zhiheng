#!/usr/bin/env python3
"""Warm a real MinerU gateway over its authenticated HTTP contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def request(url: str, token: str, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("MinerU response must be an object")
    return payload


def warmup(url: str, token_file: Path, diagnostics: Path, timeout: float) -> int:
    started = time.monotonic()
    report: dict[str, Any] = {
        "status": "running",
        "gateway_url": url,
        "token_file_sha256": hashlib.sha256(token_file.read_bytes()).hexdigest(),
        "observations": [],
    }
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("MinerU token file is empty")
        accepted = request(f"{url.rstrip('/')}/warmup", token, "POST")
        report["attempt_id"] = accepted.get("attempt_id")
        while time.monotonic() - started <= timeout:
            try:
                ready = request(f"{url.rstrip('/')}/ready", token)
                report["observations"].append(
                    {
                        key: ready.get(key)
                        for key in ("status", "gateway", "upstream", "model", "upstream_version")
                    }
                )
                if ready.get("status") == "ready" and ready.get("model") == "ready":
                    report["status"] = "passed"
                    return 0
            except (OSError, ValueError, urllib.error.URLError) as error:
                report["observations"].append({"error_class": type(error).__name__})
            time.sleep(2)
        report["failure_code"] = "mineru_warmup_timeout"
        return 1
    except (OSError, ValueError, urllib.error.URLError) as error:
        report["status"] = "failed"
        report["failure_code"] = type(error).__name__
        return 1
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
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    return warmup(args.url, args.token_file, args.diagnostics, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
