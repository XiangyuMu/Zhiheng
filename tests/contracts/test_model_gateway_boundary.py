from __future__ import annotations

import ast
from importlib.metadata import version
from pathlib import Path


def test_models_package_exports_only_public_gateway_boundary() -> None:
    import zhiheng.models as models

    assert set(models.__all__) == {
        "ImagePart",
        "ModelContentPart",
        "ModelGateway",
        "ModelRequest",
        "ModelResponse",
        "TextPart",
    }
    assert not hasattr(models, "OpenAICompatibleChatAdapter")
    assert not hasattr(models, "ApprovedOutboundPayload")


def test_network_libraries_are_only_imported_by_private_model_transports() -> None:
    runtime_files = Path("src/zhiheng").rglob("*.py")
    offenders: list[str] = []
    blocked_modules = {
        "aiohttp",
        "httpx",
        "openai",
        "requests",
        "socket",
        "urllib.request",
    }
    for path in runtime_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_network = any(
            _module_blocked(alias.name, blocked_modules)
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ) or any(
            node.module is not None
            and any(
                _module_blocked(node.module, blocked_modules)
                or _module_blocked(f"{node.module}.{alias.name}", blocked_modules)
                for alias in node.names
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        if imports_network and path.as_posix() not in {
            "src/zhiheng/models/_transports.py",
            "src/zhiheng/knowledge/pdf_worker.py",
            "src/zhiheng/knowledge/import_adapters.py",
        }:
            offenders.append(path.as_posix())

    assert offenders == []


def test_business_code_does_not_import_private_model_transports() -> None:
    runtime_files = [
        path for path in Path("src/zhiheng").rglob("*.py") if path.name != "_transports.py"
    ]
    offenders: list[str] = []
    for path in runtime_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_private_transport = any(
            (isinstance(node, ast.ImportFrom) and node.module == "zhiheng.models._transports")
            or (
                isinstance(node, ast.Import)
                and any(alias.name == "zhiheng.models._transports" for alias in node.names)
            )
            for node in ast.walk(tree)
        )
        if imports_private_transport and path.as_posix() != "src/zhiheng/models/gateway.py":
            offenders.append(path.as_posix())

    assert offenders == []


def test_base_install_import_smoke_for_runtime_boundary() -> None:
    import httpx

    import zhiheng.auth
    import zhiheng.models
    import zhiheng.privacy.gateway

    assert httpx.__version__
    assert version("openai")
    assert version("presidio-analyzer")
    assert callable(zhiheng.models.ModelGateway)
    assert callable(zhiheng.auth.SessionService)
    assert callable(zhiheng.privacy.gateway.PrivacyPipeline)


def _module_blocked(module: str, blocked_modules: set[str]) -> bool:
    return any(module == blocked or module.startswith(f"{blocked}.") for blocked in blocked_modules)
