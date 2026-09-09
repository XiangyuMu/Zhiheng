from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from zhiheng.evolution.contracts import (
    EvolutionCapability,
    EvolutionCommandContext,
    EvolutionRole,
    ProposalState,
    ReleaseBindingV1,
    ReleaseState,
)

_PROPOSAL_TRANSITIONS: Mapping[ProposalState, frozenset[ProposalState]] = {
    ProposalState.CANDIDATE: frozenset(
        {ProposalState.EVIDENCE_READY, ProposalState.DEPRECATED}
    ),
    ProposalState.EVIDENCE_READY: frozenset(
        {ProposalState.VALIDATING, ProposalState.DEPRECATED}
    ),
    ProposalState.VALIDATING: frozenset(
        {
            ProposalState.APPROVED,
            ProposalState.REJECTED,
            ProposalState.DEPRECATED,
        }
    ),
    ProposalState.APPROVED: frozenset({ProposalState.DEPRECATED}),
    ProposalState.REJECTED: frozenset({ProposalState.DEPRECATED}),
    ProposalState.DEPRECATED: frozenset(),
}

_RELEASE_TRANSITIONS: Mapping[ReleaseState, frozenset[ReleaseState]] = {
    ReleaseState.PREPARED: frozenset({ReleaseState.REPLAY, ReleaseState.ARCHIVED}),
    ReleaseState.REPLAY: frozenset({ReleaseState.SHADOW, ReleaseState.ARCHIVED}),
    ReleaseState.SHADOW: frozenset(
        {ReleaseState.CANARY, ReleaseState.ROLLED_BACK, ReleaseState.ARCHIVED}
    ),
    ReleaseState.CANARY: frozenset(
        {ReleaseState.STABLE, ReleaseState.ROLLED_BACK, ReleaseState.ARCHIVED}
    ),
    ReleaseState.STABLE: frozenset({ReleaseState.ROLLED_BACK, ReleaseState.ARCHIVED}),
    # A rollback operation is a two-event transaction: the promoted release
    # is marked rolled_back and its prior head is restored to stable.  Keep
    # that restore transition explicit in the lifecycle graph; callers still
    # need to use the dedicated rollback command (which validates the head,
    # target and approval contexts) rather than advancing arbitrary releases.
    ReleaseState.ROLLED_BACK: frozenset({ReleaseState.STABLE, ReleaseState.ARCHIVED}),
    ReleaseState.ARCHIVED: frozenset(),
}

_ROLE_CAPABILITIES: Mapping[EvolutionRole, frozenset[EvolutionCapability]] = {
    EvolutionRole.PROPOSER: frozenset({EvolutionCapability.PROPOSE}),
    EvolutionRole.VALIDATOR: frozenset({EvolutionCapability.VALIDATE}),
    EvolutionRole.REVIEWER: frozenset({EvolutionCapability.REVIEW}),
    EvolutionRole.USER_APPROVER: frozenset({EvolutionCapability.USER_APPROVE}),
    EvolutionRole.PUBLISHER: frozenset({EvolutionCapability.PUBLISH}),
}


@dataclass(frozen=True, slots=True)
class EvolutionStateMachine:
    def can_transition_proposal(
        self, current: ProposalState, next_state: ProposalState
    ) -> bool:
        return next_state in _PROPOSAL_TRANSITIONS[current]

    def validate_proposal_transition(
        self, current: ProposalState, next_state: ProposalState
    ) -> None:
        if not self.can_transition_proposal(current, next_state):
            raise ValueError(f"invalid proposal transition: {current} -> {next_state}")

    def can_transition_release(self, current: ReleaseState, next_state: ReleaseState) -> bool:
        return next_state in _RELEASE_TRANSITIONS[current]

    def validate_release_transition(self, current: ReleaseState, next_state: ReleaseState) -> None:
        if not self.can_transition_release(current, next_state):
            raise ValueError(f"invalid release transition: {current} -> {next_state}")

    def can_perform(
        self,
        role: EvolutionRole,
        capability: EvolutionCapability,
    ) -> bool:
        return capability in _ROLE_CAPABILITIES[role]

    def validate_command_context(
        self,
        context: EvolutionCommandContext,
        *,
        required_role: EvolutionRole,
        required_capability: EvolutionCapability,
    ) -> None:
        if context.role is not required_role:
            raise PermissionError(f"{required_role.value} role required")
        expected_capabilities = _ROLE_CAPABILITIES[required_role]
        if context.capabilities != expected_capabilities:
            raise PermissionError(f"{required_capability.value} capability required")
        if not self.can_perform(context.role, required_capability):
            raise PermissionError(
                f"{context.role.value} role cannot perform {required_capability.value}"
            )

    def validate_review_assignment(
        self,
        *,
        proposer: EvolutionCommandContext,
        reviewer: EvolutionCommandContext,
    ) -> None:
        self.validate_command_context(
            proposer,
            required_role=EvolutionRole.PROPOSER,
            required_capability=EvolutionCapability.PROPOSE,
        )
        self.validate_command_context(
            reviewer,
            required_role=EvolutionRole.REVIEWER,
            required_capability=EvolutionCapability.REVIEW,
        )
        if proposer.actor_id == reviewer.actor_id:
            raise ValueError("proposer and reviewer must differ")

    def validate_publish(
        self,
        *,
        publisher: EvolutionCommandContext,
        user_approver: EvolutionCommandContext,
    ) -> None:
        self.validate_command_context(
            publisher,
            required_role=EvolutionRole.PUBLISHER,
            required_capability=EvolutionCapability.PUBLISH,
        )
        self.validate_command_context(
            user_approver,
            required_role=EvolutionRole.USER_APPROVER,
            required_capability=EvolutionCapability.USER_APPROVE,
        )

    def validate_binding_immutable(
        self, original: ReleaseBindingV1, candidate: ReleaseBindingV1
    ) -> None:
        if original.canonical_digest() != candidate.canonical_digest():
            raise ValueError("release binding is immutable")
