#!/usr/bin/env python3
"""Check authenticated MinerU gateway readiness without exposing its token."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:9392")
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    origin = urlsplit(args.url)
    if (
        origin.scheme not in {"http", "https"}
        or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
        or origin.username is not None or origin.password is not None
        or origin.query or origin.fragment or origin.path not in {"", "/"}
    ):
        parser.error("MinerU gateway must be a loopback origin without credentials")
    _ = origin.port
    token = args.token_file.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"{args.url.rstrip('/')}/ready",
        headers={"Authorization": f"Bearer {token}"},
    )
    # A loopback gateway must not redirect authenticated requests elsewhere.
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=10) as response:
        payload = json.load(response)
    if payload.get("status") != "ready" or payload.get("model") != "ready":
        raise SystemExit("MinerU gateway is not model-ready")
    print(json.dumps({key: payload.get(key) for key in ("status", "gateway", "upstream", "model")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
