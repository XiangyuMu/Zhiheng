from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_pdf_parser_upstream_lock_has_reviewed_source_pins() -> None:
    lock = json.loads((ROOT / "deploy/pdf-parser/UPSTREAM.lock").read_text(encoding="utf-8"))

    assert lock["deepdoc"]["release"] == "v0.27.1"
    assert lock["deepdoc"]["commit"] == "b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e"
    assert lock["mineru"]["release"] == "mineru-3.4.5-released"
    assert lock["mineru"]["commit"] == "fbb1257a555a3fde78ae5aaaa931e3b3f8fb2883"
    for key in ("deepdoc_image_digest", "mineru_image_digest"):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", lock["build_outputs"][key])
    assert lock["build_outputs"]["status"] == "locally-built; registry-publish-required"


def test_pdf_parser_compose_is_internal_and_resource_bounded() -> None:
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    locked = (ROOT / "deploy/docker-compose.pdf.locked.yml").read_text(encoding="utf-8")

    for service, port in (("deepdoc-worker", "9390"), ("mineru-worker", "9391")):
        assert f"  {service}:" in compose
        assert 'profiles: ["pdf"]' in compose
        assert f'"{port}"' in compose
        assert "read_only: true" in compose
        assert 'cap_drop: ["ALL"]' in compose
        assert "no-new-privileges:true" in compose
        assert 'cpus: "4.0"' in compose
        assert "memory: 8G" in compose

    assert re.search(
        r"ZHIHENG_DEEPDOC_IMAGE:\?ZHIHENG_DEEPDOC_IMAGE must be a registry @sha256 digest",
        locked,
    )
    assert re.search(
        r"ZHIHENG_MINERU_IMAGE:\?ZHIHENG_MINERU_IMAGE must be a registry @sha256 digest",
        locked,
    )
