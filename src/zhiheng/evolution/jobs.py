from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id
from zhiheng.db.maintenance import ServingSQLiteConnection
from zhiheng.db.session import session_scope
from zhiheng.evaluation.g006_runner import G006ExecutionStage
from zhiheng.evaluation.proposal_execution import ProposalExecutionService
from zhiheng.evaluation.release_execution import ReleaseExecutionService
from zhiheng.evolution.contracts import (
    EvolutionCapability,
    EvolutionCommandContext,
    EvolutionRole,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.maintenance import (
    MaintenanceCadence,
    MaintenanceTrigger,
    MaintenanceTriggerKind,
    SleepLearningMaintenanceService,
)
from zhiheng.evolution.releases import ReleaseController


class EvolutionJobType(StrEnum):
    PROPOSAL_EVALUATION = "proposal-evaluation"
    REPLAY = "replay"
    CANARY = "canary"
    EVALUATION = "evaluation"
    MAINTENANCE = "maintenance"
    PROMOTION_REQUEST = "promotion-request"
    ROLLBACK_REQUEST = "rollback-request"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    job_type: EvolutionJobType
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int
    lease_owner: str | None = None
    attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobResult:
    status: str
    detail: dict[str, Any]


class JobRepository:
    def enqueue(
        self,
        session: Session,
        *,
        job_type: EvolutionJobType | str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> bool:
        session.execute(
            text(
                """
                INSERT OR IGNORE INTO jobs (
                  id, job_type, idempotency_key, payload_json, status
                )
                VALUES (
                  :id, :job_type, :idempotency_key, :payload_json, 'pending'
                )
                """
            ),
            {
                "id": new_id(),
                "job_type": str(job_type),
                "idempotency_key": idempotency_key,
                "payload_json": json_text(dict(payload)),
            },
        )
        return int(session.execute(text("SELECT changes()")).scalar_one()) == 1

    def claim_available(
        self,
        session: Session,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
        allowed_job_types: frozenset[EvolutionJobType] | None = None,
    ) -> list[ClaimedJob]:
        allowed = frozenset(EvolutionJobType) if allowed_job_types is None else allowed_job_types
        if not allowed:
            return []
        allowed_json = json_text(sorted(EvolutionJobType(kind).value for kind in allowed))
        self._dead_letter_exhausted_leases(session, allowed_json=allowed_json, limit=limit)
        rows = (
            session.execute(
                text(
                    """
                SELECT id, job_type, idempotency_key, payload_json, attempts
                FROM jobs
                WHERE available_at <= CURRENT_TIMESTAMP
                  AND job_type IN (SELECT value FROM json_each(:allowed_job_types))
                  AND job_type IN (
                    'replay',
                    'proposal-evaluation',
                    'evaluation',
                    'canary',
                    'maintenance',
                    'promotion-request',
                    'rollback-request'
                  )
                  AND (
                    status = 'pending'
                    OR (
                      status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= CURRENT_TIMESTAMP
                    )
                  )
                  AND attempts < max_attempts
                ORDER BY available_at, id
                LIMIT :limit
                """
                ),
                {"limit": limit, "allowed_job_types": allowed_json},
            )
            .mappings()
            .all()
        )

        claimed: list[ClaimedJob] = []
        for row in rows:
            session.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'processing',
                        lease_owner = :worker_id,
                        lease_expires_at = datetime('now', :lease_delta),
                        heartbeat_at = CURRENT_TIMESTAMP,
                        attempts = attempts + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :job_id
                      AND job_type IN (SELECT value FROM json_each(:allowed_job_types))
                      AND job_type IN (
                        'replay',
                        'proposal-evaluation',
                        'evaluation',
                        'canary',
                        'maintenance',
                        'promotion-request',
                        'rollback-request'
                      )
                      AND available_at <= CURRENT_TIMESTAMP
                      AND (
                        status = 'pending'
                        OR (
                          status = 'processing'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= CURRENT_TIMESTAMP
                        )
                      )
                      AND attempts < max_attempts
                    """
                ),
                {
                    "job_id": row["id"],
                    "worker_id": worker_id,
                    "lease_delta": f"+{lease_seconds} seconds",
                    "allowed_job_types": allowed_json,
                },
            )
            if int(session.execute(text("SELECT changes()")).scalar_one()) == 1:
                claimed.append(
                    ClaimedJob(
                        id=str(row["id"]),
                        job_type=EvolutionJobType(str(row["job_type"])),
                        idempotency_key=str(row["idempotency_key"]),
                        payload=_json_object(row["payload_json"]),
                        attempts=int(row["attempts"]) + 1,
                        lease_owner=worker_id,
                        attempt_id=self._record_attempt_start(session, job_id=str(row["id"])),
                    )
                )
        return claimed

    def _dead_letter_exhausted_leases(
        self, session: Session, *, allowed_json: str, limit: int,
    ) -> None:
        """A crash on the last allowed attempt must not leave a job processing forever."""
        rows = session.execute(
            text(
                """
                UPDATE jobs
                SET status='dead', lease_owner=NULL, lease_expires_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id IN (
                    SELECT id FROM jobs
                    WHERE job_type IN (SELECT value FROM json_each(:allowed_json))
                      AND status='processing' AND attempts >= max_attempts
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= CURRENT_TIMESTAMP
                    ORDER BY lease_expires_at, id LIMIT :limit
                )
                RETURNING id, payload_json
                """
            ), {"allowed_json": allowed_json, "limit": limit},
        ).mappings().all()
        for row in rows:
            session.execute(
                text(
                    "UPDATE job_attempts SET status='failed', finished_at=CURRENT_TIMESTAMP, "
                    "error_class='LeaseExpired', "
                    "error_message='final attempt lease expired; effect outcome may be committed' "
                    "WHERE job_id=:job_id AND status='processing' AND finished_at IS NULL"
                ), {"job_id": row["id"]},
            )
            session.execute(
                text(
                    "INSERT INTO dead_letters (id, job_id, payload_json, failure_summary) "
                    "VALUES (:id, :job_id, :payload_json, :failure_summary)"
                ),
                {"id": new_id(), "job_id": row["id"], "payload_json": row["payload_json"],
                 "failure_summary": "LeaseExpired: final attempt exhausted; reconcile effects"},
            )

    def complete(
        self,
        session: Session,
        job: ClaimedJob,
        *,
        result: JobResult,
    ) -> bool:
        if job.lease_owner is None or job.attempt_id is None:
            return False
        session.execute(
            text(
                """
                UPDATE jobs
                SET status = 'completed',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :job_id
                  AND status = 'processing' AND attempts = :attempts AND lease_owner = :owner
                  AND EXISTS (
                    SELECT 1 FROM job_attempts
                    WHERE id = :attempt_id AND job_id = :job_id
                      AND status = 'processing' AND finished_at IS NULL
                  )
                """
            ),
            {"job_id": job.id, "attempts": job.attempts, "owner": job.lease_owner,
             "attempt_id": job.attempt_id},
        )
        if int(session.execute(text("SELECT changes()")).scalar_one()) != 1:
            return False
        self._record_attempt_finish(
            session,
            job_id=job.id,
            attempt_id=job.attempt_id,
            status="completed",
            error_class=None,
            error_message=json_text(result.detail),
        )
        return True

    def fail(
        self,
        session: Session,
        job: ClaimedJob,
        *,
        exc: Exception,
    ) -> bool:
        if job.lease_owner is None or job.attempt_id is None:
            return False
        row = (
            session.execute(
                text("SELECT attempts, max_attempts, payload_json FROM jobs WHERE id = :job_id"),
                {"job_id": job.id},
            )
            .mappings()
            .one()
        )
        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])
        terminal = attempts >= max_attempts
        status = "dead" if terminal else "pending"
        session.execute(
            text(
                """
                UPDATE jobs
                SET status = :status,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    available_at = datetime('now', '+1 minute'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :job_id
                  AND status = 'processing' AND attempts = :attempts AND lease_owner = :owner
                  AND EXISTS (
                    SELECT 1 FROM job_attempts
                    WHERE id = :attempt_id AND job_id = :job_id
                      AND status = 'processing' AND finished_at IS NULL
                  )
                """
            ),
            {
                "job_id": job.id,
                "status": status,
                "attempts": job.attempts,
                "owner": job.lease_owner,
                "attempt_id": job.attempt_id,
            },
        )
        if int(session.execute(text("SELECT changes()")).scalar_one()) != 1:
            return False
        self._record_attempt_finish(
            session,
            job_id=job.id,
            attempt_id=job.attempt_id,
            status="failed",
            error_class=exc.__class__.__name__,
            error_message=str(exc),
        )
        if terminal:
            session.execute(
                text(
                    """
                    INSERT INTO dead_letters (
                      id, job_id, payload_json, failure_summary
                    )
                    VALUES (
                      :id, :job_id, :payload_json, :failure_summary
                    )
                    """
                ),
                {
                    "id": new_id(),
                    "job_id": job.id,
                    "payload_json": row["payload_json"],
                    "failure_summary": f"{exc.__class__.__name__}: {exc}",
                },
            )
        return True

    def _record_attempt_start(self, session: Session, *, job_id: str) -> str:
        attempt_id = new_id()
        session.execute(
            text(
                "UPDATE job_attempts SET status='failed', finished_at=CURRENT_TIMESTAMP, "
                "error_class='LeaseExpired', error_message='attempt superseded by lease reclaim' "
                "WHERE job_id=:job_id AND finished_at IS NULL"
            ),
            {"job_id": job_id},
        )
        session.execute(
            text(
                """
                INSERT INTO job_attempts (id, job_id, status)
                VALUES (:id, :job_id, 'processing')
                """
            ),
            {"id": attempt_id, "job_id": job_id},
        )
        return attempt_id

    def _record_attempt_finish(
        self,
        session: Session,
        *,
        job_id: str,
        attempt_id: str,
        status: str,
        error_class: str | None,
        error_message: str | None,
    ) -> None:
        session.execute(
            text(
                """
                UPDATE job_attempts
                SET finished_at = CURRENT_TIMESTAMP,
                    status = :status,
                    error_class = :error_class,
                    error_message = :error_message
                WHERE id = :attempt_id AND job_id = :job_id
                  AND status = 'processing' AND finished_at IS NULL
                """
            ),
            {
                "attempt_id": attempt_id,
                "job_id": job_id,
                "status": status,
                "error_class": error_class,
                "error_message": error_message,
            },
        )
        if int(session.execute(text("SELECT changes()")).scalar_one()) != 1:
            raise RuntimeError("job acknowledgement lost its exact open attempt")


class EvolutionJobExecutor:
    def __init__(
        self,
        settings: Settings,
        *,
        maintenance_service: SleepLearningMaintenanceService | None = None,
        release_controller_factory: Callable[[], ReleaseController] | None = None,
        publisher_context: EvolutionCommandContext | None = None,
        validator_context: EvolutionCommandContext | None = None,
    ) -> None:
        self._settings = settings
        self._maintenance_service = maintenance_service or SleepLearningMaintenanceService()
        self._release_controller_factory = release_controller_factory or (
            lambda: ReleaseController.from_db(
                _sqlite_connection(settings),
                deployment_secret=settings.secret_key.get_secret_value(),
            )
        )
        self._publisher_context = publisher_context
        self._validator_context = validator_context
        self._session_factory: sessionmaker[Session] | None = None
        self._stage_engine: Engine | None = None

    @property
    def allowed_job_types(self) -> frozenset[EvolutionJobType]:
        allowed = {EvolutionJobType.MAINTENANCE}
        context = self._publisher_context
        if (
            context is not None
            and context.role is EvolutionRole.PUBLISHER
            and context.has_capability(EvolutionCapability.PUBLISH)
        ):
            allowed.update(set(EvolutionJobType) - {EvolutionJobType.PROPOSAL_EVALUATION})
        validator = self._validator_context
        if (
            validator is not None
            and validator.role is EvolutionRole.VALIDATOR
            and validator.has_capability(EvolutionCapability.VALIDATE)
        ):
            allowed.add(EvolutionJobType.PROPOSAL_EVALUATION)
        return frozenset(allowed)

    def close(self) -> None:
        if self._stage_engine is not None:
            self._stage_engine.dispose()
        self._stage_engine = None
        self._session_factory = None

    def execute(self, job: ClaimedJob) -> JobResult:
        if job.job_type is EvolutionJobType.PROPOSAL_EVALUATION:
            return self._execute_proposal_evaluation(job)
        if job.job_type is EvolutionJobType.PROMOTION_REQUEST:
            return self._execute_promotion(job)
        if job.job_type is EvolutionJobType.ROLLBACK_REQUEST:
            return self._execute_rollback(job)
        if job.job_type is EvolutionJobType.MAINTENANCE:
            return self._execute_maintenance(job)
        if job.job_type is EvolutionJobType.REPLAY:
            return self._execute_release_stage(job, ReleaseState.REPLAY, "replay")
        if job.job_type is EvolutionJobType.EVALUATION:
            return self._execute_release_stage(job, ReleaseState.SHADOW, "evaluation")
        if job.job_type is EvolutionJobType.CANARY:
            return self._execute_release_stage(job, ReleaseState.CANARY, "canary")
        raise ValueError(f"unsupported job type: {job.job_type}")

    def _execute_proposal_evaluation(self, job: ClaimedJob) -> JobResult:
        if EvolutionJobType.PROPOSAL_EVALUATION not in self.allowed_job_types:
            raise PermissionError("validator command port required")
        assert self._validator_context is not None
        proposal_id = _require_str(job.payload, "proposal_id")
        execution = ProposalExecutionService(
            session_factory=self._stage_session_factory(),
            project_root=Path(__file__).resolve().parents[3],
            deployment_secret=self._settings.secret_key.get_secret_value(),
        ).execute(proposal_id=proposal_id, idempotency_key=f"proposal-job:{job.idempotency_key}")
        controller = self._release_controller_factory()
        try:
            validation = controller.validate_executed_proposal(
                proposal_id=proposal_id,
                evaluation_run_id=str(execution["id"]),
                validator_context=self._validator_context,
            )
        finally:
            _close_controller_connection(controller)
        return JobResult(
            "completed",
            {
                "proposal_id": proposal_id,
                "execution_run_id": execution["id"],
                "validation_report_id": validation.validation_report_id,
                "release_mutation": False,
                "review_required": True,
            },
        )

    def _execute_release_stage(
        self,
        job: ClaimedJob,
        next_state: ReleaseState,
        step: str,
    ) -> JobResult:
        """Advance a candidate through an auditable worker-owned stage.

        Stage jobs never synthesize evaluation results.  They require a
        persisted release id and an injected Publisher capability; missing
        inputs fail closed and are retried/dead-lettered by the job runner.
        """
        release_id = _require_str(job.payload, "release_id")
        request_id = str(job.payload.get("request_id") or job.idempotency_key)
        publisher_context = self._require_publisher_context()
        controller = self._release_controller_factory()
        try:
            execution = self._produce_stage_evidence(
                job=job,
                controller=controller,
                next_state=next_state,
            )
            release = controller.advance_release_stage(
                release_id,
                next_state=next_state,
                publisher_context=publisher_context,
                execution_run_id=str(execution["id"]),
                request_id=request_id,
                step=step,
            )
        finally:
            _close_controller_connection(controller)
        return JobResult(
            status="completed",
            detail={
                "job_type": job.job_type.value,
                "release_id": release.release_id,
                "state": release.state.value,
                "candidate_only": False,
                "release_mutation": True,
                "stage_evidence_ids": execution["trajectory_ids"],
                "execution_run_id": execution["id"],
            },
        )

    def _produce_stage_evidence(
        self,
        *,
        job: ClaimedJob,
        controller: ReleaseController,
        next_state: ReleaseState,
    ) -> dict[str, Any]:
        release_id = _require_str(job.payload, "release_id")
        if controller._connection.in_transaction:
            raise ValueError("stage execution requires a transaction-free controller")
        return ReleaseExecutionService(
            session_factory=self._stage_session_factory(),
            project_root=Path(__file__).resolve().parents[3],
            deployment_secret=self._settings.secret_key.get_secret_value(),
        ).execute(
            release_id=release_id,
            stage=G006ExecutionStage(next_state.value),
            idempotency_key=f"stage:{job.idempotency_key}",
        )

    def _stage_session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            from zhiheng.db.session import create_session_factory, create_sqlite_engine

            self._stage_engine = create_sqlite_engine(self._settings)
            self._session_factory = create_session_factory(self._stage_engine)
        return self._session_factory

    def _execute_promotion(self, job: ClaimedJob) -> JobResult:
        payload = job.payload
        release_id = _require_str(payload, "release_id")
        request_id = str(payload.get("request_id") or job.idempotency_key)
        _reject_publisher_claim(payload)
        publisher_context = self._require_publisher_context()
        user_approval_context = _user_approval_context(payload)
        controller = self._release_controller_factory()
        try:
            release = controller.promote_release(
                release_id,
                publisher_context=publisher_context,
                user_approval_context=user_approval_context,
                request_id=request_id,
            )
        finally:
            _close_controller_connection(controller)
        return JobResult(
            status="completed",
            detail={"release_id": release.release_id, "state": release.state.value},
        )

    def _execute_rollback(self, job: ClaimedJob) -> JobResult:
        payload = job.payload
        release_id = _require_str(payload, "release_id")
        request_id = str(payload.get("request_id") or job.idempotency_key)
        _reject_publisher_claim(payload)
        publisher_context = self._require_publisher_context()
        user_approval_context = _user_approval_context(payload)
        controller = self._release_controller_factory()
        try:
            release = controller.rollback_release(
                release_id,
                publisher_context=publisher_context,
                user_approval_context=user_approval_context,
                request_id=request_id,
            )
        finally:
            _close_controller_connection(controller)
        return JobResult(
            status="completed",
            detail={"release_id": release.release_id, "state": release.state.value},
        )

    def _execute_maintenance(self, job: ClaimedJob) -> JobResult:
        session_factory = self._stage_session_factory()
        with session_scope(session_factory) as session:
            outputs = self._execute_maintenance_in_session(
                session, job.payload, idempotency_key=job.idempotency_key,
            )
        return JobResult(
            status="completed",
            detail={"outputs": [asdict(output) for output in outputs]},
        )

    def _execute_maintenance_in_session(
        self,
        session: Session,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> tuple[Any, ...]:
        if "cadence" in payload:
            return self._maintenance_service.run_periodic(
                session,
                cadence=MaintenanceCadence(str(payload["cadence"])),
                target_component=_require_str(payload, "target_component"),
                evidence_refs=tuple(str(item) for item in payload.get("evidence_refs", ())),
                idempotency_key=idempotency_key,
            )
        return self._maintenance_service.handle_event_trigger(
            session,
            MaintenanceTrigger(
                kind=MaintenanceTriggerKind(str(payload["trigger_kind"])),
                target_component=_require_str(payload, "target_component"),
                evidence_refs=tuple(str(item) for item in payload.get("evidence_refs", ())),
                task_family=str(payload["task_family"]) if payload.get("task_family") else None,
                failure_tag=str(payload["failure_tag"]) if payload.get("failure_tag") else None,
                details=_mapping_value(payload.get("details", {})),
            ),
            idempotency_key=idempotency_key,
        )

    def _require_publisher_context(self) -> EvolutionCommandContext:
        if self._publisher_context is None:
            raise PermissionError("publisher command port required")
        return self._publisher_context


def process_jobs_once(
    session_factory: sessionmaker[Session],
    executor: EvolutionJobExecutor,
    *,
    worker_id: str,
    limit: int = 10,
    repository: JobRepository | None = None,
) -> int:
    job_repository = repository or JobRepository()
    with session_scope(session_factory) as session:
        claimed = job_repository.claim_available(
            session,
            worker_id=worker_id,
            limit=limit,
            allowed_job_types=executor.allowed_job_types,
        )

    completed = 0
    for job in claimed:
        try:
            result = executor.execute(job)
        except Exception as exc:
            with session_scope(session_factory) as session:
                job_repository.fail(session, job, exc=exc)
            continue
        with session_scope(session_factory) as session:
            acknowledged = job_repository.complete(session, job, result=result)
        completed += int(acknowledged)
    return completed


def enqueue_evolution_job(
    session: Session,
    *,
    job_type: EvolutionJobType | str,
    idempotency_key: str,
    payload: Mapping[str, Any],
) -> bool:
    return JobRepository().enqueue(
        session,
        job_type=job_type,
        idempotency_key=idempotency_key,
        payload=payload,
    )


def _sqlite_connection(settings: Settings) -> sqlite3.Connection:
    parsed = make_url(settings.database_url)
    db_path = parsed.database
    if parsed.get_backend_name() != "sqlite" or not db_path or db_path == ":memory:":
        raise ValueError("sqlite database path is required")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = ServingSQLiteConnection(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        connection.row_factory = sqlite3.Row
    except BaseException:
        connection.close()
        raise
    return connection


def _close_controller_connection(controller: ReleaseController) -> None:
    connection = getattr(controller, "_connection", None)
    if connection is not None:
        connection.close()


def _json_object(value: Any) -> dict[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, dict):
        raise TypeError("job payload must be a JSON object")
    return dict(loaded)


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _user_approval_context(payload: Mapping[str, Any]) -> EvolutionCommandContext:
    value = payload.get("user_approval")
    if not isinstance(value, Mapping):
        raise PermissionError("user approval context required")
    allowed_keys = {"actor_id", "role", "capabilities"}
    if any(str(key) not in allowed_keys for key in value):
        raise PermissionError("invalid user approval context")
    actor_id = value.get("actor_id")
    if not isinstance(actor_id, str) or not actor_id:
        raise PermissionError("user approval actor required")
    try:
        role = EvolutionRole(str(value.get("role", "")))
    except ValueError as exc:
        raise PermissionError("invalid user approval context") from exc
    if role is not EvolutionRole.USER_APPROVER:
        raise PermissionError("user approval role required")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, Sequence) or isinstance(capabilities, (str, bytes)):
        raise PermissionError("invalid user approval context")
    declared_capabilities = tuple(capabilities)
    if any(not isinstance(item, str) for item in declared_capabilities):
        raise PermissionError("invalid user approval context")
    if declared_capabilities != (EvolutionCapability.USER_APPROVE.value,):
        raise PermissionError("user_approve capability required")
    return command_context_for_role(actor_id, role)


def _mapping_value(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("details must be a JSON object")
    return value


def _reject_publisher_claim(payload: Mapping[str, Any]) -> None:
    if any(str(key).startswith("publisher") for key in payload):
        raise PermissionError("publisher context must be injected by worker")
