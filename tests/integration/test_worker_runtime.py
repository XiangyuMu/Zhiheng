from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from zhiheng.core.config import Settings
from zhiheng.evolution.contracts import EvolutionCommandContext, EvolutionRole
from zhiheng.worker import main as worker_main


def _settings() -> Settings:
    return Settings(environment="test", database_url="sqlite:///synthetic-worker.sqlite")


def _db_error(payload: str = "personal payload must not be logged") -> OperationalError:
    return OperationalError(
        "SELECT * FROM secrets WHERE content=:payload",
        {"payload": payload},
        RuntimeError("database unavailable"),
    )


def test_run_once_propagates_database_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> int:
        raise _db_error()

    monkeypatch.setattr(worker_main, "process_worker_once", fail)
    monkeypatch.setattr(worker_main, "_recover_configured_erase_journal", lambda _settings: None)

    with pytest.raises(OperationalError):
        worker_main.run_once(_settings())


def test_cli_once_success_uses_zero_exit_code_for_completed_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_main, "get_settings", _settings)
    monkeypatch.setattr(worker_main, "run_once", lambda *_args, **_kwargs: 7)

    assert worker_main.main(["--once"]) == 0


def test_cli_once_database_failure_is_nonzero_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> int:
        raise _db_error("sensitive user note")

    monkeypatch.setattr(worker_main, "get_settings", _settings)
    monkeypatch.setattr(worker_main, "run_once", fail)

    with caplog.at_level(logging.ERROR):
        assert worker_main.main(["--once"]) == 1

    log_text = caplog.text
    assert "worker database operation failed" in log_text
    assert "sensitive user note" not in log_text
    assert "SELECT * FROM secrets" not in log_text


def test_unknown_environment_role_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_WORKER_ROLE", "unknown")
    monkeypatch.setattr(worker_main, "get_settings", _settings)

    assert worker_main.main(["--once"]) == 2


def test_argparse_unknown_role_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc_info:
        worker_main.main(["--once", "--role", "unknown"])

    assert exc_info.value.code == 2


class _StopAfterWait:
    def __init__(self) -> None:
        self.waits: list[float] = []
        self._stopped = False

    def is_set(self) -> bool:
        return self._stopped

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self._stopped = True
        return True


def test_resident_worker_idles_without_busy_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def no_work(*_args: Any, **_kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        return 0

    stop_event = _StopAfterWait()
    monkeypatch.setattr(worker_main, "run_once", no_work)

    assert (
        worker_main.serve_forever(
            _settings(),
            stop_event=stop_event,  # type: ignore[arg-type]
            role="worker",
            idle_seconds=0.25,
            backoff_seconds=0.75,
        )
        == 0
    )

    assert calls == 1
    assert stop_event.waits == [0.25]


def test_resident_worker_backs_off_on_database_failure_without_payload_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> int:
        raise _db_error("private diary text")

    stop_event = _StopAfterWait()
    monkeypatch.setattr(worker_main, "run_once", fail)

    with caplog.at_level(logging.WARNING):
        assert (
            worker_main.serve_forever(
                _settings(),
                stop_event=stop_event,  # type: ignore[arg-type]
                role="worker",
                idle_seconds=0.25,
                backoff_seconds=0.75,
            )
            == 0
        )

    assert stop_event.waits == [0.75]
    assert "worker database operation failed" in caplog.text
    assert "private diary text" not in caplog.text
    assert "SELECT * FROM secrets" not in caplog.text


def test_signal_handlers_request_graceful_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[int, Callable[[int, object], None]] = {}

    def capture(signum: int, handler: Callable[[int, object], None]) -> None:
        handlers[signum] = handler

    stop_event = threading.Event()
    monkeypatch.setattr(signal, "signal", capture)

    worker_main._install_signal_handlers(stop_event)
    handlers[signal.SIGTERM](signal.SIGTERM, object())

    assert stop_event.is_set()
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}


def test_evolution_executor_injects_validator_context_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, EvolutionCommandContext | None] = {}

    class FakeExecutor:
        def __init__(
            self,
            _settings: Settings,
            *,
            publisher_context: EvolutionCommandContext | None = None,
            validator_context: EvolutionCommandContext | None = None,
        ) -> None:
            captured["publisher"] = publisher_context
            captured["validator"] = validator_context

    monkeypatch.setattr(worker_main, "EvolutionJobExecutor", FakeExecutor)

    worker_main._evolution_executor(_settings(), validator_id="worker-a:validator")

    assert captured["publisher"] is None
    assert captured["validator"] is not None
    assert captured["validator"].actor_id == "worker-a:validator"
    assert captured["validator"].role is EvolutionRole.VALIDATOR


def test_close_executor_uses_close_hook() -> None:
    class FakeExecutor:
        closed = False

        def close(self) -> None:
            self.closed = True

    executor = FakeExecutor()

    worker_main._close_executor(executor)  # type: ignore[arg-type]

    assert executor.closed is True


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_cli_rejects_busy_loop_or_unbounded_wait(value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        worker_main.main(["--idle-seconds", value])
    assert exc_info.value.code == 2


def test_compose_uses_single_native_worker_process() -> None:
    compose = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")

    assert 'command: ["zhiheng-worker", "--role", "publisher"]' in compose
    assert "while true" not in compose
