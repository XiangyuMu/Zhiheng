from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import threading
from collections.abc import Callable
from typing import Literal, cast

from sqlalchemy.exc import OperationalError

from zhiheng.core.config import Settings, get_settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evolution.contracts import (
    EvolutionCommandContext,
    EvolutionRole,
    command_context_for_role,
)
from zhiheng.evolution.jobs import EvolutionJobExecutor, process_jobs_once
from zhiheng.evolution.orphan_gc import OrphanArtifactGC
from zhiheng.jobs import (
    ClassificationSuggestionJobExecutor,
    KnowledgeIndexJobExecutor,
    MemoryExtractionJobExecutor,
    OutboxRepository,
    PdfParseJobExecutor,
    configured_pdf_parse_executor,
    process_classification_jobs_once,
    process_knowledge_jobs_once,
    process_memory_extraction_jobs_once,
)
from zhiheng.recovery import startup_recovery_barrier

logger = logging.getLogger(__name__)
KnowledgeExecutorFactory = Callable[[Settings], KnowledgeIndexJobExecutor]
PdfExecutorFactory = Callable[[Settings], PdfParseJobExecutor]
WorkerRole = Literal["worker", "publisher"]
DEFAULT_IDLE_SECONDS = 2.0
DEFAULT_BACKOFF_SECONDS = 5.0


def process_outbox_once(settings: Settings, *, limit: int = 10) -> int:
    engine = create_sqlite_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        repository = OutboxRepository()
        with session_scope(session_factory) as session:
            events = repository.claim_pending(session, limit=limit)
            return repository.enqueue_jobs_for_events(session, events)
    finally:
        engine.dispose()


def process_worker_once(
    settings: Settings,
    *,
    limit: int = 10,
    worker_id: str = "worker",
    knowledge_executor_factory: KnowledgeExecutorFactory | None = None,
    pdf_executor_factory: PdfExecutorFactory | None = None,
) -> int:
    engine = create_sqlite_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        repository = OutboxRepository()
        with session_scope(session_factory) as session:
            events = repository.claim_pending(session, limit=limit)
            enqueued = repository.enqueue_jobs_for_events(session, events)
        with session_scope(session_factory) as session:
            gc = OrphanArtifactGC(settings.secret_key.get_secret_value())
            plan = gc.prepare(session)
            gc.reap(session, plan)
        indexed = process_knowledge_jobs_once(
            session_factory,
            _knowledge_executor(settings, knowledge_executor_factory),
            worker_id=worker_id,
            limit=limit,
            pdf_executor=_pdf_executor(settings, pdf_executor_factory),
        )
        classified = process_classification_jobs_once(
            session_factory,
            ClassificationSuggestionJobExecutor(),
            worker_id=worker_id,
            limit=limit,
        )
        extracted = process_memory_extraction_jobs_once(
            session_factory,
            MemoryExtractionJobExecutor(),
            worker_id=worker_id,
            limit=limit,
        )
        executor = _evolution_executor(
            settings,
            validator_id=f"{worker_id}:validator",
        )
        try:
            executed = process_jobs_once(
                session_factory,
                executor,
                worker_id=worker_id,
                limit=limit,
            )
        finally:
            _close_executor(executor)
        return enqueued + indexed + classified + extracted + executed
    finally:
        engine.dispose()


def process_publisher_once(
    settings: Settings,
    *,
    limit: int = 10,
    publisher_id: str = "publisher",
    knowledge_executor_factory: KnowledgeExecutorFactory | None = None,
    pdf_executor_factory: PdfExecutorFactory | None = None,
) -> int:
    engine = create_sqlite_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        indexed = process_knowledge_jobs_once(
            session_factory,
            _knowledge_executor(settings, knowledge_executor_factory),
            worker_id=publisher_id,
            limit=limit,
            pdf_executor=_pdf_executor(settings, pdf_executor_factory),
        )
        executor = _evolution_executor(
            settings,
            publisher_context=command_context_for_role(
                publisher_id,
                EvolutionRole.PUBLISHER,
            ),
            validator_id=f"{publisher_id}:validator",
        )
        try:
            executed = process_jobs_once(
                session_factory,
                executor,
                worker_id=publisher_id,
                limit=limit,
            )
        finally:
            _close_executor(executor)
        return indexed + executed
    finally:
        engine.dispose()


def run_once(
    settings: Settings | None = None,
    *,
    knowledge_executor_factory: KnowledgeExecutorFactory | None = None,
    role: WorkerRole | str | None = None,
    limit: int = 10,
    worker_id: str | None = None,
    pdf_executor_factory: PdfExecutorFactory | None = None,
) -> int:
    worker_settings = settings or get_settings()
    _recover_configured_erase_journal(worker_settings)
    worker_role = _resolve_role(role)
    if worker_role == "publisher":
        process_outbox_once(worker_settings, limit=limit)
        return process_publisher_once(
            worker_settings,
            limit=limit,
            publisher_id=worker_id or "publisher",
            knowledge_executor_factory=knowledge_executor_factory,
            pdf_executor_factory=pdf_executor_factory,
        )
    return process_worker_once(
        worker_settings,
        limit=limit,
        worker_id=worker_id or "worker",
        knowledge_executor_factory=knowledge_executor_factory,
        pdf_executor_factory=pdf_executor_factory,
    )


def _recover_configured_erase_journal(settings: Settings) -> None:
    """Validate and replay the journal before the worker can dispatch."""
    if os.environ.get("ZHIHENG_ERASE_JOURNAL_PATH"):
        engine = create_sqlite_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            startup_recovery_barrier(settings, session_factory)
        finally:
            engine.dispose()


def log_startup(settings: Settings) -> None:
    logger.info("worker started", extra={"environment": settings.environment})


def serve_forever(
    settings: Settings,
    *,
    stop_event: threading.Event,
    role: WorkerRole,
    limit: int = 10,
    worker_id: str | None = None,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    knowledge_executor_factory: KnowledgeExecutorFactory | None = None,
    pdf_executor_factory: PdfExecutorFactory | None = None,
) -> int:
    if not all(math.isfinite(value) and value > 0 for value in (idle_seconds, backoff_seconds)):
        raise ValueError("worker wait intervals must be finite and positive")
    while not stop_event.is_set():
        try:
            completed = run_once(
                settings,
                knowledge_executor_factory=knowledge_executor_factory,
                role=role,
                limit=limit,
                worker_id=worker_id,
                pdf_executor_factory=pdf_executor_factory,
            )
        except OperationalError:
            logger.warning(
                "worker database operation failed",
                extra={"error_class": "OperationalError", "role": role},
            )
            _wait_for_stop(stop_event, backoff_seconds)
            continue
        if completed <= 0:
            _wait_for_stop(stop_event, idle_seconds)
    return 0


def run() -> None:
    raise SystemExit(main())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Zhiheng background worker.")
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument("--role", choices=("worker", "publisher"), default=None)
    parser.add_argument("--limit", type=_positive_int, default=10)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--idle-seconds", type=_positive_seconds, default=DEFAULT_IDLE_SECONDS)
    parser.add_argument(
        "--backoff-seconds",
        type=_positive_seconds,
        default=DEFAULT_BACKOFF_SECONDS,
    )
    args = parser.parse_args(argv)

    try:
        role = _resolve_role(args.role)
        settings = get_settings()
    except Exception as exc:
        logger.error(
            "worker startup configuration failed",
            extra={"error_class": exc.__class__.__name__},
        )
        return 2

    log_startup(settings)
    if args.once:
        try:
            run_once(settings, role=role, limit=args.limit, worker_id=args.worker_id)
        except OperationalError:
            logger.error(
                "worker database operation failed",
                extra={"error_class": "OperationalError", "role": role},
            )
            return 1
        return 0

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    return serve_forever(
        settings,
        stop_event=stop_event,
        role=role,
        limit=args.limit,
        worker_id=args.worker_id,
        idle_seconds=args.idle_seconds,
        backoff_seconds=args.backoff_seconds,
    )


def _resolve_role(role: WorkerRole | str | None = None) -> WorkerRole:
    candidate = role if role is not None else os.environ.get("ZHIHENG_WORKER_ROLE", "worker")
    if candidate == "worker" or candidate == "publisher":
        return cast(WorkerRole, candidate)
    raise ValueError("ZHIHENG_WORKER_ROLE must be 'worker' or 'publisher'")


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum: int, _frame: object) -> None:
        logger.info("worker shutdown requested", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _wait_for_stop(stop_event: threading.Event, seconds: float) -> None:
    stop_event.wait(seconds)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _knowledge_executor(
    settings: Settings,
    factory: KnowledgeExecutorFactory | None,
) -> KnowledgeIndexJobExecutor:
    if factory is not None:
        return factory(settings)
    return KnowledgeIndexJobExecutor(settings)


def _pdf_executor(
    settings: Settings,
    factory: PdfExecutorFactory | None,
) -> PdfParseJobExecutor | None:
    if factory is None:
        return configured_pdf_parse_executor(settings)
    return factory(settings)


def _evolution_executor(
    settings: Settings,
    *,
    publisher_context: EvolutionCommandContext | None = None,
    validator_id: str,
) -> EvolutionJobExecutor:
    return EvolutionJobExecutor(
        settings,
        publisher_context=publisher_context,
        validator_context=command_context_for_role(validator_id, EvolutionRole.VALIDATOR),
    )


def _close_executor(executor: EvolutionJobExecutor) -> None:
    executor.close()


if __name__ == "__main__":
    run()
