#!/usr/bin/env python3
"""Check authenticated MinerU gateway readiness without exposing its token."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:9392")
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"{args.url.rstrip('/')}/ready",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if payload.get("status") != "ready" or payload.get("model") != "ready":
        raise SystemExit("MinerU gateway is not model-ready")
    print(json.dumps({key: payload.get(key) for key in ("status", "gateway", "upstream", "model")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
