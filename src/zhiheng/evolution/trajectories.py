from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from zhiheng.core.ids import sha256_text

_ALLOWED_EVENT_TYPES = frozenset(
    {
        "result",
        "process",
        "quality",
        "metric",
        "user_feedback",
    }
)
_FORBIDDEN_RAW_EVENT_KEYS = (
    re.compile(r"(^|[^a-z])(raw[_-]?model[_-]?payload)($|[^a-z])"),
    re.compile(r"(^|[^a-z])(raw[_-]?tool[_-]?payload)($|[^a-z])"),
)
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "email",
        "key",
        "password",
        "phone",
        "secret",
        "set-cookie",
        "token",
        "api_key",
        "apikey",
    }
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
_SHAPE_PHONE_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class TrajectoryEvidenceState(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    ERASED = "erased"


class TrajectoryReplayability(StrEnum):
    FULL = "full"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class TrajectoryEventV1:
    schema_version: ClassVar[str] = "step0.trajectory_event.v1"

    event_id: str
    event_type: str
    created_at: str
    payload: dict[str, Any]
    previous_event_hash: str | None = None

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        previous_event_hash: str | None = None,
    ) -> TrajectoryEventV1:
        event_type = str(data["event_type"])
        if event_type not in _ALLOWED_EVENT_TYPES:
            raise ValueError(f"unsupported trajectory event type: {event_type}")
        payload = _sanitize_payload(_mapping_value(data, "payload"))
        event = cls(
            event_id=_sanitize_string(data["event_id"]),
            event_type=event_type,
            created_at=_sanitize_string(data["created_at"]),
            payload=payload,
            previous_event_hash=previous_event_hash,
        )
        return event

    @property
    def event_hash(self) -> str:
        return f"sha256:{sha256_text(self.canonical_json())}"

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "previous_event_hash": self.previous_event_hash,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class TrajectoryEnvelopeV1:
    schema_version: ClassVar[str] = "step0.trajectory_envelope.v1"

    trajectory_id: str
    task_id: str
    task_family: str
    agent_version: str
    knowledge_version: str
    environment_version: str
    created_at: str
    result: dict[str, Any]
    process: dict[str, Any]
    quality: dict[str, Any]
    failure_tags: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 1.0
    user_feedback: str | None = None
    learning_eligible: bool = True
    evidence_state: TrajectoryEvidenceState = TrajectoryEvidenceState.ACTIVE
    events: tuple[TrajectoryEventV1, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TrajectoryEnvelopeV1:
        raw_events = _sequence_value(data, "events")
        events: list[TrajectoryEventV1] = []
        previous_event_hash: str | None = None
        for raw_event in raw_events:
            event = TrajectoryEventV1.from_mapping(
                _ensure_mapping(raw_event), previous_event_hash=previous_event_hash
            )
            previous_event_hash = event.event_hash
            events.append(event)
        evidence_state = TrajectoryEvidenceState(str(data.get("evidence_state", "active")))
        return cls(
            trajectory_id=_sanitize_string(data["trajectory_id"]),
            task_id=_sanitize_string(data["task_id"]),
            task_family=_sanitize_string(data["task_family"]),
            agent_version=_sanitize_string(data["agent_version"]),
            knowledge_version=_sanitize_string(data["knowledge_version"]),
            environment_version=_sanitize_string(data["environment_version"]),
            created_at=_sanitize_string(data["created_at"]),
            result=_sanitize_payload(_mapping_value(data, "result")),
            process=_sanitize_payload(_mapping_value(data, "process")),
            quality=_sanitize_payload(_mapping_value(data, "quality")),
            failure_tags=tuple(_sanitize_string(item) for item in data.get("failure_tags", ())),
            confidence=float(data.get("confidence", 1.0)),
            user_feedback=_optional_sanitized_str(data.get("user_feedback")),
            learning_eligible=_learning_eligible_from_mapping(
                bool(data.get("learning_eligible", True)),
                result=_mapping_value(data, "result"),
                process=_mapping_value(data, "process"),
                quality=_mapping_value(data, "quality"),
                evidence_state=evidence_state,
            ),
            evidence_state=evidence_state,
            events=tuple(events),
        )

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("trajectory confidence must be between 0 and 1")
        _validate_event_chain(self.events)

    @property
    def replayability(self) -> TrajectoryReplayability:
        if self.evidence_state is TrajectoryEvidenceState.ERASED:
            return TrajectoryReplayability.DEGRADED
        if self.evidence_state is TrajectoryEvidenceState.DEGRADED:
            return TrajectoryReplayability.DEGRADED
        return TrajectoryReplayability.FULL

    @property
    def evidence_erasure_degraded(self) -> bool:
        return self.replayability is TrajectoryReplayability.DEGRADED

    @property
    def effective_learning_eligible(self) -> bool:
        return self.learning_eligible and self.replayability is TrajectoryReplayability.FULL

    @property
    def event_chain_digest(self) -> str:
        digest_input = "|".join(event.event_hash for event in self.events)
        return f"sha256:{sha256_text(digest_input)}"

    def deployment_hmac_digest(self, deployment_secret: str) -> str:
        payload = self.canonical_json()
        digest = hmac.new(
            deployment_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "agent_version": self.agent_version,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "environment_version": self.environment_version,
            "evidence_state": self.evidence_state.value,
            "events": [event.canonical_payload() for event in self.events],
            "failure_tags": list(self.failure_tags),
            "knowledge_version": self.knowledge_version,
            "learning_eligible": self.learning_eligible,
            "process": self.process,
            "quality": self.quality,
            "result": self.result,
            "task_family": self.task_family,
            "task_id": self.task_id,
            "trajectory_id": self.trajectory_id,
            "user_feedback": self.user_feedback,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    def canonical_digest(self) -> str:
        return f"sha256:{sha256_text(self.canonical_json())}"

    def as_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            **self.canonical_payload(),
            "event_chain_digest": self.event_chain_digest,
            "effective_learning_eligible": self.effective_learning_eligible,
            "replayability": self.replayability.value,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryRecordV1:
    schema_version: ClassVar[str] = "step0.trajectory_record.v1"

    trajectory_id: str
    idempotency_key_sha256: str
    request_digest: str
    deployment_hmac_digest: str
    envelope: TrajectoryEnvelopeV1

    @property
    def replayability(self) -> TrajectoryReplayability:
        return self.envelope.replayability

    @property
    def learning_eligible(self) -> bool:
        return self.envelope.effective_learning_eligible

    @property
    def evidence_erasure_degraded(self) -> bool:
        return self.envelope.evidence_erasure_degraded

    @property
    def event_chain_digest(self) -> str:
        return self.envelope.event_chain_digest

    @property
    def idempotency_key(self) -> str:
        return self.idempotency_key_sha256

    def canonical_json(self) -> str:
        return _canonical_json(
            {
                "deployment_hmac_digest": self.deployment_hmac_digest,
                "envelope": self.envelope.canonical_payload(),
                "idempotency_key_sha256": self.idempotency_key_sha256,
                "request_digest": self.request_digest,
                "trajectory_id": self.trajectory_id,
            }
        )


def build_trajectory_envelope(data: Mapping[str, Any]) -> TrajectoryEnvelopeV1:
    return TrajectoryEnvelopeV1.from_mapping(data)


def load_trajectory_fixture(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise TypeError("trajectory fixture must be a JSON object")
    return data


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _mapping_value(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data[key]
    if not isinstance(value, Mapping):
        raise TypeError(f"{key} must be a mapping")
    return value


def _ensure_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("trajectory event entries must be mappings")
    return value


def _sequence_value(data: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = data.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{key} must be a sequence")
    return value


def _optional_sanitized_str(value: Any) -> str | None:
    if value is None:
        return None
    return _sanitize_string(value)


def _sanitize_string(value: Any) -> str:
    return str(_sanitize_value(str(value)))


def _sanitize_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    return _sanitize_mapping(data)


def _sanitize_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        normalized_key = str(key).lower()
        if any(pattern.search(normalized_key) for pattern in _FORBIDDEN_RAW_EVENT_KEYS):
            raise ValueError("raw model/tool payload is not allowed in trajectories")
        if normalized_key in _SENSITIVE_KEYS:
            sanitized[str(key)] = "[REDACTED]"
            continue
        sanitized[str(key)] = _sanitize_value(value)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        redacted = _EMAIL_RE.sub("[EMAIL]", value)
        redacted = _PHONE_RE.sub("[PHONE]", redacted)
        redacted = _SHAPE_PHONE_RE.sub("[PHONE]", redacted)
        return redacted
    return value


def _validate_event_chain(events: tuple[TrajectoryEventV1, ...]) -> None:
    previous_event_hash: str | None = None
    for event in events:
        if event.previous_event_hash != previous_event_hash:
            raise ValueError("trajectory event hash chain is broken")
        previous_event_hash = event.event_hash


def _learning_eligible_from_mapping(
    base_value: bool,
    *,
    result: Mapping[str, Any],
    process: Mapping[str, Any],
    quality: Mapping[str, Any],
    evidence_state: TrajectoryEvidenceState,
) -> bool:
    if not base_value:
        return False
    if evidence_state is not TrajectoryEvidenceState.ACTIVE:
        return False
    if _contains_learning_blocker(result):
        return False
    if _contains_learning_blocker(process):
        return False
    return not _contains_learning_blocker(quality)


def _contains_learning_blocker(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if (
                normalized_key in {"redaction_status", "recheck_status"}
                and str(item).lower() == "uncertain"
            ):
                return True
            if _contains_learning_blocker(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_learning_blocker(item) for item in value)
    if isinstance(value, str):
        return value.lower() == "uncertain"
    return False
