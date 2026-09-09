from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from zhiheng.core.ids import sha256_text


class EvolutionRole(StrEnum):
    PROPOSER = "proposer"
    VALIDATOR = "validator"
    REVIEWER = "reviewer"
    USER_APPROVER = "user_approver"
    PUBLISHER = "publisher"


class EvolutionCapability(StrEnum):
    PROPOSE = "propose"
    VALIDATE = "validate"
    REVIEW = "review"
    USER_APPROVE = "user_approve"
    PUBLISH = "publish"


@dataclass(frozen=True, slots=True)
class EvolutionCommandContext:
    actor_id: str
    role: EvolutionRole
    capabilities: frozenset[EvolutionCapability]

    def has_capability(self, capability: EvolutionCapability) -> bool:
        return capability in self.capabilities


def command_context_for_role(actor_id: str, role: EvolutionRole) -> EvolutionCommandContext:
    role_capabilities = {
        EvolutionRole.PROPOSER: frozenset({EvolutionCapability.PROPOSE}),
        EvolutionRole.VALIDATOR: frozenset({EvolutionCapability.VALIDATE}),
        EvolutionRole.REVIEWER: frozenset({EvolutionCapability.REVIEW}),
        EvolutionRole.USER_APPROVER: frozenset({EvolutionCapability.USER_APPROVE}),
        EvolutionRole.PUBLISHER: frozenset({EvolutionCapability.PUBLISH}),
    }
    return EvolutionCommandContext(
        actor_id=actor_id,
        role=role,
        capabilities=role_capabilities[role],
    )


class ProposalState(StrEnum):
    CANDIDATE = "candidate"
    EVIDENCE_READY = "evidence_ready"
    VALIDATING = "validating"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class ReleaseState(StrEnum):
    PREPARED = "prepared"
    REPLAY = "replay"
    SHADOW = "shadow"
    CANARY = "canary"
    STABLE = "stable"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class ReleaseBindingV1:
    schema_version: ClassVar[str] = "step0.release_binding.v1"

    candidate_id: str
    target_component: str
    source_evaluation_ids: tuple[str, ...]
    source_evidence_refs: tuple[str, ...]
    validation_report_ref: str
    reviewer_decision_ref: str
    approved_artifact_digest: str
    rollback_target_id: str

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "target_component": self.target_component,
            "source_evaluation_ids": list(self.source_evaluation_ids),
            "source_evidence_refs": list(self.source_evidence_refs),
            "validation_report_ref": self.validation_report_ref,
            "reviewer_decision_ref": self.reviewer_decision_ref,
            "approved_artifact_digest": self.approved_artifact_digest,
            "rollback_target_id": self.rollback_target_id,
        }

    def canonical_json(self) -> str:
        payload = self.canonical_payload()
        return (
            "{"
            '"approved_artifact_digest":'
            + _json_value(payload["approved_artifact_digest"])
            + ',"candidate_id":'
            + _json_value(payload["candidate_id"])
            + ',"rollback_target_id":'
            + _json_value(payload["rollback_target_id"])
            + ',"reviewer_decision_ref":'
            + _json_value(payload["reviewer_decision_ref"])
            + ',"source_evaluation_ids":'
            + _json_value(payload["source_evaluation_ids"])
            + ',"source_evidence_refs":'
            + _json_value(payload["source_evidence_refs"])
            + ',"target_component":'
            + _json_value(payload["target_component"])
            + ',"validation_report_ref":'
            + _json_value(payload["validation_report_ref"])
            + "}"
        )

    def canonical_digest(self) -> str:
        return f"sha256:{sha256_text(self.canonical_json())}"

    def as_record(self) -> Mapping[str, Any]:
        return {"schema_version": self.schema_version, **self.canonical_payload()}

    def matches(self, other: ReleaseBindingV1) -> bool:
        return self.canonical_digest() == other.canonical_digest()


def _json_value(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ",".join(_json_value(item) for item in value) + "]"
    raise TypeError(f"unsupported canonical value type: {type(value)!r}")
