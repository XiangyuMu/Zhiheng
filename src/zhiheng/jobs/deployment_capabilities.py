"""Deployment capabilities a worker declares through its configuration.

PDF parsing is the only optional capability.  It must be declared explicitly
(``mineru``, ``deepdoc`` or ``none``) so a forgotten configuration fails at
startup instead of turning every PDF import into ``unsupported``.  See ADR 0005.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx

from zhiheng.core.config import Settings
from zhiheng.jobs.pdf_parsing import _secret_or_env, _setting_or_env
from zhiheng.knowledge.pdf_worker import (
    ParserAttemptNotFoundError,
    ParserAuthenticationError,
    ParserUnavailableError,
    ParserWorkerClient,
    ParserWorkerError,
)

PDF_BACKENDS = ("mineru", "deepdoc", "none")
PROBE_TIMEOUT_SECONDS = 5.0


def deployment_capabilities(settings: Settings) -> dict[str, dict[str, Any]]:
    """Report each deployment capability's declaration without contacting services."""

    return {"pdf_parsing": _pdf_parsing(settings)}


def deployment_capabilities_configured(capabilities: dict[str, dict[str, Any]]) -> bool:
    return all(capability["configured"] for capability in capabilities.values())


def probe_pdf_parsing(
    settings: Settings, *, http_client: httpx.Client | None = None
) -> tuple[bool, str]:
    """Ask the declared parser service for an unknown attempt.

    A 404 proves the service is reachable and accepted the token, without
    submitting work.
    """

    endpoint = _pdf_endpoint(settings)
    if endpoint is None:
        return False, "PDF parsing has no declared parser service"
    url, token = endpoint
    client = ParserWorkerClient(
        url,
        service_token=token,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        http_client=http_client,
    )
    try:
        client.status(str(uuid4()))
    except ParserAttemptNotFoundError:
        return True, f"parser service at {url} accepted the token"
    except ParserAuthenticationError:
        return False, f"parser service at {url} rejected the token"
    except ParserUnavailableError as exc:
        return False, f"parser service at {url} is unreachable: {exc}"
    except ParserWorkerError as exc:
        return False, f"parser service at {url} answered outside the contract: {exc}"
    finally:
        client.close()
    return False, f"parser service at {url} returned an unexpected attempt"


def _pdf_parsing(settings: Settings) -> dict[str, Any]:
    declared = _setting_or_env(settings, "pdf_parser_backend", "ZHIHENG_PDF_PARSER_BACKEND")
    if declared is None:
        return _report(
            None,
            False,
            "ZHIHENG_PDF_PARSER_BACKEND is not declared; set mineru, deepdoc or none",
        )
    if declared not in PDF_BACKENDS:
        return _report(
            declared,
            False,
            "ZHIHENG_PDF_PARSER_BACKEND must be mineru, deepdoc or none",
        )
    if declared == "none":
        return _report(declared, True, "PDF parsing is declared off; PDF imports are unsupported")
    env_prefix = f"ZHIHENG_PDF_{declared.upper()}"
    url = _setting_or_env(settings, f"pdf_{declared}_url", f"{env_prefix}_URL")
    if url is None:
        return _report(declared, False, f"{env_prefix}_URL is not set")
    try:
        token = _secret_or_env(settings, f"pdf_{declared}_token", f"{env_prefix}_TOKEN")
    except RuntimeError:
        return _report(declared, False, f"{env_prefix}_TOKEN_FILE cannot be read as a token")
    if token is None:
        return _report(declared, False, f"{env_prefix}_TOKEN or {env_prefix}_TOKEN_FILE is not set")
    try:
        ParserWorkerClient(url, service_token=token).close()
    except ValueError as exc:
        return _report(declared, False, f"{env_prefix} configuration is invalid: {exc}")
    return _report(declared, True, f"PDF parsing uses {declared} at {url}")


def _pdf_endpoint(settings: Settings) -> tuple[str, str] | None:
    declared = _setting_or_env(settings, "pdf_parser_backend", "ZHIHENG_PDF_PARSER_BACKEND")
    if declared not in {"mineru", "deepdoc"}:
        return None
    env_prefix = f"ZHIHENG_PDF_{declared.upper()}"
    url = _setting_or_env(settings, f"pdf_{declared}_url", f"{env_prefix}_URL")
    token = _secret_or_env(settings, f"pdf_{declared}_token", f"{env_prefix}_TOKEN")
    if url is None or token is None:
        return None
    return url, token


def _report(declared: str | None, configured: bool, detail: str) -> dict[str, Any]:
    return {"declared": declared, "configured": configured, "detail": detail}
