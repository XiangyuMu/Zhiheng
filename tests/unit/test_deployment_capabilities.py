from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from zhiheng.core.config import Settings
from zhiheng.jobs import configured_pdf_parse_executor
from zhiheng.jobs.deployment_capabilities import (
    deployment_capabilities,
    probe_pdf_parsing,
)
from zhiheng.worker import main as worker_main

SECRET = "deployment-capabilities-secret"
TOKEN = "parser-service-token"


@pytest.fixture(autouse=True)
def _clear_parser_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ZHIHENG_PDF_PARSER_BACKEND",
        "ZHIHENG_PDF_MINERU_URL",
        "ZHIHENG_PDF_MINERU_TOKEN",
        "ZHIHENG_PDF_MINERU_TOKEN_FILE",
        "ZHIHENG_PDF_DEEPDOC_URL",
        "ZHIHENG_PDF_DEEPDOC_TOKEN",
        "ZHIHENG_PDF_DEEPDOC_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, environment="test", secret_key=SECRET, **values)


def test_undeclared_pdf_parsing_is_not_configured() -> None:
    pdf = deployment_capabilities(_settings())["pdf_parsing"]

    assert pdf["declared"] is None
    assert pdf["configured"] is False
    assert "ZHIHENG_PDF_PARSER_BACKEND" in pdf["detail"]


def test_pdf_parsing_declared_off_is_configured() -> None:
    pdf = deployment_capabilities(_settings(pdf_parser_backend="none"))["pdf_parsing"]

    assert pdf == {"declared": "none", "configured": True, "detail": pdf["detail"]}


def test_declared_off_pdf_parsing_builds_no_executor() -> None:
    assert configured_pdf_parse_executor(_settings(pdf_parser_backend="none")) is None


def test_declared_backend_without_url_is_not_configured() -> None:
    pdf = deployment_capabilities(
        _settings(pdf_parser_backend="mineru", pdf_mineru_token=TOKEN)
    )["pdf_parsing"]

    assert pdf["configured"] is False
    assert "ZHIHENG_PDF_MINERU_URL" in pdf["detail"]


def test_declared_backend_with_missing_token_file_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ZHIHENG_PDF_MINERU_TOKEN_FILE", str(tmp_path / "missing-token"))
    pdf = deployment_capabilities(
        _settings(pdf_parser_backend="mineru", pdf_mineru_url="http://127.0.0.1:9392")
    )["pdf_parsing"]

    assert pdf["configured"] is False
    assert "token" in pdf["detail"]


def test_complete_declaration_is_configured_without_leaking_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("ZHIHENG_PDF_MINERU_TOKEN_FILE", str(token_file))
    capabilities = deployment_capabilities(
        _settings(pdf_parser_backend="mineru", pdf_mineru_url="http://127.0.0.1:9392")
    )

    assert capabilities["pdf_parsing"]["configured"] is True
    assert capabilities["pdf_parsing"]["declared"] == "mineru"
    assert TOKEN not in json.dumps(capabilities)


def _mineru_settings() -> Settings:
    return _settings(
        pdf_parser_backend="mineru",
        pdf_mineru_url="http://parser.test",
        pdf_mineru_token=TOKEN,
    )


def _client(status_code: int) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status_code)))


def test_probe_reports_reachable_when_parser_authenticates_unknown_attempt() -> None:
    reachable, detail = probe_pdf_parsing(_mineru_settings(), http_client=_client(404))

    assert reachable is True, detail


def test_probe_reports_rejected_token() -> None:
    reachable, detail = probe_pdf_parsing(_mineru_settings(), http_client=_client(401))

    assert reachable is False
    assert "token" in detail


def test_probe_reports_unreachable_parser() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    reachable, _detail = probe_pdf_parsing(_mineru_settings(), http_client=client)

    assert reachable is False


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(worker_main, "get_settings", lambda: settings)


def test_check_exits_one_and_prints_json_when_declaration_is_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_settings(monkeypatch, _settings())

    assert worker_main.main(["--check"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["pdf_parsing"]["configured"] is False


def test_check_exits_zero_when_pdf_parsing_is_declared_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_settings(monkeypatch, _settings(pdf_parser_backend="none"))

    assert worker_main.main(["--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pdf_parsing"]["declared"] == "none"
    assert "reachable" not in report["pdf_parsing"]


def test_check_exits_two_when_declared_parser_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_settings(monkeypatch, _mineru_settings())
    monkeypatch.setattr(
        worker_main, "probe_pdf_parsing", lambda _settings: (False, "connection refused")
    )

    assert worker_main.main(["--check"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["pdf_parsing"]["reachable"] is False


def test_check_exits_zero_when_declared_parser_is_reachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_settings(monkeypatch, _mineru_settings())
    monkeypatch.setattr(worker_main, "probe_pdf_parsing", lambda _settings: (True, "ok"))

    assert worker_main.main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out)["pdf_parsing"]["reachable"] is True


@pytest.mark.parametrize("role", ["worker", "publisher"])
def test_worker_refuses_to_start_without_declared_pdf_parsing(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    _patch_settings(monkeypatch, _settings())

    def must_not_run(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("worker must not process jobs")

    monkeypatch.setattr(worker_main, "run_once", must_not_run)
    monkeypatch.setattr(worker_main, "serve_forever", must_not_run)

    assert worker_main.main(["--once", "--role", role]) == 2


def test_worker_starts_when_pdf_parsing_is_declared_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, _settings(pdf_parser_backend="none"))
    calls: list[str] = []

    def record_run(*_args: object, **_kwargs: object) -> int:
        calls.append("run")
        return 0

    monkeypatch.setattr(worker_main, "run_once", record_run)

    assert worker_main.main(["--once"]) == 0
    assert calls == ["run"]


def test_token_file_declared_in_env_file_is_read(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ZHIHENG_PDF_PARSER_BACKEND=mineru\n"
        "ZHIHENG_PDF_MINERU_URL=http://127.0.0.1:9392\n"
        f"ZHIHENG_PDF_MINERU_TOKEN_FILE={token_file}\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file, environment="test", secret_key=SECRET)

    assert deployment_capabilities(settings)["pdf_parsing"]["configured"] is True
    assert configured_pdf_parse_executor(settings) is not None
